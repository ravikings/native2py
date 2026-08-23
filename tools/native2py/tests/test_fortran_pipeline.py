import pytest

from conftest import requires_fparser

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


def test_f2py_backend_is_pinned_to_meson(reservoir_source):
    # Left implicit, f2py picks its backend from the Python version — meson on
    # 3.12+, distutils below — so a generated service would build through two
    # different toolchains depending on the interpreter. Worse, the distutils
    # path is broken against modern setuptools: `numpy.distutils` calls a
    # `Compiler.__init__` signature that no longer exists, and the build dies
    # with "takes from 1 to 3 positional arguments but 4 were given".
    #
    # Reproduced on 3.11 + numpy 2.4 + setuptools 84 (default backend exits 1,
    # meson exits 0 and the extension computes), and found in CI as a macOS
    # 3.10/3.11 failure while 3.12 passed.
    module = fortran_parser.parse_source(
        reservoir_source, ExposeConfig(functions=["calculate_pressure"])
    )
    cmake = f2py_gen.generate_cmake(module, "reservoir", ["native/pressure.f90"])

    assert "--backend meson" in cmake
    # Before -m, because f2py treats everything after the module name as source.
    assert cmake.index("--backend meson") < cmake.index("-m pressure")


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


# --- free-form parity with the fixed-form parser -------------------------
#
# Each source below is written for one defect and owns its own fixture text.

MODERN_F90 = """module petro_api
    implicit none

    type :: pvt_state
        real(8) :: pressure
        real(8) :: rs
    end type pvt_state

contains

    function state_at(p) result(st)
        real(8), intent(in) :: p
        type(pvt_state) :: st

        st%pressure = p
        st%rs = 0.0d0
    end function state_at

    subroutine configure(api, salinity)
        real(8), intent(in) :: api
        real(8), intent(in), optional :: salinity

        continue
    end subroutine configure

    subroutine reset
        continue
    end subroutine reset

end module petro_api
"""


@pytest.fixture
def modern_source(tmp_path):
    source = tmp_path / "modern.f90"
    source.write_text(MODERN_F90)
    return source


def test_derived_type_result_is_skipped_not_bound_as_float(modern_source):
    # A1. f2py cannot return a `type(...)` at all. The old code's declaration
    # lookup simply missed and fell through to "float", so the generated
    # router emitted {"result": state_at(p)} on a value that is not a float —
    # a service that builds, imports, and returns a wrong answer.
    module = fortran_parser.parse_source(modern_source, ExposeConfig(functions=["state_at"]))

    assert [fn.name for fn in module.functions] == []
    assert [s.name for s in module.skipped] == ["state_at"]
    assert "pvt_state" in module.skipped[0].reason


@requires_fparser
def test_derived_type_argument_is_skipped_not_bound_as_float(tmp_path):
    # A1, parameter half: the same defaulting sat on the parameter path.
    source = tmp_path / "derived_arg.f90"
    source.write_text(
        """
    subroutine apply(st, factor)
        type(pvt_state), intent(inout) :: st
        real(8), intent(in) :: factor

        st%rs = st%rs * factor
    end subroutine apply
"""
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["apply"]))

    assert module.functions == []
    assert [s.name for s in module.skipped] == ["apply"]
    # The reason moved from a blanket "f2py cannot pass a derived type" to the
    # flattening shim's precondition: this routine is a bare subprogram, and
    # the shim must live inside the module that defines the type to see it.
    assert "not inside a module" in module.skipped[0].reason
    assert "flattening shim" in module.skipped[0].reason


def test_optional_attribute_is_carried_into_the_ir(modern_source):
    # A2. `optional` was never read off the attribute list, so the generated
    # endpoint demanded a value the Fortran does not require.
    module = fortran_parser.parse_source(modern_source, ExposeConfig(functions=["configure"]))

    params = {p.name: p for p in module.functions[0].parameters}
    assert params["salinity"].is_optional
    assert not params["api"].is_optional
    # The attribute must not disturb the intent parsed off the same line.
    assert params["salinity"].intent == "in"


def test_free_form_applies_implicit_typing_to_undeclared_arguments(tmp_path):
    # A7. No `implicit none` here, so `n` is INTEGER by the I-N rule and `x`
    # is REAL. Both used to default to float.
    source = tmp_path / "transitional.f90"
    source.write_text(
        """
    subroutine accumulate(n, x)
        x = x * n
    end subroutine accumulate
"""
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["accumulate"]))

    assert [(p.name, p.type) for p in module.functions[0].parameters] == [
        ("n", "int"),
        ("x", "float"),
    ]


