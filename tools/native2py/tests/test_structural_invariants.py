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


# --- synthetic fixture: clear-on-read error_flag, no compiler needed ------
#
# Both fixtures below (clear-on-read, and error-sensitive-to-a-`g`-call)
# share the same shape -- a module with `f(x)` and a clear-on-read
# `last_error()`, an IR describing exactly those two functions, and a
# matching golden.json-style document -- so the write/import/IR/document
# scaffolding is factored into one set of helpers, parameterised by the
# module's source text and its two functions' recorded results.

_CLEAR_ON_READ_FIXTURE = '''
"""A stand-in extension whose error accessor clears on read -- exactly the
shape `structural_invariants.py`'s error_flag exemption exists for."""

_error_code = 7


def f(x):
    return float(x)


def last_error():
    global _error_code
    value = _error_code
    _error_code = 0
    return value
'''


def _write_fixture_module(tmp_path: Path, module_name: str, source: str) -> Path:
    fixture_dir = tmp_path / "fixture_pkg"
    fixture_dir.mkdir()
    (fixture_dir / f"{module_name}.py").write_text(source)
    return fixture_dir


def _import_fixture(fixture_dir: Path, module_name: str):
    sys.path.insert(0, str(fixture_dir))
    try:
        return importlib.import_module(module_name)
    finally:
        sys.path.remove(str(fixture_dir))


def _f_and_last_error_module_and_document(module_name: str, f_result: float, error_result: int):
    """An `f(x)`/`last_error()` `ModuleIR` + matching document, parameterised
    by each function's recorded golden.json result -- the two fixtures in
    this section differ only in these values and in their source text."""
    module = ModuleIR(
        name=module_name,
        language="cpp",
        source_file=f"{module_name}.py",
        functions=[
            FunctionDef(name="f", parameters=[Parameter(name="x", type="float")], returns="float"),
            FunctionDef(name="last_error", parameters=[], returns="int"),
        ],
    )
    document = {
        "entries": {
            "f": {
                "kind": "function", "class": None, "name": "f",
                "constructor_arguments": [], "arguments": [1.0], "result": f_result,
            },
            "last_error": {
                "kind": "function", "class": None, "name": "last_error",
                "constructor_arguments": [], "arguments": [], "result": error_result,
            },
        }
    }
    return module, document


def test_clear_on_read_error_flag_would_fail_idempotent_without_the_exemption(tmp_path):
    """Without the error_flag exemption, `last_error` (which really does
    return different bits on successive calls, by design) would be checked
    by `idempotent` and would fail -- demonstrating the gap 2 bug directly,
    on a fixture with no compiler and no petro_api build involved."""
    fixture_dir = _write_fixture_module(tmp_path, "clear_on_read_fixture", _CLEAR_ON_READ_FIXTURE)
    package = _import_fixture(fixture_dir, "clear_on_read_fixture")

    module, document = _f_and_last_error_module_and_document("clear_on_read_fixture", 1.0, 7)
    target = si.SubprocessTarget(
        module_name="clear_on_read_fixture", sys_path=(str(fixture_dir),)
    )

    # With the declared error_flag: exempted, so no spurious idempotent
    # failure and not even checked.
    verification = VerificationConfig(
        state=StateConfig(setup=[], mutating=[], error_flag="last_error")
    )
    results = si.run_structural_invariants(
        module, document, verification, package, target, points_limit=1
    )
    idempotent = {o.function: o for o in results.by_property("idempotent")}
    assert "last_error" not in idempotent

    # Without declaring it as error_flag, `idempotent` genuinely does catch
    # the clear-on-read behaviour -- confirming the exemption above is doing
    # real work, not papering over a check that would have passed anyway.
    undeclared_verification = VerificationConfig(state=StateConfig(setup=[], mutating=[]))
    undeclared_results = si.run_structural_invariants(
        module, document, undeclared_verification, package, target, points_limit=1
    )
    undeclared_idempotent = {o.function: o for o in undeclared_results.by_property("idempotent")}
    assert undeclared_idempotent["last_error"].status == "fail"


# --- clear-on-read error_flag used as `g`: order_independent's own gap ----

_ERROR_SENSITIVE_FIXTURE = '''
"""`f`'s result depends on whether an error is still pending; `last_error`
clears it on read. If `last_error` is not excluded from being chosen as the
interposed `g` routine, order_independent spuriously reports `f` as
perturbed by its own exempted error accessor."""

_error_code = 5


def f(x):
    return float(x) + (1.0 if _error_code else 0.0)


def last_error():
    global _error_code
    value = _error_code
    _error_code = 0
    return value
'''


