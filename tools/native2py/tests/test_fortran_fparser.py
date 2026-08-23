"""The fparser2 Fortran backend, and its parity with the regex reader.

Two halves:

* the **seam** (`parsers/fortran.py`) — backend selection, the env var, and
  the rule that asking for a backend you do not have is an error rather than
  a silent downgrade. These run everywhere, with or without fparser.
* the **differential harness** — every routine in libraries/petro and in the
  petro_api F90 facade parsed under BOTH backends, asserting the resulting
  `ModuleIR` is equal symbol by symbol. This is the acceptance bar for the
  backend: a tree parser that quietly types one argument differently from the
  reader it replaces produces a service that builds, imports, passes its
  smoke test and returns a wrong number.

Where the two backends genuinely differ, the difference is named by a test
below rather than left for someone to discover. All three known differences
are the same thing: the regex reader has no grammar and accepts source that
is not valid Fortran.
"""

from pathlib import Path

import pytest

from native2py.config import ExposeConfig
from native2py.parsers import fortran as fortran_parser
from native2py.parsers import fortran_fparser, fortran_regex

REPO = Path(__file__).parents[3]
PETRO = REPO / "libraries" / "petro" / "fortran"
PETRO_INCLUDE = [PETRO / "include"]
PETRO_API = REPO / "services" / "petro_api" / "native" / "petro_api.f90"

requires_fparser = pytest.mark.skipif(
    not fortran_fparser.is_available(),
    reason=f"fparser is not installed: {fortran_fparser.unavailable_reason()}",
)


def _corpus() -> list[Path]:
    """Every legacy Fortran source the two backends must agree on."""
    files = sorted(PETRO.glob("*.f"))
    files += sorted(PETRO.glob("modern/*.f90"))
    if PETRO_API.exists():
        files.append(PETRO_API)
    return [f for f in files if f.exists()]


CORPUS = _corpus()


# --- the seam -----------------------------------------------------------


def test_regex_is_the_default_backend(monkeypatch):
    # `auto` deliberately still resolves to the reader that has been in
    # production, even on a machine where fparser2 is importable. Flipping the
    # default is a separate, deliberate change — not something that happens
    # because a wheel appeared on the build box.
    monkeypatch.delenv("NATIVE2PY_FORTRAN_PARSER", raising=False)

    assert fortran_parser.resolve_backend() == "regex"
    assert fortran_parser.resolve_backend("auto") == "regex"


def test_the_env_var_selects_a_backend(monkeypatch):
    monkeypatch.setenv("NATIVE2PY_FORTRAN_PARSER", "regex")
    assert fortran_parser.resolve_backend() == "regex"


def test_an_explicit_argument_beats_the_env_var(monkeypatch):
    monkeypatch.setenv("NATIVE2PY_FORTRAN_PARSER", "fparser2")
    assert fortran_parser.resolve_backend("regex") == "regex"


def test_an_unknown_backend_is_rejected():
    with pytest.raises(fortran_parser.ParserUnavailable, match="fortran77"):
        fortran_parser.resolve_backend("fortran77")


def test_requesting_a_missing_backend_is_an_error_not_a_downgrade(monkeypatch):
    # The whole point of the seam: a build that quietly loses symbols because
    # a wheel was missing is the failure this prevents. Asking for fparser2
    # and not having it must stop the build, not silently produce the regex
    # reader's answer.
    monkeypatch.setattr(fortran_fparser, "is_available", lambda: False)
    monkeypatch.setattr(fortran_fparser, "unavailable_reason", lambda: "no wheel")

    with pytest.raises(fortran_parser.ParserUnavailable, match="no wheel"):
        fortran_parser.resolve_backend("fparser2")


def test_auto_still_resolves_when_fparser_is_absent(monkeypatch):
    monkeypatch.delenv("NATIVE2PY_FORTRAN_PARSER", raising=False)
    monkeypatch.setattr(fortran_fparser, "is_available", lambda: False)

    assert fortran_parser.resolve_backend() == "regex"


