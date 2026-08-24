"""The bit-emission wire protocol shared by the oracle's two sides.

Layer 2 (design-verification-layers.md, spec section 2) compares a native
driver's output against the Python binding's output *bitwise*, in the same
build, on the same machine, in the same run. Both sides have to agree, byte
for byte, on how a value becomes text and back — that agreement lives here,
once, so the Fortran driver generator (T3), the C++ driver generator (T7),
and the comparator (T5) cannot drift into disagreeing with each other about
what a line means.

This module owns both directions:

* **Emitting** — turning a Python value into the wire's `<value>` text
  (`pack_float`, `format_value`) and a whole line (`format_line`).
* **Parsing** — turning driver stdout back into structured data
  (`parse_line`, `parse_lines`) and turning a `slot` string into a typed
  `Slot` (`Slot.parse`).
* **The plan** — given one golden.json entry and the IR signature it was
  recorded against, the expected list of slots (`slots_for_entry`). T3/T7
  generate their print statements from this list; T5 compares against it.
  This is the single source of truth other generators read from, so "which
  slots does this call produce" is answered in exactly one place.

Wire format (spec section 2.5)
-------------------------------

One line per observable value, tab-separated::

    <key>\\t<slot>\\t<value>

`<value>` is 16 lowercase hex digits for floats (`pack_float`), a decimal
integer for integers, `0`/`1` for booleans, or a percent-escaped string
otherwise (`escape_string`).

Slot grammar (spec section 2.5's table)
----------------------------------------

======================  ===========================================
``return``               the function's return value
``return[<n>]``          element ``n`` of a tuple return — f2py
                         surfaces ``intent(out)`` scalars/arrays as
                         return values, not as argument effects
``arg:<i>``              scalar argument ``i`` after the call
                         (``intent(inout)``)
``arg:<i>[<n>]``         element ``n`` of array argument ``i``
======================  ===========================================

**`<i>` counts over the Python-visible argument list** — the same index
space as golden.json's `arguments` array and `argument_effects` keys. This
is deliberately *not* the native call site's parameter index: `plan()`
(golden.py) drops `intent(out)` parameters from the Python argument list,
and an f2py call site additionally carries hidden `CHARACTER` length
arguments the Python signature never sees. The driver generator owns the
mapping from this Python-visible index to whatever native position the
value actually lives at; the wire format itself never exposes native
positions, so a comparator reading the wire never needs to know the native
calling convention at all.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from .ir import FunctionDef

# --- floats: the bitwise channel (spec section 2.4) ----------------------

# "Big-endian, and this is not a preference." Both native sides print the
# *integer value* of the reinterpreted bits, most-significant nibble first,
# regardless of host byte order — struct.pack(">d", ...) matches that; the
# native-endian "<d" produces the byte-reversed string on every little-endian
# host native2py runs on, and every comparison would fail. The harness must
# assert this on a known constant at startup rather than trust it (spec
# section 2.4's closing paragraph) — this constant and its expected hex are
# copied verbatim from the spec, which verified them against gfortran's
# `Z16.16`, clang's `%016llx`, and Python's `struct.pack(">d", ...).hex()`.
_SELF_CHECK_VALUE = 355.0764805578969
_SELF_CHECK_HEX = "4076313943ad6f27"


class WireEndiannessError(RuntimeError):
    """The float channel is not big-endian on this host/interpreter.

    This should be unreachable in practice — struct's ">d" format is
    byte-order-explicit by definition — but the bitwise comparison this
    module exists for is worthless if it is ever wrong, so it is checked
    rather than assumed.
    """


def _self_check() -> None:
    actual = struct.pack(">d", _SELF_CHECK_VALUE).hex()
    if actual != _SELF_CHECK_HEX:
        raise WireEndiannessError(
            f"big-endian float self-check failed: struct.pack('>d', "
            f"{_SELF_CHECK_VALUE!r}).hex() == {actual!r}, expected "
            f"{_SELF_CHECK_HEX!r}. The oracle's bitwise comparison is "
            "meaningless if this does not hold — refusing to proceed."
        )


# Run once per process import, not per call: pack_float is on the hot path
# for every slot of every entry, and the check result cannot change between
# calls in the same interpreter.
_self_check()


def pack_float(value: float) -> str:
    """The wire's `<value>` text for a float: 16 lowercase hex digits.

    `struct.pack(">d", value).hex()` — big-endian IEEE-754 double, not
    `repr` and not `float.hex()`. A `real(4)`/`float` value must be widened
    to double *before* it reaches here (spec section 2.4: widening is exact,
    so the bitwise claim survives; a 32-bit value transferred directly into
    a 64-bit slot has undefined padding bits). Widening is the driver
    generator's job (T3/T7), not this function's — `pack_float` always packs
    a full 64-bit double.
    """
    return struct.pack(">d", value).hex()


def unpack_float(hex_digits: str) -> float:
    """The inverse of `pack_float`, for tests and for reading driver output
    back into a comparable float where a comparator wants the numeric value
    rather than the hex text (the default and preferred comparison is
    hex-text equality, which sidesteps NaN != NaN)."""
    if len(hex_digits) != 16:
        raise ValueError(
            f"not a 16-hex-digit float slot value: {hex_digits!r}"
        )
    return struct.unpack(">d", bytes.fromhex(hex_digits))[0]


# --- non-float value channel (spec section 2.4/2.5) -----------------------

# Escape nothing else: the design is explicit that %09/%0A/%25 are the whole
# alphabet. Order matters — '%' must be escaped first, or the '%' introduced
# by escaping tab/newline would itself be re-escaped.
_ESCAPES = (("%", "%25"), ("\t", "%09"), ("\n", "%0A"))


def escape_string(value: str) -> str:
    """Percent-escape a string for the wire's value channel.

    Escapes only tab (`%09`), newline (`%0A`) and percent (`%25`) — nothing
    else — so a `CHARACTER` output containing a tab or newline cannot break
    the `<key>\\t<slot>\\t<value>` line protocol (spec section 2.4).
    """
    for raw, escaped in _ESCAPES:
        value = value.replace(raw, escaped)
    return value


_UNESCAPE_RE = re.compile("%09|%0A|%25")
_UNESCAPE_MAP = {"%09": "\t", "%0A": "\n", "%25": "%"}


def unescape_string(value: str) -> str:
    """The inverse of `escape_string`."""
    return _UNESCAPE_RE.sub(lambda m: _UNESCAPE_MAP[m.group(0)], value)


class WireValueError(ValueError):
    """A Python value has no representation in the wire's value channel."""