def test_free_form_honours_an_implicit_statement(tmp_path):
    # A7. An explicit IMPLICIT statement overrides the default letter rules.
    source = tmp_path / "explicit_implicit.f90"
    source.write_text(
        """
    subroutine accumulate(x)
        implicit integer (x-z)
        x = x + 1
    end subroutine accumulate
"""
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["accumulate"]))

    assert module.functions[0].parameters[0].type == "int"


def test_undeclared_argument_under_implicit_none_is_skipped(tmp_path):
    # A7. Under IMPLICIT NONE there is no type to fall back to, so the routine
    # is reported rather than guessed at.
    source = tmp_path / "strict.f90"
    source.write_text(
        """
    subroutine accumulate(x)
        implicit none
        x = x + 1
    end subroutine accumulate
"""
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["accumulate"]))

    assert module.functions == []
    assert [s.name for s in module.skipped] == ["accumulate"]
    assert "IMPLICIT NONE" in module.skipped[0].reason


def test_argument_less_routine_is_discovered_and_extracted(modern_source):
    # C1. Discovery and extraction failed together — `reset` was missing from
    # list_routine_names AND unfindable by name — so they are fixed together.
    assert "reset" in fortran_parser.list_routine_names(modern_source)

    module = fortran_parser.parse_source(modern_source, ExposeConfig(functions=["reset"]))

    fn = module.functions[0]
    assert fn.name == "reset"
    assert fn.is_subroutine
    assert fn.parameters == []


def test_argument_less_routine_does_not_swallow_a_longer_name(tmp_path):
    # C1 guard: making "(" optional must not let `reset` match `reset_all`.
    source = tmp_path / "prefixes.f90"
    source.write_text(
        """
    subroutine reset_all(x)
        real(8), intent(inout) :: x
        x = 0.0d0
    end subroutine reset_all

    subroutine reset
        continue
    end subroutine reset
"""
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["reset"]))

    assert module.functions[0].parameters == []


def test_bare_end_terminates_a_routine(tmp_path):
    # C2. The trailing keyword and name are both optional in the standard.
    source = tmp_path / "bare_end.f90"
    source.write_text(
        """
    function scale_rate(q) result(r)
        real(8), intent(in) :: q
        real(8) :: r

        r = q * 2.0d0
    end
"""
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["scale_rate"]))

    assert module.functions[0].returns == "float"


def test_bare_end_terminates_at_the_routine_not_a_nested_construct(tmp_path):
    """C2's trap: `end if` / `end do` must not be taken for the routine's own
    bare `end`, and the bare `end` must actually stop the block.

    The original version of this test put a declaration *after* the nested
    constructs and looked for it. That fixture is not legal Fortran — gfortran
    rejects it with "data declaration statement cannot appear after executable
    statements" — and it only parsed because the regex reader has no grammar.
    An fparser2 parse refused it, correctly.

    The property is still testable, just from the other side: a second routine
    follows, and it declares `n` with a different type. If `clamp`'s block ran
    past its bare `end`, it would swallow that declaration and `n` would come
    back as `str` instead of `int`.
    """
    source = tmp_path / "nested.f90"
    source.write_text(
        """
    subroutine clamp(n, x)
        integer :: n
        real(8), intent(inout) :: x
        integer :: i

        do i = 1, n
            if (x > 0.0d0) then
                x = x - 1.0d0
            end if
        end do
    end

    subroutine other(n)
        character(len=8) :: n
    end subroutine other
"""
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["clamp"]))

    params = {p.name: p for p in module.functions[0].parameters}
    assert params["x"].intent == "inout"
    # `str` here would mean the block swallowed `other`'s declaration.
    assert params["n"].type == "int"


def test_bare_end_function_terminates_a_routine(tmp_path):
    source = tmp_path / "bare_end_function.f90"
    source.write_text(
        """
    function scale_rate(q) result(r)
        real(8), intent(in) :: q
        real(8) :: r

        r = q * 2.0d0
    end function
"""
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["scale_rate"]))

    assert module.functions[0].name == "scale_rate"


def test_bare_end_module_still_attributes_the_enclosing_module(tmp_path):
    # C2. A bare `end module` used to leave fortran_module unset, which sends
    # generate_init_py looking for the symbol at the extension's top level
    # where f2py has not put it.
    source = tmp_path / "bare_module.f90"
    source.write_text(
        """
module physics
contains

    function double_it(x) result(y)
        real(8), intent(in) :: x
        real(8) :: y

        y = x * 2.0d0
    end function double_it

end module
"""
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["double_it"]))

    assert module.fortran_module == "physics"


# --- free-form line continuations ----------------------------------------
#
# Regression cover for the continuation fix. A wrapped argument list used to
# yield a parameter literally named "&\n roughness", which reached the
# generated router as `diameter: float, &` — a SyntaxError, so the service
# could not even be imported.