def test_backend_description_names_what_actually_ran(monkeypatch):
    monkeypatch.delenv("NATIVE2PY_FORTRAN_PARSER", raising=False)
    assert "regex reader" in fortran_parser.backend_description()
    assert "selected explicitly" in fortran_parser.backend_description("regex")


def test_backend_description_reports_an_unavailable_request(monkeypatch):
    monkeypatch.setattr(fortran_fparser, "is_available", lambda: False)
    monkeypatch.setattr(fortran_fparser, "unavailable_reason", lambda: "no wheel")

    assert "no wheel" in fortran_parser.backend_description("fparser2")


def test_the_seam_dispatches_to_the_selected_backend(tmp_path, monkeypatch):
    source = tmp_path / "dispatch.f90"
    source.write_text(
        """
    subroutine scale_it(q)
        real(8), intent(inout) :: q
        q = q * 2.0d0
    end subroutine scale_it
"""
    )
    monkeypatch.delenv("NATIVE2PY_FORTRAN_PARSER", raising=False)
    calls = []
    monkeypatch.setattr(
        fortran_regex, "parse_source", lambda *a, **k: calls.append("regex")
    )
    monkeypatch.setattr(
        fortran_fparser, "parse_source", lambda *a, **k: calls.append("fparser2")
    )
    monkeypatch.setattr(fortran_fparser, "is_available", lambda: True)

    fortran_parser.parse_source(source, ExposeConfig(functions=["scale_it"]))
    fortran_parser.parse_source(
        source, ExposeConfig(functions=["scale_it"]), backend="fparser2"
    )

    assert calls == ["regex", "fparser2"]


# --- availability -------------------------------------------------------


def test_unavailable_fparser_degrades_without_raising_on_import():
    # Same shape as cpp_ast: importing the module must never blow up on a
    # machine without the dependency, so `auto` can ask and move on.
    assert isinstance(fortran_fparser.is_available(), bool)
    assert isinstance(fortran_fparser.unavailable_reason(), str)
    assert isinstance(fortran_fparser.fparser_version(), str)


# --- the differential harness -------------------------------------------


@requires_fparser
@pytest.mark.skipif(not CORPUS, reason="libraries/petro is not present")
@pytest.mark.parametrize("source", CORPUS, ids=lambda p: p.name)
def test_both_backends_discover_the_same_routines(source):
    regex_names = fortran_regex.list_routine_names(source)
    tree_names = fortran_fparser.list_routine_names(source)

    assert [n.lower() for n in tree_names] == [n.lower() for n in regex_names]


@requires_fparser
@pytest.mark.skipif(not CORPUS, reason="libraries/petro is not present")
@pytest.mark.parametrize("source", CORPUS, ids=lambda p: p.name)
def test_both_backends_produce_the_same_ir(source):
    """Every routine in the file, one at a time, under both backends.

    One routine at a time because that is how native2py is actually driven —
    `expose.functions:` names what a service needs — and because a per-routine
    comparison names the routine that diverged instead of reporting that two
    large objects are unequal.
    """
    differences = []
    for name in fortran_regex.list_routine_names(source):
        expose = ExposeConfig(functions=[name])
        expected = _parse_or_error(fortran_regex, source, expose)
        actual = _parse_or_error(fortran_fparser, source, expose)
        if expected != actual:
            differences.append(f"{name}:\n  regex   : {expected}\n  fparser2: {actual}")

    assert not differences, "\n".join(differences)


def _parse_or_error(backend, source: Path, expose: ExposeConfig):
    """The backend's ModuleIR, or its refusal — both are part of the contract.

    A backend that raises where the other returns has not reached parity, so
    the exception is compared rather than allowed to fail the test on its own.
    """
    try:
        return backend.parse_source(source, expose, PETRO_INCLUDE)
    except Exception as exc:  # noqa: BLE001 — the refusal is the value under test
        return f"{type(exc).__name__}: {exc}"


@requires_fparser
@pytest.mark.skipif(not CORPUS, reason="libraries/petro is not present")
def test_the_corpus_is_actually_covered():
    # A parity test that silently parses nothing is a parity test that passes
    # forever. Assert the corpus is the size it is known to be.
    total = sum(len(fortran_regex.list_routine_names(f)) for f in CORPUS)

    assert len(CORPUS) >= 8
    assert total >= 49


