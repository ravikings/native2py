"""pybind11 binding generator (design.md section 7)."""

from __future__ import annotations

import re

from ..ir import ClassDef, FunctionDef, Method, ModuleIR, Parameter, StructDef

# Python type name -> the C++ spelling pybind11 needs in an init<> signature.
# Only the scalar types need translating; record types are already spelled
# with their C++ name in the IR.
_CPP_SPELLING = {
    "int": "int",
    "float": "double",
    "bool": "bool",
    "str": "std::string",
    "None": "void",
}


def _qualified(name: str, namespace: str | None) -> str:
    return f"{namespace}::{name}" if namespace else name


# Type spellings that are already complete on their own: qualifying them with
# the enclosing namespace would invent `petro::double`.
_BUILTIN_WORDS = {
    "void", "bool", "char", "wchar_t", "char8_t", "char16_t", "char32_t",
    "short", "int", "long", "float", "double", "signed", "unsigned",
    "const", "volatile", "auto",
}
_STDINT_RE = re.compile(r"^(u?int(_least|_fast)?(8|16|32|64)_t|u?intptr_t|u?intmax_t|size_t|ssize_t|ptrdiff_t)$")


def _needs_qualification(spelling: str) -> bool:
    """True when `spelling` names a user type written without its namespace."""
    core = spelling.replace("const", " ").replace("volatile", " ")
    core = core.replace("&", " ").replace("*", " ").strip()
    if not core or "::" in core or "<" in core:
        return False
    tokens = core.split()
    if len(tokens) != 1:
        # `unsigned long`, `long double`, ... — all builtin words.
        return False
    token = tokens[0]
    return token not in _BUILTIN_WORDS and not _STDINT_RE.match(token)


def _qualify_spelling(spelling: str, namespace: str | None) -> str:
    if namespace and _needs_qualification(spelling):
        # Keep any cv/ref decoration in place, qualify only the type name.
        return re.sub(r"([A-Za-z_]\w*)", lambda m: f"{namespace}::{m.group(1)}"
                      if m.group(1) not in _BUILTIN_WORDS else m.group(1), spelling, count=0)
    return spelling


def _param_spelling(param: Parameter, namespace: str | None) -> str:
    """The C++ type to emit for one parameter.

    Prefer the native spelling the parser recorded; `Parameter.type` is the
    *Python* name and is lossy by construction (uint64_t, int and an unscoped
    enum all collapse to "int"), so `_CPP_SPELLING` is only the fallback for
    IR produced before native types were recorded.
    """
    if param.native_type is not None:
        # Emitted verbatim. The parser resolves each type through its own
        # declaration and qualifies it there (`petro::Correlation`,
        # `std::uint64_t`), so it already knows the answer — whereas this
        # generator only knows the namespace of the record being bound.
        # Re-qualifying here guessed that every unqualified name lived in that
        # same namespace, so a `ns::Widget` method taking a global-namespace
        # `Color` emitted `ns::Color` and failed to compile with "no type
        # named 'Color' in namespace 'ns'". A name the parser left unqualified
        # is one that resolves unqualified at namespace scope, which is
        # exactly where the generated binding writes it.
        return param.native_type
    return _CPP_SPELLING.get(param.type, _qualified(param.type, namespace))


def _init_signature(params: list[Parameter], namespace: str | None) -> str:
    """C++ type list for `py::init<...>`."""
    return ", ".join(_param_spelling(p, namespace) for p in params)


def _overloaded_target(
    target: str,
    params: list[Parameter],
    namespace: str | None,
    is_const: bool = False,
) -> str:
    """`&f` is ambiguous once `f` is overloaded; spell the signature out."""
    args = ", ".join(_param_spelling(p, namespace) for p in params)
    extra = ", py::const_" if is_const else ""
    return f"py::overload_cast<{args}>(&{target}{extra})"


def _method_target(method: Method, qualified: str, namespace: str | None) -> str:
    target = f"{qualified}::{method.name}"
    if getattr(method, "is_overloaded", False):
        return _overloaded_target(
            target, method.parameters, namespace, getattr(method, "is_const", False)
        )
    return f"&{target}"


