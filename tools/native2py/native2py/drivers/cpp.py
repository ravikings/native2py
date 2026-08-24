"""The C++ oracle driver generator (T7, design-verification-layers.md §2).

Mirrors `drivers/fortran.py` (T3) in every load-bearing respect — same
`DriverResult` shape, same determinism contract (spec section 4: the source
is a pure function of `(document, module, headers, return_kinds)`, no
timestamps, no absolute paths), same "replay golden.json's recorded calls
verbatim, in file order, never `plan()`" rule (spec section 2.2) — but emits
one C++ translation unit instead of one Fortran program unit.

Turns a `(golden.json document, ModuleIR)` pair into ONE C++ driver that:

* replays exactly the calls `golden.json` recorded, in file order, with the
  file's recorded `arguments`/`constructor_arguments`;
* constructs an instance-method entry's object from the file's recorded
  `constructor_arguments` (spec section 2.9);
* prints one wire-format line per observable value (spec section 2.5),
  tab-separated, via `wire.slots_for_entry` — the single source of truth
  T3/T7/T5 all read from, so "which slots does this call produce" cannot
  drift between the Fortran and C++ generators;
* packs every float by `memcpy`-ing into a `std::uint64_t` and printing
  `%016llx` — **never** a union, **never** a pointer cast: both are strict-
  aliasing violations an optimiser is entitled to miscompile (spec section
  2.4, called out explicitly for C++);
* widens `float` to `double` before that `memcpy` — unconditionally, on
  every float value this driver prints, so a value that started out
  `double` is merely widened to itself (exact, a no-op) and a value that
  started out `float` never touches the 64-bit channel with undefined
  padding bits (spec section 2.4's "narrow floats widen; they do not get
  their own channel").

**Scope decision, not spec text (flagged per the task brief rather than
decided silently).** Spec section 2.9 says "C++ free functions and static
methods: full support" without carving out array/buffer parameters, and
this module does not implement them: a parameter that is an array
(`Parameter.is_array`), a raw-pointer buffer (`Parameter.length_param` /
`Parameter.is_mutable_buffer`), or a `std::vector<T>` return
(`returns_array`) is recorded as skipped, with a reason, exactly like a
struct-by-value entry is (spec section 2.9's one explicit v1 skip). Building
precise, allocation-free marshalling for those shapes was judged out of
proportion to this task's slice — the explicitly enumerated test rows this
module must cover are free function, static method, instance method with
constructor arguments, and the struct-by-value skip, and none of those need
arrays. Revisit this if a later task's fixture needs a bound array/buffer
routine oracle-checked.

**wire.py note.** Unlike `drivers/fortran.py` (written before `wire.py`
existed), this module imports `wire.slots_for_entry` directly rather than
reimplementing slot enumeration — see wire.py's module docstring for why
that single source of truth exists.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .. import wire
from ..ir import ClassDef, ModuleIR, Parameter

# --- result ----------------------------------------------------------------


@dataclass
class DriverResult:
    source: str
    driver_sha256: str
    skipped: dict = field(default_factory=dict)


class _SkipEntry(Exception):
    """Raised internally when one entry cannot be emitted as a call."""


# --- C++ literal formatting --------------------------------------------------


def _float_literal(value) -> str:
    """A C++ literal that is unambiguously a `double` (no `f` suffix)."""
    text = repr(float(value))
    if "e" in text or "E" in text:
        return text
    if "." not in text:
        text += ".0"
    return text


def _int_literal(value) -> str:
    return str(int(value))


def _bool_literal(value) -> str:
    return "true" if value else "false"


def _str_literal(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _quote(text: str) -> str:
    """A C++ string literal for a driver-internal label (key/slot)."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _scalar_literal(value, python_type: str) -> str:
    if python_type == "bool":
        return _bool_literal(value)
    if python_type == "int":
        return _int_literal(value)
    if python_type == "float":
        return _float_literal(value)
    if python_type == "str":
        return _str_literal(value)
    raise _SkipEntry(f"no C++ literal for value {value!r} of type {python_type!r}")


# --- C++ type spellings -----------------------------------------------------


def _is_float32(native_type: str | None) -> bool:
    spelling = (native_type or "").strip().rstrip("&").strip()
    return spelling in ("float", "const float", "float const")


def _decl_type(python_type: str, is_float32: bool) -> str:
    """The local variable type this driver declares for one scalar value.

    Deliberately independent of the exact native spelling except for the
    float/double distinction, which is the one distinction that changes
    which channel a value must go through before printing (spec section
    2.4). An `int`-family or `std::string` local's exact width/spelling does
    not affect the bits the *callee* computes — the callee's own parameter
    type governs that at the call site, via ordinary C++ implicit
    conversion — so a single stable local type is used for every such value.
    """
    if python_type == "float":
        return "float" if is_float32 else "double"
    if python_type == "int":
        return "long long"
    if python_type == "bool":
        return "bool"
    if python_type == "str":
        return "std::string"
    raise _SkipEntry(f"no C++ declaration for type {python_type!r}")


