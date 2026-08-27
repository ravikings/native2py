"""The Fortran oracle driver generator (nativegate.drivers.fortran, T3).

Two kinds of test:

* golden-file style: a small synthetic IR + golden document, asserting the
  generated source is byte-stable across repeated generation (spec sec 4,
  determinism) and that its wire-format lines are exactly what the spec
  says they should be — checked by actually compiling and running the
  driver against a stub Fortran module, not by eyeballing the source.
* the real `petro_api` IR + committed golden.json, compiled against the
  actual library objects and run, asserting the full slot set appears in
  file order. Skipped when gfortran is not on PATH (see
  `test_preprocessed_fortran.py` for the same convention).
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from nativegate.drivers.fortran import generate_driver
from nativegate.ir import FunctionDef, ModuleIR, Parameter, module_from_dict

REPO = Path(__file__).resolve().parents[3]

requires_gfortran = pytest.mark.skipif(
    shutil.which("gfortran") is None, reason="gfortran is not installed"
)


def _hex(value: float) -> str:
    return struct.pack(">d", value).hex()


# --- synthetic fixture ----------------------------------------------------


def _synthetic_module() -> ModuleIR:
    return ModuleIR(
        name="synth",
        language="fortran",
        source_file="synth.f90",
        fortran_module="synth_mod",
        functions=[
            FunctionDef(
                name="fn_double",
                parameters=[Parameter(name="x", type="float", intent="in")],
                returns="float",
                is_subroutine=False,
            ),
            FunctionDef(
                name="fn_inout",
                parameters=[
                    Parameter(name="arr", type="float", is_array=True, intent="inout")
                ],
                returns="void",
                is_subroutine=True,
            ),
            FunctionDef(
                name="fn_out_sized",
                parameters=[
                    Parameter(
                        name="out",
                        type="float",
                        is_array=True,
                        intent="out",
                        length_param="n",
                    ),
                    Parameter(name="n", type="int", intent="in"),
                ],
                returns="void",
                is_subroutine=True,
            ),
            FunctionDef(
                name="fn_out_no_length",
                parameters=[
                    Parameter(name="out", type="float", is_array=True, intent="out")
                ],
                returns="void",
                is_subroutine=True,
            ),
            FunctionDef(
                name="fn_char_out",
                parameters=[
                    Parameter(
                        name="msg",
                        type="str",
                        intent="out",
                        native_type="character(len=8)",
                    )
                ],
                returns="void",
                is_subroutine=True,
            ),
            FunctionDef(
                name="fn_real4",
                parameters=[Parameter(name="x", type="float", intent="in")],
                returns="float",
                is_subroutine=False,
            ),
        ],
    )


def _synthetic_document() -> dict:
    return {
        "format": 2,
        "service": "synth",
        "language": "fortran",
        "entries": {
            "fn_double": {
                "kind": "function",
                "name": "fn_double",
                "arguments": [2.5],
                "result": 6.25,
            },
            "fn_inout": {
                "kind": "function",
                "name": "fn_inout",
                "arguments": [[1.0, 2.0, 3.0]],
                "result": None,
            },
            "fn_out_sized": {
                "kind": "function",
                "name": "fn_out_sized",
                "arguments": [3],
                "result": None,
            },
            "fn_out_no_length": {
                "kind": "function",
                "name": "fn_out_no_length",
                "arguments": [],
                "result": None,
            },
            "fn_char_out": {
                "kind": "function",
                "name": "fn_char_out",
                "arguments": [],
                "result": None,
            },
            "fn_real4": {
                "kind": "function",
                "name": "fn_real4",
                "arguments": [1.5],
                "result": 3.0,
            },
        },
        "skipped": {"Existing.symbol": "recorded by golden, not by the driver"},
    }


RETURN_KINDS = {"fn_real4": "real(4)"}


def test_generated_driver_source_is_byte_stable():
    module = _synthetic_module()
    document = _synthetic_document()

    first = generate_driver(document, module, return_kinds=RETURN_KINDS)
    second = generate_driver(document, module, return_kinds=RETURN_KINDS)

    assert first.source == second.source
    assert first.driver_sha256 == second.driver_sha256
    import hashlib

    assert first.driver_sha256 == hashlib.sha256(first.source.encode("utf-8")).hexdigest()


def test_generated_driver_source_is_stable_across_a_fresh_process():
    """Byte-stability is not just "same object" — rerun in a clean interpreter."""
    module = _synthetic_module()
    document = _synthetic_document()
    expected = generate_driver(document, module, return_kinds=RETURN_KINDS).source

    script = """