def _emit_record(lines: list[str], record: ClassDef | StructDef) -> None:
    # Each record carries its own namespace: a service merging headers from two
    # namespaces used to have the first record's namespace applied to all of
    # them, and a struct-only service got None and emitted unqualified names.
    namespace = record.namespace
    qualified = _qualified(record.name, namespace)
    # `py::class_<Derived, Base>` is what teaches pybind11 the relationship:
    # without it, inherited methods are missing from Python and a Base& return
    # never upcasts to the derived wrapper.
    bases = getattr(record, "bases", [])
    template_args = ", ".join([qualified, *(_qualified(b, namespace) for b in bases)])
    lines.append(f'    py::class_<{template_args}>(m, "{record.name}")')

    body: list[str] = []

    # A struct with a const or reference member has no default constructor;
    # emitting py::init<>() for one is a compile error.
    if record.has_default_constructor:
        body.append("        .def(py::init<>())")

    if not isinstance(record, StructDef):
        for ctor in record.constructors:
            body.append(f"        .def(py::init<{_init_signature(ctor, namespace)}>())")
        for method in record.methods:
            target = _method_target(method, qualified, namespace)
            body.append(f'        .def("{method.name}", {target})')

    for field in record.fields:
        # A const member cannot be bound read/write: pybind11's def_readwrite
        # assigns through the pointer-to-member and fails to compile.
        binder = "def_readonly" if getattr(field, "is_const", False) else "def_readwrite"
        body.append(
            f'        .{binder}("{field.name}", &{qualified}::{field.name})'
        )

    if not body:
        # A record with nothing bindable still needs a terminated statement.
        lines[-1] += ";"
        return

    for i, line in enumerate(body):
        lines.append(line + (";" if i == len(body) - 1 else ""))


def _buffer_parameters(func: FunctionDef) -> list[Parameter]:
    """Parameters bound from a bare `T*` whose length another argument carries."""
    return [p for p in func.parameters if getattr(p, "length_param", None)]


# numpy dtype for each element type native2py binds through a raw pointer.
_NUMPY_DTYPE = {"float": "double", "int": "int", "bool": "bool"}


def _buffer_lambda(func: FunctionDef) -> str:
    """A lambda binding `f(T* data, int n, ...)` as `f(data, ...)`.

    THE LENGTH DISAPPEARS FROM THE PYTHON SIGNATURE, on purpose. It is read off
    the buffer the caller actually passed, so `n` cannot disagree with the data
    — which is the entire failure mode of a C array API. The Fortran side has
    to keep its `n` (f2py fixes the signature) and validates it instead; here
    the generator controls the signature, so the mismatch is made unspellable.

    Mutable and read-only buffers are bound differently, and the difference is
    load-bearing rather than stylistic:

    * A **read-only** `const T*` is input. pybind11 may freely convert a list
      or a wrong-dtype array into a temporary, because nothing is written back.
      `forcecast` makes the endpoint convenient.

    * A **mutable** `T*` is one the callee writes through. Conversion there is
      catastrophic and silent: pybind11 would write into a temporary and
      discard it. Measured, before this was written — `py::array_t<double>`
      handed an int64 array accepted it, converted, and lost every write; and
      `request()` without `true` wrote straight through a `writeable=False`
      array. So a mutable buffer takes an untyped `py::array`, checks the dtype
      itself, and calls `request(true)`, which raises on a read-only array.
    """
    namespace = getattr(func, "namespace", None)
    target = _qualified(func.name, namespace)
    buffers = {p.name for p in _buffer_parameters(func)}
    lengths = {p.length_param for p in _buffer_parameters(func)}

    sig: list[str] = []
    body: list[str] = []
    call_args: list[str] = []
    arg_names: list[str] = []

    for param in func.parameters:
        if param.name in lengths:
            # Supplied from the buffer's own size; never asked of the caller.
            continue
        if param.name in buffers:
            dtype = _NUMPY_DTYPE[param.type]
            arg_names.append(param.name)
            if param.is_mutable_buffer:
                sig.append(f"py::array {param.name}")
                body.append(
                    f'            if (!{param.name}.dtype().is(py::dtype::of<{dtype}>()))\n'
                    f'                throw py::type_error("{func.name}(): \'{param.name}\' must be a '
                    f'numpy array of dtype {dtype}; native2py will not convert it because '
                    f'this function writes through the buffer and a converted array is a '
                    f'temporary whose writes would be discarded.");'
                )
                body.append(
                    f'            if ({param.name}.ndim() != 1)\n'
                    f'                throw py::value_error("{func.name}(): \'{param.name}\' must be 1-D.");'
                )
                # request(true) raises on a read-only array. Without it,
                # pybind11 writes straight through `writeable=False`.
                body.append(f"            auto {param.name}_info = {param.name}.request(true);")
            else:
                sig.append(
                    f"py::array_t<{dtype}, py::array::c_style | py::array::forcecast> "
                    f"{param.name}"
                )
                body.append(f"            auto {param.name}_info = {param.name}.request();")
            continue
        sig.append(f"{_param_spelling(param, namespace)} {param.name}")
        arg_names.append(param.name)

    for param in func.parameters:
        if param.name in buffers:
            const = "" if param.is_mutable_buffer else "const "
            dtype = _NUMPY_DTYPE[param.type]
            call_args.append(f"static_cast<{const}{dtype}*>({param.name}_info.ptr)")
        elif param.name in lengths:
            owner = next(p for p in _buffer_parameters(func) if p.length_param == param.name)
            call_args.append(
                f"static_cast<{param.cpp_spelling('int')}>({owner.name}_info.size)"
            )
        else:
            call_args.append(param.name)

    invocation = f"{target}({', '.join(call_args)})"
    body.append(
        f"            {invocation};" if func.returns == "None" else f"            return {invocation};"
    )

    py_args = "".join(f', py::arg("{n}")' for n in arg_names)
    lines = [f"[]({', '.join(sig)}) {{"]
    lines += body
    lines.append("        }")
    return "\n".join(lines) + py_args


