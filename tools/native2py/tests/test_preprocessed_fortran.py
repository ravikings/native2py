"""Preprocessed Fortran (.F90/.F) and statement functions.

Both were named in ROADMAP.md as silent mis-binding hazards. Verified beyond
these tests by building: a .F90 with `#ifdef DOUBLE_IT` generated, compiled
through f2py, and returned 10.0 for scale(5.0) — the configured branch, not
the dead one.
"""

import shutil
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from native2py.cli import main
from native2py.config import ExposeConfig
from native2py.discovery import find_native_sources, requires_preprocessing
from native2py.parsers import fortran as fortran_parser
from native2py.preprocess import PreprocessError, run_c_preprocessor

requires_gfortran = pytest.mark.skipif(
    shutil.which("gfortran") is None, reason="gfortran is not installed"
)


F90_SOURCE = textwrap.dedent(
    """\
    subroutine scale(x, y)
      real(8), intent(in) :: x
      real(8), intent(out) :: y
    #ifdef DOUBLE_IT
      y = x * 2.0d0
    #else
      y = x * 3.0d0
    #endif
    end subroutine scale
    """
)


def test_uppercase_suffixes_are_discoverable(tmp_path):
    # The root of the whole gap: .F90 was not in the extension table at all,
    # so `generate` reported "No Fortran sources found" for a directory full
    # of Fortran. Being mis-parsed would at least have been visible.
    (tmp_path / "model.F90").write_text(F90_SOURCE)

    found = find_native_sources(tmp_path, "fortran")

    assert [p.name for p in found] == ["model.F90"]
    assert requires_preprocessing(found[0])


def test_discovery_does_not_double_count_on_case_insensitive_filesystems(tmp_path):
    # On macOS/Windows rglob("*.f90") and rglob("*.F90") match the same file.
    # Counting it twice compiles it twice — a duplicate-symbol link error.
    (tmp_path / "model.F90").write_text(F90_SOURCE)
    (tmp_path / "plain.f90").write_text("subroutine a\nend subroutine\n")

    found = find_native_sources(tmp_path, "fortran")

    assert sorted(p.name for p in found) == ["model.F90", "plain.f90"]


@requires_gfortran
def test_the_preprocessor_selects_exactly_one_branch(tmp_path):
    source = tmp_path / "model.F90"
    source.write_text(F90_SOURCE)

    default = run_c_preprocessor(source)
    assert "3.0d0" in default and "2.0d0" not in default
    assert "#" not in default  # no directives, no linemarkers

    defined = run_c_preprocessor(source, defines=["DOUBLE_IT"])
    assert "2.0d0" in defined and "3.0d0" not in defined


def test_a_missing_gfortran_is_an_error_not_a_misparse(tmp_path, monkeypatch):
    # The old behaviour — warn and read every #ifdef branch as live — is the
    # silent-wrong-answer class. If the preprocessor cannot run, generation
    # must stop and say why.
    monkeypatch.setattr(shutil, "which", lambda name: None)
    import native2py.preprocess as preprocess_module

    source = tmp_path / "model.F90"
    source.write_text(F90_SOURCE)

    with pytest.raises(PreprocessError, match="gfortran is not on PATH"):
        run_c_preprocessor(source)


@requires_gfortran
def test_generate_preprocesses_before_anything_parses(tmp_path, monkeypatch):
    # End to end: discovery, intent inference and the CMake source list all
    # see the preprocessed copy, and `fortran: defines:` picks the branch.
    monkeypatch.chdir(tmp_path)
    service = tmp_path / "services" / "svc"
    (service / "native").mkdir(parents=True)
    (service / "native" / "model.F90").write_text(F90_SOURCE)
    (service / "native2py.yaml").write_text(
        "name: svc\nlanguage: fortran\n"
        "expose:\n  functions:\n    - scale\n"
        "fortran:\n  defines:\n    - DOUBLE_IT\n"
    )

    result = CliRunner().invoke(main, ["generate", "svc"])
    assert result.exit_code == 0, result.output

    copy = (service / "native" / "_expanded" / "model.f90").read_text()
    assert "2.0d0" in copy and "#ifdef" not in copy
    cmake = (service / "CMakeLists.txt").read_text()
    assert "native/_expanded/model.f90" in cmake
    # And the old warning is gone — this is handled now, not apologised for.
    assert "needs the C preprocessor" not in result.output


# --- statement functions --------------------------------------------------


