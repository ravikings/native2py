"""Fortran discovery, declaration and kind-parameter handling for legacy code.

Exercised against libraries/petro — 1988-vintage fixed-form F77 decks plus a
2003 free-form F90 facade that wraps them.
"""

from pathlib import Path

from native2py.config import ExposeConfig
from native2py.generators import python_pkg_gen
from native2py.ir import FunctionDef, ModuleIR
from native2py.parsers import fixed_form
from native2py.parsers import fortran as fortran_parser
from native2py.preprocess import (
    expand_includes,
    resolve_kind_parameters,
    uses_kind_parameters,
)

PETRO = Path(__file__).parents[3] / "libraries" / "petro" / "fortran"
PETRO_INCLUDE = [PETRO / "include"]


# --- routine discovery --------------------------------------------------


def test_discovery_finds_typed_functions_in_fixed_form():
    # `DOUBLE PRECISION FUNCTION PVTRS (P)` has two tokens before FUNCTION.
    # The free-form regex allowed one, so quickstart auto-populated `expose:`
    # with only the SUBROUTINEs — half the API, silently.
    names = fortran_parser.list_routine_names(PETRO / "pvtcor.f")

    assert "PVTINI" in names  # subroutine
    assert "PVTRS" in names  # double precision function
    assert "PVTBUB" in names
    assert "PVTZ" in names


def test_discovery_covers_every_routine_in_the_petro_decks():
    # Discovery and extraction must agree: anything find_routine can extract
    # has to be something list_routine_names reports.
    for deck in sorted(PETRO.glob("*.f")):
        normalized = fixed_form.normalize_fixed_form(
            expand_includes(deck, PETRO_INCLUDE)
        )
        discovered = fortran_parser.list_routine_names(deck)

        for name in fixed_form.list_routine_names(normalized):
            assert name in discovered, f"{deck.name}: {name} not discovered"


def test_discovery_finds_argumentless_subroutine():
    # `SUBROUTINE TRNCAL` — no parentheses; works purely through COMMON.
    assert "TRNCAL" in fortran_parser.list_routine_names(PETRO / "simcor.f")


def test_free_form_multi_token_return_type(tmp_path):
    source = tmp_path / "m.f90"
    source.write_text(
        "double precision function scale_it(x)\n"
        "    double precision :: x\n"
        "    double precision :: scale_it\n"
        "    scale_it = 2 * x\n"
        "end function scale_it\n"
    )

    assert fortran_parser.list_routine_names(source) == ["scale_it"]


# --- declarations -------------------------------------------------------


def test_assumed_length_character_argument_types_as_str():
    # `CHARACTER*(*) MSG` did not match the declaration regex, so MSG fell
    # through to IMPLICIT typing (M -> INTEGER) and bound as an int: a
    # binding that compiles, runs, and passes a pointer where the callee
    # expects a string descriptor.
    module = fortran_parser.parse_source(
        PETRO / "pvtcor.f",
        ExposeConfig(functions=["PVTERR"]),
        include_paths=PETRO_INCLUDE,
    )

    params = {p.name: p.type for p in module.functions[0].parameters}
    assert params["MSG"] == "str"
    assert params["ICODE"] == "int"


def test_fixed_length_character_declaration(tmp_path):
    source = tmp_path / "w.f"
    source.write_text(
        "      SUBROUTINE WNAME (NAME)\n"
        "      CHARACTER*8 NAME\n"
        "      RETURN\n"
        "      END\n"
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["WNAME"]))

    assert module.functions[0].parameters[0].type == "str"


def test_implicit_typing_still_applies_to_undeclared_arguments():
    module = fortran_parser.parse_source(
        PETRO / "pvtcor.f",
        ExposeConfig(functions=["PVTINI"]),
        include_paths=PETRO_INCLUDE,
    )

    params = {p.name: p.type for p in module.functions[0].parameters}
    # PETRO.INC: IMPLICIT DOUBLE PRECISION (A-H,O-Z) / INTEGER (I-N)
    assert params["GRAV"] == "float"
    assert params["ICORR"] == "int"


def test_every_petro_routine_parses():
    for deck in sorted(PETRO.glob("*.f")):
        for name in fortran_parser.list_routine_names(deck):
            module = fortran_parser.parse_source(
                deck, ExposeConfig(functions=[name]), include_paths=PETRO_INCLUDE
            )
            assert module.functions, f"{deck.name}:{name} produced no IR"


def test_typed_function_return_type_is_taken_from_the_prefix():
    module = fortran_parser.parse_source(
        PETRO / "relperm.f",
        ExposeConfig(functions=["KROIL"]),
        include_paths=PETRO_INCLUDE,
    )

    fn = module.functions[0]
    assert fn.is_subroutine is False
    # KROIL starts with K, which IMPLICIT INTEGER (I-N) would type as int;
    # the DOUBLE PRECISION prefix on the FUNCTION statement wins.
    assert fn.returns == "float"


