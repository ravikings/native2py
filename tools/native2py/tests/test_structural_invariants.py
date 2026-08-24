"""T10 — structural invariants runner.

Spec: design-verification-layers.md section 3.2 (structural table), section
3.5 (mutator scoping, fresh process, setup replay).

Two kinds of test, per the task's acceptance criteria:

* A synthetic-fixture test (a plain Python module standing in for a native
  extension) needing no compiler at all: a deliberate hidden global must
  fail `order_independent` with the "add it to `mutating`" guidance.
* Compiler-marked, real-`petro_api` tests (built once per module, the same
  way `test_driverbuild.py` does): `pvt_set_fluid` is exempted as declared
  (`state.mutating`), and `no_error_flag` flags a point reached by
  deliberately driving `pvt_set_fluid`'s `api_gravity` out of range in a
  copy of golden.json's setup call.
"""

from __future__ import annotations

import copy
import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from native2py import driverbuild, golden, structural_invariants as si
from native2py.config import RangeDeclaration, ServiceConfig, StateConfig, VerificationConfig
from native2py.ir import FunctionDef, ModuleIR, Parameter, module_from_dict

REPO = Path(__file__).resolve().parents[3]

requires_gfortran = pytest.mark.skipif(
    shutil.which("gfortran") is None, reason="gfortran is not installed"
)


def _require_numpy():
    try:
        import numpy  # noqa: F401
    except ImportError:
        pytest.skip("numpy is not installed, so f2py cannot build the extension")


# --- synthetic fixture: a deliberate hidden global, no compiler needed ----

_HIDDEN_STATE_FIXTURE = '''
"""A stand-in extension with a deliberate, UNDECLARED hidden global.

Simulates the pattern order_independent exists to catch (spec section 3.5):
`f` reads process-global state that `bump` mutates, and nothing in
native2py.yaml says `bump` is a mutator.
"""

_counter = 0


def f(x):
    return float(x) + _counter


def bump(y):
    global _counter
    _counter += 1
    return float(y)


def last_error():
    return 0
'''


def _write_hidden_state_fixture(tmp_path: Path) -> Path:
    fixture_dir = tmp_path / "fixture_pkg"
    fixture_dir.mkdir()
    (fixture_dir / "hidden_state_fixture.py").write_text(_HIDDEN_STATE_FIXTURE)
    return fixture_dir


def _hidden_state_module_and_document():
    module = ModuleIR(
        name="hidden_state_fixture",
        language="cpp",
        source_file="hidden_state_fixture.py",
        functions=[
            FunctionDef(name="f", parameters=[Parameter(name="x", type="float")], returns="float"),
            FunctionDef(name="bump", parameters=[Parameter(name="y", type="float")], returns="float"),
            FunctionDef(name="last_error", parameters=[], returns="int"),
        ],
    )
    document = {
        "entries": {
            "f": {
                "kind": "function", "class": None, "name": "f",
                "constructor_arguments": [], "arguments": [1.0], "result": 1.0,
            },
            "bump": {
                "kind": "function", "class": None, "name": "bump",
                "constructor_arguments": [], "arguments": [2.0], "result": 2.0,
            },
            "last_error": {
                "kind": "function", "class": None, "name": "last_error",
                "constructor_arguments": [], "arguments": [], "result": 0,
            },
        }
    }
    return module, document


def test_hidden_global_fails_order_independent_with_add_to_mutating_message(tmp_path):
    fixture_dir = _write_hidden_state_fixture(tmp_path)
    sys.path.insert(0, str(fixture_dir))
    try:
        package = importlib.import_module("hidden_state_fixture")
    finally:
        sys.path.remove(str(fixture_dir))

    module, document = _hidden_state_module_and_document()
    verification = VerificationConfig(
        state=StateConfig(setup=[], mutating=[], error_flag="last_error")
    )
    target = si.SubprocessTarget(module_name="hidden_state_fixture", sys_path=(str(fixture_dir),))

    results = si.run_structural_invariants(
        module, document, verification, package, target, points_limit=1
    )

    order_independent = {o.function: o for o in results.by_property("order_independent")}
    assert "f" in order_independent
    outcome = order_independent["f"]
    assert outcome.status == "fail"
    assert outcome.failures, "expected at least one order_independent failure for f"
    message = outcome.failures[0].detail
    assert "bump" in message
    assert "mutating" in message
    assert "native2py.yaml" in message

    # `bump` itself is legitimately non-idempotent-with-itself only in the
    # order_independent sense above; idempotent (f(x) twice, nothing else
    # interposed) must still PASS for `f` -- the leak is only visible when
    # something else runs in between.
    idempotent = {o.function: o for o in results.by_property("idempotent")}
    assert idempotent["f"].status == "pass"


# --- real petro_api: pvt_set_fluid exempted, no_error_flag catches a point -

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
EXPANDED_DIR = REPO / "services" / "petro_api" / "native" / "_expanded"
PETRO_IR = REPO / "services" / "petro_api" / ".native2py" / "ir.json"
PETRO_GOLDEN = REPO / "services" / "petro_api" / "golden.json"
PETRO_SERVICE_DIR = REPO / "services" / "petro_api"


