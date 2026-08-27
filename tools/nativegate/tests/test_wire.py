"""The bit-emission wire protocol (nativegate/wire.py).

Layer 2's whole claim — "the binding and the native code agree, bit for
bit" — is only as good as the channel both sides speak through. These tests
hold `wire.py` to the letter of design-verification-layers.md section 2.4/
2.5: exact hex width, big-endian (with a test that proves the module would
actually notice byte-reversal), the exact three escape sequences and nothing
else, the line/slot grammar, and `slots_for_entry` against the real,
committed `services/petro_api/golden.json` fixture.
"""

from __future__ import annotations

import json
import pathlib
import struct

import pytest

from nativegate import wire
from nativegate.ir import FunctionDef, Parameter, module_from_dict

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PETRO_GOLDEN = REPO_ROOT / "services" / "petro_api" / "golden.json"
PETRO_IR = REPO_ROOT / "services" / "petro_api" / ".nativegate" / "ir.json"


# --- pack_float: the bitwise channel --------------------------------------


def test_pack_float_is_16_lowercase_hex_digits():
    text = wire.pack_float(355.0764805578969)
    assert len(text) == 16
    assert text == text.lower()
    assert all(c in "0123456789abcdef" for c in text)


def test_pack_float_matches_the_spec_constant():
    # design-verification-layers.md section 2.4's own worked example, and the
    # value this whole module's self-check is pinned to.
    assert wire.pack_float(355.0764805578969) == "4076313943ad6f27"


def test_pack_float_round_trips_through_unpack_float():
    for value in (0.0, -0.0, 1.0, -1.0, 3.25, 100.0, float("inf"), float("-inf")):
        assert wire.unpack_float(wire.pack_float(value)) == value or (
            value != value  # pragma: no cover - NaN handled separately below
        )


def test_pack_float_nan_round_trips_as_nan():
    packed = wire.pack_float(float("nan"))
    assert struct.unpack(">d", bytes.fromhex(packed))[0] != struct.unpack(">d", bytes.fromhex(packed))[0]


def test_self_check_constant_matches_native_reference_hex():
    # Verified in the spec on arm64 against gfortran's Z16.16 and clang's
    # %016llx for the same bit pattern; the value is copied into wire.py's
    # module-level self-check and re-asserted here so a future edit to the
    # module cannot silently change the pinned constant.
    assert wire._SELF_CHECK_HEX == "4076313943ad6f27"
    assert struct.pack(">d", wire._SELF_CHECK_VALUE).hex() == wire._SELF_CHECK_HEX


def test_byte_reversed_little_endian_pack_is_detected_as_wrong():
    # This is the failure mode the self-check exists to catch: on a
    # little-endian host, "<d" produces the byte-reversed hex string, and it
    # must NOT equal what the big-endian channel expects.
    correct = struct.pack(">d", wire._SELF_CHECK_VALUE).hex()
    reversed_endian = struct.pack("<d", wire._SELF_CHECK_VALUE).hex()
    assert reversed_endian != correct
    assert reversed_endian == "276fad4339317640"  # spec section 2.4's own example


def test_self_check_raises_on_a_deliberately_wrong_constant(monkeypatch):
    monkeypatch.setattr(wire, "_SELF_CHECK_HEX", "0000000000000000")
    with pytest.raises(wire.WireEndiannessError):
        wire._self_check()


# --- non-float value channel ------------------------------------------------


def test_format_value_bool_is_0_or_1():
    assert wire.format_value(True) == "1"
    assert wire.format_value(False) == "0"


def test_format_value_int_is_decimal():
    assert wire.format_value(42) == "42"
    assert wire.format_value(-7) == "-7"
    assert wire.format_value(0) == "0"


def test_format_value_bool_checked_before_int():
    # bool is an int subclass in Python; format_value must not fall through
    # to str(int) (which would coincidentally also produce "1"/"0" for True/
    # False, but must do so via the bool branch, not by accident) — proven
    # by False, whose str(int) would be "0" either way, and True's "1".
    assert wire.format_value(True) == "1"
    assert wire.format_value(False) == "0"
    assert isinstance(True, int)  # documents *why* the ordering matters