def _pointee_native_type(native_type: str | None) -> str | None:
    """Strip one trailing `*` from a recorded native pointer spelling.

    `Parameter.is_scalar_ref` params carry the pointer spelling
    (`"double*"`) — the local variable this driver declares to take the
    address of has to be the pointee type, or `&var` does not convert to
    the callee's parameter type.
    """
    if native_type is None:
        return None
    spelling = native_type.strip()
    if spelling.endswith("*"):
        return spelling[:-1].strip()
    return None


# --- scope guards: what this generator (v1) will not attempt ---------------


def _is_struct_value(value) -> bool:
    return isinstance(value, dict) and "__struct__" in value


def _refuse_unsupported_shapes(fn_or_method, entry: dict) -> None:
    """Hard-skip anything outside this module's v1 scope. See module docstring."""
    for param in fn_or_method.parameters:
        if param.is_array or getattr(param, "length_param", None) or getattr(
            param, "is_mutable_buffer", False
        ):
            raise _SkipEntry(
                f"array/buffer parameter '{param.name}' is not supported by the "
                "C++ driver (v1) — see drivers/cpp.py's module docstring"
            )
    if getattr(fn_or_method, "returns_array", False):
        raise _SkipEntry(
            "a std::vector<T> return is not supported by the C++ driver (v1) — "
            "see drivers/cpp.py's module docstring"
        )
    for value in (entry.get("arguments") or []) + (entry.get("constructor_arguments") or []):
        if _is_struct_value(value):
            raise _SkipEntry("structs by value are not supported by the C++ driver (v1)")


# --- per-parameter plan ------------------------------------------------------


@dataclass
class _Slot:
    """One native call-site argument, resolved for one call."""

    param: Parameter
    expr: str  # the C++ expression to pass at the call site (a literal or a var name)
    var: str | None  # the local variable name, if one was declared; else None
    decl: str | None  # the full declaration+init statement, or None
    is_ref: bool  # True when this parameter is a scalar reference (address-of a local)


def _matching_constructor(cls: ClassDef, n: int) -> list[Parameter] | None:
    for ctor in cls.constructors:
        if len(ctor) == n:
            return ctor
    return None


def _plan_scalar(
    idx: int, prefix: str, position: int, param: Parameter | None, value, python_type: str
) -> _Slot:
    """Resolve one call-site argument (a plain value or a scalar reference)."""
    is_ref = bool(param is not None and getattr(param, "is_scalar_ref", False))
    if not is_ref:
        return _Slot(param=param, expr=_scalar_literal(value, python_type), var=None, decl=None, is_ref=False)

    is_float32 = _is_float32(_pointee_native_type(param.native_type)) if param.native_type else False
    decl_type = _pointee_native_type(param.native_type) or _decl_type(python_type, is_float32)
    var = f"n2p_{idx}_{prefix}{position}"
    decl = f"    {decl_type} {var} = {_scalar_literal(value, python_type)};"
    return _Slot(param=param, expr=f"&{var}", var=var, decl=decl, is_ref=True)


def _plan_call(
    idx: int, entry: dict, fn_or_method
) -> tuple[list[_Slot], list[Parameter]]:
    """Resolve every call-site argument for one entry.

    Returns (slots in call order, the parameter list `arguments` was
    recorded against). For C++ every parameter is Python-visible — unlike
    Fortran's `intent(out)`, there is no dropped native slot — so
    `entry["arguments"]` lines up with `fn_or_method.parameters` 1:1.
    """
    _refuse_unsupported_shapes(fn_or_method, entry)
    params = fn_or_method.parameters
    arguments = entry.get("arguments") or []
    if len(arguments) != len(params):
        raise _SkipEntry(
            "recorded arguments do not match the current signature "
            f"({len(arguments)} recorded, {len(params)} expected)"
        )
    slots = [
        _plan_scalar(idx, "arg", i, param, value, param.type)
        for i, (param, value) in enumerate(zip(params, arguments))
    ]
    return slots, params


def _plan_constructor(idx: int, cls: ClassDef, entry: dict) -> list[_Slot]:
    ctor_args = entry.get("constructor_arguments") or []
    matched = _matching_constructor(cls, len(ctor_args))
    slots: list[_Slot] = []
    for i, value in enumerate(ctor_args):
        param = matched[i] if matched is not None else None
        python_type = param.type if param is not None else _python_type_of(value)
        slots.append(_plan_scalar(idx, "ctor", i, param, value, python_type))
    return slots


