"""Input handling and configuration: encoding, comment style, expose, language.

Covers DEFECTS.md A8, A6 (marker style), D1, D2 and C3.
"""

import warnings

import pytest
import yaml

from native2py import discovery
from native2py.config import (
    ConfigError,
    ExposeConfig,
    ExposeWarning,
    ServiceConfig,
)
from native2py.preprocess import (
    EncodingError,
    SourceEncodingWarning,
    expand_includes,
    read_source,
)

# --- A8: non-UTF-8 source ------------------------------------------------

# 'DEGREE' as latin-1: 0xB0 is not valid UTF-8 on its own.
LATIN1_DECK = b"C     TEMPERATURE IN \xb0C (VMS DECK)\n      SUBROUTINE T\n      END\n"


def test_latin1_source_is_not_replaced_with_u_fffd(tmp_path):
    source = tmp_path / "deck.f"
    source.write_bytes(LATIN1_DECK)

    with pytest.warns(SourceEncodingWarning):
        text = read_source(source)

    assert "�" not in text
    assert "\N{DEGREE SIGN}C" in text


def test_latin1_fallback_is_visible(tmp_path):
    source = tmp_path / "deck.f"
    source.write_bytes(LATIN1_DECK)

    with pytest.warns(SourceEncodingWarning, match="latin-1"):
        read_source(source)


def test_explicit_encoding_overrides_the_guess(tmp_path):
    source = tmp_path / "deck.f"
    source.write_bytes("C     \N{DEGREE SIGN}C\n".encode("cp1252"))

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        text = read_source(source, encoding="cp1252")

    assert "\N{DEGREE SIGN}C" in text


def test_explicit_encoding_that_cannot_decode_is_an_error(tmp_path):
    source = tmp_path / "deck.f"
    source.write_bytes(LATIN1_DECK)

    with pytest.raises(EncodingError):
        read_source(source, encoding="utf-8")


def test_utf8_source_warns_about_nothing(tmp_path):
    source = tmp_path / "deck.f"
    source.write_text("      SUBROUTINE T\n      END\n")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert "SUBROUTINE" in read_source(source)


def test_expand_includes_does_not_corrupt_latin1_bytes(tmp_path):
    (tmp_path / "GRID.INC").write_bytes(b"      REAL*8 A  ! \xb0C\n")
    source = tmp_path / "deck.f"
    source.write_text("      SUBROUTINE T\n      INCLUDE 'GRID.INC'\n      END\n")

    with pytest.warns(SourceEncodingWarning):
        expanded = expand_includes(source, [tmp_path])

    assert "�" not in expanded
    assert "\N{DEGREE SIGN}C" in expanded


# --- A6 support: comment style for the marker lines ----------------------


def _deck_with_include(tmp_path, body):
    (tmp_path / "GRID.INC").write_text("      REAL*8 A\n")
    source = tmp_path / "deck.f90"
    source.write_text(body)
    return source


def test_marker_lines_default_to_fixed_form(tmp_path):
    source = _deck_with_include(
        tmp_path, "      SUBROUTINE T\n      INCLUDE 'GRID.INC'\n      END\n"
    )

    expanded = expand_includes(source, [tmp_path])

    assert "C     --- native2py: expanded INCLUDE 'GRID.INC' ---" in expanded
    assert "C     --- native2py: end INCLUDE 'GRID.INC' ---" in expanded


def test_free_form_marker_lines_use_bang(tmp_path):
    source = _deck_with_include(
        tmp_path, "subroutine t\n  include 'GRID.INC'\nend subroutine t\n"
    )

    expanded = expand_includes(source, [tmp_path], comment_style="free")

    marker_lines = [ln for ln in expanded.splitlines() if "native2py:" in ln]
    assert marker_lines, "expected marker lines around the inlined file"
    # A `C` in column 1 is a syntax error in free-form.
    assert all(ln.startswith("! ") for ln in marker_lines)


def test_nested_includes_keep_the_chosen_comment_style(tmp_path):
    (tmp_path / "INNER.INC").write_text("real(8) :: b\n")
    (tmp_path / "GRID.INC").write_text("include 'INNER.INC'\nreal(8) :: a\n")
    source = tmp_path / "deck.f90"
    source.write_text("subroutine t\n  include 'GRID.INC'\nend subroutine t\n")

    expanded = expand_includes(source, [tmp_path], comment_style="free")

    assert "INNER.INC" in expanded
    assert not any(
        ln.startswith("C") for ln in expanded.splitlines() if "native2py:" in ln
    )


