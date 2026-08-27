"""C++ header parser built on the Clang AST (design.md section 7).

This is the parser design.md always specified; `cpp_regex.py` was the interim
token/brace reader. Both produce the same `ir.ModuleIR`, so generators and the
CLI do not care which one ran — see `cpp.py` for the selection logic.

What moving to a real AST buys, concretely — every one of these is a case the
regex reader could not see:

* **The preprocessor runs.** `#include`d types, `#define`d names, `#ifdef`ed
  declarations and macro-mangled `extern "C"` bridge headers (the F77_NAME
  pattern) are expanded before parsing instead of being skipped and reported.
* **Templates.** Class and function templates are recognised as templates and
  reported as unbindable-without-instantiation, rather than mis-parsed into a
  bogus declaration or dropped silently.
* **Typedefs and aliases.** `typedef double Pressure;`, `using Depth = double;`
  and `typedef struct {...} Point;` resolve to their canonical types.
* **Real name lookup.** A type is a known record because the AST says a record
  with that name is declared, not because the same header happened to spell
  `class X {` somewhere. Incompleteness is read off the declaration
  (`is_definition()`), not guessed from capitalisation.
* **Overloads, `= delete`, `= default`, copy/move constructors, abstract
  classes, access via inheritance, nested types, enums, references vs
  pointers, cv-qualification** — all are distinguished by the AST rather than
  by token shape.

What it still refuses to bind, deliberately (same rules as the interim
parser, because they are pybind11 constraints and not parsing limits):

* raw pointers to non-char, non-record types (no length information),
* raw pointer *returns* of record types (ownership would be handed to Python
  while C++ still owns the object),
* class/function templates, which have no single instantiation to bind,
* STL and user types with no entry in `ir.TYPE_MAP` and no definition in the
  service's headers.

All of those are reported through `ModuleIR.skipped` — one unbindable
signature never costs you the rest of the header.

Requires the `libclang` Python package (``pip install "nativegate[clang]"``).
`is_available()` reports whether it loaded; `cpp.py` falls back to the regex
reader when it did not.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..config import ExposeConfig
from ..ir import (
    ClassDef,
    FunctionDef,
    Method,
    ModuleIR,
    NativeTypeError,
    Parameter,
    SkippedSymbol,
    StructDef,
    size_pairings,
)

# --- libclang loading ---------------------------------------------------

# Import failure must not be fatal at import time: nativegate's Fortran half,
# and the regex C++ reader, work fine without libclang installed.
try:  # pragma: no cover - exercised by whichever branch the machine has
    from clang import cindex as _cindex
except Exception:  # ImportError, or a broken wheel
    _cindex = None

_LOAD_ERROR: str | None = None
_LOADED = False

# Where a libclang shared library usually lives when the pip wheel didn't
# bring its own. NATIVEGATE_LIBCLANG overrides all of it.
_LIBCLANG_CANDIDATES = (
    "/Library/Developer/CommandLineTools/usr/lib/libclang.dylib",
    "/opt/homebrew/opt/llvm/lib/libclang.dylib",
    "/usr/local/opt/llvm/lib/libclang.dylib",
    "/usr/lib/llvm-18/lib/libclang.so.1",
    "/usr/lib/llvm-17/lib/libclang.so.1",
    "/usr/lib/llvm-16/lib/libclang.so.1",
    "/usr/lib/x86_64-linux-gnu/libclang-18.so.1",
    "/usr/lib/x86_64-linux-gnu/libclang.so.1",
    # Windows: LLVM's own installer and the two package managers that ship it.
    # A DLL is loaded by name, so these are absolute paths on purpose —
    # set_library_file with a bare name would search PATH and could pick up an
    # unrelated libclang.dll.
    r"C:\Program Files\LLVM\bin\libclang.dll",
    r"C:\Program Files (x86)\LLVM\bin\libclang.dll",
    str(
        Path(os.environ.get("ProgramData", r"C:\ProgramData"))
        / "chocolatey/lib/llvm/tools/LLVM/bin/libclang.dll"
    ),
    str(
        Path(os.environ.get("VCPKG_ROOT", r"C:\vcpkg"))
        / "installed/x64-windows/bin/libclang.dll"
    ),
)


def _load() -> bool:
    """Import libclang and point it at a usable shared library. Idempotent."""
    global _LOADED, _LOAD_ERROR
    if _LOADED:
        return True
    if _cindex is None:
        _LOAD_ERROR = (
            "the 'libclang' Python package is not installed "
            '(pip install "nativegate[clang]")'
        )
        return False

    override = os.environ.get("NATIVEGATE_LIBCLANG")
    candidates = [override] if override else list(_LIBCLANG_CANDIDATES)

    # The pip `libclang` wheel ships its own native library and configures
    # itself on import, so try a bare Index first and only go hunting if that
    # fails.
    try:
        _cindex.Index.create()
        _LOADED = True
        return True
    except Exception as exc:  # library not found / not configured
        _LOAD_ERROR = str(exc)

    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        try:
            _cindex.Config.set_library_file(candidate)
            _cindex.Index.create()
            _LOADED = True
            return True
        except Exception as exc:
            _LOAD_ERROR = f"{candidate}: {exc}"

    if override and not Path(override).exists():
        _LOAD_ERROR = f"NATIVEGATE_LIBCLANG={override} does not exist"
    return False


def is_available() -> bool:
    """True if a Clang AST parse can actually run on this machine."""
    return _load()


def unavailable_reason() -> str:
    return _LOAD_ERROR or "unknown"


def clang_version() -> str:
    """A human-readable identifier for the libclang in use.

    clang_getClangVersion() returns a CXString whose conversion is not exposed
    consistently across libclang builds, so report the packaged version and the
    library file instead — both are what you need when a parse behaves oddly.
    """
    if not _load():
        return "unavailable"
    try:
        from importlib.metadata import version

        return f"libclang {version('libclang')}"
    except Exception:
        pass
    try:
        return Path(_cindex.conf.get_filename()).name
    except Exception:
        return "unknown version"


# --- parse configuration ------------------------------------------------


DEFAULT_STD = "c++17"

_PLATFORM_ARGS: list[str] | None = None


def _platform_args() -> list[str]:
    """Flags that let libclang find the standard library and its own builtins.

    The pip `libclang` wheel is a bare front end: it has no notion of where
    the SDK or its own `stdarg.h` live, so `#include <string>` fails and every
    type that came from it silently degrades to `int` under error recovery.
    That degradation is precisely the class of bug an AST parser is supposed
    to eliminate, so the search happens up front and is cached.
    """
    global _PLATFORM_ARGS
    if _PLATFORM_ARGS is not None:
        return _PLATFORM_ARGS

    args: list[str] = []

    if sys.platform == "darwin":
        sdk = _run(["xcrun", "--show-sdk-path"])
        if sdk:
            args += ["-isysroot", sdk]

    resource_dir = _run(["clang", "-print-resource-dir"]) or _run(
        ["xcrun", "clang", "-print-resource-dir"]
    )
    builtin_includes = []
    if resource_dir:
        builtin_includes.append(Path(resource_dir) / "include")
    builtin_includes += sorted(
        Path("/Library/Developer/CommandLineTools/usr/lib/clang").glob("*/include"),
        reverse=True,
    )
    for candidate in builtin_includes:
        if (candidate / "stdarg.h").exists():
            # -idirafter, not -isystem: clang's builtin headers must come
            # *after* the C++ standard library's, or libc++'s <cstddef> finds
            # the C <stddef.h> first and the whole standard library falls over
            # with "your header search paths are not configured properly".
            args += ["-idirafter", str(candidate)]
            break

    _PLATFORM_ARGS = args
    return args


def _run(command: list[str]) -> str | None:
    try:
        out = subprocess.run(
            command, capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value if out.returncode == 0 and value else None


@dataclass(frozen=True)
class ClangOptions:
    """Compiler flags for the parse. Mirrors nativegate.yaml's `clang:` block."""

    std: str = DEFAULT_STD
    include_paths: tuple[str, ...] = ()
    defines: tuple[str, ...] = ()
    extra_args: tuple[str, ...] = ()
    # `-x <language>`. `.h` is ambiguous: a C header using `restrict` or a K&R
    # declaration is not valid C++, and forcing `-x c++` reports the failure as
    # a C++ diagnostic. Defaults to C++ so existing callers — config.ClangConfig
    # among them, which does not know about this field — are unchanged.
    language: str = "c++"
    # extern "C" functions whose bare `T*` arguments are scalars passed by
    # reference. See config.ClangConfig.scalar_ref_functions for why this is
    # opt-in per function rather than inferred.
    scalar_ref_functions: tuple[str, ...] = ()

    def command_line(self, header: Path) -> list[str]:
        args = ["-x", self.language, "-fsyntax-only"]
        # A C++ -std with -x c (or the reverse) is a hard clang error, so the
        # standard is only passed when it belongs to the selected language.
        is_cpp_language = "c++" in self.language
        is_cpp_std = self.std.startswith("c++") or self.std.startswith("gnu++")
        if is_cpp_language == is_cpp_std:
            args.append(f"-std={self.std}")
        args += _platform_args()
        # A header almost always includes its siblings by bare name.
        args.append(f"-I{header.parent}")
        args.extend(f"-I{p}" for p in self.include_paths)
        args.extend(f"-D{d}" for d in self.defines)
        args.extend(self.extra_args)
        return args