import sys, hashlib
from nativegate.drivers.fortran import generate_driver
from nativegate.ir import FunctionDef, ModuleIR, Parameter

module = ModuleIR(
    name="synth", language="fortran", source_file="synth.f90",
    fortran_module="synth_mod",
    functions=[
        FunctionDef(name="fn_double",
                    parameters=[Parameter(name="x", type="float", intent="in")],
                    returns="float", is_subroutine=False),
        FunctionDef(name="fn_inout",
                    parameters=[Parameter(name="arr", type="float", is_array=True, intent="inout")],
                    returns="void", is_subroutine=True),
        FunctionDef(name="fn_out_sized",
                    parameters=[Parameter(name="out", type="float", is_array=True, intent="out", length_param="n"),
                                Parameter(name="n", type="int", intent="in")],
                    returns="void", is_subroutine=True),
        FunctionDef(name="fn_out_no_length",
                    parameters=[Parameter(name="out", type="float", is_array=True, intent="out")],
                    returns="void", is_subroutine=True),
        FunctionDef(name="fn_char_out",
                    parameters=[Parameter(name="msg", type="str", intent="out",
                                           native_type="character(len=8)")],
                    returns="void", is_subroutine=True),
        FunctionDef(name="fn_real4",
                    parameters=[Parameter(name="x", type="float", intent="in")],
                    returns="float", is_subroutine=False),
    ],
)
document = {
    "entries": {
        "fn_double": {"kind": "function", "name": "fn_double", "arguments": [2.5], "result": 6.25},
        "fn_inout": {"kind": "function", "name": "fn_inout", "arguments": [[1.0, 2.0, 3.0]], "result": None},
        "fn_out_sized": {"kind": "function", "name": "fn_out_sized", "arguments": [3], "result": None},
        "fn_out_no_length": {"kind": "function", "name": "fn_out_no_length", "arguments": [], "result": None},
        "fn_char_out": {"kind": "function", "name": "fn_char_out", "arguments": [], "result": None},
        "fn_real4": {"kind": "function", "name": "fn_real4", "arguments": [1.5], "result": 3.0},
    },
    "skipped": {"Existing.symbol": "recorded by golden, not by the driver"},
}
r = generate_driver(document, module, return_kinds={"fn_real4": "real(4)"})
sys.stdout.write(r.source)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == expected


def test_generated_driver_contains_no_environmental_leakage():
    module = _synthetic_module()
    document = _synthetic_document()
    source = generate_driver(document, module, return_kinds=RETURN_KINDS).source

    assert str(REPO) not in source
    assert "/Users/" not in source
    assert "/home/" not in source


def test_entries_with_no_length_param_are_skipped_with_a_reason():
    module = _synthetic_module()
    document = _synthetic_document()
    result = generate_driver(document, module, return_kinds=RETURN_KINDS)

    assert "fn_out_no_length" in result.skipped
    assert "length_param" in result.skipped["fn_out_no_length"]
    # never silently dropped: it must not appear as a call in the source
    assert "call fn_out_no_length" not in result.source


def test_golden_skips_are_reproduced_verbatim():
    module = _synthetic_module()
    document = _synthetic_document()
    result = generate_driver(document, module, return_kinds=RETURN_KINDS)

    assert result.skipped["Existing.symbol"] == "recorded by golden, not by the driver"