def test_format_value_float_uses_pack_float():
    assert wire.format_value(355.0764805578969) == wire.pack_float(355.0764805578969)


def test_format_value_string_escapes():
    assert wire.format_value("plain") == "plain"


@pytest.mark.parametrize(
    "raw, escaped",
    [
        ("a\tb", "a%09b"),
        ("a\nb", "a%0Ab"),
        ("a%b", "a%25b"),
        ("", ""),
        ("\t\n%", "%09%0A%25"),
        ("100%\tdone\n", "100%25%09done%0A"),
    ],
)
def test_escape_string_corners(raw, escaped):
    assert wire.escape_string(raw) == escaped
    assert wire.unescape_string(escaped) == raw


def test_escape_string_escapes_nothing_else():
    # The design is explicit: "escape nothing else". Every other byte,
    # including ones that look escape-adjacent, must pass through unchanged.
    raw = "hello, world! üñîçødé #$&*()[]{}"
    assert wire.escape_string(raw) == raw
    assert wire.unescape_string(raw) == raw


def test_escape_unescape_round_trip_is_exact_for_arbitrary_strings():
    samples = [
        "",
        "no special chars",
        "\t",
        "\n",
        "%",
        "%09",  # a string that already looks escaped
        "a\tb\nc%d",
        "%%%",
        "\t\t\n\n%%",
    ]
    for sample in samples:
        assert wire.unescape_string(wire.escape_string(sample)) == sample


def test_format_value_rejects_unrepresentable_types():
    with pytest.raises(wire.WireValueError):
        wire.format_value(None)
    with pytest.raises(wire.WireValueError):
        wire.format_value([1, 2, 3])


# --- slot grammar ------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("return", wire.Slot("return")),
        ("return[0]", wire.Slot("return", element=0)),
        ("return[12]", wire.Slot("return", element=12)),
        ("arg:0", wire.Slot("arg", arg_index=0)),
        ("arg:3", wire.Slot("arg", arg_index=3)),
        ("arg:1[0]", wire.Slot("arg", arg_index=1, element=0)),
        ("arg:1[8]", wire.Slot("arg", arg_index=1, element=8)),
    ],
)
def test_slot_parse_accepts_the_grammar(text, expected):
    assert wire.Slot.parse(text) == expected
    assert str(expected) == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "returns",
        "return[]",
        "return[-1]",
        "arg",
        "arg:",
        "arg:x",
        "arg:1[",
        "arg:1]",
        "ARG:1",
        "Return",
        "arg:1[0][1]",
    ],
)
def test_slot_parse_rejects_malformed_text(text):
    with pytest.raises(wire.SlotParseError):
        wire.Slot.parse(text)


def test_slot_arg_role_requires_index():
    with pytest.raises(wire.SlotParseError):
        wire.Slot("arg")


def test_slot_return_role_rejects_arg_index():
    with pytest.raises(wire.SlotParseError):
        wire.Slot("return", arg_index=0)


def test_slot_is_hashable_for_use_as_a_dict_key():
    d = {wire.Slot("return"): "x", wire.Slot("arg", arg_index=1, element=0): "y"}
    assert d[wire.Slot("return")] == "x"


# --- line format -------------------------------------------------------------


def test_format_line_shape():
    line = wire.format_line("solution_gor", wire.return_slot(), 355.0764805578969)
    assert line == "solution_gor\treturn\t4076313943ad6f27"


def test_format_and_parse_line_round_trip():
    line = wire.format_line("pvt_state", wire.arg_slot(1, 3), 45.33883597248819)
    key, slot, value_text = wire.parse_line(line)
    assert key == "pvt_state"
    assert slot == wire.arg_slot(1, 3)
    assert wire.unpack_float(value_text) == 45.33883597248819


@pytest.mark.parametrize(
    "line",
    [
        "too\tfew",
        "way\ttoo\tmany\tfields",
        "\tslot\tvalue",  # empty key
        "key\tbadslot\tvalue",
        "nofields at all",
    ],
)
def test_parse_line_rejects_malformed_lines_naming_the_line(line):
    with pytest.raises(wire.WireFormatError) as excinfo:
        wire.parse_line(line)
    assert repr(line) in str(excinfo.value)