# --- type mapping -------------------------------------------------------


def _kinds(*names):
    """TypeKind members by name — the set varies across libclang versions."""
    return {
        getattr(_cindex.TypeKind, n) for n in names if hasattr(_cindex.TypeKind, n)
    }


def _builtin_map() -> dict:
    return {
        **{k: "None" for k in _kinds("VOID")},
        **{k: "bool" for k in _kinds("BOOL")},
        **{
            k: "int"
            for k in _kinds(
                "CHAR_U", "USHORT", "UINT", "ULONG", "ULONGLONG", "UINT128",
                "SHORT", "INT", "LONG", "LONGLONG", "INT128", "WCHAR",
                "CHAR16", "CHAR32",
            )
        },
        **{k: "float" for k in _kinds("FLOAT", "DOUBLE", "LONGDOUBLE", "FLOAT128")},
        # A lone `char` is a one-character string on the Python side, matching
        # ir.TYPE_MAP. `char*` is handled separately, as a C string.
        **{k: "str" for k in _kinds("CHAR_S", "SCHAR", "UCHAR")},
    }


_CHAR_KIND_NAMES = ("CHAR_S", "SCHAR", "UCHAR", "CHAR_U")


def _strip_refs(t):
    while t.kind in _kinds("LVALUEREFERENCE", "RVALUEREFERENCE"):
        t = t.get_pointee()
    return t


def _record_short_name(t) -> str | None:
    """Unqualified name of the record/enum a type refers to, if any."""
    decl = t.get_declaration()
    if decl is None or decl.kind == _cindex.CursorKind.NO_DECL_FOUND:
        return None
    if decl.spelling:
        return decl.spelling.split("::")[-1].split("<")[0]
    return None


def _qualified_decl_name(decl) -> str | None:
    """`petro::Correlation` for a declaration, walking namespaces and records.

    The Python type name is lossy by construction (every enum is `int`), so a
    generator that must emit C++ needs the spelling as written *and* qualified
    enough to be valid at namespace scope in the generated binding file.
    """
    if decl is None or not decl.spelling:
        return None
    parts = [decl.spelling.split("<")[0]]
    parent = decl.semantic_parent
    while parent is not None and parent.kind != _cindex.CursorKind.TRANSLATION_UNIT:
        if parent.spelling and parent.kind in (
            _cindex.CursorKind.NAMESPACE,
            _cindex.CursorKind.STRUCT_DECL,
            _cindex.CursorKind.CLASS_DECL,
            _cindex.CursorKind.UNION_DECL,
            _cindex.CursorKind.CLASS_TEMPLATE,
        ):
            parts.append(parent.spelling.split("<")[0])
        parent = parent.semantic_parent
    return "::".join(reversed(parts))


_NAMED_TYPE_DECL_KINDS = lambda: (  # noqa: E731 - needs _cindex loaded first
    _cindex.CursorKind.STRUCT_DECL,
    _cindex.CursorKind.CLASS_DECL,
    _cindex.CursorKind.UNION_DECL,
    _cindex.CursorKind.ENUM_DECL,
    _cindex.CursorKind.TYPEDEF_DECL,
    _cindex.CursorKind.TYPE_ALIAS_DECL,
)


def _native_spelling(t) -> str | None:
    """The C++ type as written, qualified — `std::uint64_t`, `petro::Correlation`.

    Top-level `const` and reference decoration are dropped: constness travels
    separately on `ir.Parameter.is_const`, and what a generator needs here is
    the type to name inside `py::init<...>`.
    """
    base = _strip_refs(t)
    decl = base.get_declaration()
    if (
        decl is not None
        and decl.kind != _cindex.CursorKind.NO_DECL_FOUND
        and decl.kind in _NAMED_TYPE_DECL_KINDS()
    ):
        qualified = _qualified_decl_name(decl)
        if qualified:
            return qualified
    spelling = base.spelling.strip()
    if spelling.startswith("const "):
        spelling = spelling[len("const ") :].strip()
    return spelling or None