CONTINUED_F90 = """module flow
    implicit none
contains

    function tubing_bhp(wellhead_p, q_oil, q_water, q_gas, diameter, &
                        roughness, tvd, md, nseg) result(pbh)
        real(8), intent(in) :: wellhead_p, q_oil, q_water, q_gas
        real(8), intent(in) :: diameter, roughness, tvd, md
        integer, intent(in) :: nseg
        real(8) :: pbh

        pbh = wellhead_p + tvd
    end function tubing_bhp

end module flow
"""


@pytest.fixture
def continued_source(tmp_path):
    source = tmp_path / "flow.f90"
    source.write_text(CONTINUED_F90)
    return source


def test_wrapped_argument_list_yields_clean_parameter_names(continued_source):
    module = fortran_parser.parse_source(continued_source, ExposeConfig(functions=["tubing_bhp"]))

    fn = module.functions[0]
    assert [p.name for p in fn.parameters] == [
        "wellhead_p",
        "q_oil",
        "q_water",
        "q_gas",
        "diameter",
        "roughness",
        "tvd",
        "md",
        "nseg",
    ]
    # The declarations on the continued half must be found too, or the
    # wrapped arguments come back with a guessed type.
    assert {p.name: p.type for p in fn.parameters}["nseg"] == "int"
    assert {p.name: p.type for p in fn.parameters}["roughness"] == "float"


def test_generated_router_from_a_wrapped_signature_is_valid_python(continued_source):
    module = fortran_parser.parse_source(continued_source, ExposeConfig(functions=["tubing_bhp"]))
    router = python_pkg_gen.generate_router_py(module, "flow")

    assert "&" not in router
    # The failure this guards was a SyntaxError at import, so compiling is
    # the assertion that actually matters.
    compile(router, "router.py", "exec")


def test_continuation_line_may_reopen_with_its_own_ampersand(tmp_path):
    source = tmp_path / "reopened.f90"
    source.write_text(
        """
    function joined(alpha, &
        &          beta) result(total)
        real(8), intent(in) :: alpha, beta
        real(8) :: total

        total = alpha + beta
    end function joined
"""
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["joined"]))

    assert [p.name for p in module.functions[0].parameters] == ["alpha", "beta"]


def test_bang_inside_a_character_literal_is_not_a_comment():
    normalized = fortran_parser.normalize_free_form(
        "        write (*,*) 'rate ! per day'   ! trailing note\n"
    )

    assert "'rate ! per day'" in normalized
    assert "trailing note" not in normalized


def test_discovery_sees_routines_with_continued_signatures(continued_source):
    # quickstart populates `expose:` from this, so discovery has to normalize
    # for the same reason extraction does.
    assert fortran_parser.list_routine_names(continued_source) == ["tubing_bhp"]


# --- code-review findings [3][4][5]: free-form normalization edge cases ------

def test_whole_line_comment_between_continuations_does_not_end_the_statement():
    """A comment line is legal *between* continuation lines.

    `_strip_inline_comment` reduces it to blank; flushing the pending
    continuation on that blank closed the statement early and truncated the
    argument list, so `q_gas` was lost from the signature entirely.
    """
    normalized = fortran_parser.normalize_free_form(
        "    function bhp(wellhead_p, q_oil, &\n"
        "        ! flow rates, stb/d\n"
        "        q_gas) result(pbh)\n"
    )

    assert normalized.splitlines()[0].strip() == (
        "function bhp(wellhead_p, q_oil, q_gas) result(pbh)"
    )


def test_character_literal_continued_across_lines_keeps_its_bang():
    """Quote state must carry across a `&` continuation.

    Resetting it per physical line made the `!` inside a still-open literal
    look like a comment, silently truncating the string.
    """
    normalized = fortran_parser.normalize_free_form(
        "        msg = 'rate&\n     &! per day'\n"
    )

    assert "per day" in normalized


def test_internal_procedure_declarations_do_not_retype_the_outer_routine(tmp_path):
    """`contains` opens internal procedures whose declarations are not ours.

    The body capture runs to the routine's end and swallows them, and
    `_parse_declarations` keeps the last match for a name — so a helper
    declaring `integer :: a` inside a function whose own `a` is `real(8)`
    silently retyped the binding to int. It compiles, it runs, and it hands
    Fortran the wrong thing.
    """
    source = tmp_path / "inner.f90"
    source.write_text(
        """function outer(a) result(r)
    real(8), intent(in) :: a
    real(8) :: r
    r = a
contains
    subroutine helper(a)
        integer :: a
    end
end function outer
"""
    )

    fn = fortran_parser.parse_source(
        source, ExposeConfig(functions=["outer"])
    ).functions[0]

    assert fn.parameters[0].type == "float"
