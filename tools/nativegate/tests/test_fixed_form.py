"""FORTRAN 77 fixed-form parsing, INCLUDE expansion, and IMPLICIT typing.

Exercised against libraries/petro — real legacy petroleum engineering F77.
"""

from pathlib import Path

import pytest

from nativegate.config import ExposeConfig
from nativegate.discovery import detect_language, find_native_sources, is_fixed_form
from nativegate.parsers import fixed_form
from nativegate.parsers import fortran as fortran_parser
from nativegate.preprocess import IncludeError, expand_includes

PETRO = Path(__file__).parents[3] / "libraries" / "petro" / "fortran"
PETRO_INCLUDE = [PETRO / "include"]

# These tests deliberately parse real legacy F77 rather than a synthetic
# fixture — that's the whole point of them. Skip rather than fail if the
# library isn't in the tree.
pytestmark = pytest.mark.skipif(
    not (PETRO / "pvtcor.f").exists(),
    reason="libraries/petro not present in this checkout",
)


# --- discovery ----------------------------------------------------------

def test_detects_fixed_form_extensions():
    assert detect_language(Path("x.f")) == "fortran"
    assert detect_language(Path("x.for")) == "fortran"
    assert is_fixed_form(Path("x.f"))
    assert not is_fixed_form(Path("x.f90"))


def test_discovery_skips_generated_expanded_sources(tmp_path):
    # nativegate writes INCLUDE-expanded copies under native/_expanded/.
    # Re-discovering them as inputs compiles every routine twice and the
    # link fails with "redefinition of f2py_rout_...".
    (tmp_path / "a.f").write_text("      SUBROUTINE A\n      END\n")
    (tmp_path / "_expanded").mkdir()
    (tmp_path / "_expanded" / "a.f").write_text("      SUBROUTINE A\n      END\n")

    found = find_native_sources(tmp_path, "fortran")

    assert [p.name for p in found] == ["a.f"]


# --- fixed-form normalization -------------------------------------------

def test_strips_column_one_comments():
    src = "C     THIS IS A COMMENT\n      X = 1\n*     ALSO A COMMENT\n"

    assert fixed_form.normalize_fixed_form(src).splitlines() == ["X = 1"]


def test_joins_continuation_lines():
    # Real BBDPDZ signature, split across two lines with '&' in column 6.
    src = (
        "      SUBROUTINE BBDPDZ (P, QO, QW, QG, DIA, THETA, EPS,\n"
        "     &                   DPDZ, HL, IREG)\n"
    )

    normalized = fixed_form.normalize_fixed_form(src)

    assert normalized == "SUBROUTINE BBDPDZ (P, QO, QW, QG, DIA, THETA, EPS, DPDZ, HL, IREG)"


def test_truncates_sequence_number_columns():
    # Columns 1-72 are code; 73+ is the punch-card sequence number.
    # "      X = 1" is 11 chars, so pad to exactly 72 before the sequence field.
    src = "      X = 1".ljust(72) + "SEQ00010\n"

    assert fixed_form.normalize_fixed_form(src).strip() == "X = 1"


# --- IMPLICIT typing ----------------------------------------------------

def test_default_implicit_rules():
    # No IMPLICIT statement: I-N integer, everything else real.
    implicit = fixed_form.parse_implicit_map("      X = 1")

    assert fixed_form.implicit_type_of("N", implicit) == "integer"
    assert fixed_form.implicit_type_of("X", implicit) == "real"


def test_parses_implicit_from_petro_include():
    # PETRO.INC declares IMPLICIT DOUBLE PRECISION (A-H,O-Z) / INTEGER (I-N).
    expanded = expand_includes(PETRO / "pvtcor.f", PETRO_INCLUDE)
    implicit = fixed_form.parse_implicit_map(
        fixed_form.normalize_fixed_form(expanded)
    )

    assert fixed_form.implicit_type_of("GRAV", implicit) == "double precision"
    assert fixed_form.implicit_type_of("ICORR", implicit) == "integer"


def test_implicit_none_yields_no_defaults():
    implicit = fixed_form.parse_implicit_map("      IMPLICIT NONE")

    assert implicit == {}
    assert fixed_form.implicit_type_of("X", implicit) is None