# --- kind parameters ----------------------------------------------------


def test_resolves_kind_parameter_to_a_literal():
    source = (
        "module m\n"
        "  integer, parameter :: dp = kind(1.0d0)\n"
        "contains\n"
        "  function f(x) result(y)\n"
        "    real(dp), intent(in) :: x\n"
        "    real(dp) :: y\n"
        "  end function f\n"
        "end module m\n"
    )

    resolved = resolve_kind_parameters(source)

    assert "real(8), intent(in) :: x" in resolved
    assert "real(dp)" not in resolved


def test_resolves_selected_real_kind():
    source = (
        "integer, parameter :: wp = selected_real_kind(15, 307)\n  real(wp) :: x\n"
    )

    assert "real(8) :: x" in resolve_kind_parameters(source)


def test_resolves_single_precision_kind():
    source = "integer, parameter :: sp = kind(1.0)\n  real(sp) :: y\n"

    assert "real(4) :: y" in resolve_kind_parameters(source)


def test_kind_equals_spelling_is_resolved():
    source = "integer, parameter :: dp = kind(1.0d0)\n  real(kind=dp) :: z\n"

    assert "real(8) :: z" in resolve_kind_parameters(source)


def test_unresolvable_kind_is_left_alone():
    # Better to fail loudly at compile time than to silently pick a width.
    source = "  real(some_external_kind) :: x\n"

    assert resolve_kind_parameters(source) == source


def test_source_without_kind_parameters_is_untouched():
    source = "  real(8) :: x\n  integer :: n\n"

    assert resolve_kind_parameters(source) == source
    assert uses_kind_parameters(source) is False


def test_petro_facade_needs_kind_resolution():
    source = (PETRO / "modern" / "petro_api.f90").read_text()

    assert uses_kind_parameters(source) is True
    assert "real(dp)" not in resolve_kind_parameters(source)


# --- mixed module / top-level re-export ---------------------------------


def test_init_py_reexports_module_and_top_level_routines_together():
    # A modern F90 facade over fixed-form decks puts module-scoped routines
    # under `_native.<module>.<name>` and bare F77 routines at the top level
    # of the same extension. Both must be re-exported flat.
    module = ModuleIR(name="petro", language="fortran", source_file="petro.f90")
    module.functions = [
        FunctionDef(name="oil_fvf", fortran_module="petro_api"),
        FunctionDef(name="PVTRS", fortran_module=None),
    ]

    init_py = python_pkg_gen.generate_init_py(module)

    assert "oil_fvf = _native.petro_api.oil_fvf" in init_py
    assert "PVTRS = _native.pvtrs" in init_py
    assert '__all__ = ["oil_fvf", "PVTRS"]' in init_py


# --- F77 intent inference -----------------------------------------------
#
# F77 declares no intent. Without inference f2py binds every scalar as
# intent(in), so a routine's results are computed and then discarded.


def _routine(deck: str, name: str):
    normalized = fixed_form.normalize_fixed_form(
        expand_includes(PETRO / deck, PETRO_INCLUDE)
    )
    return fixed_form.find_routine(normalized, name), normalized


def test_assigned_scalars_are_outputs():
    # SUBROUTINE STEP (DTIN, DTOUT, ICONV): DTIN is read, the other two are
    # assigned. Before inference this bound as step(dtin, dtout, iconv) -> None.
    routine, normalized = _routine("simcor.f", "STEP")

    intents = fixed_form.infer_intents(routine, normalized)

    assert intents == {"dtin": "in", "dtout": "out", "iconv": "out"}


def test_single_output_argument():
    routine, normalized = _routine("pvtcor.f", "GASVIS")

    assert fixed_form.infer_intents(routine, normalized)["vg"] == "out"


def test_read_before_write_is_inout():
    # TRAVER: `IF (NSEG .LT. 1) NSEG = 20` reads NSEG in the condition and
    # writes it in the guarded statement — both halves of a logical IF matter.
    routine, normalized = _routine("hydrau.f", "TRAVER")

    intents = fixed_form.infer_intents(routine, normalized)

    assert intents["nseg"] == "inout"
    assert intents["pbh"] == "out"


def test_declarations_are_not_reads():
    # SIMINI's arguments appear only in a comparison and a WRITE. Counting
    # `DOUBLE PRECISION X` or `DIMENSION A(N)` as a read made every declared
    # output look like an input.
    routine, normalized = _routine("simcor.f", "SIMINI")

    assert fixed_form.infer_intents(routine, normalized) == {
        "mx": "in",
        "my": "in",
        "mz": "in",
    }