def test_unknown_comment_style_is_rejected(tmp_path):
    source = _deck_with_include(tmp_path, "      SUBROUTINE T\n      END\n")

    with pytest.raises(ValueError, match="comment_style"):
        expand_includes(source, [tmp_path], comment_style="lisp")


def test_positional_depth_and_stack_still_work(tmp_path):
    """The recursive call passes _depth/_stack positionally; keep that working."""
    source = _deck_with_include(
        tmp_path, "      SUBROUTINE T\n      INCLUDE 'GRID.INC'\n      END\n"
    )

    assert "REAL*8 A" in expand_includes(source, [tmp_path], 0, ())


# --- D1: empty expose block ---------------------------------------------


def _write_yaml(tmp_path, data):
    (tmp_path / "native2py.yaml").write_text(yaml.dump(data, sort_keys=False))
    (tmp_path / "native" / "sub").mkdir(parents=True, exist_ok=True)
    (tmp_path / "native" / "calc.hpp").write_text("struct S { double x; };\n")
    return tmp_path


def test_direct_expose_config_stays_permissive():
    """The C++ parsers construct ExposeConfig() and expect everything exposed."""
    assert ExposeConfig().is_exposed("Anything")


def test_empty_expose_block_in_yaml_warns(tmp_path):
    _write_yaml(
        tmp_path,
        {"name": "svc", "language": "cpp", "expose": {"classes": [], "functions": []}},
    )

    with pytest.warns(ExposeWarning, match="expose: all"):
        config = ServiceConfig.load(tmp_path)

    # Behaviour is unchanged — only the silence is fixed.
    assert config.expose.is_exposed("Anything")


def test_missing_expose_block_in_yaml_warns(tmp_path):
    _write_yaml(tmp_path, {"name": "svc", "language": "cpp"})

    with pytest.warns(ExposeWarning):
        ServiceConfig.load(tmp_path)


def test_expose_all_is_an_explicit_opt_in(tmp_path):
    _write_yaml(tmp_path, {"name": "svc", "language": "cpp", "expose": "all"})

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        config = ServiceConfig.load(tmp_path)

    assert config.expose.all is True
    assert config.expose.is_exposed("Anything")


def test_expose_all_true_key_is_an_explicit_opt_in(tmp_path):
    _write_yaml(tmp_path, {"name": "svc", "language": "cpp", "expose": {"all": True}})

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        config = ServiceConfig.load(tmp_path)

    assert config.expose.is_exposed("Anything")


def test_expose_all_false_binds_nothing_without_names(tmp_path):
    _write_yaml(tmp_path, {"name": "svc", "language": "cpp", "expose": {"all": False}})

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        config = ServiceConfig.load(tmp_path)

    assert not config.expose.is_exposed("Anything")


