"""T5 — the oracle comparator and CLI (`native2py oracle check|show`).

Spec `design-verification-layers.md` sections 2.1, 2.2, 2.6, 2.10, section 4
rule 6. Two kinds of test, matching the sibling T3/T4 files' convention:

* unit tests against synthetic slot streams (no compiler needed) — one per
  section 2.10 classification signature, plus the section 2.2 sequence-
  divergence hard failure.
* a compiler-marked, real-service integration test: `oracle_check` against
  the real `petro_api` service must pass, and a driver deliberately
  sabotaged to swap two arguments must fail with the argument-order/
  transposition classification.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from native2py import driverbuild, golden, oracle, wire
from native2py.drivers.fortran import generate_driver
from native2py.ir import module_from_dict
from native2py.wire import Slot, arg_slot, pack_float, return_slot

REPO = Path(__file__).resolve().parents[3]

requires_gfortran = pytest.mark.skipif(
    shutil.which("gfortran") is None, reason="gfortran is not installed"
)


def _require_numpy():
    try:
        import numpy  # noqa: F401
    except ImportError:
        pytest.skip("numpy is not installed, so f2py cannot build the extension")


def hx(value: float) -> str:
    return pack_float(value)


# --- _classify: one per spec section 2.10 signature -------------------------


def test_classify_returns_none_when_every_slot_agrees():
    driver = {return_slot(): hx(1.5)}
    python = {return_slot(): hx(1.5)}
    assert oracle._classify("f", driver, python) is None


def test_classify_argument_order_or_intent_wrong():
    # Every slot of the call differs, wildly — not a small relative error.
    driver = {arg_slot(0): hx(10.0), arg_slot(1): hx(20.0)}
    python = {arg_slot(0): hx(999.0), arg_slot(1): hx(-5.0)}
    message = oracle._classify("f", driver, python)
    assert oracle.READING_ARGUMENT_ORDER in message


def test_classify_row_column_major_transposition():
    # Array slots are right but permuted: same multiset, different slots.
    driver = {arg_slot(0, 0): hx(1.0), arg_slot(0, 1): hx(2.0), arg_slot(0, 2): hx(3.0)}
    python = {arg_slot(0, 0): hx(2.0), arg_slot(0, 1): hx(3.0), arg_slot(0, 2): hx(1.0)}
    message = oracle._classify("f", driver, python)
    assert oracle.READING_TRANSPOSITION in message


def test_classify_float32_narrowing():
    # ~7 significant digits agreement then diverge: float32 epsilon scale.
    native = 355.0764805578969
    narrowed = float(__import__("struct").unpack(">f", __import__("struct").pack(">f", native))[0])
    assert narrowed != native
    driver = {return_slot(): hx(native)}
    python = {return_slot(): hx(narrowed)}
    message = oracle._classify("f", driver, python)
    assert oracle.READING_NARROWING in message


def test_classify_inplace_output_not_surfaced():
    # Return is right; arg: slots are simply missing on the Python side.
    driver = {
        return_slot(): hx(1.0),
        arg_slot(1, 0): hx(2.0),
        arg_slot(1, 1): hx(3.0),
    }
    python = {return_slot(): hx(1.0)}
    message = oracle._classify("f", driver, python)
    assert oracle.READING_INPLACE_MISSING in message


def test_classify_flags_or_link_uniform_tiny_drift():
    # Everything differs slightly, uniformly — near machine epsilon, not
    # narrowing-scale, and not "wildly" different.
    driver = {arg_slot(0): hx(1.0), arg_slot(1): hx(2.0)}
    python = {
        arg_slot(0): hx(1.0 * (1 + 1e-14)),
        arg_slot(1): hx(2.0 * (1 + 1e-14)),
    }
    message = oracle._classify("f", driver, python)
    assert oracle.READING_FLAGS_OR_LINK in message


# --- compare_documents: sequence discipline (spec section 2.2) -------------


def test_sequence_divergence_when_python_cannot_run_what_the_driver_ran():
    order = ["a", "b", "c"]
    driver_skip = {}  # driver ran all three
    driver_slots = {
        "a": {return_slot(): hx(1.0)},
        "b": {return_slot(): hx(2.0)},
        "c": {return_slot(): hx(3.0)},
    }
    python_errors = {"b": "ValueError: boom"}  # python side failed on b
    python_slots = {"a": {return_slot(): hx(1.0)}}

    covered, skipped, failures = oracle.compare_documents(
        order, driver_skip, driver_slots, python_errors, python_slots
    )
    assert covered == ["a"]
    assert skipped == {}
    assert len(failures) == 1
    assert oracle.READING_SEQUENCE_DIVERGED in failures[0]
    assert "b" in failures[0]
    # Nothing at or after "b" is compared: entry "c" is never named.
    assert "'c'" not in " ".join(failures)
    assert " c " not in " ".join(failures)


def test_sequence_divergence_when_driver_skipped_what_python_ran():
    order = ["a", "b"]
    driver_skip = {"a": "the Fortran driver (v1) only calls free functions"}
    driver_slots = {}
    python_errors = {}  # python ran "a" successfully
    python_slots = {"a": {return_slot(): hx(1.0)}, "b": {return_slot(): hx(2.0)}}

    covered, skipped, failures = oracle.compare_documents(
        order, driver_skip, driver_slots, python_errors, python_slots
    )
    assert covered == []
    assert len(failures) == 1
    assert oracle.READING_SEQUENCE_DIVERGED in failures[0]
    assert "a" in failures[0]


def test_both_sides_skipping_the_same_entry_is_not_a_failure():
    order = ["a", "b"]
    reason = "no public constructor callable from Python"
    driver_skip = {"a": reason}
    python_errors = {"a": reason}
    driver_slots = {"b": {return_slot(): hx(9.0)}}
    python_slots = {"b": {return_slot(): hx(9.0)}}

    covered, skipped, failures = oracle.compare_documents(
        order, driver_skip, driver_slots, python_errors, python_slots
    )
    assert failures == []
    assert covered == ["b"]
    assert skipped == {"a": reason}


def test_zero_coverage_is_a_failure_at_the_report_level():
    report = oracle.OracleReport(covered=0, skipped={}, failures=[])
    assert report.passed is False
    report_with_skip_only = oracle.OracleReport(covered=0, skipped={"x": "reason"}, failures=[])
    assert report_with_skip_only.passed is False


def test_a_clean_run_passes():
    report = oracle.OracleReport(covered=3, skipped={}, failures=[])
    assert report.passed is True


# --- python_slot_values: wire.pack_float on the Python side (T2 reuse) -----


def test_python_slot_values_scalar_return():
    slots = [return_slot()]
    values = oracle.python_slot_values(slots, 355.0764805578969, {})
    assert values[return_slot()] == pack_float(355.0764805578969)


def test_python_slot_values_array_argument_effect():
    slots = [arg_slot(1, 0), arg_slot(1, 1)]
    values = oracle.python_slot_values(slots, None, {"1": [10.0, 20.0]})
    assert values[arg_slot(1, 0)] == pack_float(10.0)
    assert values[arg_slot(1, 1)] == pack_float(20.0)


# --- driver_slots_by_key: T2's parser wired to a real document -------------


def test_driver_slots_by_key_uses_wire_parse_lines(tmp_path):
    document = {
        "entries": {
            "solution_gor": {"kind": "function", "name": "solution_gor", "arguments": [2000.0], "result": 355.076},
        }
    }
    from native2py.ir import FunctionDef, ModuleIR, Parameter

    fn = FunctionDef(
        name="solution_gor",
        returns="float",
        parameters=[Parameter(name="p", type="float", intent="in")],
    )
    module = ModuleIR(name="petro_api", language="fortran", source_file="petro_api.f90", functions=[fn])
    stdout = f"solution_gor\treturn\t{pack_float(355.076)}\n"
    result = oracle.driver_slots_by_key(document, module, {}, stdout)
    assert result == {"solution_gor": {return_slot(): pack_float(355.076)}}


# --- oracle_show: coverage + slot layout, no build (real fixture) ----------

PETRO_SERVICE_DIR = REPO / "services" / "petro_api"


def _require_petro_fixture():
    if not (PETRO_SERVICE_DIR / "golden.json").exists() or not (
        PETRO_SERVICE_DIR / ".native2py" / "ir.json"
    ).exists():
        pytest.skip("services/petro_api golden.json/IR are not present in this checkout")


def test_oracle_show_lists_entries_and_slots_without_building():
    _require_petro_fixture()
    report = oracle.oracle_show("petro_api", service_dir=PETRO_SERVICE_DIR)
    assert report.entries
    keys = {e.key for e in report.entries}
    assert "solution_gor" in keys
    solution_gor = next(e for e in report.entries if e.key == "solution_gor")
    assert solution_gor.slots == ["return"]


# --- compiler-marked end-to-end tests ---------------------------------------

FORTRAN_SOURCES = [
    "petro_api.f90",
    "flash.f",
    "hydrau.f",
    "matsol.f",
    "pvtcor.f",
    "relperm.f",
    "simcor.f",
    "wellib.f",
]
EXPANDED_DIR = PETRO_SERVICE_DIR / "native" / "_expanded"


def _require_petro_extension_fixture():
    _require_petro_fixture()
    if not all((EXPANDED_DIR / s).exists() for s in FORTRAN_SOURCES):
        pytest.skip("services/petro_api/native/_expanded is not present in this checkout")


@requires_gfortran
def test_oracle_check_passes_against_the_real_petro_api_service():
    _require_petro_extension_fixture()
    _require_numpy()

    report = oracle.oracle_check("petro_api", service_dir=PETRO_SERVICE_DIR)

    assert report.failures == [], f"unexpected oracle failure(s): {report.failures}"
    assert report.covered > 0
    assert report.passed is True
    assert len(report.driver_sha256) == 64


@requires_gfortran
def test_a_sabotaged_driver_is_classified_as_argument_order_or_transposition():
    """Swap two arguments in a copy of the generated driver, then run the
    same comparison `oracle_check` would — the classification must land on
    the argument-order or the permuted-array signature, never silently pass.
    """
    _require_petro_extension_fixture()
    _require_numpy()

    module = module_from_dict(json.loads((PETRO_SERVICE_DIR / ".native2py" / "ir.json").read_text()))
    document = json.loads((PETRO_SERVICE_DIR / "golden.json").read_text())
    driver = generate_driver(document, module)

    # `vogel_rate(res_pressure, flowing_bhp, q_max)` is called with three
    # distinct floats (4000.0, 2500.0, 6000.0 per the facade's recorded
    # entry) — swap the first two positional arguments in the emitted
    # Fortran call site. The call is
    # `c<idx>_result = vogel_rate(c<idx>_res_pressure, c<idx>_flowing_bhp, c<idx>_q_max)`
    # (native2py's own local variable naming, taken straight from the
    # parameter names), so swap the two `init` assignments' right-hand
    # sides instead of the call site, which has the same effect (the values
    # feeding "res_pressure" and "flowing_bhp" are exchanged) without having
    # to know f2py's parameter order.
    import re as _re

    key = "vogel_rate"
    assert key in document["entries"], "fixture assumption: vogel_rate is recorded"
    entry = document["entries"][key]
    assert len(entry["arguments"]) >= 2

    lines = driver.source.splitlines()
    var_prefix = None
    for i, line in enumerate(lines):
        if line.strip() == f"! --- {key} ---":
            var_prefix_match = _re.search(r"c(\d+)_", "\n".join(lines[i : i + 6]))
            if var_prefix_match:
                idx = var_prefix_match.group(1)
                var_prefix = f"c{idx}_"
            break
    assert var_prefix, f"could not locate the {key} call block in the generated driver"

    # Find the two init lines for this call's first two arguments and swap
    # their right-hand sides.
    param_names = [f"{var_prefix}{name}" for name in ("res_pressure", "flowing_bhp")]
    rhs_by_var = {}
    line_index_by_var = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        for var in param_names:
            if stripped.startswith(f"{var} ="):
                rhs_by_var[var] = stripped.split("=", 1)[1].strip()
                line_index_by_var[var] = i
    assert set(rhs_by_var) == set(param_names), (
        f"could not find both init lines for {param_names} in the driver:\n{driver.source}"
    )
    a, b = param_names
    lines[line_index_by_var[a]] = lines[line_index_by_var[a]].replace(rhs_by_var[a], rhs_by_var[b])
    lines[line_index_by_var[b]] = lines[line_index_by_var[b]].replace(rhs_by_var[b], rhs_by_var[a])
    sabotaged_source = "\n".join(lines)
    assert sabotaged_source != driver.source

    build_dir = Path(driverbuild.__file__).parent  # placeholder, replaced below
    import tempfile

    work_root = Path(tempfile.mkdtemp(prefix="n2p_oracle_sabotage_"))
    try:
        sources_dir, source_names = oracle._fortran_sources(PETRO_SERVICE_DIR)
        extension_source_name = oracle._facade_source_name(module, source_names)
        module_name = oracle._sanitize_module_name("petro_api_sabotage")

        ext_build_dir = work_root / "ext_build"
        compile_commands = driverbuild.build_extension_with_compile_commands(
            [sources_dir / n for n in source_names], module_name, ext_build_dir
        )

        driver_result = driverbuild.build_and_run_driver(
            sabotaged_source,
            compile_commands,
            extension_source_name,
            source_names,
            work_root / "driver",
        )

        driver_slots = oracle.driver_slots_by_key(document, module, driver.skipped, driver_result.stdout)

        extension = oracle._import_extension(module_name, oracle._f2py_extension_cwd(ext_build_dir))
        package = oracle._package_namespace(module, extension)

        functions_by_name = {fn.name: fn for fn in module.functions}
        python_slots = {}
        python_errors = {}
        for k, e in document.get("entries", {}).items():
            if k in driver.skipped:
                continue
            fn = functions_by_name.get(e.get("name") or k)
            if fn is None:
                python_errors[k] = "no matching function in the IR"
                continue
            try:
                result, effects = golden.invoke(e, package)
            except Exception as exc:  # noqa: BLE001
                python_errors[k] = f"{type(exc).__name__}: {exc}"
                continue
            slots = wire.slots_for_entry(e, fn)
            python_slots[k] = oracle.python_slot_values(slots, result, effects)

        order = list(document.get("entries", {}).keys())
        covered, skipped, failures = oracle.compare_documents(
            order, driver.skipped, driver_slots, python_errors, python_slots
        )

        assert failures, "the sabotaged driver must not pass"
        vogel_failure = next((f for f in failures if f.startswith(f"{key}:")), None)
        assert vogel_failure is not None, (
            f"expected a failure for {key!r}, got: {failures}"
        )
        assert (
            oracle.READING_ARGUMENT_ORDER in vogel_failure
            or oracle.READING_TRANSPOSITION in vogel_failure
        ), f"unexpected classification: {vogel_failure}"
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


# --- T6: `oracle record` and the optional oracle.json -----------------------
#
# Spec design-verification-layers.md section 2.6 (file is provenance, not the
# gate), section 2.7 (schema), section 4 rules 7-9 (canonical serialization,
# everything hashed, nothing environmental).


def test_build_oracle_document_schema_is_byte_stable(tmp_path):
    """Freeze the serialization of a synthetic report: `indent=2`,
    `sort_keys=False`, LF, trailing newline — matching golden.write()."""
    from native2py.ir import FunctionDef, ModuleIR, Parameter

    module = ModuleIR(
        name="petro_api",
        language="fortran",
        source_file="petro_api.f90",
        functions=[
            FunctionDef(
                name="solution_gor",
                returns="float",
                parameters=[Parameter(name="p", type="float", intent="in")],
            )
        ],
    )
    golden_document = {
        "entries": {
            "solution_gor": {"arguments": [2000.0], "result": 355.076},
        },
        "skipped": {"Grid.solve": "no public constructor callable from Python"},
    }
    python_slots = {"solution_gor": {return_slot(): pack_float(355.076)}}
    current_provenance = {
        "platform": "Darwin 24.6.0",
        "machine": "arm64",
        "python": "3.11.5",
        "python_implementation": "CPython",
        "numpy": "2.4.6",
        "fortran_compiler": "GNU Fortran (Homebrew GCC 16.2.0) 16.2.0",
        "cxx_compiler": "Apple clang version 17.0.0",
        "native2py": "0.1.0",
        "sources": {"services/petro_api/native/petro_api.f90": "deadbeef"},
        "compile_flags": ["-O2", "-fPIC", "-std=legacy"],
    }

    document = oracle.build_oracle_document(
        "petro_api",
        module,
        golden_document,
        python_slots,
        current_provenance,
        driver_sha256="a" * 64,
        link_target_sha256="b" * 64,
    )

    path = tmp_path / "oracle.json"
    oracle.write_oracle_document(path, document)
    text = path.read_text()

    expected = json.dumps(
        {
            "format": 1,
            "service": "petro_api",
            "language": "fortran",
            "provenance": {
                **current_provenance,
                "driver_sha256": "a" * 64,
                "link_target_sha256": "b" * 64,
            },
            "entries": {
                "solution_gor": {
                    "arguments": [2000.0],
                    "slots": {"return": pack_float(355.076)},
                },
            },
            "skipped": {"Grid.solve": "no public constructor callable from Python"},
        },
        indent=2,
        sort_keys=False,
    ) + "\n"

    assert text == expected
    assert text.endswith("\n")
    assert "\r" not in text
    # Nothing environmental (spec section 4 rule 9): no timestamp/path/host
    # keys sneak into the document beyond what the caller supplied.
    assert set(document["provenance"]) == set(current_provenance) | {
        "driver_sha256",
        "link_target_sha256",
    }


def test_build_oracle_document_records_uncovered_entries_as_skipped():
    from native2py.ir import ModuleIR

    module = ModuleIR(name="petro_api", language="fortran", source_file="petro_api.f90", functions=[])
    golden_document = {
        "entries": {
            "solution_gor": {"arguments": [2000.0], "result": 355.076},
            "oil_fvf": {"arguments": [2000.0], "result": 1.2},
        },
        "skipped": {},
    }
    # oil_fvf was not covered this run (e.g. a Python-side error) — not in
    # python_slots.
    python_slots = {"solution_gor": {return_slot(): pack_float(355.076)}}

    document = oracle.build_oracle_document(
        "petro_api", module, golden_document, python_slots, {}, "a" * 64, "b" * 64
    )
    assert "oil_fvf" not in document["entries"]
    assert "oil_fvf" in document["skipped"]
    assert "solution_gor" in document["entries"]


# --- T6: provenance-mismatch refusal (historical diff, not the live gate) --


def test_provenance_matches_build_checks_only_the_declared_fields():
    recorded = {
        "platform": "Darwin 24.6.0",
        "fortran_compiler": "GNU Fortran 16.2.0",
        "cxx_compiler": "clang 17",
        "compile_flags": ["-O2"],
        "sources": {"a.f90": "deadbeef"},
        "python": "3.11.5",  # NOT in the match set — irrelevant to a bitwise claim
    }
    current_same_build = dict(recorded, python="3.12.0")
    assert oracle._provenance_matches_build(recorded, current_same_build) is True

    current_different_flags = dict(recorded, compile_flags=["-O3"])
    assert oracle._provenance_matches_build(recorded, current_different_flags) is False

    current_different_sources = dict(recorded, sources={"a.f90": "cafef00d"})
    assert oracle._provenance_matches_build(recorded, current_different_sources) is False


def test_provenance_mismatch_message_names_the_pinned_build_and_never_offers_tolerance():
    recorded = {"platform": "Darwin 24.6.0", "compile_flags": ["-O2"], "sources": {}}
    current = {"platform": "Linux 6.1.0", "compile_flags": ["-O2"], "sources": {}}
    message = oracle._provenance_mismatch_message("petro_api", recorded, current)
    assert "pinned to the build image" in message
    assert "platform: recorded 'Darwin 24.6.0', now 'Linux 6.1.0'" in message
    assert "tolerance" in message and "no tolerance mode" in message.lower()
    assert "live oracle check above still ran" in message


def test_historical_bit_diff_reports_mismatches_bitwise_no_tolerance():
    recorded_entries = {
        "solution_gor": {"arguments": [2000.0], "slots": {"return": pack_float(355.076)}},
    }
    # A last-bit difference IS a diff — there is no tolerance to absorb it.
    python_slots = {"solution_gor": {return_slot(): pack_float(355.0760000001)}}
    diffs = oracle._historical_bit_diff(recorded_entries, python_slots)
    assert len(diffs) == 1
    assert "solution_gor.return" in diffs[0]
    assert "recorded" in diffs[0] and "now" in diffs[0]


def test_historical_bit_diff_is_empty_when_bits_match_exactly():
    recorded_entries = {
        "solution_gor": {"arguments": [2000.0], "slots": {"return": pack_float(355.076)}},
    }
    python_slots = {"solution_gor": {return_slot(): pack_float(355.076)}}
    assert oracle._historical_bit_diff(recorded_entries, python_slots) == []


# --- T6: stale driver hash (spec section 2.6's "every generator change
# invalidates every recorded oracle file") -----------------------------------


def test_driver_hash_mismatch_message_explains_generator_change_and_recording():
    message = oracle._driver_hash_mismatch_message("aaaa", "bbbb")
    assert "aaaa" in message and "bbbb" in message
    assert "generator changed" in message or "generator" in message
    assert "re-record" in message.lower() or "oracle record" in message
    assert "upgrading the tool" in message


@requires_gfortran
def test_stale_driver_hash_fails_check_loudly(tmp_path):
    """A committed oracle.json whose driver_sha256 no longer matches the
    freshly generated driver must fail `oracle_check` immediately, before
    any build happens, with a message naming the generator and re-recording
    — never a silent pass and never a bare hash-mismatch string."""
    _require_petro_extension_fixture()
    _require_numpy()

    service_dir = tmp_path / "petro_api"
    shutil.copytree(PETRO_SERVICE_DIR, service_dir)
    stale_document = {
        "format": 1,
        "service": "petro_api",
        "language": "fortran",
        "provenance": {"driver_sha256": "0" * 64},
        "entries": {},
        "skipped": {},
    }
    (service_dir / oracle.ORACLE_FILENAME).write_text(json.dumps(stale_document))

    with pytest.raises(oracle.OracleError) as excinfo:
        oracle.oracle_check("petro_api", service_dir=service_dir)

    message = str(excinfo.value)
    assert "0" * 64 in message
    assert "generator changed" in message
    assert "upgrading the tool" in message


# --- T6: compiler-marked record -> check round trip on petro_api -----------


@requires_gfortran
def test_oracle_record_then_check_round_trip_on_petro_api(tmp_path):
    """`oracle record` writes a passing check's bits to oracle.json; a
    subsequent `oracle_check` in the SAME build image must then diff
    bitwise against it and find nothing to report (no tolerance mode, spec
    section 2.6/4 rule 9's "no tolerance mode")."""
    _require_petro_extension_fixture()
    _require_numpy()

    service_dir = tmp_path / "petro_api"
    shutil.copytree(PETRO_SERVICE_DIR, service_dir)
    oracle_path = service_dir / oracle.ORACLE_FILENAME
    assert not oracle_path.exists()

    document = oracle.oracle_record("petro_api", service_dir=service_dir)
    assert oracle_path.exists()
    assert document["service"] == "petro_api"
    assert document["entries"], "record must cover at least one entry"
    assert len(document["provenance"]["driver_sha256"]) == 64
    assert len(document["provenance"]["link_target_sha256"]) == 64
    assert document["provenance"]["compile_flags"]

    # Byte-stable, canonical serialization on disk.
    text = oracle_path.read_text()
    assert text.endswith("\n") and "\r" not in text
    reloaded = json.loads(text)
    assert reloaded == document

    # Same build image: `check` must now diff bitwise against oracle.json
    # and find no mismatch — same machine, same object code, same run.
    report = oracle.oracle_check("petro_api", service_dir=service_dir)
    assert report.passed
    assert report.historical_diff_refused is None, report.historical_diff_refused
    assert report.historical_diff == [], report.historical_diff


@requires_gfortran
def test_oracle_check_refuses_historical_diff_on_provenance_mismatch_but_still_checks_live(tmp_path):
    """A recorded oracle.json pinned to a different build image (spec
    section 2.6: "the file is pinned to its build image") must not be
    numerically compared — the CLI refuses that comparison with a message
    naming the mismatch, but the live bitwise check against the fresh driver
    still runs and can still pass."""
    _require_petro_extension_fixture()
    _require_numpy()

    service_dir = tmp_path / "petro_api"
    shutil.copytree(PETRO_SERVICE_DIR, service_dir)

    document = oracle.oracle_record("petro_api", service_dir=service_dir)
    oracle_path = service_dir / oracle.ORACLE_FILENAME

    # Mutate the recorded provenance to simulate a different build image —
    # driver_sha256 stays correct (that check is independent), but the
    # platform the file claims to have been built on no longer matches.
    document["provenance"]["platform"] = "Linux 5.15.0 (a different build image)"
    oracle.write_oracle_document(oracle_path, document)

    report = oracle.oracle_check("petro_api", service_dir=service_dir)

    # The live check is unaffected by the stale provenance.
    assert report.passed, report.failures
    assert report.covered > 0

    # But the historical comparison is refused, not tolerance-compared.
    assert report.historical_diff == []
    assert report.historical_diff_refused is not None
    assert "pinned to the build image" in report.historical_diff_refused
    assert "platform" in report.historical_diff_refused
    assert "live oracle check above still ran" in report.historical_diff_refused


# --- Gap A (T5/T7 integration): oracle_check dispatches to the C++ path ----
#
# T7's C++ driver generator (drivers/cpp.py) and the generalized
# driverbuild.py (language="cpp") existed but were never wired into
# oracle_check/oracle_show/oracle_record for a C++ module — this proves the
# actual integration: a real pybind11 extension, a real C++ oracle driver,
# built and run through the same `oracle_check` entry point the Fortran path
# uses, bitwise-comparing a real extension call against the driver.

requires_cxx_cmake = pytest.mark.skipif(
    shutil.which("cmake") is None
    or (shutil.which("clang++") is None and shutil.which("g++") is None),
    reason="cmake and a C++ compiler are required",
)


def _pybind11_importable() -> bool:
    try:
        import pybind11  # noqa: F401
    except ImportError:
        return False
    return True


CPP_FIXTURE_HEADER = "#pragma once\nint n2p_add_ints(int a, int b);\n"
CPP_FIXTURE_IMPL = (
    '#include "fixture.hpp"\n\nint n2p_add_ints(int a, int b) { return a + b; }\n'
)


def _cpp_oracle_service_dir(tmp_path: Path) -> Path:
    from native2py.ir import FunctionDef, ModuleIR, Parameter, module_to_dict

    service_dir = tmp_path / "services" / "synth_cpp"
    native_dir = service_dir / "native"
    native_dir.mkdir(parents=True, exist_ok=True)
    (native_dir / "fixture.hpp").write_text(CPP_FIXTURE_HEADER)
    (native_dir / "fixture.cpp").write_text(CPP_FIXTURE_IMPL)

    module = ModuleIR(
        name="synth_cpp",
        language="cpp",
        source_file="fixture.hpp",
        functions=[
            FunctionDef(
                name="n2p_add_ints",
                parameters=[
                    Parameter(name="a", type="int", intent="in"),
                    Parameter(name="b", type="int", intent="in"),
                ],
                returns="int",
            )
        ],
    )
    ir_path = service_dir / oracle.IR_RELATIVE_PATH
    ir_path.parent.mkdir(parents=True, exist_ok=True)
    ir_path.write_text(json.dumps(module_to_dict(module)))

    golden_document = {
        "format": 2,
        "service": "synth_cpp",
        "language": "cpp",
        "entries": {
            "n2p_add_ints": {
                "kind": "function",
                "name": "n2p_add_ints",
                "arguments": [2, 3],
                "result": 5,
            }
        },
        "skipped": {},
    }
    golden.write(service_dir / golden.GOLDEN_FILENAME, golden_document)
    return service_dir


@requires_cxx_cmake
def test_oracle_check_builds_and_runs_a_real_cpp_driver(tmp_path):
    if not _pybind11_importable():
        pytest.skip("pybind11 is not importable from this interpreter")

    service_dir = _cpp_oracle_service_dir(tmp_path)

    report = oracle.oracle_check("synth_cpp", service_dir=service_dir)

    assert report.passed, report.failures
    assert report.covered == 1
    assert report.failures == []


@requires_cxx_cmake
def test_oracle_show_lists_cpp_entries_without_building(tmp_path):
    if not _pybind11_importable():
        pytest.skip("pybind11 is not importable from this interpreter")

    service_dir = _cpp_oracle_service_dir(tmp_path)
    report = oracle.oracle_show("synth_cpp", service_dir=service_dir)

    assert [e.key for e in report.entries] == ["n2p_add_ints"]
    assert report.entries[0].slots == ["return"]