def test_a_statement_function_definition_is_not_a_read(tmp_path):
    # `DBL(X) = X*2*B` is a declaration wearing an assignment's clothes.
    # Counting its right-hand side as a read marked B — written before any
    # real read — as intent(inout), demanding a value the routine never
    # consumes. fparser2 is no help (it classifies the line as a plain
    # Assignment_Stmt — measured), so the rule is semantic: a subscripted
    # assignment to something never declared as an array cannot be an
    # array-element write, hence statement function.
    source = tmp_path / "stf.f"
    source.write_text(
        "      SUBROUTINE CALC(A, B, RES)\n"
        "      DOUBLE PRECISION A, B, RES\n"
        "      DOUBLE PRECISION DBL\n"
        "      DBL(X) = X * 2.0D0 * B\n"
        "      B = A + 1.0D0\n"
        "      RES = DBL(A)\n"
        "      END\n"
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["CALC"]))
    intents = {p.name: p.intent for p in module.functions[0].parameters}

    assert intents == {"A": "in", "B": "out", "RES": "out"}


def test_a_real_array_write_is_still_a_write(tmp_path):
    # The rule must never over-fire: skipping a genuine `TMP(I) = expr` would
    # lose the reads in expr, and a lost read can flip a genuinely-read
    # argument to intent(out) — which f2py then drops from the call.
    source = tmp_path / "arr.f"
    source.write_text(
        "      SUBROUTINE FILL(SCALE, OUT)\n"
        "      DOUBLE PRECISION SCALE, OUT\n"
        "      DIMENSION TMP(10)\n"
        "      TMP(1) = SCALE * 2.0D0\n"
        "      OUT = TMP(1)\n"
        "      END\n"
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["FILL"]))
    intents = {p.name: p.intent for p in module.functions[0].parameters}

    assert intents["SCALE"] == "in"  # its read through TMP(1)= survived
    assert intents["OUT"] == "out"


def test_a_character_substring_write_is_still_a_write(tmp_path):
    # `STR(1:3) = ...` subscripts a non-array too, but the `:` says substring
    # — a real write, not a statement function.
    source = tmp_path / "sub.f"
    source.write_text(
        "      SUBROUTINE TAG(N, LABEL)\n"
        "      INTEGER N\n"
        "      CHARACTER*8 LABEL\n"
        "      IF (N .GT. 0) LABEL(1:3) = 'POS'\n"
        "      END\n"
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["TAG"]))
    intents = {p.name: p.intent for p in module.functions[0].parameters}

    assert intents["N"] == "in"


# --- CHARACTER outputs ----------------------------------------------------


def test_a_fixed_length_character_output_binds(tmp_path):
    # Measured with numpy 2.5's f2py, meson backend: `character*32,
    # intent(out)` builds and returns the value. The old blanket demotion
    # ("f2py cannot return a CHARACTER argument") threw this capability away.
    source = tmp_path / "msg.f"
    source.write_text(
        "      SUBROUTINE STATUS(ICODE, MSG)\n"
        "      INTEGER ICODE\n"
        "      CHARACTER*32 MSG\n"
        "      MSG = 'OK'\n"
        "      END\n"
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["STATUS"]))
    intents = {p.name: p.intent for p in module.functions[0].parameters}

    assert intents["MSG"] == "out"
    assert module.skipped == []


def test_an_assumed_length_character_output_stays_demoted(tmp_path):
    # The dangerous one. `character*(*), intent(out)` BUILDS, IMPORTS, and
    # silently returns b'' — measured, and worse than a build failure. It
    # stays an input, with the measurement in the reason.
    source = tmp_path / "msg.f"
    source.write_text(
        "      SUBROUTINE STATUS(ICODE, MSG)\n"
        "      INTEGER ICODE\n"
        "      CHARACTER*(*) MSG\n"
        "      MSG = 'OK'\n"
        "      END\n"
    )

    module = fortran_parser.parse_source(source, ExposeConfig(functions=["STATUS"]))
    intents = {p.name: p.intent for p in module.functions[0].parameters}

    assert intents["MSG"] == "in"
    assert "silently returns an empty string" in module.skipped[0].reason
    assert "CHARACTER*80" in module.skipped[0].reason  # the way out, named


def test_the_endpoint_decodes_the_bytes_f2py_returns(tmp_path):
    # f2py hands CHARACTER back as bytes, blank-padded to the declared length.
    # Bytes are not JSON; the padding is Fortran bookkeeping, not data.
    from native2py.generators import python_pkg_gen

    source = tmp_path / "msg.f"
    source.write_text(
        "      SUBROUTINE STATUS(ICODE, MSG)\n"
        "      INTEGER ICODE\n"
        "      CHARACTER*32 MSG\n"
        "      MSG = 'OK'\n"
        "      END\n"
    )
    module = fortran_parser.parse_source(source, ExposeConfig(functions=["STATUS"]))
    module.name = "msg"

    router = python_pkg_gen.generate_router_py(module, "msg")

    assert "_n2p_text(MSG)" in router
    namespace = {"STATUS": lambda icode: b"FAILED" + b" " * 26}
    stripped = "\n".join(
        line for line in router.splitlines() if not line.startswith("from . import")
    )
    exec(compile(stripped, "router.py", "exec"), namespace)
    assert namespace["STATUS_endpoint"](1) == {"MSG": "FAILED"}