def _python_type_of(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    raise _SkipEntry(f"no Python type recognised for constructor argument {value!r}")


# --- emitting the wire protocol ---------------------------------------------


def _print_scalar_slot(key: str, slot: str, expr: str, python_type: str) -> list[str]:
    key_q, slot_q = _quote(key), _quote(slot)
    if python_type == "float":
        return [
            "    {",
            f"        double n2p_dbl = static_cast<double>({expr});",
            "        std::uint64_t n2p_bits;",
            "        std::memcpy(&n2p_bits, &n2p_dbl, sizeof n2p_bits);",
            f'        std::printf("%s\\t%s\\t%016llx\\n", {key_q}, {slot_q}, '
            "(unsigned long long)n2p_bits);",
            "    }",
        ]
    if python_type == "int":
        return [
            "    {",
            f"        long long n2p_i = static_cast<long long>({expr});",
            f'        std::printf("%s\\t%s\\t%lld\\n", {key_q}, {slot_q}, n2p_i);',
            "    }",
        ]
    if python_type == "bool":
        return [
            "    {",
            f'        std::printf("%s\\t%s\\t%s\\n", {key_q}, {slot_q}, ({expr}) ? "1" : "0");',
            "    }",
        ]
    if python_type == "str":
        return [
            "    {",
            f"        std::string n2p_s = n2p_escape({expr});",
            f'        std::printf("%s\\t%s\\t%s\\n", {key_q}, {slot_q}, n2p_s.c_str());',
            "    }",
        ]
    raise _SkipEntry(f"no wire formatting for type {python_type!r}")


# --- emitting one call's C++ --------------------------------------------------


def _qualified(name: str, namespace: str | None) -> str:
    return f"{namespace}::{name}" if namespace else name


def _emit_call(
    idx: int,
    key: str,
    entry: dict,
    module: ModuleIR,
    classes_by_name: dict,
    functions_by_name: dict,
    return_kind: str | None = None,
) -> list[str]:
    """Returns the C++ statement lines for one call, including its wire output."""
    kind = entry.get("kind", "function")
    if kind == "function":
        fn = functions_by_name.get(entry.get("name") or key)
        if fn is None:
            raise _SkipEntry("no matching C++ function in the IR")
        cls = None
    elif kind in ("static", "method"):
        cls = classes_by_name.get(entry.get("class"))
        if cls is None:
            raise _SkipEntry(f"no matching C++ class {entry.get('class')!r} in the IR")
        fn = next((m for m in cls.methods if m.name == entry.get("name")), None)
        if fn is None:
            raise _SkipEntry(f"no matching method {entry.get('name')!r} on {cls.name!r} in the IR")
    else:
        raise _SkipEntry(f"unsupported entry kind {kind!r}")

    ir_function_like = fn  # duck-typed: has .returns and .parameters, like FunctionDef
    call_slots, visible_params = _plan_call(idx, entry, fn)

    lines: list[str] = []

    ctor_var = None
    if kind == "method":
        ctor_slots = _plan_constructor(idx, cls, entry)
        _refuse_unsupported_shapes_ctor(ctor_slots)
        for slot in ctor_slots:
            if slot.decl:
                lines.append(slot.decl)
        ctor_var = f"n2p_{idx}_instance"
        ctor_args = ", ".join(s.expr for s in ctor_slots)
        namespace = cls.namespace
        lines.append(f"    {_qualified(cls.name, namespace)} {ctor_var}({ctor_args});")

    for slot in call_slots:
        if slot.decl:
            lines.append(slot.decl)

    call_args = ", ".join(s.expr for s in call_slots)
    result_var = None
    has_result = fn.returns != "void"
    if kind == "static":
        target = f"{_qualified(cls.name, cls.namespace)}::{fn.name}"
    elif kind == "method":
        target = f"{ctor_var}.{fn.name}"
    else:
        target = _qualified(fn.name, getattr(fn, "namespace", None))

    call_expr = f"{target}({call_args})"
    if has_result:
        result_var = f"n2p_{idx}_result"
        is_float32 = fn.returns == "float" and (return_kind or "").strip().lower() == "float"
        decl_type = _decl_type(fn.returns, is_float32) if fn.returns != "void" else "void"
        lines.append(f"    {decl_type} {result_var} = {call_expr};")
    else:
        lines.append(f"    {call_expr};")

    # wire.slots_for_entry is the single source of truth for what to print.
    wire_slots = wire.slots_for_entry(entry, ir_function_like)
    arg_by_index = {i: slot for i, slot in enumerate(call_slots)}

    for wslot in wire_slots:
        if wslot.role == "return":
            if wslot.element is not None:
                raise _SkipEntry(
                    "a tuple/array return is not supported by the C++ driver (v1)"
                )
            lines.extend(_print_scalar_slot(key, str(wslot), result_var, fn.returns))
        else:
            slot = arg_by_index.get(wslot.arg_index)
            param = visible_params[wslot.arg_index] if wslot.arg_index < len(visible_params) else None
            if slot is None or not slot.is_ref:
                raise _SkipEntry(
                    f"argument {wslot.arg_index} was recorded as modified in place, "
                    "but is not a scalar-reference parameter the C++ driver (v1) can "
                    "read back"
                )
            if wslot.element is not None:
                raise _SkipEntry(
                    "an array in-place effect is not supported by the C++ driver (v1)"
                )
            python_type = param.type if param is not None else "float"
            lines.extend(_print_scalar_slot(key, str(wslot), slot.var, python_type))

    return lines


def _refuse_unsupported_shapes_ctor(ctor_slots: list[_Slot]) -> None:
    # Constructor arguments are already scope-checked for structs by
    # `_refuse_unsupported_shapes` (called from `_plan_call` before this
    # runs, on the same entry) — nothing further to check here today. Kept
    # as an explicit hook so a future constructor-side array/buffer case
    # fails loudly rather than silently compiling something wrong.
    return None


# --- assembling the whole driver ---------------------------------------------

_HELPERS = """\
static std::string n2p_escape(const std::string& raw) {
    // Percent-escape tab (%09), newline (%0A) and percent (%25) so a string
    // output containing one of them cannot break the wire protocol's line
    // format (spec section 2.4). Order matters in the caller's alphabet, but
    // here each input byte is classified once, so there is no double-escape
    // hazard the way there is composing string replacements in sequence.
    static const char hexd[] = "0123456789ABCDEF";
    std::string out;
    out.reserve(raw.size());
    for (unsigned char c : raw) {
        if (c == 9 || c == 10 || c == 37) {
            out.push_back('%');
            out.push_back(hexd[c / 16]);
            out.push_back(hexd[c % 16]);
        } else {
            out.push_back(static_cast<char>(c));
        }
    }
    return out;
}
"""


def generate_driver(
    document: dict,
    module: ModuleIR,
    headers: str | list[str],
    return_kinds: dict | None = None,
) -> DriverResult:
    """Build the driver source for `document`'s recorded entries.

    `headers` are the header(s) the driver `#include`s to see the
    declarations it calls — passed explicitly, the same convention
    `generators/pybind_gen.generate_bindings` uses, rather than read off
    `ModuleIR.source_file`: embedding whatever absolute path a parser run
    happened to capture would violate spec section 4's "no absolute paths"
    determinism rule.

    `return_kinds` maps an entry key to `"float"` or `"double"` — the native
    C++ spelling of that entry's return type — for the same reason
    `drivers/fortran.py` accepts `return_kinds`: the IR's `.returns` is
    already collapsed to the Python type and cannot distinguish `float` from
    `double`. An entry absent from it is assumed `double`.
    """
    if isinstance(headers, str):
        headers = [headers]
    return_kinds = return_kinds or {}

    classes_by_name = {cls.name: cls for cls in module.classes}
    functions_by_name = {fn.name: fn for fn in module.functions}
    skipped: dict = dict(document.get("skipped") or {})

    all_lines: list[str] = []
    for idx, (key, entry) in enumerate(sorted_entries(document)):
        try:
            lines = _emit_call(
                idx, key, entry, module, classes_by_name, functions_by_name, return_kinds.get(key)
            )
        except _SkipEntry as exc:
            skipped[key] = str(exc)
            continue
        all_lines.append(f"    // --- {key} ---")
        all_lines.extend(lines)

    source = _assemble(headers, all_lines)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return DriverResult(source=source, driver_sha256=digest, skipped=skipped)


def sorted_entries(document: dict):
    """`document["entries"]` in file order — never re-sorted (spec sec 2.2)."""
    return list((document.get("entries") or {}).items())


def _assemble(headers: list[str], lines: list[str]) -> str:
    include_lines = "\n".join(f'#include "{h}"' for h in headers)
    body = "\n".join(lines)
    return (
        "// Generated by native2py. Do not edit by hand — regenerate the oracle driver.\n"
        "#include <cstdio>\n"
        "#include <cstdint>\n"
        "#include <cstring>\n"
        "#include <string>\n"
        "\n"
        f"{include_lines}\n"
        "\n"
        f"{_HELPERS}\n"
        "int main() {\n"
        f"{body}\n"
        "\n"
        "    return 0;\n"
        "}\n"
    )
