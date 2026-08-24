"""Validation of `state:`/`invariants:`/`ranges:` in native2py.yaml (T8).

Spec: design-verification-layers.md sections 3.2 (declared vocabulary,
no_error_flag), 3.3 (closed vocabulary -- no eval, ever), 3.4 (ranges, no
default range), 3.5 (setup/mutating/error_flag).
"""

from pathlib import Path

import pytest
import yaml

from native2py.config import (
    BoundsProperty,
    ConfigError,
    MonotoneProperty,
    RangeDeclaration,
    ScalesLinearlyInProperty,
    ServiceConfig,
    StateConfig,
    SumToOneProperty,
    SymmetricInProperty,
    VerificationConfig,
    _load_verification,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
KNOWN = {"pvt_set_fluid", "solution_gor", "oil_fvf", "last_error", "saturations"}


def _write_service(tmp_path: Path, extra_yaml: str) -> Path:
    service_dir = tmp_path / "svc"
    service_dir.mkdir()
    native_dir = service_dir / "native"
    native_dir.mkdir()
    (native_dir / "svc.f90").write_text(
        "subroutine pvt_set_fluid(api_gravity)\n"
        "  real(8), intent(in) :: api_gravity\n"
        "end subroutine\n"
        "function solution_gor(pressure) result(rs)\n"
        "  real(8), intent(in) :: pressure\n"
        "  real(8) :: rs\n"
        "end function\n"
        "function oil_fvf(pressure) result(bo)\n"
        "  real(8), intent(in) :: pressure\n"
        "  real(8) :: bo\n"
        "end function\n"
        "function last_error() result(icode)\n"
        "  integer :: icode\n"
        "end function\n"
    )
    (service_dir / "native2py.yaml").write_text(
        "name: svc\n"
        "language: fortran\n"
        "expose:\n"
        "  classes: []\n"
        "  functions: [pvt_set_fluid, solution_gor, oil_fvf, last_error]\n"
        + extra_yaml
    )
    return service_dir


# --- example block from the spec / task, full round trip -----------------

EXAMPLE_YAML = """
state:
  setup: [pvt_set_fluid]
  mutating: [pvt_set_fluid]
  error_flag: last_error
invariants:
  solution_gor:
    - bounds: {min: 0.0}
    - monotone: {in: pressure, direction: nondecreasing}
ranges:
  pressure: [14.7, 10000.0]
"""


def test_example_block_parses_into_typed_objects(tmp_path):
    service_dir = _write_service(tmp_path, EXAMPLE_YAML)
    cfg = ServiceConfig.load(service_dir)
    v = cfg.verification

    assert v.state == StateConfig(
        setup=["pvt_set_fluid"], mutating=["pvt_set_fluid"], error_flag="last_error"
    )
    assert v.invariants["solution_gor"] == [
        BoundsProperty(min=0.0, max=None),
        MonotoneProperty(parameter="pressure", direction="nondecreasing"),
    ]
    assert v.ranges["pressure"] == RangeDeclaration(lo=14.7, hi=10000.0)
    assert v.has_range("pressure")
    assert not v.has_range("temperature")


def test_example_block_round_trips_through_save(tmp_path):
    service_dir = _write_service(tmp_path, EXAMPLE_YAML)
    cfg = ServiceConfig.load(service_dir)
    cfg.save(service_dir)

    reloaded = ServiceConfig.load(service_dir)
    assert reloaded.verification == cfg.verification


# --- closed vocabulary: no eval, ever -------------------------------------


def test_unknown_vocabulary_word_is_rejected(tmp_path):
    service_dir = _write_service(
        tmp_path,
        "invariants:\n  solution_gor:\n    - expr: \"x > 0\"\n",
    )
    with pytest.raises(ConfigError, match="closed"):
        ServiceConfig.load(service_dir)


def test_eval_style_expression_key_is_rejected(tmp_path):
    # A plausible attempt at sneaking in an escape hatch under a vocabulary-
    # adjacent name. It must be rejected exactly like any other unknown word.
    service_dir = _write_service(
        tmp_path,
        "invariants:\n  solution_gor:\n    - eval: \"result >= 0\"\n",
    )
    with pytest.raises(ConfigError, match="closed"):
        ServiceConfig.load(service_dir)


@pytest.mark.parametrize(
    "word", ["bounds", "monotone", "sum_to_one", "symmetric_in", "scales_linearly_in"]
)
def test_every_vocabulary_word_is_accepted_in_isolation(tmp_path, word):
    payloads = {
        "bounds": "{min: 0.0}",
        "monotone": "{in: pressure, direction: nondecreasing}",
        "sum_to_one": "[pressure, pressure]",  # names need not be real params here
        "symmetric_in": "[pressure, pressure]",
        "scales_linearly_in": "{in: pressure}",
    }
    extra = f"invariants:\n  solution_gor:\n    - {word}: {payloads[word]}\n"
    if word == "sum_to_one":
        # sum_to_one requires unique fields; use two distinct names instead.
        extra = "invariants:\n  solution_gor:\n    - sum_to_one: [pressure, api_gravity]\n"
    service_dir = _write_service(tmp_path, extra)
    cfg = ServiceConfig.load(service_dir)
    assert cfg.verification.invariants["solution_gor"]


def test_two_vocabulary_words_in_one_entry_is_rejected(tmp_path):
    service_dir = _write_service(
        tmp_path,
        "invariants:\n  solution_gor:\n    - bounds: {min: 0.0}\n      monotone: {in: pressure, direction: nondecreasing}\n",
    )
    with pytest.raises(ConfigError, match="exactly one property"):
        ServiceConfig.load(service_dir)


# --- reference validation --------------------------------------------------


def test_state_setup_must_name_an_exposed_function(tmp_path):
    service_dir = _write_service(tmp_path, "state:\n  setup: [not_a_real_function]\n")
    with pytest.raises(ConfigError, match="not_a_real_function"):
        ServiceConfig.load(service_dir)


def test_state_mutating_must_name_an_exposed_function(tmp_path):
    service_dir = _write_service(tmp_path, "state:\n  mutating: [nope]\n")
    with pytest.raises(ConfigError, match="nope"):
        ServiceConfig.load(service_dir)


def test_state_error_flag_must_name_an_exposed_function(tmp_path):
    service_dir = _write_service(tmp_path, "state:\n  error_flag: nonexistent\n")
    with pytest.raises(ConfigError, match="nonexistent"):
        ServiceConfig.load(service_dir)


def test_invariants_key_must_name_an_exposed_function(tmp_path):
    service_dir = _write_service(
        tmp_path, "invariants:\n  not_a_function:\n    - bounds: {min: 0.0}\n"
    )
    with pytest.raises(ConfigError, match="not_a_function"):
        ServiceConfig.load(service_dir)


def test_monotone_in_must_name_a_real_parameter_when_ir_is_checked(tmp_path):
    service_dir = _write_service(
        tmp_path,
        "invariants:\n  solution_gor:\n    - monotone: {in: not_a_param, direction: nondecreasing}\n",
    )
    cfg = ServiceConfig.load(service_dir)  # parse-time: no IR, so this passes
    assert cfg.verification.invariants["solution_gor"][0].parameter == "not_a_param"

    from native2py.ir import FunctionDef, ModuleIR, Parameter

    module = ModuleIR(
        name="svc",
        language="fortran",
        source_file="svc.f90",
        functions=[
            FunctionDef(name="solution_gor", parameters=[Parameter(name="pressure", type="float")])
        ],
    )
    with pytest.raises(ConfigError, match="not_a_param"):
        cfg.verification.validate_against_ir(module)


def test_monotone_in_a_real_parameter_passes_ir_validation(tmp_path):
    service_dir = _write_service(tmp_path, EXAMPLE_YAML)
    cfg = ServiceConfig.load(service_dir)

    from native2py.ir import FunctionDef, ModuleIR, Parameter

    module = ModuleIR(
        name="svc",
        language="fortran",
        source_file="svc.f90",
        functions=[
            FunctionDef(name="solution_gor", parameters=[Parameter(name="pressure", type="float")])
        ],
    )
    cfg.verification.validate_against_ir(module)  # must not raise


def test_direction_must_be_a_recognised_enum_value(tmp_path):
    service_dir = _write_service(
        tmp_path,
        "invariants:\n  solution_gor:\n    - monotone: {in: pressure, direction: sideways}\n",
    )
    with pytest.raises(ConfigError, match="direction"):
        ServiceConfig.load(service_dir)


def test_bounds_needs_at_least_min_or_max(tmp_path):
    service_dir = _write_service(tmp_path, "invariants:\n  solution_gor:\n    - bounds: {}\n")
    with pytest.raises(ConfigError, match="min"):
        ServiceConfig.load(service_dir)


def test_bounds_with_only_max_is_accepted(tmp_path):
    service_dir = _write_service(tmp_path, "invariants:\n  solution_gor:\n    - bounds: {max: 1.0}\n")
    cfg = ServiceConfig.load(service_dir)
    assert cfg.verification.invariants["solution_gor"] == [BoundsProperty(min=None, max=1.0)]


def test_sum_to_one_fields_and_optional_tolerance(tmp_path):
    service_dir = _write_service(
        tmp_path,
        "invariants:\n  solution_gor:\n    - sum_to_one: [pressure, api_gravity]\n      tolerance: 1e-12\n",
    )
    cfg = ServiceConfig.load(service_dir)
    assert cfg.verification.invariants["solution_gor"] == [
        SumToOneProperty(fields=["pressure", "api_gravity"], tolerance=1e-12)
    ]


def test_sum_to_one_without_tolerance_defaults_to_zero(tmp_path):
    service_dir = _write_service(
        tmp_path, "invariants:\n  solution_gor:\n    - sum_to_one: [pressure, api_gravity]\n"
    )
    cfg = ServiceConfig.load(service_dir)
    assert cfg.verification.invariants["solution_gor"][0].tolerance == 0.0


def test_sum_to_one_rejects_duplicate_fields(tmp_path):
    service_dir = _write_service(
        tmp_path, "invariants:\n  solution_gor:\n    - sum_to_one: [pressure, pressure]\n"
    )
    with pytest.raises(ConfigError, match="more than once"):
        ServiceConfig.load(service_dir)


def test_sum_to_one_rejects_empty_list(tmp_path):
    service_dir = _write_service(tmp_path, "invariants:\n  solution_gor:\n    - sum_to_one: []\n")
    with pytest.raises(ConfigError):
        ServiceConfig.load(service_dir)


# --- ranges: no default, lo < hi, finite ------------------------------------


def test_range_lo_must_be_less_than_hi(tmp_path):
    service_dir = _write_service(tmp_path, "ranges:\n  pressure: [10000.0, 14.7]\n")
    with pytest.raises(ConfigError, match="lo < hi"):
        ServiceConfig.load(service_dir)


def test_range_equal_lo_hi_is_rejected(tmp_path):
    service_dir = _write_service(tmp_path, "ranges:\n  pressure: [14.7, 14.7]\n")
    with pytest.raises(ConfigError, match="lo < hi"):
        ServiceConfig.load(service_dir)


def test_range_must_be_finite(tmp_path):
    service_dir = _write_service(tmp_path, "ranges:\n  pressure: [.inf, 10000.0]\n")
    with pytest.raises(ConfigError, match="finite"):
        ServiceConfig.load(service_dir)


def test_range_must_have_exactly_two_values(tmp_path):
    service_dir = _write_service(tmp_path, "ranges:\n  pressure: [14.7]\n")
    with pytest.raises(ConfigError):
        ServiceConfig.load(service_dir)


def test_absent_range_is_not_a_parse_time_error(tmp_path):
    # No `ranges:` block at all -- a swept parameter with no declared range
    # is not an error at parse time (it becomes `uncovered` at run time,
    # a later task). The parser must distinguish "absent" cleanly.
    service_dir = _write_service(tmp_path, "")
    cfg = ServiceConfig.load(service_dir)
    assert cfg.verification.ranges == {}
    assert not cfg.verification.has_range("pressure")


def test_no_verification_blocks_at_all_yields_empty_config(tmp_path):
    service_dir = _write_service(tmp_path, "")
    cfg = ServiceConfig.load(service_dir)
    assert cfg.verification == VerificationConfig()
    assert cfg.verification.is_empty


# --- permissive expose (`expose: all` / empty expose) skips the fixed list --


def test_permissive_expose_all_does_not_restrict_state_references(tmp_path):
    service_dir = tmp_path / "svc2"
    service_dir.mkdir()
    (service_dir / "native").mkdir()
    (service_dir / "native" / "svc.f90").write_text("subroutine anything\nend subroutine\n")
    (service_dir / "native2py.yaml").write_text(
        "name: svc2\nlanguage: fortran\nexpose: all\n"
        "state:\n  setup: [whatever_symbol]\n"
    )
    cfg = ServiceConfig.load(service_dir)
    assert cfg.verification.state.setup == ["whatever_symbol"]


# --- direct _load_verification unit tests (no ServiceConfig scaffolding) ---


def test_load_verification_directly_on_the_spec_example():
    data = yaml.safe_load(EXAMPLE_YAML)
    v = _load_verification(data, Path("native2py.yaml"), KNOWN)
    assert v.state.mutating == ["pvt_set_fluid"]
    assert v.ranges["pressure"].lo == 14.7
    assert v.ranges["pressure"].hi == 10000.0


def test_state_block_rejects_unknown_keys():
    data = {"state": {"setp": ["pvt_set_fluid"]}}
    with pytest.raises(ConfigError, match="unrecognised key"):
        _load_verification(data, Path("native2py.yaml"), KNOWN)


def test_ranges_block_must_be_a_mapping():
    data = {"ranges": [1, 2, 3]}
    with pytest.raises(ConfigError, match="mapping"):
        _load_verification(data, Path("native2py.yaml"), KNOWN)


def test_invariants_block_must_be_a_mapping():
    data = {"invariants": ["bounds"]}
    with pytest.raises(ConfigError, match="mapping"):
        _load_verification(data, Path("native2py.yaml"), KNOWN)


def test_invariants_entry_list_must_be_non_empty():
    data = {"invariants": {"solution_gor": []}}
    with pytest.raises(ConfigError, match="non-empty"):
        _load_verification(data, Path("native2py.yaml"), KNOWN)


# --- the real petro_api service (regression: names must be real) -----------


def test_petro_api_native2py_yaml_parses_and_validates_against_real_ir():
    service_dir = REPO_ROOT / "services" / "petro_api"
    cfg = ServiceConfig.load(service_dir)
    v = cfg.verification

    assert v.state.setup == ["pvt_set_fluid"]
    assert v.state.mutating == ["pvt_set_fluid"]
    assert v.state.error_flag == "last_error"
    assert "solution_gor" in v.invariants
    assert "oil_fvf" in v.invariants
    assert v.has_range("pressure")

    from native2py.parsers import fortran

    module = fortran.parse_source(service_dir / "native" / "petro_api.f90", cfg.expose)
    v.validate_against_ir(module)  # must not raise: real function/param names


# --- `lattice:` — corners/scatter YAML surface (gap 1) ---------------------


LATTICE_YAML = """
ranges:
  pressure: [14.7, 10000.0]
lattice:
  n: 9
  scatter:
    seed: 12345
    count: 8
  corners:
    solution_gor:
      - [14.7, 60.0, 0.65]
      - [10000.0, 60.0, 0.65]
"""


def test_lattice_block_parses_into_typed_objects(tmp_path):
    service_dir = _write_service(tmp_path, LATTICE_YAML)
    cfg = ServiceConfig.load(service_dir)
    lat = cfg.verification.lattice

    assert lat.n == 9
    assert lat.scatter.seed == 12345
    assert lat.scatter.count == 8
    assert lat.corners == {
        "solution_gor": [(14.7, 60.0, 0.65), (10000.0, 60.0, 0.65)]
    }
    assert not lat.is_empty


def test_lattice_block_round_trips_through_save(tmp_path):
    service_dir = _write_service(tmp_path, LATTICE_YAML)
    cfg = ServiceConfig.load(service_dir)
    cfg.save(service_dir)

    reloaded = ServiceConfig.load(service_dir)
    assert reloaded.verification.lattice == cfg.verification.lattice
    assert reloaded.verification == cfg.verification


def test_no_lattice_block_yields_empty_lattice_config(tmp_path):
    service_dir = _write_service(tmp_path, "")
    cfg = ServiceConfig.load(service_dir)
    assert cfg.verification.lattice.is_empty
    assert cfg.verification.lattice.n is None
    assert cfg.verification.lattice.scatter.count == 0
    assert cfg.verification.lattice.corners == {}
    assert cfg.verification.is_empty


def test_lattice_n_must_be_an_integer_ge_2(tmp_path):
    service_dir = _write_service(tmp_path, "lattice:\n  n: 1\n")
    with pytest.raises(ConfigError, match=">= 2"):
        ServiceConfig.load(service_dir)


def test_lattice_n_rejects_non_integer(tmp_path):
    service_dir = _write_service(tmp_path, "lattice:\n  n: 3.5\n")
    with pytest.raises(ConfigError, match="integer"):
        ServiceConfig.load(service_dir)


def test_lattice_block_rejects_unknown_keys(tmp_path):
    service_dir = _write_service(tmp_path, "lattice:\n  bogus: 1\n")
    with pytest.raises(ConfigError, match="unrecognised key"):
        ServiceConfig.load(service_dir)


def test_scatter_block_rejects_unknown_keys(tmp_path):
    service_dir = _write_service(tmp_path, "lattice:\n  scatter:\n    bogus: 1\n")
    with pytest.raises(ConfigError, match="unrecognised key"):
        ServiceConfig.load(service_dir)


def test_scatter_count_must_be_non_negative(tmp_path):
    service_dir = _write_service(
        tmp_path, "lattice:\n  scatter:\n    seed: 1\n    count: -1\n"
    )
    with pytest.raises(ConfigError, match=">= 0"):
        ServiceConfig.load(service_dir)


def test_scatter_count_requires_a_seed(tmp_path):
    service_dir = _write_service(tmp_path, "lattice:\n  scatter:\n    count: 8\n")
    with pytest.raises(ConfigError, match="seed"):
        ServiceConfig.load(service_dir)


def test_scatter_seed_must_be_an_integer(tmp_path):
    service_dir = _write_service(
        tmp_path, "lattice:\n  scatter:\n    seed: not-a-number\n    count: 1\n"
    )
    with pytest.raises(ConfigError, match="integer"):
        ServiceConfig.load(service_dir)


def test_corners_must_reference_an_exposed_function(tmp_path):
    service_dir = _write_service(
        tmp_path, "lattice:\n  corners:\n    not_exposed:\n      - [1.0]\n"
    )
    with pytest.raises(ConfigError, match="not in"):
        ServiceConfig.load(service_dir)


def test_corners_entry_must_be_a_non_empty_list(tmp_path):
    service_dir = _write_service(
        tmp_path, "lattice:\n  corners:\n    solution_gor: []\n"
    )
    with pytest.raises(ConfigError, match="non-empty"):
        ServiceConfig.load(service_dir)


def test_corners_each_entry_must_be_a_list(tmp_path):
    service_dir = _write_service(
        tmp_path, "lattice:\n  corners:\n    solution_gor: [14.7]\n"
    )
    with pytest.raises(ConfigError, match="positional argument"):
        ServiceConfig.load(service_dir)