def test_array_fully_defined_by_the_routine_is_an_output():
    # THOMAS writes X(N) then X(I) reading only X(I+1), which it has already
    # written. So X is a pure output despite appearing on the right-hand side.
    routine, normalized = _routine("matsol.f", "THOMAS")

    intents = fixed_form.infer_intents(routine, normalized)

    assert intents["x"] == "out"
    assert intents["a"] == "in"
    assert intents["n"] == "in"


def test_element_assignment_does_not_define_the_whole_array():
    # LSOR does `X(L) = X(L) + ...` after reading X — updating elements is
    # not defining the array, so X remains an input as well.
    routine, normalized = _routine("matsol.f", "LSOR")

    intents = fixed_form.infer_intents(routine, normalized)

    assert intents["x"] == "inout"
    assert intents["itused"] == "out"
    assert intents["resid"] == "out"


def test_call_arguments_resolve_against_the_callee(tmp_path):
    # CALL passes by reference, so without knowing the callee every argument
    # has to be assumed written. Resolving it keeps pure inputs as inputs.
    source = (
        "      SUBROUTINE OUTER (A, B)\n"
        "      DOUBLE PRECISION A, B\n"
        "      CALL INNER (A, B)\n"
        "      RETURN\n"
        "      END\n"
        "      SUBROUTINE INNER (X, Y)\n"
        "      DOUBLE PRECISION X, Y\n"
        "      Y = 2.0D0 * X\n"
        "      RETURN\n"
        "      END\n"
    )
    normalized = fixed_form.normalize_fixed_form(source)
    routine = fixed_form.find_routine(normalized, "OUTER")

    intents = fixed_form.infer_intents(routine, normalized)

    assert intents == {"a": "in", "b": "out"}


def test_unresolvable_callee_widens_conservatively(tmp_path):
    # The callee lives in another file. Guessing "in" would drop a real
    # output; widening to inout keeps the argument in the signature and
    # returns it, which is wrong only in being verbose.
    source = (
        "      SUBROUTINE OUTER (A)\n"
        "      DOUBLE PRECISION A\n"
        "      CALL SOMEWHERE_ELSE (A)\n"
        "      RETURN\n"
        "      END\n"
    )
    normalized = fixed_form.normalize_fixed_form(source)
    routine = fixed_form.find_routine(normalized, "OUTER")

    assert fixed_form.infer_intents(routine, normalized)["a"] == "inout"


def test_recursive_call_does_not_hang():
    source = (
        "      SUBROUTINE R (A)\n"
        "      DOUBLE PRECISION A\n"
        "      CALL R (A)\n"
        "      RETURN\n"
        "      END\n"
    )
    normalized = fixed_form.normalize_fixed_form(source)
    routine = fixed_form.find_routine(normalized, "R")

    assert fixed_form.infer_intents(routine, normalized)["a"] == "inout"


def test_intents_reach_the_ir():
    module = fortran_parser.parse_source(
        PETRO / "simcor.f",
        ExposeConfig(functions=["STEP"]),
        include_paths=PETRO_INCLUDE,
    )

    intents = {p.name: p.intent for p in module.functions[0].parameters}
    assert intents == {"DTIN": "in", "DTOUT": "out", "ICONV": "out"}


def test_character_output_is_reported_not_silently_bound():
    # PVTERR assigns MSG, but f2py cannot return a CHARACTER argument.
    module = fortran_parser.parse_source(
        PETRO / "pvtcor.f",
        ExposeConfig(functions=["PVTERR"]),
        include_paths=PETRO_INCLUDE,
    )

    params = {p.name: p.intent for p in module.functions[0].parameters}
    assert params["ICODE"] == "out"
    assert params["MSG"] == "in"
    assert any("CHARACTER" in s.reason for s in module.skipped)


# --- f2py directive injection -------------------------------------------


def test_injects_directives_after_the_routine_statement():
    source = "      SUBROUTINE STEP (DTIN, DTOUT, ICONV)\n      RETURN\n      END\n"

    out = fixed_form.inject_intent_directives(
        source,
        {"STEP": {"DTIN": ("in", False), "DTOUT": ("out", False), "ICONV": ("out", False)}},
    )

    lines = out.splitlines()
    assert lines[0].strip().startswith("SUBROUTINE STEP")
    assert lines[1] == "Cf2py intent(out) DTOUT"
    assert lines[2] == "Cf2py intent(out) ICONV"
    # intent(in) is the default; emitting it would just be noise.
    assert "DTIN" not in out.split("SUBROUTINE STEP (DTIN, DTOUT, ICONV)")[1]


def test_directives_go_after_a_continued_routine_statement():
    source = (
        "      SUBROUTINE BBDPDZ (P, QO, QW, QG, DIA, THETA, EPS,\n"
        "     &                   DPDZ, HL, IREG)\n"
        "      RETURN\n"
        "      END\n"
    )

    out = fixed_form.inject_intent_directives(
        source, {"BBDPDZ": {"DPDZ": ("out", False)}}
    )

    lines = out.splitlines()
    # Must not land between the two halves of the argument list.
    assert lines[1].strip().startswith("&")
    assert lines[2] == "Cf2py intent(out) DPDZ"