# --- what the tree parser gets right on its own -------------------------


@requires_fparser
def test_intents_and_optionality_survive_the_tree(tmp_path):
    source = tmp_path / "modern.f90"
    source.write_text(
        """
module fluids
    implicit none
contains
    subroutine configure(api, salinity, cells)
        real(8), intent(in) :: api
        real(8), intent(in), optional :: salinity
        integer, intent(inout), dimension(10) :: cells
        api = api
    end subroutine configure
end module fluids
"""
    )

    module = fortran_fparser.parse_source(source, ExposeConfig(functions=["configure"]))

    params = {p.name: p for p in module.functions[0].parameters}
    assert params["api"].intent == "in" and not params["api"].is_optional
    assert params["salinity"].is_optional
    assert params["cells"].intent == "inout" and params["cells"].is_array
    # f2py nests a module's routines under the module name in the extension.
    assert module.fortran_module == "fluids"


@requires_fparser
def test_a_derived_type_argument_is_refused_not_defaulted(tmp_path):
    source = tmp_path / "derived.f90"
    source.write_text(
        """
    subroutine apply(st)
        type(pvt_state), intent(inout) :: st
        continue
    end subroutine apply
"""
    )

    module = fortran_fparser.parse_source(source, ExposeConfig(functions=["apply"]))

    assert module.functions == []
    assert [s.name for s in module.skipped] == ["apply"]
    assert "derived type" in module.skipped[0].reason


@requires_fparser
def test_an_internal_procedure_cannot_retype_an_outer_argument(tmp_path):
    # The regex reader needed an explicit cut at `contains` to stop a helper's
    # `integer :: a` from silently retyping the outer `a`. On the tree the
    # helper's declarations live in a sibling node, so it cannot happen.
    source = tmp_path / "internal.f90"
    source.write_text(
        """
    subroutine outer(a)
        real(8), intent(inout) :: a
        a = helper(a)
    contains
        function helper(v) result(w)
            real(8) :: v, w
            integer :: a
            a = 1
            w = v
        end function helper
    end subroutine outer
"""
    )

    module = fortran_fparser.parse_source(source, ExposeConfig(functions=["outer"]))

    assert module.functions[0].parameters[0].type == "float"


@requires_fparser
def test_an_assumed_length_character_argument_maps_to_str(tmp_path):
    # `CHARACTER*(*)` carries no length. Stringifying the whole type spec
    # leaves the suffix attached and the mapping misses, which is how such an
    # argument used to fall through to IMPLICIT and come back an integer.
    source = tmp_path / "chars.f"
    source.write_text(
        "      SUBROUTINE LABEL (MSG, N)\n"
        "      CHARACTER*(*) MSG\n"
        "      INTEGER N\n"
        "      N = 1\n"
        "      END\n"
    )

    module = fortran_fparser.parse_source(source, ExposeConfig(functions=["LABEL"]))

    params = {p.name: p for p in module.functions[0].parameters}
    assert params["MSG"].type == "str"


@requires_fparser
def test_symbol_tables_do_not_leak_between_files(tmp_path):
    # fparser2's SYMBOL_TABLES is a module-level global singleton that the
    # parser appends to. Two parses in one process would otherwise share
    # state, and a name that exists in neither file resolves to the wrong
    # one — a silent wrong answer, not a crash.
    from fparser.two.symbol_table import SYMBOL_TABLES

    first = tmp_path / "first.f90"
    first.write_text(
        "module alpha\ncontains\n"
        "    subroutine one(x)\n        real(8), intent(in) :: x\n"
        "    end subroutine one\nend module alpha\n"
    )
    second = tmp_path / "second.f90"
    second.write_text(
        "module beta\ncontains\n"
        "    subroutine two(y)\n        integer, intent(in) :: y\n"
        "    end subroutine two\nend module beta\n"
    )

    fortran_fparser.parse_source(first, ExposeConfig(functions=["one"]))
    assert not SYMBOL_TABLES._symbol_tables

    module = fortran_fparser.parse_source(second, ExposeConfig(functions=["two"]))
    assert module.fortran_module == "beta"
    assert not SYMBOL_TABLES._symbol_tables


