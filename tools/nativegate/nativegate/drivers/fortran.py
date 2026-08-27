"""The Fortran oracle driver generator (T3, design-verification-layers.md §2).

Turns a `(golden.json document, ModuleIR)` pair into ONE Fortran driver
translation unit that:

* replays exactly the calls `golden.json` recorded, in file order, with the
  file's recorded arguments (never `plan()` — see spec §2.2: the committed
  file may hold hand-edited inputs that `plan()` would not reproduce);
* re-inserts the native parameters the Python side never sees — `intent(out)`
  scalars/arrays and, for an `intent(out)` array, the size Fortran needs at
  the call site, taken from `length_param`;
* prints one wire-format line per observable value (spec §2.5), tab
  separated, floats as `transfer`-ed `integer(8)` in lowercase `Z16.16` hex,
  ints/bools by value, strings trimmed and percent-escaped.

Determinism (spec §4): the generated source is a pure function of
`(document, module)` — no timestamps, no absolute paths, no environment.
`driver_sha256` is the SHA-256 of exactly the bytes returned as `source`.

**wire.py note.** At the time this was written, `nativegate/wire.py` (T2, the
shared `slots_for_entry` source of truth) did not exist yet. The slot
enumeration below (`_slots_for_call`) is this module's OWN implementation of
the §2.4/§2.5 rules and is not guaranteed to agree byte-for-byte with
whatever T2 lands with — in particular the handling of an entry with more
than one observable output where one of the outputs is itself an array has
no worked example in the spec table (`return[<n>]` is only defined for a
*scalar* tuple element). This module extends that pattern to
`return[<n>][<m>]` for an array at tuple position `n`, and to a bare
`return[<n>]` (no outer index) for the single-output-is-an-array case. Both
are independent design calls, not spec text. If `wire.py` appears with a
`slots_for_entry` function, this module should import it and drop its own
copy rather than keep two implementations that can drift apart.

Similarly, `ir.py`'s `FunctionDef` carries no field for a function's native
*return* kind (only the already-collapsed Python type in `.returns`), so
there is no way to know from the IR alone whether a function's return value
is `real(4)` or `real(8)`. Rather than extend the shared IR schema for this
one generator, `generate_driver` accepts an optional `return_kinds` mapping
(`{entry_key: "real(4)" | "real(8)"}`); an entry absent from it is assumed
`real(8)`, matching every real routine in this codebase (`petro_api`'s
`real(dp)` with `dp = kind(1.0d0)`).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..ir import FunctionDef, ModuleIR, Parameter

# --- result -----------------------------------------------------------


@dataclass
class DriverResult:
    source: str
    driver_sha256: str
    skipped: dict = field(default_factory=dict)


class _SkipEntry(Exception):
    """Raised internally when one entry cannot be emitted as a call."""


# --- Fortran literal formatting -----------------------------------------


def _float_literal(value) -> str:
    """`repr`-exact double-precision Fortran literal (`d0` exponent)."""
    text = repr(float(value))
    if "e" in text or "E" in text:
        marker = "e" if "e" in text else "E"
        mantissa, _, exponent = text.partition(marker)
        if "." not in mantissa:
            mantissa += ".0"
        return f"{mantissa}d{exponent}"
    if "." not in text:
        text += ".0"
    return f"{text}d0"


def _int_literal(value) -> str:
    return str(int(value))


def _bool_literal(value) -> str:
    return ".true." if value else ".false."


def _str_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _quote(text: str) -> str:
    """A Fortran CHARACTER literal for a driver-internal label (key/slot)."""
    return "'" + text.replace("'", "''") + "'"


def _scalar_literal(value, python_type: str) -> str:
    if python_type == "bool":
        return _bool_literal(value)
    if python_type == "int":
        return _int_literal(value)
    if python_type == "float":
        return _float_literal(value)
    if python_type == "str":
        return _str_literal(value)
    raise _SkipEntry(f"no Fortran literal for value {value!r} of type {python_type!r}")


def _array_literal(values: list, python_type: str) -> str:
    return "(/ " + ", ".join(_scalar_literal(v, python_type) for v in values) + " /)"


# --- Fortran type declarations -------------------------------------------


def _is_real4(param: Parameter) -> bool:
    native = (param.native_type or "").lower().replace(" ", "")
    return native in ("real", "real*4", "real(4)", "real(kind=4)")


def _decl_type(python_type: str, is_real4: bool, native_type: str | None) -> str:
    if python_type == "float":
        return "real(4)" if is_real4 else "real(8)"
    if python_type == "int":
        return "integer"
    if python_type == "bool":
        return "logical"
    if python_type == "str":
        if native_type and native_type.lower().startswith("character"):
            return native_type
        return "character(len=256)"
    raise _SkipEntry(f"no Fortran declaration for type {python_type!r}")


# --- per-parameter plan ---------------------------------------------------


@dataclass
class _Slot:
    """One native call-site argument, resolved for one call."""

    param: Parameter
    var: str  # the local Fortran variable name
    decl: str  # the full declaration statement (without trailing newline)
    init: str | None  # an assignment statement, or None if uninitialised
    size: int | None  # element count, for arrays


def _plan_call(idx: int, key: str, entry: dict, fn: FunctionDef) -> tuple[list[_Slot], list[Parameter]]:
    """Resolve every native parameter for one call.

    Returns (slots in native parameter order, the python-visible parameter
    list in the same order golden.plan() built the recorded `arguments`).
    """
    if entry.get("kind", "function") != "function":
        raise _SkipEntry(
            "the Fortran driver (v1) only calls free functions/subroutines, "
            f"not entry kind {entry.get('kind')!r}"
        )

    visible_params = [p for p in fn.parameters if p.intent != "out"]
    arguments = entry.get("arguments") or []
    if len(arguments) != len(visible_params):
        raise _SkipEntry(
            "recorded arguments do not match the current signature "
            f"({len(arguments)} recorded, {len(visible_params)} expected)"
        )
    for value in arguments:
        if isinstance(value, dict) and "__struct__" in value:
            raise _SkipEntry(
                "structs by value are not supported by the Fortran driver (v1)"
            )

    value_by_name = {p.name: v for p, v in zip(visible_params, arguments)}

    slots: list[_Slot] = []
    for param in fn.parameters:
        var = f"c{idx}_{param.name}"
        is_real4 = _is_real4(param)
        decl_type = _decl_type(param.type, is_real4, param.native_type)

        if param.intent != "out":
            value = value_by_name[param.name]
            if param.is_array:
                size = len(value)
                decl = f"{decl_type} :: {var}({size})"
                init = f"{var} = {_array_literal(value, param.type)}"
            else:
                size = None
                decl = f"{decl_type} :: {var}"
                init = f"{var} = {_scalar_literal(value, param.type)}"
        else:
            if param.is_array:
                if not param.length_param:
                    raise _SkipEntry(
                        f"intent(out) array '{param.name}' has no length_param — "
                        "the driver cannot size it at the call site"
                    )
                size_value = value_by_name.get(param.length_param)
                if size_value is None:
                    raise _SkipEntry(
                        f"intent(out) array '{param.name}' is sized by "
                        f"'{param.length_param}', which is not a visible argument"
                    )
                size = int(size_value)
                decl = f"{decl_type} :: {var}({size})"
                init = None
            else:
                size = None
                decl = f"{decl_type} :: {var}"
                init = None

        slots.append(_Slot(param=param, var=var, decl=decl, init=init, size=size))

    return slots, visible_params


# --- wire slots (own copy — see module docstring) -------------------------


def _slots_for_call(entry: dict, fn: FunctionDef, visible_params: list[Parameter]) -> list[str]:
    """The exact slot names this call must print, in the order printed."""
    slots: list[str] = []

    outputs: list[Parameter | None] = []
    if not fn.is_subroutine and fn.returns != "void":
        outputs.append(None)  # None marks "the function's own return value"
    outputs.extend(p for p in fn.parameters if p.intent == "out")

    if len(outputs) == 1:
        out = outputs[0]
        is_array = out is not None and out.is_array
        if is_array:
            slots.extend(f"return[{n}]" for n in range(_out_size(out, entry, visible_params)))
        else:
            slots.append("return")
    elif len(outputs) > 1:
        for k, out in enumerate(outputs):
            if out is not None and out.is_array:
                size = _out_size(out, entry, visible_params)
                slots.extend(f"return[{k}][{n}]" for n in range(size))
            else:
                slots.append(f"return[{k}]")

    value_by_name = {p.name: v for p, v in zip(visible_params, entry.get("arguments") or [])}
    for i, param in enumerate(visible_params):
        if param.intent != "inout":
            continue
        if param.is_array:
            size = len(value_by_name[param.name])
            slots.extend(f"arg:{i}[{n}]" for n in range(size))
        else:
            slots.append(f"arg:{i}")

    return slots


def _out_size(param: Parameter, entry: dict, visible_params: list[Parameter]) -> int:
    value_by_name = {p.name: v for p, v in zip(visible_params, entry.get("arguments") or [])}
    size_value = value_by_name.get(param.length_param)
    if size_value is None:
        raise _SkipEntry(
            f"intent(out) array '{param.name}' is sized by "
            f"'{param.length_param}', which is not a visible argument"
        )
    return int(size_value)


# --- emitting one call's Fortran -------------------------------------------


def _print_scalar_slot(key: str, slot: str, expr: str, python_type: str, is_real4: bool) -> list[str]:
    label = f"write(*,'(A,A1,A,A1,A)') {_quote(key)}, char(9), {_quote(slot)}, char(9), trim(n2p_valstr)"
    if python_type == "float":
        widen = f"n2p_dbl = real({expr}, 8)" if is_real4 else f"n2p_dbl = {expr}"
        return [
            widen,
            "n2p_bits = transfer(n2p_dbl, 0_8)",
            "call n2p_hex64(n2p_bits, n2p_hex)",
            "n2p_valstr = n2p_hex",
            label,
        ]
    if python_type == "int":
        return [f"write(n2p_valstr, '(I0)') {expr}", label]
    if python_type == "bool":
        return [
            f"if ({expr}) then",
            "  n2p_valstr = '1'",
            "else",
            "  n2p_valstr = '0'",
            "end if",
            label,
        ]
    if python_type == "str":
        return [f"call n2p_escape(trim({expr}), n2p_valstr)", label]
    raise _SkipEntry(f"no wire formatting for type {python_type!r}")


def _emit_call(
    idx: int, key: str, entry: dict, fn: FunctionDef, return_kind: str | None
) -> tuple[list[str], list[str]]:
    """Returns (declaration lines, executable-statement lines) for one call.

    Kept separate because Fortran requires every specification statement in
    a program unit to precede every executable statement — interleaving
    per-call declarations with per-call executable statements (call N's
    `write` before call N+1's `real(8) ::`) does not compile.
    """
    slots, visible_params = _plan_call(idx, key, entry, fn)
    wire_slots = _slots_for_call(entry, fn, visible_params)

    decls: list[str] = []
    lines: list[str] = []
    for slot in slots:
        decls.append(f"    {slot.decl}")
    result_var = None
    result_is_real4 = (return_kind or "").lower().replace(" ", "") in (
        "real",
        "real*4",
        "real(4)",
        "real(kind=4)",
    )
    if not fn.is_subroutine and fn.returns != "void":
        result_var = f"c{idx}_result"
        decl_type = _decl_type(fn.returns, result_is_real4, None)
        decls.append(f"    {decl_type} :: {result_var}")

    for slot in slots:
        if slot.init is not None:
            lines.append(f"    {slot.init}")

    call_args = ", ".join(slot.var for slot in slots)
    if fn.is_subroutine:
        lines.append(f"    call {fn.name}({call_args})")
    else:
        lines.append(f"    {result_var} = {fn.name}({call_args})")

    # Outputs, in the order _slots_for_call enumerated them.
    outputs: list[tuple[str, Parameter | None]] = []
    if result_var is not None:
        outputs.append((result_var, None))
    out_params = [(s.var, s.param) for s in slots if s.param.intent == "out"]
    outputs.extend(out_params)

    slot_iter = iter(wire_slots)
    if len(outputs) == 1:
        var, param = outputs[0]
        python_type = fn.returns if param is None else param.type
        is_real4 = result_is_real4 if param is None else _is_real4(param)
        if param is not None and param.is_array:
            size = next(s.size for s in slots if s.param is param)
            for n in range(size):
                slot_name = next(slot_iter)
                lines.extend(
                    _print_scalar_slot(key, slot_name, f"{var}({n + 1})", python_type, is_real4)
                )
        else:
            slot_name = next(slot_iter)
            lines.extend(_print_scalar_slot(key, slot_name, var, python_type, is_real4))
    elif len(outputs) > 1:
        for var, param in outputs:
            python_type = fn.returns if param is None else param.type
            is_real4 = result_is_real4 if param is None else _is_real4(param)
            if param is not None and param.is_array:
                size = next(s.size for s in slots if s.param is param)
                for n in range(size):
                    slot_name = next(slot_iter)
                    lines.extend(
                        _print_scalar_slot(key, slot_name, f"{var}({n + 1})", python_type, is_real4)
                    )
            else:
                slot_name = next(slot_iter)
                lines.extend(_print_scalar_slot(key, slot_name, var, python_type, is_real4))

    # arg:<i> / arg:<i>[<n>] — intent(inout) python-visible arguments.
    for i, param in enumerate(visible_params):
        if param.intent != "inout":
            continue
        slot_obj = next(s for s in slots if s.param is param)
        if param.is_array:
            for n in range(slot_obj.size):
                slot_name = next(slot_iter)
                lines.extend(
                    _print_scalar_slot(
                        key, slot_name, f"{slot_obj.var}({n + 1})", param.type, False
                    )
                )
        else:
            slot_name = next(slot_iter)
            lines.extend(_print_scalar_slot(key, slot_name, slot_obj.var, param.type, False))

    return decls, lines


# --- helper subprograms, verbatim in every driver --------------------------

_HELPERS = """\
contains

    subroutine n2p_escape(raw, escaped)
        ! Percent-escape tab (%09), newline (%0A) and percent (%25) so a
        ! CHARACTER value containing one of them cannot break the wire
        ! protocol's line format. `raw` must already be trimmed by the
        ! caller — trailing blanks are the declared string normalisation
        ! (spec sec 2.4), not something this routine decides.
        character(len=*), intent(in)  :: raw
        character(len=*), intent(out) :: escaped
        integer :: i, j, code
        character(len=16), parameter :: hexd = '0123456789ABCDEF'
        escaped = ' '
        j = 0
        do i = 1, len_trim(raw)
            code = iachar(raw(i:i))
            if (code == 9 .or. code == 10 .or. code == 37) then
                escaped(j+1:j+1) = '%'
                escaped(j+2:j+2) = hexd(code/16+1:code/16+1)
                escaped(j+3:j+3) = hexd(mod(code,16)+1:mod(code,16)+1)
                j = j + 3
            else
                escaped(j+1:j+1) = raw(i:i)
                j = j + 1
            end if
        end do
    end subroutine n2p_escape

    subroutine n2p_hex64(bits, hexstr)
        ! 16 lowercase hex digits of an integer(8) bit pattern. The `Z16.16`
        ! edit descriptor prints uppercase, so this lowercases afterwards —
        ! spec sec 2.4 requires lowercase, and this must not depend on any
        ! locale or environment setting to stay deterministic.
        integer(8), intent(in) :: bits
        character(len=16), intent(out) :: hexstr
        integer :: i, code
        write(hexstr, '(Z16.16)') bits
        do i = 1, 16
            code = iachar(hexstr(i:i))
            if (code >= iachar('A') .and. code <= iachar('F')) then
                hexstr(i:i) = achar(code + 32)
            end if
        end do
    end subroutine n2p_hex64

end program n2p_oracle_driver
"""


def generate_driver(
    document: dict, module: ModuleIR, return_kinds: dict | None = None
) -> DriverResult:
    """Build the driver source for `document`'s recorded entries.

    `return_kinds` maps an entry key to the native Fortran spelling of that
    function's return kind ("real(4)" vs "real(8)") — see the module
    docstring for why the IR alone cannot answer this. Entries not present
    default to real(8)/double precision.
    """
    return_kinds = return_kinds or {}
    functions = {fn.name: fn for fn in module.functions}
    skipped: dict = dict(document.get("skipped") or {})

    all_decls: list[str] = []
    all_execs: list[str] = []
    for idx, (key, entry) in enumerate(sorted_entries(document)):
        fn = functions.get(entry.get("name") or key)
        if fn is None:
            skipped[key] = "no matching Fortran function in the IR"
            continue
        try:
            decls, execs = _emit_call(idx, key, entry, fn, return_kinds.get(key))
        except _SkipEntry as exc:
            skipped[key] = str(exc)
            continue
        all_decls.append(f"    ! --- {key} ---")
        all_decls.extend(decls)
        all_execs.append(f"    ! --- {key} ---")
        all_execs.extend(execs)

    source = _assemble(module, all_decls, all_execs)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return DriverResult(source=source, driver_sha256=digest, skipped=skipped)


def sorted_entries(document: dict):
    """`document["entries"]` in file order — never re-sorted (spec sec 2.2)."""
    return list((document.get("entries") or {}).items())


def _assemble(module: ModuleIR, decls: list[str], execs: list[str]) -> str:
    use_line = f"    use {module.fortran_module}\n" if module.fortran_module else ""
    decl_body = "\n".join(decls)
    exec_body = "\n".join(execs)
    return (
        "program n2p_oracle_driver\n"
        f"{use_line}"
        "    implicit none\n"
        "    real(8) :: n2p_dbl\n"
        "    integer(8) :: n2p_bits\n"
        "    character(len=16) :: n2p_hex\n"
        "    character(len=512) :: n2p_valstr\n"
        f"{decl_body}\n"
        "\n"
        f"{exec_body}\n"
        "\n"
        f"{_HELPERS}"
    )