def test_scalar_inout_uses_value_semantics():
    # f2py's intent(inout) on a scalar demands the caller pass a 0-d array;
    # intent(in,out) takes a value and returns the result.
    source = "      SUBROUTINE BUMP (N)\n      RETURN\n      END\n"

    out = fixed_form.inject_intent_directives(source, {"BUMP": {"N": ("inout", False)}})

    assert "Cf2py intent(in,out) N" in out


def test_array_output_asks_the_caller_for_storage():
    # Arrays are routinely dimensioned by a PARAMETER from an INCLUDE deck,
    # so a bare intent(out) would have f2py allocate MXCELL elements a call.
    source = "      SUBROUTINE THOMAS (X)\n      RETURN\n      END\n"

    out = fixed_form.inject_intent_directives(source, {"THOMAS": {"X": ("out", True)}})

    assert "Cf2py intent(in,out) X" in out


def test_routine_with_no_outputs_gets_no_directives():
    source = "      SUBROUTINE PURE (A)\n      RETURN\n      END\n"

    out = fixed_form.inject_intent_directives(source, {"PURE": {"A": ("in", False)}})

    assert "Cf2py" not in out
    assert out == source


# --- generated endpoints ------------------------------------------------


def test_endpoint_drops_output_arguments_from_its_signature():
    from native2py.ir import Parameter

    module = ModuleIR(name="petro", language="fortran", source_file="x.f")
    module.functions = [
        FunctionDef(
            name="STEP",
            parameters=[
                Parameter(name="dtin", type="float", intent="in"),
                Parameter(name="dtout", type="float", intent="out"),
                Parameter(name="iconv", type="int", intent="out"),
            ],
            is_subroutine=True,
        )
    ]

    router = python_pkg_gen.generate_router_py(module, "petro")

    assert "def STEP_endpoint(dtin: float):" in router
    assert "dtout, iconv = STEP(dtin)" in router
    assert '"dtout": dtout' in router
    assert '"iconv": iconv' in router


def test_endpoint_keeps_inout_arguments_and_returns_them():
    from native2py.ir import Parameter

    module = ModuleIR(name="petro", language="fortran", source_file="x.f")
    module.functions = [
        FunctionDef(
            name="TRAVER",
            parameters=[
                Parameter(name="pwh", type="float", intent="in"),
                Parameter(name="nseg", type="int", intent="inout"),
                Parameter(name="pbh", type="float", intent="out"),
            ],
            is_subroutine=True,
        )
    ]

    router = python_pkg_gen.generate_router_py(module, "petro")

    assert "def TRAVER_endpoint(pwh: float, nseg: int):" in router
    assert "nseg, pbh = TRAVER(pwh, nseg)" in router


# --- encoding and preprocessed decks (DEFECTS.md A8, C3) ----------------


def test_vms_era_deck_survives_include_expansion_intact(tmp_path):
    """A latin-1 deck must not be silently rewritten with U+FFFD.

    The bytes below are what a VMS-era listing header actually contains; the
    old `read_text(errors="replace")` turned every one of them into U+FFFD in
    the _expanded copy that f2py then compiled.
    """
    import pytest

    from native2py.preprocess import SourceEncodingWarning

    (tmp_path / "PETRO.INC").write_bytes(b"      REAL*8 TREF\nC     TREF IN \xb0C\n")
    deck = tmp_path / "probe.f"
    deck.write_bytes(
        b"C     (c) 1988 \xa9 ACME\n"
        b"      SUBROUTINE PROBE1(API)\n"
        b"      INCLUDE 'PETRO.INC'\n"
        b"      END\n"
    )

    with pytest.warns(SourceEncodingWarning):
        expanded = expand_includes(deck, [tmp_path])

    assert "�" not in expanded
    assert "\N{COPYRIGHT SIGN}" in expanded
    assert "\N{DEGREE SIGN}C" in expanded
    assert "REAL*8 TREF" in expanded


def test_preprocessed_deck_is_flagged_rather_than_parsed_as_if_all_branches_live(tmp_path):
    from native2py import discovery

    deck = tmp_path / "PROBE.F"
    deck.write_text(
        "      SUBROUTINE PROBE1(API)\n"
        "#ifdef DOUBLE_PRECISION\n"
        "      REAL*8 API\n"
        "#else\n"
        "      REAL*4 API\n"
        "#endif\n"
        "      END\n"
    )

    assert discovery.detect_language(deck) == "fortran"
    assert discovery.requires_preprocessing(deck)
    assert discovery.find_cpp_directives(deck.read_text()) == ["ifdef", "else", "endif"]