_STD_STRING_SPELLINGS = frozenset(
    {"std::string", "std::__1::string", "std::__cxx11::string", "string"}
)


def _is_std_string(t) -> bool:
    """True for std::string in any of its spellings.

    libclang keeps the sugar: the canonical type of `std::string` is spelled
    `std::string` on libc++ and `std::__cxx11::basic_string<char, ...>` on
    libstdc++, and a user typedef adds another layer on top. All of them are
    the same std::string that pybind11 converts to a Python str.
    """
    canonical = t.get_canonical()
    spelling = canonical.spelling.replace("const ", "").strip()
    if "basic_string<char" in spelling or spelling in _STD_STRING_SPELLINGS:
        return True
    declaration = canonical.get_declaration()
    return bool(
        declaration
        and declaration.spelling == "basic_string"
        and "char" in declaration.type.spelling
    )


def _is_incomplete_record(t) -> bool:
    decl = t.get_declaration()
    if decl is None:
        return False
    if decl.kind not in (
        _cindex.CursorKind.STRUCT_DECL,
        _cindex.CursorKind.CLASS_DECL,
        _cindex.CursorKind.UNION_DECL,
    ):
        return False
    return not decl.is_definition()


# C++ operator -> the Python special method pybind11 should bind it as, keyed
# by (spelling, argument count). The count matters: a member `operator-` with
# one argument is subtraction, with none it is negation, and binding one as the
# other would compile and then compute something else entirely.
_OPERATOR_DUNDERS = {
    ("operator+", 1): "__add__",
    ("operator-", 1): "__sub__",
    ("operator*", 1): "__mul__",
    ("operator/", 1): "__truediv__",
    ("operator%", 1): "__mod__",
    ("operator+", 0): "__pos__",
    ("operator-", 0): "__neg__",
    ("operator==", 1): "__eq__",
    ("operator!=", 1): "__ne__",
    ("operator<", 1): "__lt__",
    ("operator<=", 1): "__le__",
    ("operator>", 1): "__gt__",
    ("operator>=", 1): "__ge__",
    ("operator[]", 1): "__getitem__",
    ("operator()", 1): "__call__",
    ("operator!", 0): "__invert__",
}

# Why the rest are refused rather than approximated.
_OPERATOR_REFUSALS = {
    "operator=": "assignment has no Python equivalent — Python rebinds names, it does not assign through them",
    "operator+=": "a compound assignment must return the mutated object for Python's `__iadd__` protocol, and a C++ `T&` return needs a return-value policy nativegate cannot infer",
    "operator-=": "same as operator+=",
    "operator*=": "same as operator+=",
    "operator/=": "same as operator+=",
    "operator++": "Python has no increment operator",
    "operator--": "Python has no decrement operator",
    "operator->": "member access cannot be forwarded through pybind11",
    "operator new": "allocation is not something Python may call",
    "operator delete": "deallocation is managed by pybind11's holder, not by Python",
}


def _operator_dunder(spelling: str, arg_count: int) -> str | None:
    return _OPERATOR_DUNDERS.get((spelling, arg_count))


def _operator_refusal(spelling: str, arg_count: int) -> str:
    """Why this operator is not bound — never just 'unsupported'."""
    base = spelling.split("(")[0].strip()
    if base in _OPERATOR_REFUSALS:
        return (
            f"'{spelling}' is not bound: {_OPERATOR_REFUSALS[base]}. "
            "Expose a named method instead, or exclude the symbol in "
            "nativegate.yaml."
        )
    return (
        f"'{spelling}' with {arg_count} argument(s) has no Python special "
        "method nativegate maps it to. Expose a named method instead, or "
        "exclude the symbol in nativegate.yaml."
    )


class PointerLengthUnknown(NativeTypeError):
    """A pointer to a scalar, whose length is not knowable from the type alone.

    Still a NativeTypeError, so every existing caller keeps refusing it. What
    the subclass adds is the information `_parameters` needs to reconsider:
    the element type, and whether the callee may write through it. If another
    argument of the same function turns out to be this pointer's length, the
    pair becomes a bindable array; otherwise this propagates unchanged.
    """

    def __init__(self, message: str, *, element: str, is_mutable: bool) -> None:
        super().__init__(message)
        self.element = element
        self.is_mutable = is_mutable


def _refuse_mutable_vector_ref(t, *, is_return: bool) -> None:
    """Refuse `std::vector<T>&` — pybind11 discards writes through it, silently.

    THIS IS THE REASON std::vector SUPPORT IS SAFE TO HAVE AT ALL.

    `<pybind11/stl.h>` converts a Python list to a *temporary* std::vector for
    the duration of the call. For an input parameter — by value or by
    `const&` — that is exactly right. For a non-const `std::vector<T>&`, which
    in C++ means "I am going to write to your vector", the callee writes into
    the temporary and the temporary is destroyed on return. The caller's list
    is unchanged.

    Nothing raises. Nothing warns. It compiles, it runs, it returns. Measured
    with a real pybind11 module:

        void scale_in_place(std::vector<double>& v, double k);

        >>> data = [1.0, 2.0, 3.0]
        >>> probe.scale_in_place(data, 10.0)
        >>> data
        [1.0, 2.0, 3.0]          # writes discarded

    A service that binds that builds, imports, passes a smoke test, and returns
    unmodified data to whoever asked. That is the exact failure class this
    project treats as the worst kind, so the binding is refused instead.

    Returning `std::vector<T>&` is fine to bind — pybind11 copies it out, and a
    copy of a returned value is what Python wants anyway.
    """
    if is_return or t.kind not in _kinds("LVALUEREFERENCE"):
        return
    pointee = t.get_pointee()
    if not _is_std_vector(pointee):
        return
    if pointee.is_const_qualified() or pointee.get_canonical().is_const_qualified():
        return
    raise NativeTypeError(
        f"'{t.spelling}' is a mutable reference to a std::vector. pybind11 "
        "converts the caller's list into a temporary vector, so anything the "
        "function writes through this parameter is discarded when the call "
        "returns — silently, with no error. Take 'const std::vector<T>&' if "
        "the data is input, or RETURN the modified vector, or exclude the "
        "symbol in nativegate.yaml."
    )


def _is_std_vector(t) -> bool:
    """True for std::vector<T> in any spelling, through typedefs."""
    canonical = t.get_canonical()
    spelling = canonical.spelling.replace("const ", "").strip()
    return spelling.startswith("std::vector<") or spelling.startswith(
        "std::__1::vector<"
    )