def _require_petro_fixture():
    if not all((EXPANDED_DIR / s).exists() for s in FORTRAN_SOURCES):
        pytest.skip("services/petro_api/native/_expanded is not present in this checkout")
    if not PETRO_IR.exists() or not PETRO_GOLDEN.exists():
        pytest.skip("services/petro_api IR/golden.json are not present in this checkout")


@pytest.fixture(scope="module")
def petro_extension(tmp_path_factory):
    """Build the real petro_api f2py extension ONCE per test module and
    import it, the same way test_driverbuild.py's petro_extension_build
    fixture does -- returns (package, SubprocessTarget)."""
    _require_petro_fixture()
    _require_numpy()
    if shutil.which("gfortran") is None:
        pytest.skip("gfortran is not installed")

    build_dir = tmp_path_factory.mktemp("petro_si_build")
    sources = [EXPANDED_DIR / s for s in FORTRAN_SOURCES]
    module_name = "n2p_t10_petro"
    driverbuild.build_extension_with_compile_commands(
        sources,
        module_name,
        build_dir,
        only=[
            "pvt_set_fluid", "solution_gor", "oil_fvf", "oil_viscosity",
            "gas_z_factor", "bubble_point", "pvt_state", "tubing_bhp",
            "vogel_rate", "last_error",
        ],
    )
    run_cwd = build_dir.parent / f"{build_dir.name}-f2py-cwd"
    sys.path.insert(0, str(run_cwd))
    try:
        extension = importlib.import_module(module_name)
    finally:
        sys.path.remove(str(run_cwd))
    # f2py nests routines under <extension>.<fortran module name>.<routine>
    # (petro_api.f90 declares `module petro_api`) rather than at the
    # extension's top level -- the generated services/petro_api/python
    # wrapper flattens this (see its __init__.py), but this test talks to
    # the raw extension directly, so it has to reach through the same
    # nesting the wrapper does.
    package = extension.petro_api
    target = si.SubprocessTarget(
        module_name=module_name, sys_path=(str(run_cwd),), attr_path=("petro_api",)
    )
    return package, target


def _load_petro_module_and_document():
    module = module_from_dict(json.loads(PETRO_IR.read_text()))
    document = json.loads(PETRO_GOLDEN.read_text())
    return module, document


@requires_gfortran
def test_pvt_set_fluid_is_exempted_from_idempotent_and_order_independent(petro_extension):
    package, target = petro_extension
    module, document = _load_petro_module_and_document()
    config = ServiceConfig.load(PETRO_SERVICE_DIR)
    config.verification.validate_against_ir(module)

    results = si.run_structural_invariants(
        module, document, config.verification, package, target, points_limit=1
    )

    for property_name in ("idempotent", "order_independent"):
        functions_checked = {o.function for o in results.by_property(property_name)}
        assert "pvt_set_fluid" not in functions_checked, (
            f"{property_name} must exempt the declared mutator pvt_set_fluid "
            "(spec section 3.5), but it was checked"
        )


@requires_gfortran
def test_no_error_flag_catches_an_out_of_range_setup(petro_extension):
    """Drive pvt_set_fluid's api_gravity below 1.0 (libraries/petro/fortran/
    pvtcor.f's PVTINI sets IERR = -1 there) so `last_error` is nonzero after
    every subsequent call replaying that setup -- no_error_flag must flag
    it."""
    package, target = petro_extension
    module, document = _load_petro_module_and_document()
    config = ServiceConfig.load(PETRO_SERVICE_DIR)
    config.verification.validate_against_ir(module)

    sabotaged_document = copy.deepcopy(document)
    sabotaged_document["entries"]["pvt_set_fluid"]["arguments"] = [0.5, 0.65, 180.0, 2]

    results = si.run_structural_invariants(
        module, sabotaged_document, config.verification, package, target, points_limit=1
    )

    no_error_flag = {o.function: o for o in results.by_property("no_error_flag")}
    assert "solution_gor" in no_error_flag
    outcome = no_error_flag["solution_gor"]
    assert outcome.status == "fail", (
        "no_error_flag should have caught last_error() reporting nonzero "
        "after the sabotaged pvt_set_fluid setup"
    )
    assert "last_error" in outcome.failures[0].detail


@requires_gfortran
def test_finite_and_total_pass_on_the_real_service(petro_extension):
    package, target = petro_extension
    module, document = _load_petro_module_and_document()
    config = ServiceConfig.load(PETRO_SERVICE_DIR)
    config.verification.validate_against_ir(module)

    results = si.run_structural_invariants(
        module, document, config.verification, package, target, points_limit=3
    )

    for outcome in results.by_property("finite") + results.by_property("total"):
        assert outcome.status == "pass", (
            f"{outcome.property} failed for {outcome.function}: {outcome.failures}"
        )


@requires_gfortran
def test_idempotent_passes_for_non_mutating_petro_routines(petro_extension):
    package, target = petro_extension
    module, document = _load_petro_module_and_document()
    config = ServiceConfig.load(PETRO_SERVICE_DIR)
    config.verification.validate_against_ir(module)

    results = si.run_structural_invariants(
        module, document, config.verification, package, target, points_limit=1
    )

    idempotent = results.by_property("idempotent")
    assert idempotent, "expected at least one non-mutating routine to be checked"
    for outcome in idempotent:
        assert outcome.status == "pass", f"idempotent failed for {outcome.function}: {outcome.failures}"
