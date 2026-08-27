"""Recovering a routine's comment header into `FunctionDef.doc`.

This text is not decoration. `python_pkg_gen` turns `doc` into the wrapper's
Python docstring, FastAPI publishes that as the route `description`, and
FastMCP hands the description to a model as the MCP tool description — so it
is the only thing a model knows about a native routine beyond its signature.
The failure mode of getting it wrong is therefore not an ugly page, it is a
model calling a Beggs and Brill traverse with a gas rate in the wrong units.

Everything here parses real Fortran with the real fparser2 backend (and, where
parity is the point, the regex reader as well). There are no mocked trees: the
whole risk in this feature is what a real 1988 deck actually looks like, and a
hand-built tree cannot show that.
"""

from pathlib import Path

import pytest

from nativegate.config import ExposeConfig
from nativegate.parsers import fortran_fparser, fortran_regex

from conftest import requires_fparser

REPO = Path(__file__).parents[3]
PETRO = REPO / "libraries" / "petro" / "fortran"
PETRO_INCLUDE = [PETRO / "include"]


def _doc(source: Path, name: str, backend=fortran_fparser, includes=None) -> str | None:
    module = backend.parse_source(
        source, ExposeConfig(functions=[name]), includes or []
    )
    return module.functions[0].doc


# --- the two comment conventions ----------------------------------------


@requires_fparser
def test_a_fixed_form_c_header_becomes_the_doc(tmp_path):
    source = tmp_path / "legacy.f"
    source.write_text(
        "C=====================================================\n"
        "      SUBROUTINE SETRHO (GRAV, RHO)\n"
        "C=====================================================\n"
        "C     CONVERT API GRAVITY TO OIL DENSITY, LB/CUFT.\n"
        "C     VALID FOR 5 .LE. GRAV .LE. 65.\n"
        "C-----------------------------------------------------\n"
        "      DOUBLE PRECISION GRAV, RHO\n"
        "      RHO = 141.5D0 / (131.5D0 + GRAV) * 62.4D0\n"
        "      END\n"
    )

    assert _doc(source, "SETRHO") == (
        "CONVERT API GRAVITY TO OIL DENSITY, LB/CUFT.\n"
        "VALID FOR 5 .LE. GRAV .LE. 65."
    )


@requires_fparser
@pytest.mark.parametrize("marker", ["C", "c", "*", "!"])
def test_every_fixed_form_comment_marker_is_recognised(tmp_path, marker):
    # All four are legal in column 1 and all four appear in the wild: `C` and
    # `*` in the 1988 decks, `c` from anyone who used a lowercase editor, `!`
    # from F90-era edits to fixed-form files.
    source = tmp_path / f"marker_{ord(marker)}.f"
    source.write_text(
        "      SUBROUTINE NOOP (X)\n"
        f"{marker}     DOES NOTHING AT ALL.\n"
        "      DOUBLE PRECISION X\n"
        "      X = X\n"
        "      END\n"
    )

    assert _doc(source, "NOOP") == "DOES NOTHING AT ALL."


@requires_fparser
def test_a_free_form_bang_header_above_the_routine_becomes_the_doc(tmp_path):
    source = tmp_path / "modern.f90"
    source.write_text(
        "module fluids\n"
        "    implicit none\n"
        "contains\n"
        "    !-----------------------------------------------\n"
        "    ! Oil formation volume factor, rb/stb.\n"
        "    ! Standing (1947) correlation.\n"
        "    !-----------------------------------------------\n"
        "    function bo(pressure) result(value)\n"
        "        real, intent(in) :: pressure\n"
        "        real :: value\n"
        "        value = 1.0 + 1.0e-5 * pressure\n"
        "    end function bo\n"
        "end module fluids\n"
    )

    assert _doc(source, "bo") == (
        "Oil formation volume factor, rb/stb.\nStanding (1947) correlation."
    )


@requires_fparser
def test_the_header_inside_the_routine_wins_over_the_one_above_it(tmp_path):
    # Both positions are conventional, and when a deck has both, the one
    # inside the routine is the one written about the routine — the run above
    # it is as likely to be the previous routine's trailing note, because F77
    # does not require a blank line between program units.
    source = tmp_path / "both.f"
    source.write_text(
        "C     TRAILING NOTE FROM WHATEVER CAME BEFORE.\n"
        "      SUBROUTINE PICKME (X)\n"
        "C     THE REAL DESCRIPTION.\n"
        "      DOUBLE PRECISION X\n"
        "      X = 1.0D0\n"
        "      END\n"
    )

    assert _doc(source, "PICKME") == "THE REAL DESCRIPTION."