def _vector_element(t):
    """The T of a std::vector<T>, or None if it cannot be read off the AST."""
    canonical = t.get_canonical()
    try:
        if canonical.get_num_template_arguments() > 0:
            return canonical.get_template_argument_type(0)
    except (AttributeError, TypeError):  # older libclang on some platforms
        pass
    return None


def _map_type(t, known_records: frozenset[str], *, is_return: bool) -> tuple[str, bool]:
    """Map a clang Type to (python_type, is_array), or raise NativeTypeError.

    The refusals mirror cpp_regex._normalize_type so that switching parsers
    never changes which signatures are considered safe — only how accurately
    they are recognised.
    """
    # Checked BEFORE _strip_refs, because for std::vector the reference and its
    # constness are the whole question — see _refuse_mutable_vector_ref.
    _refuse_mutable_vector_ref(t, is_return=is_return)

    t = _strip_refs(t)
    spelling = t.spelling

    if _is_std_vector(t):
        element = _vector_element(t)
        if element is None:
            raise NativeTypeError(
                f"'{spelling}' is a std::vector whose element type could not be "
                "read from the AST. Expose a function taking a concrete vector "
                "(std::vector<double>, std::vector<int>, ...) instead, or "
                "exclude the symbol in nativegate.yaml."
            )
        inner, inner_is_array = _map_type(element, known_records, is_return=is_return)
        if inner_is_array:
            raise NativeTypeError(
                f"'{spelling}' is a nested container. pybind11 will convert it, "
                "but nativegate has no IR shape for a list of lists, so the "
                "generated Pydantic model would be wrong. Flatten it (pass the "
                "data and its shape separately), or exclude the symbol."
            )
        if inner not in ("float", "int", "bool", "str"):
            raise NativeTypeError(
                f"'{spelling}' has element type '{inner}', which is not a scalar "
                "pybind11 converts element-wise. Use a vector of a numeric type "
                "or std::string, or exclude the symbol in nativegate.yaml."
            )
        return inner, True

    # `double values[4]` / `double values[]` as a *field*; parameters decay to
    # pointers and are recovered from the source tokens by the caller.
    array_kinds = _kinds("CONSTANTARRAY", "INCOMPLETEARRAY", "VARIABLEARRAY", "DEPENDENTSIZEDARRAY")
    if t.kind in array_kinds:
        element, _ = _map_type(t.element_type, known_records, is_return=is_return)
        return element, True

    if t.kind in _kinds("POINTER"):
        pointee = _strip_refs(t.get_pointee())
        canonical_pointee = pointee.get_canonical()

        if any(canonical_pointee.kind in _kinds(n) for n in _CHAR_KIND_NAMES):
            # `const char*` is a C string, the one pointer with an unambiguous
            # Python meaning. A *non-const* `char*` is an output buffer with no
            # length convention: bound as a Python str, pybind11 materialises a
            # temporary that C++ then writes through, so the write is lost or
            # lands in freed memory.
            if pointee.is_const_qualified() or canonical_pointee.is_const_qualified():
                return "str", False
            raise NativeTypeError(
                f"'{spelling}' is a non-const 'char*': an output buffer with no "
                "length convention. Binding it as a Python str would have C++ "
                "write through a temporary. Return std::string (or take "
                "'const char*' for input), or exclude the symbol in nativegate.yaml."
            )

        name = _record_short_name(pointee)
        if name and name in known_records:
            if is_return:
                raise NativeTypeError(
                    f"'{spelling}' returns a raw pointer to '{name}'; binding it would "
                    "hand ownership to Python while C++ still owns the object. Return by "
                    "value or by reference instead."
                )
            return name, False

        if name and _is_incomplete_record(pointee):
            raise NativeTypeError(
                f"'{spelling}' refers to '{name}', which is only forward-declared "
                "(an incomplete type). pybind11 needs the full definition to bind it. "
                f"Add the header that defines '{name}' to this service's native/ "
                "directory, or exclude the symbol in nativegate.yaml."
            )

        # A pointer to a scalar MIGHT still be bindable — as an array, if some
        # other parameter of the same function carries its length. That cannot
        # be decided here, one type at a time, so the decision is deferred to
        # _parameters, which can see the whole signature. Raising a distinct
        # exception (carrying what was learned about the pointee) is how the
        # information gets there without _map_type growing a second job.
        element = _builtin_map().get(canonical_pointee.kind)
        if element in ("float", "int", "bool"):
            raise PointerLengthUnknown(
                f"'{spelling}' is a pointer; nativegate cannot infer its length. "
                "Wrap it in a type that carries its own size (std::vector, a "
                "span, or a struct), pass its length in an argument named after "
                "it (`n`, `n_values`, `values_len`, ...), or exclude the symbol "
                "in nativegate.yaml.",
                element=element,
                is_mutable=not (
                    pointee.is_const_qualified()
                    or canonical_pointee.is_const_qualified()
                ),
            )

        raise NativeTypeError(
            f"'{spelling}' is a pointer; nativegate cannot infer its length. "
            "Wrap it in a type that carries its own size (std::vector, a span, "
            "or a struct) or exclude the symbol in nativegate.yaml."
        )

    if t.kind in _kinds("ENUM") or t.get_canonical().kind in _kinds("ENUM"):
        # A scoped or unscoped enum crosses the boundary as its integer value.
        return "int", False

    if _is_std_string(t):
        return "str", False

    canonical = t.get_canonical()
    builtin = _builtin_map().get(canonical.kind)
    if builtin is not None:
        return builtin, False

    name = _record_short_name(t)
    if name and name in known_records:
        return name, False

    if name and _is_incomplete_record(t):
        raise NativeTypeError(
            f"'{spelling}' refers to '{name}', which is only forward-declared "
            "(an incomplete type). pybind11 needs the full definition to bind it. "
            f"Add the header that defines '{name}' to this service's native/ "
            "directory, or exclude the symbol in nativegate.yaml."
        )

    raise NativeTypeError(
        f"Unsupported native type '{spelling}': add a mapping in ir.TYPE_MAP, "
        "define the type in this service's headers, or exclude this symbol "
        "from nativegate.yaml."
    )


def _declared_as_array(cursor) -> bool:
    """True if a parameter was *written* as an array (`double v[]`).

    C decays an array parameter to a pointer before the AST sees it, so the
    only surviving evidence is the source spelling. `is_array` on the IR is
    what tells the generated service to accept a sequence there.
    """
    try:
        tokens = [tok.spelling for tok in cursor.get_tokens()]
    except Exception:
        return False
    return "[" in tokens


