"""T12 -- `invariants.json` and its aggregate gate (nativegate/invariants.py).

Spec: design-verification-layers.md section 3.7 (schema, `uncovered`),
section 2.6 (CI ordering), section 5.

Two test styles, matching test_structural_invariants.py's convention:

* A plain-Python fixture module standing in for a native extension --
  exercises `verify_invariants` end to end (structural + declared +
  document assembly) with no compiler in the loop, since T10's fresh-process
  worker only needs `sys.executable`, not gfortran.
* A real-`petro_api` test (IR + committed golden.json only, no build) for
  `uncovered`, since `tubing_bhp`'s undeclared ranges are a property of the
  real service's signature, not something worth re-deriving in a fixture.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from nativegate import invariants as inv
from nativegate import structural_invariants as si
from nativegate.config import (
    BoundsProperty,
    MonotoneProperty,
    RangeDeclaration,
    ServiceConfig,
    StateConfig,
    VerificationConfig,
)
from nativegate.ir import FunctionDef, ModuleIR, Parameter, module_from_dict

REPO = Path(__file__).resolve().parents[3]

# --- a plain-Python fixture: no compiler needed -----------------------------

_FIXTURE_SOURCE = '''
"""Stand-in extension for T12's schema/aggregate tests."""


def f(x):
    return 1.0 + x


def last_error():
    return 0
