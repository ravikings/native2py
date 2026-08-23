"""Derived-type arguments, flattened through a generated Fortran shim.

f2py cannot pass a derived type — that has not changed. What changed is that
native2py now generates a Fortran SHIM inside the module's `_expanded` copy:
it takes the components as ordinary scalars, builds the type, calls the real
routine, and copies output components back. The original tree stays
read-only, same rule as every transform under `_expanded/`.

Verified beyond these tests by building: the generated service compiled
through f2py and `density_n2p(2000, 640, 3)` returned 2000/(10.732*640)*3
exactly, both directly and through the generated endpoint.
"""

import textwrap

import pytest
from click.testing import CliRunner

from native2py.cli import main
from native2py.config import ExposeConfig
from native2py.parsers import fortran as fortran_parser

from conftest import requires_fparser

# Every test here needs fparser2: the derived-type reader lives only on the
# tree-parser path, so on the regex fallback there is nothing to assert.
pytestmark = requires_fparser

FLUID_F90 = textwrap.dedent(
    """\
    module fluid_mod
        implicit none
        integer, parameter :: dp = kind(1.0d0)

        type :: fluid_state
            real(dp) :: pressure
            real(dp) :: temperature
            integer  :: ncomp
        end type fluid_state

    contains

        subroutine density(state, rho)
            type(fluid_state), intent(in) :: state
            real(dp), intent(out) :: rho
            rho = state%pressure / (10.732d0 * state%temperature) * state%ncomp
        end subroutine density

    end module fluid_mod
    """
)


def _parse(tmp_path, source=FLUID_F90, functions=("density",)):
    path = tmp_path / "fluid.f90"
    path.write_text(source)
    return fortran_parser.parse_source(
        path, ExposeConfig(functions=list(functions))
    )


def test_the_derived_argument_flattens_into_its_components(tmp_path):
    module = _parse(tmp_path)
    fn = module.functions[0]

    # Published under its own name, imported from the shim.
    assert fn.name == "density"
    assert fn.cpp_name == "density_n2p"
    assert [(p.name, p.type, p.intent) for p in fn.parameters] == [
        ("state_pressure", "float", "in"),
        ("state_temperature", "float", "in"),
        ("state_ncomp", "int", "in"),
        ("rho", "float", "out"),
    ]


def test_the_shim_builds_the_type_and_calls_the_real_routine(tmp_path):
    module = _parse(tmp_path)

    (shim,) = module.fortran_shims
    assert "subroutine density_n2p(state_pressure, state_temperature" in shim
    assert "type(fluid_state) :: state" in shim
    assert "state%pressure = state_pressure" in shim
    assert "call density(state, rho)" in shim
    # Component spellings are RESOLVED (real(8), not real(dp)) so the shim
    # parses under f2py whether or not the kind-rewrite pass runs on the file.
    assert "real(8), intent(in) :: state_pressure" in shim
    assert "real(dp)" not in shim


def test_an_output_derived_argument_copies_components_back(tmp_path):
    source = FLUID_F90.replace(
        "type(fluid_state), intent(in) :: state",
        "type(fluid_state), intent(inout) :: state",
    )
    assert source != FLUID_F90  # a silent no-op replace tests nothing
    module = _parse(tmp_path, source)

    (shim,) = module.fortran_shims
    # inout: components go in before the call AND come back after it —
    # without the copy-back, the callee's writes die inside the shim.
    assert "state%pressure = state_pressure" in shim
    assert "state_pressure = state%pressure" in shim
    outputs = [p for p in module.functions[0].parameters if p.intent == "inout"]
    assert [p.name for p in outputs][:3] == [
        "state_pressure",
        "state_temperature",
        "state_ncomp",
    ]


def test_a_bare_routine_with_a_derived_argument_is_refused_with_the_reason(tmp_path):
    # The shim constructs the type, so it must live where the type is visible
    # — inside the defining module. A bare subprogram has no such place.
    source = textwrap.dedent(
        """\
        subroutine apply(st, factor)
            type(pvt_state), intent(inout) :: st
            real(8), intent(in) :: factor
            st%rs = st%rs * factor
        end subroutine apply
        """
    )
    module = _parse(tmp_path, source, functions=("apply",))

    assert module.functions == []
    assert "not inside a module" in module.skipped[0].reason


def test_an_array_component_is_refused_with_the_component_named(tmp_path):
    source = FLUID_F90.replace(
        "integer  :: ncomp",
        "integer  :: ncomp\n        real(dp) :: table(10)",
    )
    assert source != FLUID_F90  # a silent no-op replace tests nothing
    module = _parse(tmp_path, source)

    assert module.functions == []
    reason = module.skipped[0].reason
    assert "table" in reason and "array" in reason


def test_generate_appends_the_shim_inside_the_module(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    service = tmp_path / "services" / "fluid"
    (service / "native").mkdir(parents=True)
    (service / "native" / "fluid.f90").write_text(FLUID_F90)
    (service / "native2py.yaml").write_text(
        "name: fluid\nlanguage: fortran\nexpose:\n  functions:\n    - density\n"
    )

    result = CliRunner().invoke(main, ["generate", "fluid"])
    assert result.exit_code == 0, result.output

    copy = (service / "native" / "_expanded" / "fluid.f90").read_text()
    # Inside the module (before `end module`), because the shim constructs
    # fluid_state and the type is only visible from in there.
    assert copy.index("subroutine density_n2p") < copy.index("end module")
    # f2py wraps the shim, not the original it cannot express.
    cmake = (service / "CMakeLists.txt").read_text()
    assert "only: density_n2p :" in cmake
    # And the package publishes the original name.
    init = (service / "python" / "fluid" / "__init__.py").read_text()
    assert "density = _native.fluid_mod.density_n2p" in init
    assert '__all__ = ["density"]' in init