def _is_extern_c(cursor) -> bool:
    """True when this declaration sits inside an `extern "C"` block."""
    parent = cursor.lexical_parent
    while parent is not None and parent.kind != _cindex.CursorKind.TRANSLATION_UNIT:
        if parent.kind == _cindex.CursorKind.LINKAGE_SPEC:
            return True
        parent = parent.lexical_parent
    return False


def _parameters(
    cursor,
    known_records: frozenset[str],
    *,
    scalar_ref_ok: bool = False,
    extern_c: bool = False,
) -> list[Parameter]:
    """Map every argument, resolving bare pointers against the whole signature.

    A `double*` carries no length, so it cannot be bound from its type alone.
    But `void scale(double* data, int n, double k)` does say how long `data`
    is — in another argument. That is only visible with the full parameter
    list in hand, so `_map_type` defers the decision by raising
    PointerLengthUnknown and it is settled here.

    The pairing uses `ir.size_pairings`, the same inference the Fortran router
    uses to validate `n == len(props)`. Deliberately shared: two heuristics
    that drifted would mean the same signature read differently depending on
    which language it came from. It is conservative by design — an ambiguous
    case yields no pairing and the pointer stays refused, because a false
    pairing silently binds the wrong argument as a length.
    """
    params: list[Parameter] = []
    deferred: dict[str, PointerLengthUnknown] = {}

    for index, arg in enumerate(cursor.get_arguments()):
        name = arg.spelling or f"arg{index}"
        try:
            py_type, is_array = _map_type(arg.type, known_records, is_return=False)
        except PointerLengthUnknown as exc:
            # Provisional: typed from the pointee so the pairing below can see
            # it as an array candidate. Replaced or re-raised before returning.
            deferred[name] = exc
            params.append(
                Parameter(
                    name=name,
                    type=exc.element,
                    is_array=True,
                    native_type=_native_spelling(arg.type),
                    is_const=_is_const_type(arg.type),
                )
            )
            continue
        params.append(
            Parameter(
                name=name,
                type=py_type,
                is_array=is_array or _declared_as_array(arg),
                native_type=_native_spelling(arg.type),
                is_const=_is_const_type(arg.type),
            )
        )

    if not deferred:
        return params

    by_name = {p.name: p for p in params}
    paired = {array: length for length, array in size_pairings(params)}

    # THE SCALAR-BY-REFERENCE FALLBACK, and why it is gated twice.
    #
    # In an `extern "C"` bridge to Fortran, EVERY argument is a pointer,
    # because Fortran has no pass-by-value — `void pvtini(double* api, ...)`
    # takes four scalars. Refusing those as "pointer with no length" refused
    # the single most common C-linkage signature there is.
    #
    # But `T*` is also how C passes an ARRAY whose length travels some other
    # way, and binding an array as one scalar hands the callee a pointer to a
    # single stack double it will read past — the silent-wrong-answer class.
    # Hence two gates:
    #   1. extern "C" only. In C++ linkage a scalar in/out is spelled `T&`,
    #      which already binds; a bare `T*` there usually IS an array.
    #   2. no size-word integer anywhere in the signature. `krload(double* sw,
    #      double* krw, double* kro, double* pcw, int* n)` has four arrays
    #      sharing one `n` — the pairing above is ambiguous, and the presence
    #      of `n` says "these are arrays", so everything stays refused rather
    #      than becoming five scalars.
    #
    # Non-const scalars are marked intent="inout": the binding returns their
    # final value, so an output like PVTERR's `int* icode` cannot be silently
    # dropped into a discarded temporary.
    from ..ir import _SIZE_WORDS, names_a_size_of

    def _signature_has_size_word() -> bool:
        for candidate in params:
            if candidate.type != "int":
                continue
            lowered = candidate.name.lower()
            if lowered in _SIZE_WORDS:
                return True
            if any(names_a_size_of(candidate.name, other.name) for other in params):
                return True
        return False

    scalar_ref = scalar_ref_ok and not _signature_has_size_word()

    for name, exc in deferred.items():
        length = paired.get(name)
        if length is None:
            if scalar_ref:
                target = by_name[name]
                target.is_array = False
                target.length_param = None
                target.is_scalar_ref = True
                target.intent = "inout" if exc.is_mutable else "in"
                continue
            # Nothing in the signature names this pointer's length. Refuse it
            # exactly as before — guessing here is how a service binds the
            # wrong argument as an extent and reads past the end of a buffer.
            if extern_c:
                raise NativeTypeError(
                    str(exc)
                    + f" If every `T*` argument of '{cursor.spelling}' is a "
                    "SCALAR passed by reference (the Fortran-linkage "
                    "convention), list it under `clang.scalar_ref_functions` "
                    "in nativegate.yaml — but check the Fortran first: an "
                    "array whose extent lives in a PARAMETER or COMMON block "
                    "looks identical in the C prototype, and binding it as a "
                    "scalar corrupts memory."
                ) from exc
            raise exc
        by_name[name].length_param = length
        by_name[name].is_mutable_buffer = exc.is_mutable

    return params


def _is_const_type(t) -> bool:
    """True for a `const`-qualified declaration (`const double x`).

    A const data member bound with def_readwrite is a compile error inside
    pybind11's templates; the generator emits def_readonly instead.
    """
    try:
        if t.is_const_qualified():
            return True
        stripped = _strip_refs(t)
        return bool(stripped is not t and stripped.is_const_qualified())
    except Exception:
        return False


def _mark_overloads(entries) -> None:
    """Flag every entry whose name is declared more than once.

    `&Cls::viscosity` is ambiguous the moment a second `viscosity` exists, so
    the binding has to go through py::overload_cast<...>; on the HTTP side two
    same-named routes would silently shadow each other.
    """
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.name] = counts.get(entry.name, 0) + 1
    for entry in entries:
        if counts[entry.name] > 1:
            entry.is_overloaded = True


# --- declaration walking ------------------------------------------------


def _in_file(cursor, path: Path) -> bool:
    """True if the cursor was written in this header rather than an include.

    Everything reachable through `#include` belongs to the header that
    declared it; binding it here would emit it twice when both headers are in
    the same service.
    """
    location = cursor.location
    if location is None or location.file is None:
        return False
    try:
        return Path(location.file.name).resolve() == path
    except OSError:
        return str(location.file.name) == str(path)


_RECORD_KINDS = lambda: (  # noqa: E731 - needs _cindex loaded first
    _cindex.CursorKind.CLASS_DECL,
    _cindex.CursorKind.STRUCT_DECL,
)


