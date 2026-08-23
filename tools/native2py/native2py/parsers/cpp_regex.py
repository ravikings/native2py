"""C++ header parser — regex/brace fallback reader.

The Clang AST parser in `cpp_ast.py` is the primary front end (design.md
section 7) and is used whenever libclang is importable. This module is the
fallback for machines without it: it works on the token and brace structure
of the header rather than on a full AST, which covers the declaration subset
native2py binds (classes, structs, free functions, `extern "C"` blocks) as
long as the header is self-contained.

What it structurally cannot do, and `cpp_ast.py` does:

* run the preprocessor — `#include`d types, `#define`d names and `#ifdef`
  branches are invisible here, and macro-mangled declarations are reported as
  unreadable rather than expanded;
* templates, `= delete`/`= default`, abstract classes, copy/move constructor
  detection, typedef and `using`-alias resolution, nested namespaces.

`native2py inspect --parser regex` selects it explicitly; otherwise see
`cpp.py` for how a backend is chosen.

What it is careful about, because the previous line-regex version was not:

* **Comments.** Prose and commented-out code were parsed as declarations.
* **Access specifiers.** `public:` was matched as a *return type*, so any
  class whose access label sat on its own line failed the whole header.
  Worse, when it didn't fail, private methods were bound as public.
* **Constructors.** They have no return type and never matched, so
  `has_default_constructor` was a guess and constructors taking arguments
  were unreachable from Python.
* **cv-qualifiers and pointers.** `const char*`, `const double&` and
  `double*` all raised and took the rest of the header down with them.

Unbindable symbols are now *skipped and reported* (see ModuleIR.skipped)
rather than aborting the file. One method taking a `std::vector` should not
cost you the other thirty in the same class.
"""

from __future__ import annotations

import re
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
    map_type,
    size_pairings,
)

_EXPOSE_ATTR = "[[native2py::expose]]"

_NAMESPACE_RE = re.compile(r"\bnamespace\s+(\w+)\s*\{")

# `class Foo final : public Bar {` / `struct Foo {`
_RECORD_RE = re.compile(
    r"\b(?P<kind>class|struct)\s+(?P<name>\w+)\s*"
    r"(?:final\s*)?(?::(?P<bases>[^{;]*))?\{",
)

# One entry of a base-clause: `public Bar`, `virtual public ns::Bar`.
_BASE_RE = re.compile(
    r"^(?:virtual\s+)?(?:(?P<access>public|private|protected)\s+)?"
    r"(?:virtual\s+)?(?P<name>[\w:]+)$"
)

_ACCESS_RE = re.compile(r"^\s*(public|private|protected)\s*:", re.IGNORECASE)

# A declarator: optional specifier tokens, a name, a parenthesised argument
# list, optional trailing qualifiers.
_DECLARATOR_RE = re.compile(
    r"^(?P<spec>[\w\s:<>,&*~\[\]]*?)"
    r"(?P<name>~?\w+)\s*"
    r"\((?P<params>[^()]*)\)"
    r"(?P<trailing>[\w\s:=&*]*)$",
    re.DOTALL,
)

# Specifier keywords that carry no type information.
_DROPPED_SPECIFIERS = frozenset(
    {"static", "virtual", "inline", "explicit", "friend", "constexpr", "extern"}
)


# --- lexical cleanup ----------------------------------------------------


def _strip_comments(source: str) -> str:
    """Remove // and /* */ comments, preserving newlines so brace matching and
    any future line reporting stay aligned with the original file."""
    out = []
    i, n = 0, len(source)
    while i < n:
        if source.startswith("//", i):
            while i < n and source[i] != "\n":
                i += 1
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            chunk = source[i : end + 2 if end != -1 else n]
            out.append("\n" * chunk.count("\n"))
            i = end + 2 if end != -1 else n
        elif source[i] in "\"'":
            quote = source[i]
            out.append(source[i])
            i += 1
            while i < n and source[i] != quote:
                if source[i] == "\\":
                    out.append(source[i])
                    i += 1
                if i < n:
                    out.append(source[i])
                    i += 1
            if i < n:
                out.append(source[i])
                i += 1
        else:
            out.append(source[i])
            i += 1
    return "".join(out)