def test_named_expose_does_not_warn(tmp_path):
    _write_yaml(
        tmp_path, {"name": "svc", "language": "cpp", "expose": {"classes": ["S"]}}
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        config = ServiceConfig.load(tmp_path)

    assert config.expose.is_exposed("S")
    assert not config.expose.is_exposed("Other")


def test_expose_all_round_trips_through_save(tmp_path):
    ServiceConfig(name="svc", language="cpp", expose=ExposeConfig(all=True)).save(
        tmp_path
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert ServiceConfig.load(tmp_path).expose.all is True


def test_nonsense_expose_string_is_rejected(tmp_path):
    _write_yaml(tmp_path, {"name": "svc", "language": "cpp", "expose": "everything"})

    with pytest.raises(ConfigError, match="expose"):
        ServiceConfig.load(tmp_path)


# --- D2: language ---------------------------------------------------------


def _fortran_service(tmp_path, data):
    (tmp_path / "native").mkdir(parents=True, exist_ok=True)
    (tmp_path / "native" / "pvt.f90").write_text(
        "subroutine t(x)\n  real(8) :: x\nend subroutine t\n"
    )
    (tmp_path / "native2py.yaml").write_text(yaml.dump(data, sort_keys=False))
    return tmp_path


def test_missing_language_does_not_default_to_cpp(tmp_path):
    _fortran_service(tmp_path, {"name": "svc", "expose": {"functions": ["t"]}})

    assert ServiceConfig.load(tmp_path).language == "fortran"


def test_missing_language_with_no_sources_is_an_error(tmp_path):
    (tmp_path / "native").mkdir()
    (tmp_path / "native2py.yaml").write_text(
        yaml.dump({"name": "svc", "expose": {"functions": ["t"]}})
    )

    with pytest.raises(ConfigError, match="language"):
        ServiceConfig.load(tmp_path)


def test_language_conflicting_with_the_sources_is_an_error(tmp_path):
    _fortran_service(
        tmp_path, {"name": "svc", "language": "cpp", "expose": {"functions": ["t"]}}
    )

    with pytest.raises(ConfigError, match="conflicts"):
        ServiceConfig.load(tmp_path)


def test_unsupported_language_is_rejected(tmp_path):
    _fortran_service(
        tmp_path, {"name": "svc", "language": "c++", "expose": {"functions": ["t"]}}
    )

    with pytest.raises(ConfigError, match="Supported languages"):
        ServiceConfig.load(tmp_path)


def test_declared_language_still_wins_when_it_matches(tmp_path):
    _fortran_service(
        tmp_path, {"name": "svc", "language": "fortran", "expose": {"functions": ["t"]}}
    )

    assert ServiceConfig.load(tmp_path).language == "fortran"


# --- C3: preprocessed Fortran -------------------------------------------


def test_uppercase_suffix_requires_preprocessing(tmp_path):
    source = tmp_path / "PVT.F90"
    source.write_text("subroutine t\nend subroutine t\n")

    assert discovery.requires_preprocessing(source)
    # detect_language is deliberately unchanged.
    assert discovery.detect_language(source) == "fortran"


def test_lowercase_suffix_with_cpp_directives_requires_preprocessing(tmp_path):
    source = tmp_path / "pvt.f90"
    source.write_text(
        "#ifdef LEGACY\nsubroutine t\nend subroutine t\n#endif\n"
    )

    assert discovery.requires_preprocessing(source)


def test_ordinary_free_form_needs_no_preprocessing(tmp_path):
    source = tmp_path / "pvt.f90"
    source.write_text("subroutine t\nend subroutine t\n")

    assert not discovery.requires_preprocessing(source)


def test_cpp_headers_are_not_reported_as_needing_fortran_preprocessing(tmp_path):
    header = tmp_path / "calc.hpp"
    header.write_text("#include <cmath>\nstruct S { double x; };\n")

    assert not discovery.requires_preprocessing(header)


def test_find_cpp_directives_reports_what_was_found():
    found = discovery.find_cpp_directives(
        "#ifdef LEGACY\n#  include 'x.h'\n#define A 1\n#endif\n#ifdef LEGACY\n"
    )

    assert found == ["ifdef", "include", "define", "endif"]


def test_find_cpp_directives_ignores_ordinary_fortran():
    assert discovery.find_cpp_directives("subroutine t\n! # not a directive\nend\n") == []


# --- code-review finding [1] -------------------------------------------------

def test_expose_false_binds_nothing_rather_than_everything(tmp_path):
    """`expose: false` must not be coerced to "not stated".

    `_load_expose` did `expose_data = expose_data or {}`, and `False or {}` is
    `{}` — indistinguishable from an omitted key. So an explicit "bind
    nothing" took the permissive branch and bound the entire native API, the
    exact opposite of what was written.
    """
    (tmp_path / "native").mkdir()
    (tmp_path / "native" / "a.hpp").write_text("struct S { int x; };\n")
    (tmp_path / "native2py.yaml").write_text("name: svc\nlanguage: cpp\nexpose: false\n")

    expose = ServiceConfig.load(tmp_path).expose

    assert expose.all is False
    assert not expose.is_exposed("AnythingAtAll")


def test_expose_true_is_an_explicit_opt_in(tmp_path):
    (tmp_path / "native").mkdir()
    (tmp_path / "native" / "a.hpp").write_text("struct S { int x; };\n")
    (tmp_path / "native2py.yaml").write_text("name: svc\nlanguage: cpp\nexpose: true\n")

    assert ServiceConfig.load(tmp_path).expose.all is True