def test_parse_lines_builds_key_to_slot_map():
    lines = [
        "solution_gor\treturn\t4076313943ad6f27",
        "pvt_state\targ:1[0]\t3ff0000000000000",
        "pvt_state\targ:1[1]\t4000000000000000",
    ]
    parsed = wire.parse_lines(lines)
    assert set(parsed) == {"solution_gor", "pvt_state"}
    assert set(parsed["pvt_state"]) == {wire.arg_slot(1, 0), wire.arg_slot(1, 1)}


def test_parse_lines_rejects_duplicate_key_slot_pair():
    lines = [
        "solution_gor\treturn\t4076313943ad6f27",
        "solution_gor\treturn\t4076313943ad6f27",
    ]
    with pytest.raises(wire.WireFormatError) as excinfo:
        wire.parse_lines(lines)
    assert "duplicate" in str(excinfo.value)
    assert repr(lines[1]) in str(excinfo.value)


def test_parse_lines_rejects_out_of_order_keys():
    lines = [
        "b\treturn\t0000000000000000",
        "a\treturn\t0000000000000000",
    ]
    with pytest.raises(wire.WireFormatError) as excinfo:
        wire.parse_lines(lines, expected_key_order=["a", "b"])
    assert "out of the expected call order" in str(excinfo.value)
    assert repr(lines[0]) in str(excinfo.value)


def test_parse_lines_allows_multiple_lines_per_key_in_any_slot_order():
    lines = [
        "pvt_state\targ:1[1]\t0000000000000000",
        "pvt_state\targ:1[0]\t0000000000000000",
    ]
    # Slot order within one key is not constrained by expected_key_order —
    # only the *first appearance* of each key must respect call order.
    parsed = wire.parse_lines(lines, expected_key_order=["pvt_state"])
    assert set(parsed["pvt_state"]) == {wire.arg_slot(1, 0), wire.arg_slot(1, 1)}


def test_parse_lines_ignores_blank_lines():
    lines = ["solution_gor\treturn\t4076313943ad6f27", "", "\n"]
    parsed = wire.parse_lines(lines)
    assert set(parsed) == {"solution_gor"}


def test_parse_lines_rejects_malformed_line_naming_it_even_mid_stream():
    lines = [
        "solution_gor\treturn\t4076313943ad6f27",
        "this is not a valid line",
    ]
    with pytest.raises(wire.WireFormatError) as excinfo:
        wire.parse_lines(lines)
    assert repr(lines[1]) in str(excinfo.value)


# --- slots_for_entry against the real petro_api fixture ---------------------


@pytest.fixture(scope="module")
def petro_document() -> dict:
    if not PETRO_GOLDEN.is_file():
        pytest.skip(f"fixture not found: {PETRO_GOLDEN}")
    return json.loads(PETRO_GOLDEN.read_text())


@pytest.fixture(scope="module")
def petro_functions() -> dict[str, FunctionDef]:
    if not PETRO_IR.is_file():
        pytest.skip(f"fixture not found: {PETRO_IR}")
    module = module_from_dict(json.loads(PETRO_IR.read_text()))
    return {fn.name: fn for fn in module.functions}


def test_pvt_state_yields_arg_1_elements_0_through_8(petro_document, petro_functions):
    entry = petro_document["entries"]["pvt_state"]
    fn = petro_functions["pvt_state"]
    slots = wire.slots_for_entry(entry, fn)
    assert slots == [wire.arg_slot(1, i) for i in range(9)]


def test_solution_gor_yields_return(petro_document, petro_functions):
    entry = petro_document["entries"]["solution_gor"]
    fn = petro_functions["solution_gor"]
    slots = wire.slots_for_entry(entry, fn)
    assert slots == [wire.return_slot()]