def format_value(value) -> str:
    """The wire's `<value>` text for any scalar the protocol carries.

    Dispatches on Python type: `bool` -> `0`/`1`, `int` -> decimal, `float`
    -> `pack_float` (16 hex digits), `str` -> `escape_string`. `bool` is
    checked before `int` because `bool` is an `int` subclass in Python.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return pack_float(value)
    if isinstance(value, str):
        return escape_string(value)
    raise WireValueError(
        f"no wire representation for {value!r} of type {type(value).__name__}"
    )


# --- slot grammar (spec section 2.5) ---------------------------------------


class SlotParseError(ValueError):
    """A `slot` field could not be parsed as one of the four wire forms."""


_RETURN_RE = re.compile(r"^return(?:\[(\d+)\])?$")
_ARG_RE = re.compile(r"^arg:(\d+)(?:\[(\d+)\])?$")


@dataclass(frozen=True)
class Slot:
    """One observable value's address within a call.

    `role` is `"return"` or `"arg"`. For `"arg"`, `arg_index` is the
    Python-visible argument index (see module docstring — golden.json's
    `arguments`/`argument_effects` index space, not the native parameter
    position). `element` is the sub-index for `return[<n>]`/`arg:<i>[<n>]`
    (a tuple-return position or an array element) and is `None` for the
    scalar forms `return`/`arg:<i>`.
    """

    role: str  # "return" | "arg"
    arg_index: int | None = None
    element: int | None = None

    def __post_init__(self) -> None:
        if self.role not in ("return", "arg"):
            raise SlotParseError(f"unknown slot role {self.role!r}")
        if self.role == "arg" and self.arg_index is None:
            raise SlotParseError("an 'arg' slot needs arg_index")
        if self.role == "return" and self.arg_index is not None:
            raise SlotParseError("a 'return' slot must not carry arg_index")

    def __str__(self) -> str:
        base = "return" if self.role == "return" else f"arg:{self.arg_index}"
        return base if self.element is None else f"{base}[{self.element}]"

    @classmethod
    def parse(cls, text: str) -> "Slot":
        match = _RETURN_RE.match(text)
        if match:
            element = match.group(1)
            return cls("return", element=int(element) if element is not None else None)
        match = _ARG_RE.match(text)
        if match:
            index, element = match.groups()
            return cls(
                "arg",
                arg_index=int(index),
                element=int(element) if element is not None else None,
            )
        raise SlotParseError(
            f"malformed slot {text!r}: expected 'return', 'return[<n>]', "
            "'arg:<i>' or 'arg:<i>[<n>]'"
        )


def return_slot(element: int | None = None) -> Slot:
    return Slot("return", element=element)


def arg_slot(index: int, element: int | None = None) -> Slot:
    return Slot("arg", arg_index=index, element=element)


# --- line format (spec section 2.5) -----------------------------------------


class WireFormatError(ValueError):
    """A line, or a stream of lines, violates the wire protocol.

    Always carries the offending line's text (spec: "a malformed line,
    duplicate (key, slot), or key out of expected order is an error carrying
    the offending line text") — never just an index or a summary, so the
    failure is legible without re-running the driver.
    """


def format_line(key: str, slot: "Slot | str", value) -> str:
    """One wire line: `<key>\\t<slot>\\t<value>`.

    `value` is the already-Python-typed value (a float, int, bool or str);
    `format_value` produces its `<value>` text. Pass a pre-formatted string
    only if the caller has already escaped/packed it itself.
    """
    slot_text = str(slot)
    value_text = value if isinstance(value, str) and _looks_preformatted(value) else format_value(value)
    return f"{key}\t{slot_text}\t{value_text}"


def _looks_preformatted(_value: str) -> bool:
    # format_value already handles str via escape_string; there is no case
    # where a caller needs to bypass it. Kept as an explicit hook (rather
    # than silently double-escaping) in case a future caller hands in text
    # it already escaped itself — today, always False.
    return False


def parse_line(line: str) -> tuple[str, Slot, str]:
    """Split one wire line into `(key, slot, raw_value_text)`.

    `raw_value_text` is returned unparsed (still hex/decimal/escaped) —
    callers that know the expected type call `unpack_float`/`int`/
    `unescape_string` themselves, because this function has no way to know
    which channel a given slot uses without the IR.

    Raises `WireFormatError` carrying the offending line on anything that
    is not exactly three tab-separated fields, or whose slot field does not
    parse.
    """
    fields = line.split("\t")
    if len(fields) != 3:
        raise WireFormatError(
            f"malformed wire line (expected 3 tab-separated fields, got "
            f"{len(fields)}): {line!r}"
        )
    key, slot_text, value_text = fields
    if not key:
        raise WireFormatError(f"malformed wire line (empty key): {line!r}")
    try:
        slot = Slot.parse(slot_text)
    except SlotParseError as exc:
        raise WireFormatError(f"malformed wire line ({exc}): {line!r}") from exc
    return key, slot, value_text


def parse_lines(lines, expected_key_order: list[str] | None = None) -> dict[str, dict[Slot, str]]:
    """Parse a whole driver output stream into `{key: {slot: raw_value}}`.

    Enforces, across the whole stream, the three failure modes spec section
    2.5 names: a malformed line, a duplicate `(key, slot)` pair, or a key
    appearing out of the expected order. Each is a hard `WireFormatError`
    carrying the offending line's text — never a skip, per spec section 2.2
    ("the driver executes exactly the file's entries ... nothing after it is
    compared").

    `expected_key_order` is the call order the driver was generated from
    (golden.json's entry order, spec section 2.2) — omit it to skip the
    ordering check (e.g. when parsing a partial stream for a unit test).
    """
    result: dict[str, dict[Slot, str]] = {}
    seen_pairs: set[tuple[str, Slot]] = set()
    expected = list(expected_key_order) if expected_key_order is not None else None
    next_expected_position = 0
    key_position: dict[str, int] = {}

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        if line == "":
            continue
        key, slot, value_text = parse_line(line)

        if expected is not None:
            if key not in key_position:
                # First time this key appears: it must be the next one due,
                # per the file's (and therefore the driver's) call order.
                if next_expected_position >= len(expected) or expected[next_expected_position] != key:
                    raise WireFormatError(
                        f"key {key!r} appears out of the expected call order "
                        f"(expected {expected[next_expected_position:next_expected_position + 1] or '(end)'} next): "
                        f"{line!r}"
                    )
                key_position[key] = next_expected_position
                next_expected_position += 1
            # A later line for an already-started key is fine (multiple
            # slots per call); only the key's *first* appearance is ordered.

        pair = (key, slot)
        if pair in seen_pairs:
            raise WireFormatError(
                f"duplicate slot {slot} for key {key!r}: {line!r}"
            )
        seen_pairs.add(pair)
        result.setdefault(key, {})[slot] = value_text

    return result


# --- slots_for_entry: the source of truth (spec section 2.5, T3/T7/T5) ----


def slots_for_entry(entry: dict, ir_function: FunctionDef) -> list[Slot]:
    """The expected slot list for one golden.json entry.

    This is the single source of truth the Fortran/C++ driver generators
    (T3/T7) emit their print statements from, and the comparator (T5)
    compares against — so "which slots does this call produce" is answered
    in exactly one place rather than three generators independently
    reverse-engineering f2py/pybind11's output conventions.

    `entry` is one value of golden.json's `"entries"` mapping (as `golden.py`
    writes it): `{"arguments": [...], "result": ..., "argument_effects":
    {...}, ...}`. `ir_function` is that entry's `FunctionDef` from the IR
    (used to confirm whether the routine has a return path at all — a
    subroutine with no `intent(out)` parameter and no recorded result has
    no `return` slot).

    Slot derivation:

    * **Return.** `ir_function` has a return path when its declared return
      type is not `"void"`, or it declares at least one `intent(out)`
      parameter (f2py folds those into the return tuple — spec section
      2.5's table). Where there is a return path, the *shape* of the
      recorded `entry["result"]` decides the slot(s): `None` -> no slot (a
      subroutine whose only outputs are `intent(inout)` arguments, which
      golden.py already records as `None` — see golden.py's `invoke`
      docstring); a scalar (`int`/`float`/`bool`/`str`) -> one `return`
      slot; a flat list -> `return[<n>]` for each element, in order (f2py's
      tuple-of-outputs convention). A function declaring a return path whose
      recorded result is `None` is a golden-recording anomaly, not a driver
      bug, and is treated the same as "no return slot" here — the mismatch
      would already have been caught upstream by golden itself.
    * **Arguments.** One `arg:<i>` (or, for a list-valued effect,
      `arg:<i>[<n>]` per element) for every key in the recorded
      `entry["argument_effects"]` — golden.py only records an effect for an
      argument the call actually modified in place, and that is exactly the
      set of argument positions this layer needs to observe. `<i>` is
      already the Python-visible index (golden.py builds
      `argument_effects` keyed that way), so it is used as-is — see the
      module docstring for why this is *not* the native parameter index.

    Order: `return`/`return[<n>]` slots first (in tuple order), then
    `arg:<i>[...]` slots in ascending argument-index order (and ascending
    element order within one argument) — the order the driver generator
    emits print statements in and the order a comparator walks.
    """
    slots: list[Slot] = []

    has_return_path = ir_function.returns != "void" or any(
        p.intent == "out" for p in ir_function.parameters
    )
    result = entry.get("result")
    if has_return_path and result is not None:
        if isinstance(result, list):
            slots.extend(return_slot(i) for i in range(len(result)))
        else:
            slots.append(return_slot())

    effects = entry.get("argument_effects") or {}
    for index_text in sorted(effects, key=int):
        index = int(index_text)
        value = effects[index_text]
        if isinstance(value, list):
            slots.extend(arg_slot(index, i) for i in range(len(value)))
        else:
            slots.append(arg_slot(index))

    return slots