@requires_fparser
def test_a_continued_header_does_not_swallow_its_own_argument_list(tmp_path):
    # The comment run starts after the LAST line of the opening statement.
    # Anchored on the first line instead, the continuation lines themselves
    # (which are not comments) would end the run immediately and the doc would
    # be lost — which is exactly what wellib.f's NODAL does in the corpus.
    source = tmp_path / "wrapped.f"
    source.write_text(
        "      SUBROUTINE WRAPPED (ALPHA, BETA,\n"
        "     *                    GAMMA)\n"
        "C     THREE ARGUMENTS, TWO LINES.\n"
        "      DOUBLE PRECISION ALPHA, BETA, GAMMA\n"
        "      GAMMA = ALPHA + BETA\n"
        "      END\n"
    )

    assert _doc(source, "WRAPPED") == "THREE ARGUMENTS, TWO LINES."


# --- what must NOT end up in a tool description --------------------------


@requires_fparser
def test_a_routine_with_no_comment_has_no_doc(tmp_path):
    source = tmp_path / "bare.f"
    source.write_text(
        "      SUBROUTINE BARE (X)\n"
        "      DOUBLE PRECISION X\n"
        "      X = 0.0D0\n"
        "      END\n"
    )

    assert _doc(source, "BARE") is None


@requires_fparser
def test_a_banner_only_header_leaves_the_doc_empty_rather_than_full_of_rules(tmp_path):
    # Legacy decks separate routines with rules of `=`, `-` and `*`. None of
    # them say anything, and a description consisting of 71 equals signs is
    # worse than no description at all — the generator has a truthful skeleton
    # for the None case, and this must fall through to it rather than invent.
    source = tmp_path / "banner.f"
    source.write_text(
        "      SUBROUTINE BANNER (X)\n"
        "C=====================================================\n"
        "C*****************************************************\n"
        "C-----------------------------------------------------\n"
        "C\n"
        "      DOUBLE PRECISION X\n"
        "      X = 0.0D0\n"
        "      END\n"
    )

    assert _doc(source, "BANNER") is None


@requires_fparser
def test_a_rule_with_words_in_it_is_kept(tmp_path):
    # The banner test is "no letters or digits", not "starts with a dash":
    # revision lines drawn as rules are real documentation.
    source = tmp_path / "dated.f"
    source.write_text(
        "      SUBROUTINE DATED (X)\n"
        "C---- 05-FEB-1996: FIXED DIVIDE BY ZERO WHEN API .LT. 5.0 ----\n"
        "      DOUBLE PRECISION X\n"
        "      X = 0.0D0\n"
        "      END\n"
    )

    assert _doc(source, "DATED") == (
        "---- 05-FEB-1996: FIXED DIVIDE BY ZERO WHEN API .LT. 5.0 ----"
    )


@requires_fparser
def test_an_absurdly_long_header_is_truncated(tmp_path):
    body = "".join(f"C     LINE {i:03d} OF THE CHANGE LOG\n" for i in range(80))
    source = tmp_path / "changelog.f"
    source.write_text(
        "      SUBROUTINE LOGGED (X)\n"
        + body
        + "      DOUBLE PRECISION X\n"
        "      X = 0.0D0\n"
        "      END\n"
    )

    doc = _doc(source, "LOGGED")

    assert doc is not None
    assert len(doc.splitlines()) == 30
    assert doc.startswith("LINE 000")


# --- the docstring-injection hazard --------------------------------------


@requires_fparser
def test_hostile_comment_text_is_carried_verbatim(tmp_path):
    # A 1988 programmer drawing a box with quotes, or ending a line with a
    # backslash, must not be able to break generated Python. The parser does
    # NOT achieve that by editing the deck's words: neutralising a `"""` here
    # would rewrite a routine's documentation to suit one consumer's quoting
    # rules, and the IR promises the deck's own text to every consumer.
    #
    # Escaping therefore lives in the generator, which is the layer that knows
    # it is emitting a docstring. This test pins the parser half — the text
    # survives intact — and the test below pins the generator half end to end.
    source = tmp_path / "hostile.f"
    source.write_text(
        '      SUBROUTINE HOSTILE (X)\n'
        'C     A HEADER WITH """ IN IT AND A PATH C:\\TEMP\\ \n'
        'C     AND A TRAILING BACKSLASH \\\n'
        "      DOUBLE PRECISION X\n"
        "      X = 0.0D0\n"
        "      END\n"
    )

    doc = _doc(source, "HOSTILE")

    assert doc is not None
    assert 'A HEADER WITH """ IN IT' in doc
    assert "C:\\TEMP\\" in doc
    assert any(line.endswith("\\") for line in doc.splitlines())


