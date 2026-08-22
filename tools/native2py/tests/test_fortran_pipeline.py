import pytest

from native2py.config import ExposeConfig
from native2py.generators import f2py_gen, python_pkg_gen, test_gen
from native2py.parsers import fortran as fortran_parser

def test_extracts_function_result(reservoir_source):
    module = fortran_parser.parse_source(reservoir_source, ExposeConfig(functions=["calculate_pressure"]))

    assert len(module.functions) == 1
    fn = module.functions[0]
    assert fn.name == "calculate_pressure"
    assert not fn.is_subroutine
    assert fn.returns == "float"
    assert [p.name for p in fn.parameters] == ["density", "temperature"]


def test_detects_enclosing_fortran_module(reservoir_source):
    # pressure.f90 wraps its routines in `module physics ... end module physics`.
    # f2py nests compiled routines under physics.physics.<name> because of this,
    # so the generator needs to know the wrapper name to re-export correctly
    # (verified against a real gfortran/f2py build — see conversation history).
    module = fortran_parser.parse_source(reservoir_source, ExposeConfig(functions=["calculate_pressure"]))

    assert module.fortran_module == "physics"


def test_init_py_rebinds_through_fortran_module_nesting(reservoir_source):
    module = fortran_parser.parse_source(reservoir_source, ExposeConfig(functions=["calculate_pressure", "normalize"]))
    init_py = python_pkg_gen.generate_init_py(module)

    # module.name is "pressure" here (the source file's stem) since this test
    # parses directly without the CLI's config.name override; the extension
    # symbol follows module.name while the Fortran wrapper name is "physics".
    assert "from ._native import pressure as _native" in init_py
    assert "calculate_pressure = _native.physics.calculate_pressure" in init_py
    assert "normalize = _native.physics.normalize" in init_py
    # Must NOT try to import routines directly off the extension module —
    # f2py doesn't expose them there when they're module-wrapped.
    assert "from ._native.pressure import" not in init_py


def test_module_less_routine_has_no_fortran_module(tmp_path):
    source = tmp_path / "freefunc.f90"
    source.write_text(
        """
    function bare_calc(x) result(y)
        real(8), intent(in) :: x
        real(8) :: y

        y = x * 2.0d0
    end function bare_calc
    """
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["bare_calc"]))

    assert module.fortran_module is None
    init_py = python_pkg_gen.generate_init_py(module)
    assert "from ._native.freefunc import bare_calc" in init_py


def test_extracts_subroutine_with_array_param(reservoir_source):
    module = fortran_parser.parse_source(reservoir_source, ExposeConfig(functions=["normalize"]))

    fn = module.functions[0]
    assert fn.is_subroutine
    values_param = next(p for p in fn.parameters if p.name == "values")
    n_param = next(p for p in fn.parameters if p.name == "n")
    assert values_param.is_array
    assert not n_param.is_array
    assert n_param.type == "int"


def test_requires_explicit_expose_list(reservoir_source):
    with pytest.raises(ValueError, match="expose.functions"):
        fortran_parser.parse_source(reservoir_source, ExposeConfig())


def test_missing_routine_raises(reservoir_source):
    with pytest.raises(ValueError, match="Could not find"):
        fortran_parser.parse_source(reservoir_source, ExposeConfig(functions=["does_not_exist"]))


def test_extracts_one_routine_from_a_huge_file(tmp_path):
    """Simulates a large legacy oil-industry template: hundreds of unrelated
    routines plus one target. Only the target should be parsed/extracted —
    the file being huge and full of routines native2py doesn't understand
    must not matter."""
    noise = "\n\n".join(
        f"""
    subroutine legacy_routine_{i}(x)
        real(8), intent(inout) :: x
        x = x * {i}.0d0
    end subroutine legacy_routine_{i}
    """
        for i in range(500)
    )

    huge_source = f"""
module giant_template
contains
{noise}

    function target_calc(a, b) result(c)
        real(8), intent(in) :: a
        real(8), intent(in) :: b
        real(8) :: c

        c = a + b
    end function target_calc

{noise}
end module giant_template
"""
    huge_file = tmp_path / "giant_template.f90"
    huge_file.write_text(huge_source)

    module = fortran_parser.parse_source(huge_file, ExposeConfig(functions=["target_calc"]))

    assert len(module.functions) == 1
    assert module.functions[0].name == "target_calc"


def test_generates_f2py_cmake(reservoir_source):
    module = fortran_parser.parse_source(reservoir_source, ExposeConfig(functions=["calculate_pressure"]))
    cmake = f2py_gen.generate_cmake(module, "reservoir", ["native/pressure.f90"])

    assert "-m pressure" in cmake  # f2py module name follows module.name, not the service name
    assert "numpy.f2py -c" in cmake
    assert "${CMAKE_CURRENT_SOURCE_DIR}/native/pressure.f90" in cmake  # must be absolute, not relative


def test_generates_router_without_shadowing_import(reservoir_source):
    module = fortran_parser.parse_source(reservoir_source, ExposeConfig(functions=["calculate_pressure"]))
    router_py = python_pkg_gen.generate_router_py(module, "reservoir")

    assert "from . import calculate_pressure" in router_py
    assert "def calculate_pressure_endpoint(" in router_py
    assert "def calculate_pressure(" not in router_py


def test_generates_array_aware_smoke_test(reservoir_source):
    module = fortran_parser.parse_source(reservoir_source, ExposeConfig(functions=["normalize"]))
    test_py = test_gen.generate_python_api_test(module, "reservoir")

    assert "import numpy as np" in test_py
    assert "np.array(" in test_py