def _namespace_of(cursor) -> str | None:
    """`a::b` for a declaration nested in namespaces, else None.

    Anonymous namespaces and `extern "C"` blocks are not name scopes we can
    spell in generated code, so they are skipped in the chain.
    """
    parts = []
    parent = cursor.semantic_parent
    while parent is not None and parent.kind != _cindex.CursorKind.TRANSLATION_UNIT:
        if parent.kind == _cindex.CursorKind.NAMESPACE and parent.spelling:
            parts.append(parent.spelling)
        parent = parent.semantic_parent
    return "::".join(reversed(parts)) or None


def _is_public(cursor) -> bool:
    return cursor.access_specifier == _cindex.AccessSpecifier.PUBLIC


def _is_deleted(cursor) -> bool:
    # is_deleted_method() is missing on some libclang builds; fall back to the
    # source tokens, where `= delete` is unmistakable.
    getter = getattr(cursor, "is_deleted_method", None)
    if callable(getter):
        try:
            return bool(getter())
        except Exception:
            pass
    try:
        tokens = [t.spelling for t in cursor.get_tokens()]
    except Exception:
        return False
    return "delete" in tokens and "=" in tokens


def _bool_method(cursor, name: str, default: bool = False) -> bool:
    getter = getattr(cursor, name, None)
    if not callable(getter):
        return default
    try:
        return bool(getter())
    except Exception:
        return default


def _error_at(cursor, errors: dict[int, str]) -> str | None:
    """The compiler error covering this declaration, if any.

    Clang recovers from an unknown type by pretending it was `int` — so a
    method returning an unincluded `std::vector<double>` parses cleanly as
    `int b()` and would be bound as returning an integer. Any declaration the
    compiler complained about is therefore refused rather than trusted; the
    diagnostic itself is the reason reported to the user.
    """
    if not errors:
        return None
    extent = cursor.extent
    if extent is None or extent.start.file is None:
        return None
    for line in range(extent.start.line, extent.end.line + 1):
        if line in errors:
            return errors[line]
    return None


def _parse_record(
    cursor,
    module: ModuleIR,
    known_records: frozenset[str],
    errors: dict[int, str] | None = None,
) -> ClassDef | StructDef:
    errors = errors or {}
    name = cursor.spelling
    namespace = _namespace_of(cursor)
    is_struct = cursor.kind == _cindex.CursorKind.STRUCT_DECL

    methods: list[Method] = []
    fields: list[Parameter] = []
    constructors: list[list[Parameter]] = []
    bases: list[str] = []
    saw_constructor = False
    saw_public_default_ctor = False
    blocks_default_ctor = False

    for child in cursor.get_children():
        kind = child.kind

        if kind == _cindex.CursorKind.CXX_BASE_SPECIFIER:
            base_name = _record_short_name(child.type)
            if not base_name:
                continue
            if not _is_public(child):
                # Private/protected inheritance is an implementation detail:
                # the conversion Derived* -> Base* is inaccessible, so telling
                # pybind11 about it would not compile.
                continue
            if base_name not in known_records:
                module.skipped.append(
                    SkippedSymbol(
                        f"{name} : {base_name}",
                        f"inherits from '{base_name}', which this service does not "
                        "bind. Python callers will not see anything inherited from "
                        f"it — expose '{base_name}' too, or ignore this if its "
                        "members are not needed.",
                    )
                )
                continue
            bases.append(base_name)
            continue

        broken = _error_at(child, errors)

        if kind == _cindex.CursorKind.CONSTRUCTOR:
            saw_constructor = True
            if not _is_public(child) or _is_deleted(child):
                continue
            if broken:
                module.skipped.append(
                    SkippedSymbol(f"{name}::{name}", _unparsed_reason(broken))
                )
                continue
            # A copy/move constructor is not a Python-facing way to build the
            # object; pybind11 provides copying separately.
            if _bool_method(child, "is_copy_constructor") or _bool_method(
                child, "is_move_constructor"
            ):
                continue
            if _bool_method(child, "is_default_constructor") or not list(
                child.get_arguments()
            ):
                saw_public_default_ctor = True
                continue
            try:
                constructors.append(_parameters(child, known_records))
            except NativeTypeError as exc:
                module.skipped.append(SkippedSymbol(f"{name}::{name}", str(exc)))
            continue

        if kind == _cindex.CursorKind.CXX_METHOD:
            if not _is_public(child) or _is_deleted(child):
                continue
            # Operators used to be dropped here, silently and without a
            # `skipped` entry — the one thing this parser is not allowed to do.
            # pybind11 binds them perfectly well as Python special methods, so
            # the ones with an honest mapping are bound and the rest are
            # reported with the reason they cannot be.
            operator_dunder = None
            if child.spelling.startswith("operator"):
                arg_count = len(list(child.get_arguments()))
                operator_dunder = _operator_dunder(child.spelling, arg_count)
                if operator_dunder is None:
                    module.skipped.append(
                        SkippedSymbol(
                            f"{name}::{child.spelling}",
                            _operator_refusal(child.spelling, arg_count),
                        )
                    )
                    continue
            if broken:
                module.skipped.append(
                    SkippedSymbol(
                        f"{name}::{child.spelling}", _unparsed_reason(broken)
                    )
                )
                continue
            try:
                returns, returns_array = _map_type(
                    child.result_type, known_records, is_return=True
                )
                parameters = _parameters(child, known_records)
            except NativeTypeError as exc:
                module.skipped.append(
                    SkippedSymbol(f"{name}::{child.spelling}", str(exc))
                )
                continue
            methods.append(
                Method(
                    name=operator_dunder or child.spelling,
                    parameters=parameters,
                    returns=returns,
                    is_static=child.is_static_method(),
                    is_const=_bool_method(child, "is_const_method"),
                    returns_array=returns_array,
                    # Only set for operators: the published name is the dunder,
                    # but the address to take is still `Cls::operator+`.
                    cpp_name=child.spelling if operator_dunder else None,
                )
            )
            continue

        if kind == _cindex.CursorKind.FIELD_DECL:
            field_is_const = _is_const_type(child.type)
            if field_is_const or child.type.kind in _kinds(
                "LVALUEREFERENCE", "RVALUEREFERENCE"
            ):
                # A const or reference member — public or not — suppresses the
                # implicit default constructor, so py::init<>() for this record
                # would not compile.
                blocks_default_ctor = True
            if not _is_public(child):
                continue
            if broken:
                module.skipped.append(
                    SkippedSymbol(f"{name}.{child.spelling}", _unparsed_reason(broken))
                )
                continue
            try:
                py_type, is_array = _map_type(
                    child.type, known_records, is_return=False
                )
            except NativeTypeError as exc:
                module.skipped.append(
                    SkippedSymbol(f"{name}.{child.spelling}", str(exc))
                )
                continue
            fields.append(
                Parameter(
                    name=child.spelling,
                    type=py_type,
                    is_array=is_array,
                    native_type=_native_spelling(child.type),
                    is_const=field_is_const,
                )
            )
            continue

        if kind in (
            _cindex.CursorKind.FUNCTION_TEMPLATE,
            _cindex.CursorKind.CLASS_TEMPLATE,
        ):
            module.skipped.append(
                SkippedSymbol(
                    f"{name}::{child.spelling}",
                    "is a template; pybind11 binds instantiations, not templates. "
                    "Add a non-template wrapper for the instantiation you need, "
                    "or exclude the symbol in nativegate.yaml.",
                )
            )
            continue

    # An abstract class cannot be instantiated from Python at all — emitting
    # py::init<> for one is a compile error inside pybind11's templates.
    abstract = _bool_method(cursor, "is_abstract_record")
    if abstract:
        constructors = []

    _mark_overloads(methods)

    # libclang exposes no reliable "has a default constructor" query on the
    # record cursor across builds, so it is derived: a user-declared non-default
    # constructor, or a const/reference member, removes the implicit one.
    has_default = (
        not abstract
        and (saw_public_default_ctor or not saw_constructor)
        and (saw_public_default_ctor or not blocks_default_ctor)
    )

    if is_struct and not methods and not bases and fields:
        return StructDef(
            name=name,
            namespace=namespace,
            fields=fields,
            has_default_constructor=has_default,
        )

    return ClassDef(
        name=name,
        namespace=namespace,
        methods=methods,
        has_default_constructor=has_default,
        constructors=constructors,
        fields=fields,
        bases=bases,
    )