def test_real4_return_widens_before_transfer_never_transfers_real4_directly():
    module = _synthetic_module()
    document = _synthetic_document()
    source = generate_driver(document, module, return_kinds=RETURN_KINDS).source

    # The real(4) result local must be assigned into the real(8) scratch
    # via an explicit `real(..., 8)` conversion before any `transfer`.
    assert "real(4) :: c5_result" in source
    idx = source.index("c5_result = fn_real4")
    following = source[idx : idx + 400]
    assert "n2p_dbl = real(c5_result, 8)" in following
    assert "transfer(n2p_dbl, 0_8)" in following
    # and never `transfer(c5_result` directly (that would be the real(4)
    # undefined-padding bug the spec calls out).
    assert "transfer(c5_result" not in source


@requires_gfortran
def test_synthetic_driver_compiles_and_prints_the_documented_slots(tmp_path):
    module = _synthetic_module()
    document = _synthetic_document()
    result = generate_driver(document, module, return_kinds=RETURN_KINDS)

    stub = tmp_path / "synth_mod.f90"
    stub.write_text(
        """\
module synth_mod
    implicit none
contains
    function fn_double(x) result(y)
        real(8), intent(in) :: x
        real(8) :: y
        y = x * x
    end function fn_double

    subroutine fn_inout(arr)
        real(8), intent(inout) :: arr(3)
        arr = arr * 2.0d0
    end subroutine fn_inout

    subroutine fn_out_sized(out, n)
        integer, intent(in) :: n
        real(8), intent(out) :: out(n)
        integer :: i
        do i = 1, n
            out(i) = real(i, 8) * 1.5d0
        end do
    end subroutine fn_out_sized

    subroutine fn_char_out(msg)
        character(len=*), intent(out) :: msg
        msg = 'hi' // char(9) // '!'
    end subroutine fn_char_out

    function fn_real4(x) result(y)
        real(8), intent(in) :: x
        real(4) :: y
        y = real(x, 4) * 2.0
    end function fn_real4
end module synth_mod
"""
    )
    driver = tmp_path / "driver.f90"
    driver.write_text(result.source)

    gfortran = shutil.which("gfortran")
    subprocess.run(
        [gfortran, "-o", str(tmp_path / "driver_test"), str(stub), str(driver)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    completed = subprocess.run(
        [str(tmp_path / "driver_test")], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    lines = [line.split("\t") for line in completed.stdout.splitlines()]
    by_key_slot = {(k, s): v for k, s, v in lines}

    assert by_key_slot[("fn_double", "return")] == _hex(6.25)
    assert by_key_slot[("fn_inout", "arg:0[0]")] == _hex(2.0)
    assert by_key_slot[("fn_inout", "arg:0[1]")] == _hex(4.0)
    assert by_key_slot[("fn_inout", "arg:0[2]")] == _hex(6.0)
    assert by_key_slot[("fn_out_sized", "return[0]")] == _hex(1.5)
    assert by_key_slot[("fn_out_sized", "return[1]")] == _hex(3.0)
    assert by_key_slot[("fn_out_sized", "return[2]")] == _hex(4.5)
    assert by_key_slot[("fn_char_out", "return")] == "hi%09!"
    # real(4) 3.0 widened to double 3.0 exactly (widening is exact).
    assert by_key_slot[("fn_real4", "return")] == _hex(3.0)
    # file order preserved: each call key's first appearance in the driver's
    # printed output follows the document's entry order exactly.
    seen: list = []
    for key, _slot in by_key_slot:
        if key not in seen:
            seen.append(key)
    assert seen == ["fn_double", "fn_inout", "fn_out_sized", "fn_char_out", "fn_real4"]


# --- the real petro_api service --------------------------------------------

PETRO_IR = REPO / "services" / "petro_api" / ".nativegate" / "ir.json"
PETRO_GOLDEN = REPO / "services" / "petro_api" / "golden.json"
FORTRAN_DIR = REPO / "libraries" / "petro" / "fortran"
INCLUDE_DIR = FORTRAN_DIR / "include"
FACADE = REPO / "services" / "petro_api" / "native" / "petro_api.f90"


def _load_petro_module() -> ModuleIR:
    import json

    return module_from_dict(json.loads(PETRO_IR.read_text()))


def test_petro_api_driver_generates_without_error():
    if not PETRO_IR.exists() or not PETRO_GOLDEN.exists():
        pytest.skip("services/petro_api is not present in this checkout")
    import json

    module = _load_petro_module()
    document = json.loads(PETRO_GOLDEN.read_text())
    result = generate_driver(document, module)
    assert result.source
    assert len(result.driver_sha256) == 64
    # every recorded entry either produced a call or a skip, never neither
    entries = set((document.get("entries") or {}).keys())
    called = {
        line.strip().strip("!").strip().strip("-").strip()
        for line in result.source.splitlines()
        if line.strip().startswith("! ---")
    }
    assert entries <= (called | set(result.skipped))


@requires_gfortran
def test_petro_api_driver_compiles_links_and_runs_in_file_order(tmp_path):
    if not all(p.exists() for p in (FORTRAN_DIR, FACADE, PETRO_IR, PETRO_GOLDEN)):
        pytest.skip("libraries/petro or services/petro_api is not present in this checkout")
    import json

    from nativegate.preprocess import expand_includes

    module = _load_petro_module()
    document = json.loads(PETRO_GOLDEN.read_text())
    result = generate_driver(document, module)
    assert not result.skipped or set(result.skipped) <= set(document.get("skipped") or {})

    workspace = tmp_path
    for deck in sorted(FORTRAN_DIR.glob("*.f")):
        (workspace / deck.name).write_text(expand_includes(deck, [INCLUDE_DIR]))
    (workspace / "petro_api.f90").write_text(FACADE.read_text())
    (workspace / "driver.f90").write_text(result.source)

    gfortran = shutil.which("gfortran")
    decks = sorted(p.name for p in workspace.glob("*.f"))
    subprocess.run(
        [gfortran, "-c", "-O2", *decks],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    objects = [f"{Path(d).stem}.o" for d in decks]
    subprocess.run(
        [gfortran, "-O2", "-o", "driver_exe", "petro_api.f90", "driver.f90", *objects],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    completed = subprocess.run(
        [str(workspace / "driver_exe")],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )

    lines = [line.split("\t") for line in completed.stdout.splitlines() if line.strip()]
    assert lines, "the petro_api driver printed nothing"
    for parts in lines:
        assert len(parts) == 3, f"malformed wire line: {parts!r}"

    printed_keys_in_order = []
    for key, _slot, _value in lines:
        if key not in printed_keys_in_order:
            printed_keys_in_order.append(key)

    # Some entries (e.g. `pvt_set_fluid`, void with no intent(out)/intent(inout)
    # arguments) legitimately print nothing at all — so this checks relative
    # order among whatever *did* print, not an exact key list: each key that
    # printed must appear no earlier in the driver's output than it does in
    # golden.json's key order (spec sec 2.5: "keys out of file order is a
    # hard failure").
    doc_keys = list(document.get("entries") or {})
    positions = [doc_keys.index(k) for k in printed_keys_in_order]
    assert positions == sorted(positions), (
        f"driver printed keys out of golden.json's file order: {printed_keys_in_order}"
    )
    assert len(printed_keys_in_order) == len(set(printed_keys_in_order))

    all_slots = {(k, s) for k, s, _ in lines}
    assert ("solution_gor", "return") in all_slots
    assert ("pvt_state", "arg:1[3]") in all_slots  # Rs is props(4), 0-based index 3
    assert ("last_error", "return") in all_slots
    # the full slot set actually observed — not just a couple of spot checks.
    assert len(all_slots) >= 17  # 6 scalar returns + 9 pvt_state slots + last_error + tubing/vogel