def _function_target(func: FunctionDef) -> str:
    """`&f` for a free function, qualified by its own namespace.

    Free functions used to be emitted unqualified, so anything outside the
    global namespace failed with "use of undeclared identifier".
    """
    if _buffer_parameters(func):
        return _buffer_lambda(func)
    namespace = getattr(func, "namespace", None)
    target = _qualified(func.name, namespace)
    if getattr(func, "is_overloaded", False):
        return _overloaded_target(target, func.parameters, namespace)
    return f"&{target}"


def _base_first(classes: list[ClassDef]) -> list[ClassDef]:
    """Order classes so every base is registered before anything derived.

    `py::class_<Derived, Base>` requires Base to already exist in the module;
    registering them in header order raises "generic_type: type Base is not
    registered" at *import* time, long after the build succeeded. Headers are
    merged alphabetically, so this is not a rare ordering — it is a coin flip.

    A cycle is impossible in valid C++ (you cannot derive from an incomplete
    type), but the walk is guarded anyway so a malformed IR cannot hang codegen.
    """
    by_name = {cls.name: cls for cls in classes}
    ordered: list[ClassDef] = []
    placed: set[str] = set()

    def place(cls: ClassDef, pending: frozenset[str]) -> None:
        if cls.name in placed or cls.name in pending:
            return
        for base in cls.bases:
            parent = by_name.get(base)
            if parent is not None:
                place(parent, pending | {cls.name})
        placed.add(cls.name)
        ordered.append(cls)

    for cls in classes:
        place(cls, frozenset())
    return ordered


def generate_bindings(module: ModuleIR, header_names: str | list[str]) -> str:
    """Emit one pybind11 module binding every exposed symbol.

    `header_names` may be several headers: a service with foo.hpp and bar.hpp
    produces ONE extension including both. A Python extension can only have a
    single PYBIND11_MODULE (one init symbol per .so), so generating one
    bindings file per header would leave all but one orphaned — which is
    exactly what used to happen, silently dropping those classes.
    """
    if isinstance(header_names, str):
        header_names = [header_names]

    module_symbol = f"{module.name}_cpp"

    # Only when something is actually bound through a raw pointer: numpy.h
    # pulls in numpy's headers at build time and numpy at runtime, and a C++
    # service that binds no buffers should not acquire that dependency.
    needs_numpy = any(_buffer_parameters(fn) for fn in module.functions)

    lines = [
        "// Generated by native2py. Do not edit by hand — re-run `native2py generate`.",
        "#include <pybind11/pybind11.h>",
        # std::vector<T> <-> Python list. The conversion is a COPY in both
        # directions, which is why the parser refuses a non-const
        # `std::vector<T>&`: writes through one land in the temporary and are
        # discarded. See parsers/cpp_ast._refuse_mutable_vector_ref.
        "#include <pybind11/stl.h>",
        *(["#include <pybind11/numpy.h>"] if needs_numpy else []),
        "#include <cstdint>",
        "#include <string>",
        "#include <vector>",
        "",
        *[f'#include "{h}"' for h in header_names],
        "",
        "namespace py = pybind11;",
        "",
        f"PYBIND11_MODULE({module_symbol}, m) {{",
    ]

    # Structs first: a method returning one can only be bound after the type
    # itself is registered with pybind11.
    for struct in module.structs:
        _emit_record(lines, struct)

    for cls in _base_first(module.classes):
        _emit_record(lines, cls)

    for func in module.functions:
        lines.append(f'    m.def("{func.name}", {_function_target(func)});')

    lines.append("}")
    lines.append("")
    return "\n".join(lines)