# --- INCLUDE expansion --------------------------------------------------

def test_expands_include_from_search_path():
    expanded = expand_includes(PETRO / "pvtcor.f", PETRO_INCLUDE)

    # No *executable* INCLUDE statement may survive (the expansion leaves
    # 'C ... expanded INCLUDE ...' marker comments, which are inert).
    active_includes = [
        line
        for line in expanded.splitlines()
        if line[:1] not in ("C", "c", "*", "!") and "INCLUDE" in line.upper()
    ]
    assert active_includes == []
    assert "IMPLICIT DOUBLE PRECISION" in expanded


def test_unresolvable_include_raises_with_search_path():
    with pytest.raises(IncludeError, match="cannot resolve INCLUDE"):
        expand_includes(PETRO / "pvtcor.f", [])


def test_include_cycle_is_caught(tmp_path):
    (tmp_path / "a.f").write_text("      INCLUDE 'b.inc'\n")
    (tmp_path / "b.inc").write_text("      INCLUDE 'a.f'\n")

    with pytest.raises(IncludeError, match="nesting deeper"):
        expand_includes(tmp_path / "a.f", [tmp_path])


# --- routine extraction + typing ----------------------------------------

def test_extracts_f77_routine_ending_in_bare_end():
    # F77 routines close with a bare `END`, not `END FUNCTION <name>`, so the
    # free-form block finder never matches them.
    normalized = fixed_form.normalize_fixed_form(
        expand_includes(PETRO / "pvtcor.f", PETRO_INCLUDE)
    )

    routine = fixed_form.find_routine(normalized, "PVTBUB")

    assert routine["kind"] == "function"
    assert routine["params"] == ["RSTGT"]


def test_implicit_typing_drives_parameter_types():
    module = fortran_parser.parse_source(
        PETRO / "pvtcor.f",
        ExposeConfig(functions=["PVTINI"]),
        include_paths=PETRO_INCLUDE,
    )

    params = {p.name: p.type for p in module.functions[0].parameters}

    # I-N -> INTEGER; the rest DOUBLE PRECISION. Matches f2py's own signature.
    assert params == {
        "GRAV": "float",
        "GASSG": "float",
        "TEMPF": "float",
        "ICORR": "int",
    }


def test_dimension_statement_marks_arrays():
    module = fortran_parser.parse_source(
        PETRO / "matsol.f",
        ExposeConfig(functions=["THOMAS"]),
        include_paths=PETRO_INCLUDE,
    )

    params = {p.name: p for p in module.functions[0].parameters}

    assert all(params[n].is_array for n in ("A", "B", "C", "D", "X"))
    assert not params["N"].is_array
    assert params["N"].type == "int"


def test_function_return_type_from_prefix():
    module = fortran_parser.parse_source(
        PETRO / "pvtcor.f",
        ExposeConfig(functions=["PVTRS"]),
        include_paths=PETRO_INCLUDE,
    )

    fn = module.functions[0]
    assert not fn.is_subroutine
    assert fn.returns == "float"


def test_subroutine_has_void_return():
    module = fortran_parser.parse_source(
        PETRO / "pvtcor.f",
        ExposeConfig(functions=["PVTINI"]),
        include_paths=PETRO_INCLUDE,
    )

    assert module.functions[0].is_subroutine
    assert module.functions[0].returns == "void"


def test_f77_has_no_enclosing_fortran_module():
    # Fixed-form F77 predates modules, so f2py exposes routines at top level
    # with no nesting for __init__.py to bridge.
    module = fortran_parser.parse_source(
        PETRO / "pvtcor.f",
        ExposeConfig(functions=["PVTRS"]),
        include_paths=PETRO_INCLUDE,
    )

    assert module.fortran_module is None


def test_missing_routine_lists_available_names():
    with pytest.raises(ValueError, match="Routines found"):
        fortran_parser.parse_source(
            PETRO / "pvtcor.f",
            ExposeConfig(functions=["NOSUCHROUTINE"]),
            include_paths=PETRO_INCLUDE,
        )