def _unparsed_reason(diagnostic: str) -> str:
    return (
        f"the compiler rejected this declaration ({diagnostic}); it cannot be bound "
        "until it compiles. Usual causes: a header the parser could not find (add "
        "its directory to `clang.include_paths` in nativegate.yaml), or a name that "
        "comes from a macro whose definition is not reachable from this header "
        "(add the header that defines the macro, or -D it via `clang.defines`)."
    )


def _parse_function(
    cursor,
    module: ModuleIR,
    known_records: frozenset[str],
    errors: dict[int, str] | None = None,
    scalar_ref_functions: frozenset[str] = frozenset(),
) -> None:
    broken = _error_at(cursor, errors or {})
    if broken:
        module.skipped.append(
            SkippedSymbol(cursor.spelling, _unparsed_reason(broken))
        )
        return
    try:
        returns, returns_array = _map_type(
            cursor.result_type, known_records, is_return=True
        )
        parameters = _parameters(
            cursor,
            known_records,
            # Both conditions, and the second is the load-bearing one: the
            # extern "C" check narrows WHERE the convention can apply, and the
            # per-function opt-in is the user asserting it DOES apply — see
            # config.ClangConfig.scalar_ref_functions and the FLASH2 case.
            scalar_ref_ok=_is_extern_c(cursor)
            and (
                cursor.spelling in scalar_ref_functions
                or "*" in scalar_ref_functions
            ),
            extern_c=_is_extern_c(cursor),
        )
    except NativeTypeError as exc:
        module.skipped.append(SkippedSymbol(cursor.spelling, str(exc)))
        return
    module.functions.append(
        FunctionDef(
            name=cursor.spelling,
            parameters=parameters,
            returns=returns,
            # Without this a function in `namespace petro` is emitted as
            # `&bubble_point` — "use of undeclared identifier".
            namespace=_namespace_of(cursor),
            returns_array=returns_array,
        )
    )


def _walk(cursor, path: Path, handler, *, depth: int = 0) -> None:
    """Visit declarations in this file, descending through namespaces and
    `extern "C"` blocks — those group declarations without changing what is
    declared."""
    for child in cursor.get_children():
        if child.kind in (
            _cindex.CursorKind.NAMESPACE,
            _cindex.CursorKind.LINKAGE_SPEC,
            _cindex.CursorKind.UNEXPOSED_DECL,
        ):
            _walk(child, path, handler, depth=depth + 1)
            continue
        if not _in_file(child, path):
            continue
        handler(child)


# --- translation units --------------------------------------------------


_TU_CACHE: dict[tuple, object] = {}


def _translation_unit(path: Path, options: ClangOptions):
    if not _load():
        raise NativeTypeError(
            f"the Clang parser is unavailable: {unavailable_reason()}"
        )

    resolved = path.resolve()
    try:
        stamp = resolved.stat().st_mtime_ns
    except OSError:
        stamp = 0
    key = (str(resolved), stamp, options)
    cached = _TU_CACHE.get(key)
    if cached is not None:
        return cached

    index = _cindex.Index.create()
    flags = (
        _cindex.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES
        | _cindex.TranslationUnit.PARSE_INCOMPLETE
    )
    tu = index.parse(str(resolved), args=options.command_line(resolved), options=flags)
    _TU_CACHE[key] = tu
    return tu


def _diagnostics(tu) -> list[str]:
    """Errors worth telling the user about.

    A header that doesn't compile still yields a partial AST, and nativegate
    binds whatever parsed — but silently binding half a header because an
    `#include` was unreachable is exactly the failure the regex parser was
    criticised for. Surface it.
    """
    messages = []
    for diagnostic in tu.diagnostics:
        if diagnostic.severity < _cindex.Diagnostic.Error:
            continue
        location = diagnostic.location
        where = (
            f"{Path(location.file.name).name}:{location.line}"
            if location and location.file
            else "?"
        )
        messages.append(f"{where}: {diagnostic.spelling}")
    return messages


def _fatal_diagnostics(tu) -> list[str]:
    """Errors that make the rest of the parse untrustworthy (a missing include)."""
    return [
        d.spelling
        for d in tu.diagnostics
        if d.severity >= _cindex.Diagnostic.Fatal
    ]


def _error_lines(tu, path: Path) -> dict[int, str]:
    """Line -> message for compiler errors raised *in this header*."""
    resolved = path.resolve()
    lines: dict[int, str] = {}
    for diagnostic in tu.diagnostics:
        if diagnostic.severity < _cindex.Diagnostic.Error:
            continue
        location = diagnostic.location
        if location is None or location.file is None:
            continue
        try:
            same_file = Path(location.file.name).resolve() == resolved
        except OSError:
            same_file = str(location.file.name) == str(resolved)
        if same_file:
            lines.setdefault(location.line, diagnostic.spelling)
    return lines