@requires_fparser
def test_hostile_comment_text_still_generates_valid_python(tmp_path):
    """The end-to-end property: a hostile deck comment cannot break the router.

    Executed rather than merely compiled — source carrying an escaped-out
    docstring would still *parse*; the question is whether the text can run.
    Note there is no DeprecationWarning filter here: the generator emits
    repr() for anything carrying a backslash, so there is no invalid escape
    sequence to warn about. That warning being gone is the point.
    """
    from nativegate.generators.python_pkg_gen import generate_router_py
    from nativegate.ir import FunctionDef, ModuleIR, Parameter

    source = tmp_path / "hostile.f"
    source.write_text(
        '      SUBROUTINE HOSTILE (X)\n'
        'C     A HEADER WITH """ THEN BREACH = 1\n'
        'C     AND A PATH C:\\TEMP\\\n'
        "      DOUBLE PRECISION X\n"
        "      X = 0.0D0\n"
        "      END\n"
    )
    doc = _doc(source, "HOSTILE")

    module = ModuleIR(
        name="deck",
        language="fortran",
        source_file="hostile.f",
        functions=[
            FunctionDef(
                name="hostile",
                parameters=[Parameter(name="x", type="float")],
                returns="None",
                is_subroutine=True,
                doc=doc,
            )
        ],
    )
    code = generate_router_py(module, "deck")
    body = "\n".join(
        line for line in code.splitlines() if not line.startswith("from . import")
    )
    namespace: dict = {"hostile": lambda x: None}
    exec(compile(body, "router.py", "exec"), namespace)  # noqa: S102

    assert "BREACH" not in namespace, "deck comment escaped the docstring and ran"


# --- the real corpus ------------------------------------------------------


@requires_fparser
@pytest.mark.skipif(not PETRO.exists(), reason="libraries/petro is not present")
def test_a_real_1988_deck_yields_prose_and_not_ascii_art():
    doc = _doc(PETRO / "pvtcor.f", "PVTBUB", includes=PETRO_INCLUDE)

    assert doc == (
        "BUBBLE POINT PRESSURE, PSIA, FOR A TARGET SOLUTION GOR RSTGT.\n"
        "SOLVED BY NEWTON ON PVTRS SINCE THE STANDING FORM INVERTS\n"
        "ANALYTICALLY BUT VAZQUEZ-BEGGS AND GLASO DO NOT."
    )


@requires_fparser
@pytest.mark.skipif(not PETRO.exists(), reason="libraries/petro is not present")
def test_an_expanded_include_does_not_leak_into_the_doc():
    # PVTINI's header is followed immediately by INCLUDE 'PETRO.INC', and
    # nativegate expands that before parsing. Without the expansion marker
    # ending the comment run, PETRO.INC's own eight-line banner about COMMON
    # block ordering was appended to the description of every routine in every
    # deck that includes it — measured, and the reason the marker is a
    # terminator.
    doc = _doc(PETRO / "pvtcor.f", "PVTINI", includes=PETRO_INCLUDE)

    assert doc is not None
    assert doc.startswith("SET THE FLUID DESCRIPTION")
    assert "PETRO.INC" not in doc
    assert "DO NOT REORDER" not in doc


@requires_fparser
@pytest.mark.skipif(not PETRO.exists(), reason="libraries/petro is not present")
def test_both_backends_recover_the_same_doc():
    # The differential harness in test_fortran_fparser.py compares whole
    # ModuleIRs, so this is already covered there; it is asserted directly
    # here because the shared cleaning rules in `fixed_form` are the reason it
    # holds, and a regression in them should name itself.
    for name in ("PVTBUB", "PVTINI", "PVTZ"):
        tree = _doc(PETRO / "pvtcor.f", name, includes=PETRO_INCLUDE)
        reader = _doc(
            PETRO / "pvtcor.f", name, backend=fortran_regex, includes=PETRO_INCLUDE
        )
        assert tree == reader, name
        assert tree