def _strip_preprocessor(source: str) -> str:
    """Drop #include/#define/#if lines.

    native2py does not run a preprocessor, so a declaration whose name comes
    from a macro (the F77_NAME mangling pattern used in Fortran bridge
    headers) cannot be resolved here. Those declarations survive this step
    and are reported as skipped by the declarator parser rather than
    silently dropped.
    """
    lines = []
    continued = False
    for line in source.splitlines():
        stripped = line.lstrip()
        if continued or stripped.startswith("#"):
            continued = line.rstrip().endswith("\\")
            lines.append("")
            continue
        lines.append(line)
    return "\n".join(lines)


def _matching_brace(source: str, open_index: int) -> int:
    """Index of the '}' matching the '{' at open_index, or -1."""
    depth = 0
    for i in range(open_index, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_top_level(text: str, separator: str = ",") -> list[str]:
    """Split on `separator` at paren/angle/bracket depth zero."""
    parts, depth, current = [], 0, ""
    for ch in text:
        if ch in "(<[":
            depth += 1
        elif ch in ")>]":
            depth -= 1
        if ch == separator and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


def _strip_inline_bodies(text: str) -> str:
    """Replace `{ ... }` blocks with ';' so an inline method body can't be
    mistaken for a declaration, and its internal ';' don't split statements.

    The replacement has to be a ';' rather than nothing: `double a() { ... }`
    carries no trailing semicolon of its own, so deleting the body outright
    would run `double a()` into whatever declaration follows it and lose both.
    """
    out, depth = [], 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
            if depth == 0:
                out.append(";")
        elif depth == 0:
            out.append(ch)
    return "".join(out)


# --- type mapping -------------------------------------------------------


class PointerLengthUnknown(NativeTypeError):
    """A pointer to a scalar whose length is not knowable from the type alone.

    Mirrors cpp_ast.PointerLengthUnknown so both C++ readers defer the same
    decision to the same place. Still a NativeTypeError, so any caller that
    does not know about the deferral keeps refusing it.
    """

    def __init__(self, message: str, *, element: str, is_mutable: bool) -> None:
        super().__init__(message)
        self.element = element
        self.is_mutable = is_mutable


# Pointee spellings that map to a scalar native2py can bind as a buffer.
_SCALAR_POINTER_ELEMENTS = {
    "double": "float", "float": "float", "long double": "float",
    "int": "int", "short": "int", "long": "int", "long long": "int",
    "unsigned": "int", "unsigned int": "int", "unsigned short": "int",
    "unsigned long": "int", "size_t": "int", "std::size_t": "int",
    "int8_t": "int", "int16_t": "int", "int32_t": "int", "int64_t": "int",
    "uint8_t": "int", "uint16_t": "int", "uint32_t": "int", "uint64_t": "int",
    "bool": "bool",
}


# `operator+`, `operator[]`, `operator()`, `operator<=`, `operator bool` ...
_OPERATOR_SPELLING_RE = re.compile(r"\boperator\s*(?:\[\]|\(\)|[^\s(\w]+|\w+)")


# `std::vector<T>` in the spellings a header actually uses, tolerating the
# spaces a formatter may leave inside the angle brackets.
_VECTOR_RE = re.compile(r"\bstd::vector\s*<\s*(.+)\s*>\s*$")


def _normalize_type(
    raw: str, known_records: frozenset[str] = frozenset(), *, is_return: bool = False
) -> tuple[str, bool]:
    """Map a C++ type to (python_type, is_array).

    Strips cv-qualifiers and reference markers. `char*`/`const char*` map to
    str. Class and struct types declared in the same header map to themselves,
    so a method returning a `PvtState` is bindable instead of being dropped.

    A pointer to a *known record* is fine as a parameter — pybind11 passes the
    held pointer and no ownership changes hands. A pointer *return* is not:
    the default return-value policy would take ownership of a pointer the C++
    side still owns, and the second free is a crash in someone else's stack
    frame. A pointer to anything else has no length information and is
    likewise refused.
    """
    # Checked on the RAW spelling, before cv-qualifiers and `&` are tokenised
    # away, because for std::vector the reference and its constness are the
    # whole question. pybind11 converts a Python list into a TEMPORARY vector,
    # so a non-const `std::vector<T>&` — which in C++ means "I will write to
    # your vector" — has its writes discarded when the temporary dies. Silently:
    # it compiles, runs, and hands back unmodified data. Same refusal, and the
    # same wording, as cpp_ast._refuse_mutable_vector_ref.
    raw_text = raw.strip()
    if "vector" in raw_text and "&" in raw_text and not is_return:
        before_ref = raw_text.split("&")[0]
        if _VECTOR_RE.search(before_ref) and "const" not in before_ref.split():
            raise NativeTypeError(
                f"'{raw_text}' is a mutable reference to a std::vector. pybind11 "
                "converts the caller's list into a temporary vector, so anything "
                "the function writes through this parameter is discarded when the "
                "call returns — silently, with no error. Take "
                "'const std::vector<T>&' if the data is input, or RETURN the "
                "modified vector, or exclude the symbol in native2py.yaml."
            )

    text = " ".join(raw.replace("*", " * ").replace("&", " & ").split())
    tokens = [t for t in text.split(" ") if t not in ("const", "volatile", "&")]

    pointer_depth = tokens.count("*")
    tokens = [t for t in tokens if t != "*"]
    base = " ".join(tokens).strip()

    if not base:
        raise NativeTypeError("empty type")

    if pointer_depth == 1 and base in ("char", "signed char", "unsigned char"):
        # Only `const char*` is a C string. A non-const `char*` is an output
        # buffer with no length convention: bound as a Python str, pybind11
        # materialises a temporary that C++ then writes through, and the write
        # is lost or corrupts freed memory. (Same refusal as cpp_ast.)
        if "const" in text.split("*")[0].split():
            return "str", False
        raise NativeTypeError(
            f"'{raw.strip()}' is a non-const 'char*': an output buffer with no "
            "length convention. Binding it as a Python str would have C++ write "
            "through a temporary. Return std::string (or take 'const char*' for "
            "input), or exclude the symbol in native2py.yaml."
        )

    vector_match = _VECTOR_RE.match(base)
    if vector_match:
        # pybind11's <pybind11/stl.h> converts std::vector<T> <-> list, by copy.
        inner, inner_is_array = _normalize_type(
            vector_match.group(1), known_records, is_return=is_return
        )
        if inner_is_array:
            raise NativeTypeError(
                f"'{raw.strip()}' is a nested container. pybind11 will convert "
                "it, but native2py has no IR shape for a list of lists, so the "
                "generated Pydantic model would be wrong. Flatten it (pass the "
                "data and its shape separately), or exclude the symbol."
            )
        if inner not in ("float", "int", "bool", "str"):
            raise NativeTypeError(
                f"'{raw.strip()}' has element type '{inner}', which is not a "
                "scalar pybind11 converts element-wise. Use a vector of a "
                "numeric type or std::string, or exclude the symbol."
            )
        return inner, True

    if base in known_records:
        if pointer_depth == 0:
            return base, False
        if pointer_depth == 1 and not is_return:
            return base, False
        raise NativeTypeError(
            f"'{raw.strip()}' returns a raw pointer to '{base}'; binding it would "
            "hand ownership to Python while C++ still owns the object. Return by "
            "value or by reference instead."
        )

    if pointer_depth:
        # A capitalised unknown base is almost always a forward-declared class
        # rather than a numeric buffer; saying "cannot infer its length" there
        # would send you looking for the wrong fix.
        if base[:1].isupper():
            raise NativeTypeError(
                f"'{raw.strip()}' refers to '{base}', which is only forward-declared "
                "(an incomplete type). pybind11 needs the full definition to bind it. "
                f"Add the header that defines '{base}' to this service's native/ "
                "directory, or exclude the symbol in native2py.yaml."
            )
        # A pointer to a scalar may still be bindable as an array, if another
        # parameter of the same function carries its length. That needs the
        # whole signature, so the decision is deferred to _parse_params — the
        # same shape as cpp_ast.PointerLengthUnknown, and for the same reason.
        if base in _SCALAR_POINTER_ELEMENTS:
            raise PointerLengthUnknown(
                f"'{raw.strip()}' is a pointer; native2py cannot infer its "
                "length. Wrap it in a type that carries its own size "
                "(std::vector, a span, or a struct), pass its length in an "
                "argument named after it (`n`, `n_values`, `values_len`, ...), "
                "or exclude the symbol in native2py.yaml.",
                element=_SCALAR_POINTER_ELEMENTS[base],
                is_mutable="const" not in raw.split("*")[0].split(),
            )
        raise NativeTypeError(
            f"'{raw.strip()}' is a pointer; native2py cannot infer its length. "
            "Wrap it in a type that carries its own size (std::vector, a span, "
            "or a struct) or exclude the symbol in native2py.yaml."
        )

    return map_type(base), False


def _parse_params(raw: str, known_records: frozenset[str] = frozenset()) -> list[Parameter]:
    raw = raw.strip()
    if not raw or raw == "void":
        return []

    params: list[Parameter] = []
    deferred: dict[str, PointerLengthUnknown] = {}
    for index, part in enumerate(_split_top_level(raw)):
        # Drop a default argument: `int n = 4` -> `int n`.
        part = part.split("=", 1)[0].strip()
        if not part:
            continue

        # Array parameter spelled `double values[]`.
        is_array = bool(re.search(r"\[\s*\d*\s*\]$", part))
        part = re.sub(r"\[\s*\d*\s*\]$", "", part).strip()

        match = re.match(r"^(?P<type>.*?)(?P<name>\w+)$", part, re.DOTALL)
        if match and match.group("type").strip():
            type_text, name = match.group("type"), match.group("name")
        else:
            # Unnamed parameter: `double` / `const char*`.
            type_text, name = part, f"arg{index}"

        try:
            py_type, pointer_array = _normalize_type(type_text, known_records)
        except PointerLengthUnknown as exc:
            deferred[name] = exc
            params.append(Parameter(name=name, type=exc.element, is_array=True))
            continue
        params.append(
            Parameter(name=name, type=py_type, is_array=is_array or pointer_array)
        )

    if deferred:
        by_name = {p.name: p for p in params}
        paired = {array: length for length, array in size_pairings(params)}
        for name, exc in deferred.items():
            length = paired.get(name)
            if length is None:
                # Nothing names this pointer's length; refuse it as before.
                raise exc
            by_name[name].length_param = length
            by_name[name].is_mutable_buffer = exc.is_mutable

    return params


# --- declaration scanning -----------------------------------------------


def _statements(body: str):
    """Yield (statement_text, access_after) for each ';'-terminated statement,
    tracking access specifiers as they are encountered."""
    access = None
    for chunk in _split_top_level(_strip_inline_bodies(body), ";"):
        text = chunk
        while True:
            match = _ACCESS_RE.match(text)
            if not match:
                break
            access = match.group(1).lower()
            text = text[match.end() :]
        text = text.strip()
        if text:
            yield text, access


@dataclass
class _Decl:
    """One classified declaration.

    kind is "method", "ctor", "field", or "operator" — the last meaning a
    recognised operator this reader will not attempt to bind, so the caller
    can report it rather than drop it.
    """

    kind: str
    name: str = ""
    returns: str = ""
    params: str = ""
    exposed: bool = False


def _parse_declarator(statement: str, record_name: str | None) -> _Decl | None:
    """Classify one ';'-terminated declaration, or None if it isn't one."""
    exposed = _EXPOSE_ATTR in statement
    text = statement.replace(_EXPOSE_ATTR, " ").strip()

    # Checked FIRST, because a symbolic operator never reaches the branches
    # below: _DECLARATOR_RE's name group is `~?\w+`, which cannot match the
    # `+` in `operator+`, so `Vec2 operator+(const Vec2&) const` fell through
    # to the data-member branch and was reported as a field called "const".
    # cpp_ast binds these as Python special methods; this reader cannot parse
    # them reliably, so it names them honestly and says so.
    operator = _OPERATOR_SPELLING_RE.search(text)
    if operator and "(" in text:
        return _Decl(kind="operator", name=operator.group(0).strip(), exposed=exposed)

    match = _DECLARATOR_RE.match(text)
    if not match:
        # No argument list: a data member, or something we don't model.
        field = re.match(r"^(?P<type>.*?)(?P<name>\w+)$", text, re.DOTALL)
        if field and field.group("type").strip():
            return _Decl(
                kind="field",
                name=field.group("name"),
                returns=field.group("type"),
                exposed=exposed,
            )
        return None

    name = match.group("name")
    spec_tokens = [
        t for t in match.group("spec").split() if t not in _DROPPED_SPECIFIERS
    ]

    # A destructor is genuinely nothing to bind: pybind11 destroys the held
    # object itself, and Python has no `__del__` contract worth wiring to it.
    if name.startswith("~"):
        return None
    # Operators ARE bindable — cpp_ast binds them as Python special methods —
    # but this reader's declarator pattern cannot match `operator[]`,
    # `operator()` or `operator<` reliably, and half-parsing one is worse than
    # not parsing it. Reported rather than silently dropped, with the reason,
    # so the difference between the two backends is visible instead of being
    # discovered as a missing method.
    if "operator" in name:
        return _Decl(kind="operator", name=name, exposed=exposed)
    if record_name and name == record_name and not spec_tokens:
        return _Decl(kind="ctor", name=name, params=match.group("params"), exposed=exposed)
    if not spec_tokens:
        # No return type and not a constructor — a macro-generated name, or a
        # declaration this parser does not model.
        return None

    return _Decl(
        kind="method",
        name=name,
        returns=" ".join(spec_tokens),
        params=match.group("params"),
        exposed=exposed,
    )


def _unparsed_name(statement: str) -> str | None:
    """Best-effort identifier for a declaration that failed to parse, so the
    skip report names something a human can search the header for."""
    match = re.search(r"(\w+)\s*\(", statement)
    return match.group(1) if match else None


def _parse_bases(
    clause: str | None, kind: str, known_records: frozenset[str]
) -> list[str]:
    """Public base classes bound by this service, from a `: public Bar` clause.

    Only public bases are reported: with private or protected inheritance the
    Derived -> Base conversion is inaccessible, so naming it in
    `py::class_<Derived, Base>` would not compile. Bases the service does not
    bind are dropped for the same reason — pybind11 needs the base registered.
    """
    if not clause:
        return []
    default_access = "public" if kind == "struct" else "private"

    bases = []
    for part in _split_top_level(clause):
        match = _BASE_RE.match(part.strip())
        if not match:
            continue
        access = (match.group("access") or default_access).lower()
        base = match.group("name").split("::")[-1]
        if access == "public" and base in known_records:
            bases.append(base)
    return bases


def _parse_record(
    kind: str,
    name: str,
    body: str,
    namespace: str | None,
    module: ModuleIR,
    known_records: frozenset[str] = frozenset(),
    base_clause: str | None = None,
):
    """Build a ClassDef or StructDef from one class/struct body."""
    default_access = "public" if kind == "struct" else "private"
    bases = _parse_bases(base_clause, kind, known_records)

    methods: list[Method] = []
    fields: list[Parameter] = []
    constructors: list[list[Parameter]] = []
    saw_any_constructor = False

    for statement, access in _statements(body):
        effective = access or default_access
        decl = _parse_declarator(statement, name)
        if decl is not None and decl.kind == "operator":
            # Reported, not dropped. cpp_ast binds operators as Python special
            # methods; this reader cannot parse their declarations reliably, so
            # the difference is made visible instead of being discovered later
            # as a method that is simply missing.
            module.skipped.append(
                SkippedSymbol(
                    f"{name}::{decl.name}",
                    f"'{decl.name}' is an operator. The Clang AST parser binds "
                    "operators as Python special methods; this regex reader "
                    "cannot parse their declarations reliably. Install libclang "
                    '(pip install "native2py[clang]") to bind it, or expose a '
                    "named method instead.",
                )
            )
            continue
        if decl is not None and decl.kind == "operator":
            module.skipped.append(
                SkippedSymbol(
                    f"{name}::{decl.name}",
                    f"'{decl.name}' is an operator. The Clang AST parser binds "
                    "operators as Python special methods; this regex reader "
                    "cannot parse their declarations reliably. Install libclang "
                    '(pip install "native2py[clang]") to bind it, or expose a '
                    "named method instead.",
                )
            )
            continue
        if decl is None:
            continue

        if decl.kind == "ctor":
            saw_any_constructor = True
            if effective != "public":
                continue
            try:
                constructors.append(_parse_params(decl.params, known_records))
            except NativeTypeError as exc:
                module.skipped.append(
                    SkippedSymbol(f"{name}::{name}", str(exc))
                )
            continue

        if effective != "public":
            continue

        if decl.kind == "field":
            try:
                py_type, is_array = _normalize_type(decl.returns, known_records)
            except NativeTypeError as exc:
                module.skipped.append(SkippedSymbol(f"{name}.{decl.name}", str(exc)))
                continue
            fields.append(Parameter(name=decl.name, type=py_type, is_array=is_array))
            continue

        try:
            returns, _ = _normalize_type(decl.returns, known_records, is_return=True)
            parameters = _parse_params(decl.params, known_records)
        except NativeTypeError as exc:
            module.skipped.append(SkippedSymbol(f"{name}::{decl.name}", str(exc)))
            continue
        methods.append(
            Method(
                name=decl.name,
                parameters=parameters,
                returns=returns,
                is_static="static" in statement.split("(")[0],
            )
        )

    if kind == "struct" and not methods and not bases and fields:
        return StructDef(name=name, namespace=namespace, fields=fields)

    # A class with only non-default constructors cannot be `py::init<>()`.
    has_default = (not saw_any_constructor) or any(not p for p in constructors)
    return ClassDef(
        name=name,
        namespace=namespace,
        methods=methods,
        has_default_constructor=has_default,
        constructors=[c for c in constructors if c],
        fields=fields,
        bases=bases,
    )


def defined_record_names(path: Path) -> frozenset[str]:
    """Records *defined* (not merely forward-declared) in this header.

    A service's headers all compile into one extension, so a type
    forward-declared in one and defined in another is complete by the time
    pybind11 sees it. Callers union these across every header and feed the
    result back as `extra_known_records`.
    """
    return frozenset(m.group("name") for m in _RECORD_RE.finditer(path.read_text()))


def parse_header(
    path: Path,
    expose: ExposeConfig,
    extra_known_records: frozenset[str] = frozenset(),
) -> ModuleIR:
    """Parse a C++ header into a language-neutral ModuleIR.

    A symbol is included if it carries `[[native2py::expose]]` OR is named in
    native2py.yaml's `expose:` block (config takes precedence for legacy code
    that can't be annotated in place — see design.md section 9).

    `extra_known_records` names types defined in sibling headers of the same
    service; without it, a type only forward-declared here is treated as
    incomplete and any symbol using it is skipped.
    """
    source = _strip_preprocessor(_strip_comments(path.read_text()))

    namespace_match = _NAMESPACE_RE.search(source)
    namespace = namespace_match.group(1) if namespace_match else None

    module = ModuleIR(name=path.stem, language="cpp", source_file=str(path))

    # Pass 1: locate every record and blank its text out, so the free-function
    # scan cannot see member declarations. Collecting the names before any
    # body is parsed is what makes a method returning a type declared later in
    # the same header (`PvtState properties_at(...)`) resolvable — a single
    # forward pass would have to give up on it.
    records = []
    remainder = list(source)
    for match in _RECORD_RE.finditer(source):
        open_index = source.index("{", match.end() - 1)
        close_index = _matching_brace(source, open_index)
        if close_index == -1:
            continue

        records.append(
            (
                match.group("kind"),
                match.group("name"),
                source[open_index + 1 : close_index],
                match.group("bases"),
            )
        )
        for i in range(match.start(), close_index + 1):
            if remainder[i] != "\n":
                remainder[i] = " "

    # A forward declaration (`class FluidModel;`) names an INCOMPLETE type.
    # pybind11 needs a complete type to build a caster, so binding a method
    # that takes one fails at compile time with
    # "incomplete type 'FluidModel' used in type trait expression" — a wall
    # of template errors pointing into pybind11 internals, not at your header.
    # Treating forward declarations as bindable is what used to produce that.
    #
    # The type may still be completed by a sibling header in the same service
    # (all of a service's headers compile into one extension), so the caller
    # passes those names in via `extra_known_records`.
    defined_here = frozenset(name for _kind, name, _body, _bases in records)
    known_records = defined_here | extra_known_records

    # Pass 2: parse the bodies.
    for kind, record_name, body, base_clause in records:
        if not expose.is_exposed(record_name):
            continue
        parsed = _parse_record(
            kind, record_name, body, namespace, module, known_records, base_clause
        )
        if isinstance(parsed, StructDef):
            module.structs.append(parsed)
        else:
            module.classes.append(parsed)

    # Free functions, including everything inside `extern "C"` and `namespace`
    # blocks — those braces group declarations, they don't scope them away.
    outside = "".join(remainder)
    outside = re.sub(r'\bextern\s+"C"\s*\{', " ", outside)
    outside = re.sub(r"\bnamespace\s+\w*\s*\{", " ", outside)
    outside = outside.replace("}", " ")

    for statement, _access in _statements(outside):
        decl = _parse_declarator(statement, None)
        if decl is not None and decl.kind == "operator":
            # Reported, not dropped. cpp_ast binds operators as Python special
            # methods; this reader cannot parse their declarations reliably, so
            # the difference is made visible instead of being discovered later
            # as a method that is simply missing.
            module.skipped.append(
                SkippedSymbol(
                    decl.name,
                    f"'{decl.name}' is an operator. The Clang AST parser binds "
                    "operators as Python special methods; this regex reader "
                    "cannot parse their declarations reliably. Install libclang "
                    '(pip install "native2py[clang]") to bind it, or expose a '
                    "named method instead.",
                )
            )
            continue
        if decl is not None and decl.kind == "operator":
            module.skipped.append(
                SkippedSymbol(
                    decl.name,
                    f"'{decl.name}' is an operator. The Clang AST parser binds "
                    "operators as Python special methods; this regex reader "
                    "cannot parse their declarations reliably. Install libclang "
                    '(pip install "native2py[clang]") to bind it, or expose a '
                    "named method instead.",
                )
            )
            continue
        if decl is None or decl.kind != "method":
            # A statement that clearly wants to be a function declaration but
            # didn't parse is reported, not dropped. Returning an empty module
            # for a header full of declarations reads as "nothing to bind here"
            # when the truth is "this parser could not read it" — which is what
            # macro-mangled extern "C" bridge headers look like.
            if decl is None and "(" in statement and not statement.startswith("typedef"):
                name = _unparsed_name(statement)
                if name:
                    module.skipped.append(
                        SkippedSymbol(
                            name,
                            "could not parse this declaration; if the name comes from a "
                            "macro, native2py does not run a preprocessor — declare the "
                            "symbol with its expanded name to bind it.",
                        )
                    )
            continue
        if not expose.is_exposed(decl.name):
            continue
        try:
            returns, _ = _normalize_type(decl.returns, known_records, is_return=True)
            parameters = _parse_params(decl.params, known_records)
        except NativeTypeError as exc:
            module.skipped.append(SkippedSymbol(decl.name, str(exc)))
            continue
        module.functions.append(
            FunctionDef(name=decl.name, parameters=parameters, returns=returns)
        )

    # One macro spelled 28 times is one problem, not 28. Collapse identical
    # (name, reason) pairs so the report stays readable.
    seen: set[tuple[str, str]] = set()
    deduped = []
    for entry in module.skipped:
        key = (entry.name, entry.reason)
        if key not in seen:
            seen.add(key)
            deduped.append(entry)
    module.skipped = deduped

    return module