def test_clear_on_read_error_flag_excluded_as_g_in_order_independent(tmp_path):
    fixture_dir = _write_fixture_module(tmp_path, "error_sensitive_fixture", _ERROR_SENSITIVE_FIXTURE)
    package = _import_fixture(fixture_dir, "error_sensitive_fixture")

    module, document = _f_and_last_error_module_and_document("error_sensitive_fixture", 2.0, 5)
    target = si.SubprocessTarget(
        module_name="error_sensitive_fixture", sys_path=(str(fixture_dir),)
    )

    # Declared as error_flag: `last_error` must never be chosen as `g`, so
    # `f`'s order_independent passes (its own worker process starts with
    # _error_code == 5 fresh each time, unaffected by anything but
    # last_error, which is never interposed).
    verification = VerificationConfig(
        state=StateConfig(setup=[], mutating=[], error_flag="last_error")
    )
    results = si.run_structural_invariants(
        module, document, verification, package, target, points_limit=1
    )
    order_independent = {o.function: o for o in results.by_property("order_independent")}
    assert order_independent["f"].status == "pass"

    # Undeclared: `last_error` IS chosen as `g`, and genuinely does perturb
    # `f` (clearing `_error_code` changes `f`'s second-call result) --
    # confirming the exemption above prevents a real, reproducible spurious
    # failure, not a scenario that would have passed anyway.
    undeclared_verification = VerificationConfig(state=StateConfig(setup=[], mutating=[]))
    undeclared_results = si.run_structural_invariants(
        module, document, undeclared_verification, package, target, points_limit=1
    )
    undeclared_order_independent = {
        o.function: o for o in undeclared_results.by_property("order_independent")
    }
    outcome = undeclared_order_independent["f"]
    assert outcome.status == "fail"
    assert "last_error" in outcome.failures[0].detail


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
def test_last_error_is_exempted_from_idempotent_and_order_independent(petro_extension):
    """`last_error` (`state.error_flag`, `PVTERR` in
    libraries/petro/fortran/pvtcor.f) reads AND CLEARS the stored error
    code: calling it twice legitimately returns different bits, the first
    call the code, the second 0. `idempotent`/`order_independent` must
    exempt it the same way `pvt_set_fluid` (`state.mutating`) is exempted,
    or this structural check fails on a routine working exactly as
    designed, not on a bug (see StateConfig's docstring / this task's gap
    2)."""
    package, target = petro_extension
    module, document = _load_petro_module_and_document()
    config = ServiceConfig.load(PETRO_SERVICE_DIR)
    config.verification.validate_against_ir(module)
    assert config.verification.state.error_flag == "last_error"

    results = si.run_structural_invariants(
        module, document, config.verification, package, target, points_limit=1
    )

    # `idempotent` fully excludes the error_flag accessor (calling it twice
    # in a row is not idempotent by construction), the same way `mutating`
    # routines are excluded.
    idempotent_functions = {o.function for o in results.by_property("idempotent")}
    assert "last_error" not in idempotent_functions, (
        "idempotent must exempt the declared error_flag accessor last_error "
        "(clear-on-read is not idempotent by construction), but it was checked"
    )

    # `order_independent` still checks `last_error` as `f` (nothing suspect
    # about that on its own) -- it must pass, i.e. it must not have been
    # spuriously perturbed by being interposed as someone else's `g` either
    # (see the dedicated fixture test for the direct demonstration of that).
    order_independent = {o.function: o for o in results.by_property("order_independent")}
    assert order_independent["last_error"].status == "pass"


@requires_gfortran
def test_last_error_regression_against_a_triggered_error(petro_extension):
    """Regression for gap 2: before the exemption, driving pvt_set_fluid's
    api_gravity out of range (the same sabotage
    test_no_error_flag_catches_an_out_of_range_setup uses) makes
    `last_error()` return a nonzero code on the first call of any
    idempotent-style pair and 0 on the second -- a real, reproducible
    "different bits on the second call" that has nothing to do with a bug.
    With the exemption in place, `last_error` must not even be checked by
    `idempotent`/`order_independent`, so this never surfaces as a spurious
    failure."""
    package, target = petro_extension
    module, document = _load_petro_module_and_document()
    config = ServiceConfig.load(PETRO_SERVICE_DIR)
    config.verification.validate_against_ir(module)

    sabotaged_document = copy.deepcopy(document)
    sabotaged_document["entries"]["pvt_set_fluid"]["arguments"] = [0.5, 0.65, 180.0, 2]

    results = si.run_structural_invariants(
        module, sabotaged_document, config.verification, package, target, points_limit=1
    )

    idempotent_functions = {o.function for o in results.by_property("idempotent")}
    assert "last_error" not in idempotent_functions


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