'''


def _write_fixture(tmp_path: Path) -> Path:
    fixture_dir = tmp_path / "fixture_pkg"
    fixture_dir.mkdir()
    (fixture_dir / "invariants_fixture.py").write_text(_FIXTURE_SOURCE)
    return fixture_dir


def _fixture_module() -> ModuleIR:
    return ModuleIR(
        name="invariants_fixture",
        language="cpp",
        source_file="invariants_fixture.py",
        functions=[
            FunctionDef(name="f", parameters=[Parameter(name="x", type="float")], returns="float"),
            FunctionDef(name="last_error", parameters=[], returns="int"),
        ],
    )


def _fixture_document() -> dict:
    return {
        "entries": {
            "f": {
                "kind": "function", "class": None, "name": "f",
                "constructor_arguments": [], "arguments": [1.0], "result": 2.0,
            },
            "last_error": {
                "kind": "function", "class": None, "name": "last_error",
                "constructor_arguments": [], "arguments": [], "result": 0,
            },
        }
    }


def _fixture_verification() -> VerificationConfig:
    return VerificationConfig(
        state=StateConfig(setup=[], mutating=[], error_flag="last_error"),
        invariants={
            "f": [
                BoundsProperty(min=1.0),
                MonotoneProperty(parameter="x", direction="nondecreasing"),
            ]
        },
        ranges={"x": RangeDeclaration(lo=0.0, hi=10.0)},
    )


def _import_fixture(tmp_path: Path):
    fixture_dir = _write_fixture(tmp_path)
    sys.path.insert(0, str(fixture_dir))
    try:
        package = importlib.import_module("invariants_fixture")
    finally:
        sys.path.remove(str(fixture_dir))
    target = si.SubprocessTarget(module_name="invariants_fixture", sys_path=(str(fixture_dir),))
    return package, target


# --- schema: byte-stable serialization --------------------------------------


def test_invariants_document_is_byte_stable(tmp_path):
    package, target = _import_fixture(tmp_path)
    module = _fixture_module()
    document = _fixture_document()
    verification = _fixture_verification()

    result = inv.verify_invariants(
        module, document, verification, package, target,
        n=3, points_limit=2, environment={"platform": "test-platform"},
    )

    assert result.passed
    doc = result.document

    assert doc["format"] == inv.FORMAT_VERSION
    assert doc["service"] == "invariants_fixture"
    assert doc["provenance"] == {"platform": "test-platform"}
    assert doc["lattice"] == {
        "points_per_sweep": 3,
        "ranges": {"x": [0.0, 10.0]},
        "corners": {},
        "scatter": {"count": 0, "seed": None},
    }
    assert doc["state"] == {
        "setup": [],
        "mutating": [],
        "error_flag": "last_error",
    }
    assert doc["checked"]["f"]["status"] == "pass"
    assert set(doc["checked"]["f"]["properties"]) == {
        "finite", "total", "no_error_flag", "idempotent", "order_independent",
        "bounds{min: 1.0}", "monotone{in: x, direction: nondecreasing}",
    }
    assert doc["uncovered"] == {}

    # Re-serializing must be exactly what write()/read() round-trip through --
    # indent=2, sort_keys=False, LF, trailing newline (matching golden.write()).
    path = tmp_path / "invariants.json"
    inv.write(path, doc)
    raw = path.read_bytes()
    assert raw == (json.dumps(doc, indent=2, sort_keys=False) + "\n").encode()
    assert raw.endswith(b"\n")
    assert b"\r" not in raw
    assert inv.read(path) == doc


# --- empty checked block is a hard failure, never a silent pass ------------


def test_empty_checked_is_a_hard_failure(tmp_path):
    package, target = _import_fixture(tmp_path)
    module = _fixture_module()
    # No `state.error_flag`, so an empty document does not also trip
    # `_check_no_error_flag`'s "no golden.json entry for the declared
    # accessor" KeyError -- this test wants EmptyCheckedError specifically.
    verification = VerificationConfig()

    with pytest.raises(inv.EmptyCheckedError, match="silently checks nothing"):
        inv.verify_invariants(
            module, {"entries": {}}, verification, package, target,
            n=3, points_limit=1, environment={},
        )


# --- uncovered: a real service's undeclared range (no compiler needed) -----

PETRO_IR = REPO / "services" / "petro_api" / ".nativegate" / "ir.json"
PETRO_GOLDEN = REPO / "services" / "petro_api" / "golden.json"
PETRO_SERVICE_DIR = REPO / "services" / "petro_api"


def _require_petro_fixture():
    if not PETRO_IR.exists() or not PETRO_GOLDEN.exists():
        pytest.skip("services/petro_api IR/golden.json are not present in this checkout")


def test_uncovered_reports_tubing_bhps_undeclared_ranges():
    """`tubing_bhp` has no `ranges:` declared for any of its numeric
    parameters (only `pressure` is declared, and it has none named
    `pressure`) -- design-verification-layers.md section 3.4: "a swept
    parameter with no declared range is an error, not a guess, recorded
    under `uncovered`". This needs only the IR and the committed golden.json
    -- no build, no compiler, no gfortran."""
    _require_petro_fixture()

    module = module_from_dict(json.loads(PETRO_IR.read_text()))
    document = json.loads(PETRO_GOLDEN.read_text())
    config = ServiceConfig.load(PETRO_SERVICE_DIR)
    config.verification.validate_against_ir(module)

    functions_by_name = {fn.name: fn for fn in module.functions}
    from nativegate import lattice as lattice_mod

    lattices = si.build_lattices(document, functions_by_name, config.verification, n=5)

    doc = inv.build_invariants_document(
        module, document, config.verification,
        si.StructuralInvariantResults(()), {}, {}, lattices,
        n=5, environment={},
    )

    assert "tubing_bhp" in doc["uncovered"]
    assert "no range declared for parameter(s)" in doc["uncovered"]["tubing_bhp"]
    # `pressure` IS declared, and tubing_bhp does not have a `pressure`
    # parameter -- so none of its own numeric parameters were swept.
    tubing_bhp_fn = functions_by_name["tubing_bhp"]
    for p in tubing_bhp_fn.parameters:
        if p.name != "pressure":
            assert p.name in doc["uncovered"]["tubing_bhp"]


# --- gap 1: corners/scatter/n sourced from VerificationConfig.lattice ------


def _fixture_verification_with_lattice(**lattice_kwargs) -> VerificationConfig:
    from nativegate.config import LatticeConfig, ScatterDeclaration

    return VerificationConfig(
        state=StateConfig(setup=[], mutating=[], error_flag="last_error"),
        invariants={
            "f": [
                BoundsProperty(min=1.0),
                MonotoneProperty(parameter="x", direction="nondecreasing"),
            ]
        },
        ranges={"x": RangeDeclaration(lo=0.0, hi=10.0)},
        lattice=LatticeConfig(**lattice_kwargs),
    )


def test_build_invariants_document_sources_n_scatter_corners_from_config(tmp_path):
    """`build_invariants_document`'s `n`/`scatter_seed`/`scatter_count`/
    `corners` kwargs must not require an explicit caller any more -- a
    `VerificationConfig.lattice` populated from `nativegate.yaml` (gap 1)
    is enough. No explicit kwarg is passed to `build_invariants_document`
    below beyond the mandatory positional arguments."""
    from nativegate.config import ScatterDeclaration

    package, target = _import_fixture(tmp_path)
    module = _fixture_module()
    document = _fixture_document()
    verification = _fixture_verification_with_lattice(
        n=7,
        scatter=ScatterDeclaration(seed=99, count=2),
        corners={"f": [(3.0,), (4.0,)]},
    )

    functions_by_name = {fn.name: fn for fn in module.functions}
    lattices = si.build_lattices(document, functions_by_name, verification, n=7)

    doc = inv.build_invariants_document(
        module, document, verification,
        si.StructuralInvariantResults(()), {}, {}, lattices,
        environment={},
    )

    assert doc["lattice"]["points_per_sweep"] == 7
    assert doc["lattice"]["scatter"] == {"count": 2, "seed": 99}
    assert doc["lattice"]["corners"] == {"f": [[3.0], [4.0]]}


def test_build_invariants_document_explicit_kwargs_override_config(tmp_path):
    """An explicit kwarg still wins over the declared config -- callers
    (tests, or any other direct caller) are not forced onto the YAML
    surface.

    Note (design ambiguity, flagged rather than silently decided): `None`
    is used as the "not overridden, read from config" sentinel for
    `scatter_seed`/`corners`, which is indistinguishable from a caller
    explicitly wanting "no seed"/"no corners" -- there is no `object()`
    sentinel here, matching this module's existing style for `n`/
    `points_limit`/etc. `n` and `scatter_count` (whose domains exclude
    `None`) are unambiguous and are asserted here; `corners={}` (falsy, not
    `None`) also overrides cleanly since the check is `is None`, not
    truthiness.
    """
    package, target = _import_fixture(tmp_path)
    module = _fixture_module()
    document = _fixture_document()
    from nativegate.config import ScatterDeclaration

    verification = _fixture_verification_with_lattice(
        n=7, scatter=ScatterDeclaration(seed=99, count=2), corners={"f": [(3.0,)]}
    )
    functions_by_name = {fn.name: fn for fn in module.functions}
    lattices = si.build_lattices(document, functions_by_name, verification, n=3)

    doc = inv.build_invariants_document(
        module, document, verification,
        si.StructuralInvariantResults(()), {}, {}, lattices,
        n=3, scatter_count=0, corners={},
        environment={},
    )

    assert doc["lattice"]["points_per_sweep"] == 3
    assert doc["lattice"]["scatter"]["count"] == 0
    assert doc["lattice"]["corners"] == {}


def test_verify_invariants_lattice_config_reaches_actual_checks(tmp_path):
    """The declared `lattice:` block does not just change what gets
    *reported* -- it changes which points `si.build_lattices` (and
    therefore every structural/declared check) actually evaluates. `n=7`
    declared via config, with no `n=` kwarg passed to `verify_invariants`,
    must produce the same `points_per_sweep` as passing `n=7` explicitly."""
    package, target = _import_fixture(tmp_path)
    module = _fixture_module()
    document = _fixture_document()
    verification = _fixture_verification_with_lattice(n=7)

    result = inv.verify_invariants(
        module, document, verification, package, target,
        points_limit=2, environment={},
    )
    assert result.document["lattice"]["points_per_sweep"] == 7