def _user_roots(path: Path, options: ClangOptions) -> tuple[Path, ...]:
    """Directories whose headers belong to this project rather than the system.

    Records defined under these count as bindable types; records defined under
    the SDK or the standard library do not — `std::vector` is "defined" in the
    translation unit too, and treating it as a known record would emit a
    py::class_ for it.
    """
    roots = [path.parent]
    for include in options.include_paths:
        try:
            roots.append(Path(include).resolve())
        except OSError:
            continue
    return tuple(roots)


def _under(candidate: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _defined_records(tu, path: Path, options: ClangOptions) -> set[str]:
    """Names of records defined in this header or in a project header it includes."""
    roots = _user_roots(path, options)
    names: set[str] = set()

    def visit(cursor):
        for child in cursor.get_children():
            if (
                child.kind in _RECORD_KINDS()
                and child.is_definition()
                and child.spelling
            ):
                location = child.location
                if location is not None and location.file is not None:
                    try:
                        origin = Path(location.file.name).resolve()
                    except OSError:
                        origin = Path(location.file.name)
                    if origin == path or _under(origin, roots):
                        names.add(child.spelling)
            visit(child)

    visit(tu.cursor)
    return names


def defined_record_names(
    path: Path, options: ClangOptions | None = None
) -> frozenset[str]:
    """Records *defined* (not merely forward-declared) in this header.

    Unlike the regex reader's textual scan, a class only counts here if the
    AST says its definition is in this file — a forward declaration or a
    mention inside a comment or a disabled `#ifdef` branch does not.
    """
    options = options or ClangOptions()
    if not is_available():
        return frozenset()
    tu = _translation_unit(path, options)
    resolved = path.resolve()

    names: set[str] = set()

    def visit(cursor):
        if cursor.kind in _RECORD_KINDS() and cursor.is_definition() and cursor.spelling:
            names.add(cursor.spelling)

    _walk(tu.cursor, resolved, visit)
    return frozenset(names)


def parse_header(
    path: Path,
    expose: ExposeConfig,
    extra_known_records: frozenset[str] = frozenset(),
    options: ClangOptions | None = None,
) -> ModuleIR:
    """Parse a C++ header into a language-neutral ModuleIR via the Clang AST.

    Signature-compatible with `cpp_regex.parse_header`, plus `options` for the
    compiler flags the AST parse needs (standard, include paths, defines).

    A symbol is included if it carries `[[nativegate::expose]]` OR is named in
    nativegate.yaml's `expose:` block. `extra_known_records` names types defined
    in sibling headers of the same service — all of a service's headers compile
    into one extension, so such a type is complete by the time pybind11 sees it
    even though it is incomplete here.
    """
    options = options or ClangOptions()
    resolved = path.resolve()
    tu = _translation_unit(path, options)

    module = ModuleIR(name=path.stem, language="cpp", source_file=str(path))
    module.diagnostics = _diagnostics(tu)

    fatal = _fatal_diagnostics(tu)
    if fatal:
        # A missing #include stops clang from reporting anything further, and
        # every unresolved name silently becomes `int` under error recovery.
        # The AST past that point is fiction — binding it would generate a
        # service whose signatures disagree with the C++ it calls — so the
        # whole header is refused, loudly.
        module.skipped.append(
            SkippedSymbol(path.name, _unparsed_reason("; ".join(fatal)))
        )
        return module

    # Pass 1: every record the service could bind — those defined in this file
    # (so a method returning a type declared later still resolves) plus those
    # defined in headers this one #includes from the service's own directories.
    # An included type is complete and belongs to whichever header declared it;
    # refusing it here would drop every method that uses one.
    defined_here = _defined_records(tu, resolved, options)
    errors = _error_lines(tu, path)
    known_records = frozenset(defined_here) | extra_known_records

    # Pass 2: build the IR.
    scalar_refs = frozenset(getattr(options, "scalar_ref_functions", ()) or ())

    def build(cursor):
        kind = cursor.kind

        if kind in _RECORD_KINDS():
            if not cursor.is_definition() or not cursor.spelling:
                return  # forward declaration, or an anonymous record
            if not expose.is_exposed(cursor.spelling):
                return
            parsed = _parse_record(cursor, module, known_records, errors)
            if isinstance(parsed, StructDef):
                module.structs.append(parsed)
            else:
                module.classes.append(parsed)
            return

        if kind == _cindex.CursorKind.CLASS_TEMPLATE:
            if expose.is_exposed(cursor.spelling):
                module.skipped.append(
                    SkippedSymbol(
                        cursor.spelling,
                        "is a class template; pybind11 binds instantiations, "
                        "not templates. A typedef does NOT help — measured: "
                        "libclang reports Buffer<double> with no members, both "
                        "with and without an explicit instantiation, so there "
                        "is nothing to bind. Write a non-template class or free "
                        "functions over the instantiation you need and expose "
                        "those, or exclude it in nativegate.yaml.",
                    )
                )
            return

        if kind == _cindex.CursorKind.FUNCTION_TEMPLATE:
            if expose.is_exposed(cursor.spelling):
                module.skipped.append(
                    SkippedSymbol(
                        cursor.spelling,
                        "is a function template; pybind11 binds instantiations, not "
                        "templates. Add a non-template overload for the types you "
                        "need, or exclude it in nativegate.yaml.",
                    )
                )
            return

        if kind == _cindex.CursorKind.FUNCTION_DECL:
            if not expose.is_exposed(cursor.spelling):
                return
            _parse_function(
                cursor,
                module,
                known_records,
                errors,
                scalar_ref_functions=scalar_refs,
            )
            return

        if kind == _cindex.CursorKind.TYPEDEF_DECL:
            # `typedef struct { double x; } Point;` — the record itself is
            # anonymous, so bind it under the typedef name.
            underlying = cursor.underlying_typedef_type
            decl = underlying.get_declaration()
            if (
                decl is not None
                and decl.kind in _RECORD_KINDS()
                and decl.is_definition()
                and not decl.spelling
                and expose.is_exposed(cursor.spelling)
            ):
                parsed = _parse_record(decl, module, known_records, errors)
                parsed.name = cursor.spelling
                if isinstance(parsed, StructDef):
                    module.structs.append(parsed)
                else:
                    module.classes.append(parsed)

    _walk(tu.cursor, resolved, build)

    # Free functions overload just as members do: the same &name ambiguity
    # applies, and two same-named HTTP routes shadow each other.
    _mark_overloads(module.functions)

    # One macro spelled 28 times is one problem, not 28.
    seen: set[tuple[str, str]] = set()
    deduped = []
    for entry in module.skipped:
        key = (entry.name, entry.reason)
        if key not in seen:
            seen.add(key)
            deduped.append(entry)
    module.skipped = deduped

    return module