# --- documented differences ---------------------------------------------
#
# Three fixtures in test_fortran_pipeline.py are not valid Fortran. The regex
# reader accepts them because it has no grammar; a real parser refuses. Each
# is named here so the difference is a decision on record rather than a
# surprise the next person has to re-derive. None of them is reachable from
# the default backend, which is still the regex reader.


@requires_fparser
def test_implicit_outside_a_program_unit_is_refused(tmp_path):
    # `implicit integer (x-z)` at file scope belongs to no program unit and is
    # a syntax error. The regex reader scans the whole file for IMPLICIT lines
    # and so applies it to every routine in the file.
    source = tmp_path / "loose_implicit.f90"
    source.write_text(
        "    implicit integer (x-z)\n\n"
        "    subroutine accumulate(x)\n        x = x + 1\n"
        "    end subroutine accumulate\n"
    )

    with pytest.raises(ValueError, match="not valid Fortran"):
        fortran_fparser.parse_source(source, ExposeConfig(functions=["accumulate"]))


@requires_fparser
def test_a_declaration_after_an_executable_statement_is_not_honoured(tmp_path):
    # Declarations must precede execution. Here `x` is declared after a DO
    # loop, so it is not part of the specification part and its intent(inout)
    # is not seen; `x` falls back to IMPLICIT typing. The regex reader picks
    # the declaration up anyway because it scans the routine body as text.
    source = tmp_path / "late_decl.f90"
    source.write_text(
        "    subroutine clamp(n, x)\n"
        "        integer :: n\n"
        "        integer :: i\n"
        "        do i = 1, n\n            i = i\n        end do\n"
        "        real(8), intent(inout) :: x\n"
        "        x = x + n\n"
        "    end\n"
    )

    tree = fortran_fparser.parse_source(source, ExposeConfig(functions=["clamp"]))
    regex = fortran_regex.parse_source(source, ExposeConfig(functions=["clamp"]))

    params = {p.name: p for p in tree.functions[0].parameters}
    # Both agree on the type; only the misplaced intent is lost.
    assert params["x"].type == "float"
    assert params["x"].intent == "in"
    assert {p.name: p.intent for p in regex.functions[0].parameters}["x"] == "inout"


@requires_fparser
def test_a_refusal_names_the_file_and_offers_the_regex_reader(tmp_path):
    source = tmp_path / "broken.f90"
    source.write_text("    subroutine oops(\n")

    with pytest.raises(ValueError) as excinfo:
        fortran_fparser.parse_source(source, ExposeConfig(functions=["oops"]))

    message = str(excinfo.value)
    assert "broken.f90" in message
    assert "parser: regex" in message


# --- refusals shared with the regex reader ------------------------------


@requires_fparser
def test_expose_functions_is_still_required(tmp_path):
    source = tmp_path / "empty.f90"
    source.write_text("    subroutine noop\n    end subroutine noop\n")

    with pytest.raises(ValueError, match="expose.functions"):
        fortran_fparser.parse_source(source, ExposeConfig())


@requires_fparser
def test_a_missing_routine_is_an_error(tmp_path):
    source = tmp_path / "present.f90"
    source.write_text("    subroutine noop\n    end subroutine noop\n")

    with pytest.raises(ValueError, match="does_not_exist"):
        fortran_fparser.parse_source(
            source, ExposeConfig(functions=["does_not_exist"])
        )


@requires_fparser
def test_an_undeclared_argument_under_implicit_none_is_skipped(tmp_path):
    source = tmp_path / "strict.f90"
    source.write_text(
        "    subroutine accumulate(x)\n"
        "        implicit none\n"
        "        x = x + 1\n"
        "    end subroutine accumulate\n"
    )

    module = fortran_fparser.parse_source(
        source, ExposeConfig(functions=["accumulate"])
    )

    assert module.functions == []
    assert [s.name for s in module.skipped] == ["accumulate"]
    assert "IMPLICIT NONE" in module.skipped[0].reason