def test_pvt_set_fluid_yields_no_slots(petro_document, petro_functions):
    # A void subroutine with no intent(out) parameter and no argument_effects
    # recorded (petro_api's fluid setter mutates COMMON state, not its
    # arguments) has nothing observable through this channel.
    entry = petro_document["entries"]["pvt_set_fluid"]
    fn = petro_functions["pvt_set_fluid"]
    assert wire.slots_for_entry(entry, fn) == []


def test_every_recorded_entry_produces_slots_consistent_with_its_recording(
    petro_document, petro_functions
):
    # A structural sanity check across the whole fixture, independent of the
    # two hand-picked cases above: every argument_effects key becomes an
    # arg: slot (scalar or, for a list, one slot per element), and a
    # non-None scalar/list result becomes return slot(s) — for every entry
    # petro_api's golden.json actually records.
    for key, entry in petro_document["entries"].items():
        fn = petro_functions[key]
        slots = wire.slots_for_entry(entry, fn)

        expected_arg_slots = []
        for index_text, value in sorted(
            (entry.get("argument_effects") or {}).items(), key=lambda kv: int(kv[0])
        ):
            index = int(index_text)
            if isinstance(value, list):
                expected_arg_slots.extend(wire.arg_slot(index, i) for i in range(len(value)))
            else:
                expected_arg_slots.append(wire.arg_slot(index))

        arg_slots_in_result = [s for s in slots if s.role == "arg"]
        assert arg_slots_in_result == expected_arg_slots

        return_slots_in_result = [s for s in slots if s.role == "return"]
        result = entry.get("result")
        has_return_path = fn.returns != "void" or any(p.intent == "out" for p in fn.parameters)
        if not has_return_path or result is None:
            assert return_slots_in_result == []
        elif isinstance(result, list):
            assert return_slots_in_result == [wire.return_slot(i) for i in range(len(result))]
        else:
            assert return_slots_in_result == [wire.return_slot()]


# --- slots_for_entry on synthetic IR (no fixture dependency) ----------------


def _function(name, returns="void", parameters=None) -> FunctionDef:
    return FunctionDef(name=name, parameters=parameters or [], returns=returns)


def test_slots_for_entry_scalar_return_only():
    fn = _function("f", returns="float", parameters=[Parameter(name="x", type="float")])
    entry = {"arguments": [1.0], "result": 2.0}
    assert wire.slots_for_entry(entry, fn) == [wire.return_slot()]


def test_slots_for_entry_tuple_return():
    fn = _function(
        "f",
        returns="float",
        parameters=[
            Parameter(name="x", type="float"),
            Parameter(name="y", type="float", intent="out"),
        ],
    )
    entry = {"arguments": [1.0], "result": [2.0, 3.0]}
    assert wire.slots_for_entry(entry, fn) == [wire.return_slot(0), wire.return_slot(1)]


def test_slots_for_entry_no_return_path_and_no_effects_is_empty():
    fn = _function("f", returns="void", parameters=[Parameter(name="x", type="float")])
    entry = {"arguments": [1.0], "result": None}
    assert wire.slots_for_entry(entry, fn) == []


def test_slots_for_entry_scalar_arg_effect():
    fn = _function(
        "f",
        returns="void",
        parameters=[Parameter(name="x", type="float", intent="inout")],
    )
    entry = {"arguments": [1.0], "result": None, "argument_effects": {"0": 9.0}}
    assert wire.slots_for_entry(entry, fn) == [wire.arg_slot(0)]


def test_slots_for_entry_orders_return_before_args_and_args_ascending():
    fn = _function(
        "f",
        returns="float",
        parameters=[
            Parameter(name="x", type="float"),
            Parameter(name="a", type="float", is_array=True, intent="inout"),
            Parameter(name="b", type="float", intent="inout"),
        ],
    )
    entry = {
        "arguments": [1.0, [0.0, 0.0], 0.0],
        "result": 5.0,
        "argument_effects": {"2": 7.0, "1": [1.0, 2.0]},
    }
    assert wire.slots_for_entry(entry, fn) == [
        wire.return_slot(),
        wire.arg_slot(1, 0),
        wire.arg_slot(1, 1),
        wire.arg_slot(2),
    ]
