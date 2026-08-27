"""`libraries:` for Fortran services — compiling shared decks into the extension.

THE GAP THESE COVER

`libraries:` used to be C++-only. C++ links a CMake target through
`add_subdirectory` + `target_link_libraries`; f2py has no equivalent, because
`f2py -c` takes a list of SOURCES and compiles them itself. A Fortran service
could therefore name a library and get nothing at all: `services/petro_api`
bound the ten routines of its F90 facade, produced an extension, and died at
import with `undefined symbol: iprvog_` — the seven F77 decks the facade calls
into were never compiled.

Verified beyond these tests by building the real image and calling it:
`tubing_bhp` (the routine that reaches IPRVOG in wellib.f) returned
2964.7917938859628, its recorded golden value, exactly.
"""

from pathlib import Path

import pytest
from click import ClickException

from nativegate.cli import _fortran_library_inputs
from nativegate.config import ServiceConfig


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A libraries/<name>/ tree with decks and an include/ directory."""
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "libraries" / "petro" / "fortran"
    (root / "include").mkdir(parents=True)
    (root / "include" / "PETRO.INC").write_text("      COMMON /FLUID/ API\n")
    for name in ("flash.f", "wellib.f"):
        (root / name).write_text(
            "      SUBROUTINE X\n      INCLUDE 'PETRO.INC'\n      END\n"
        )
    (root / "modern").mkdir()
    (root / "modern" / "petro_api.f90").write_text("subroutine y\nend subroutine\n")
    return tmp_path


def _config(**kwargs):
    return ServiceConfig(name="svc", language="fortran", **kwargs)


def test_library_sources_are_found_across_the_whole_tree(library):
    sources, _ = _fortran_library_inputs(_config(libraries=["petro"]))

    names = sorted(p.name for p in sources)
    # Nested directories too: a legacy tree does not keep everything flat.
    assert names == ["flash.f", "petro_api.f90", "wellib.f"]


def test_include_directories_are_discovered_not_demanded(library):
    # A legacy tree keeps its COMMON blocks in include/*.INC next to the decks.
    # Making the user restate that in `include_paths:` would be asking them to
    # repeat something the library already says.
    _, includes = _fortran_library_inputs(_config(libraries=["petro"]))

    assert [p.name for p in includes] == ["include"]


def test_a_cmakelists_is_not_required(library):
    # The C++ path demands one because it declares a CMake target to link.
    # f2py never links a target — it compiles the sources — so requiring the
    # file would refuse a perfectly good Fortran library.
    assert not (Path("libraries/petro") / "CMakeLists.txt").exists()

    sources, _ = _fortran_library_inputs(_config(libraries=["petro"]))

    assert sources


def test_a_missing_library_is_reported_by_name(library):
    with pytest.raises(ClickException, match="does not exist"):
        _fortran_library_inputs(_config(libraries=["nope"]))


def test_a_library_with_no_fortran_is_refused(tmp_path, monkeypatch):
    # Naming a C++ library from a Fortran service cannot work, and failing here
    # beats failing later with a link error nobody can read.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "libraries" / "cpplib").mkdir(parents=True)
    (tmp_path / "libraries" / "cpplib" / "a.cpp").write_text("int f();")

    with pytest.raises(ClickException, match="contains no Fortran sources"):
        _fortran_library_inputs(_config(libraries=["cpplib"]))


def test_already_expanded_copies_are_not_picked_up_again(library):
    # native/_expanded/ is generated output. Compiling a previous run's copies
    # alongside the originals is a duplicate-symbol link error.
    expanded = Path("libraries/petro/fortran/_expanded")
    expanded.mkdir()
    (expanded / "flash.f").write_text("      SUBROUTINE X\n      END\n")

    sources, _ = _fortran_library_inputs(_config(libraries=["petro"]))

    assert all("_expanded" not in p.parts for p in sources)
    assert sorted(p.name for p in sources).count("flash.f") == 1


# --- end-to-end through `generate` ---------------------------------------


def _service_with_library(tmp_path):
    """A Fortran service whose facade calls a routine defined in a library."""
    service = tmp_path / "services" / "svc"
    (service / "native").mkdir(parents=True)
    (service / "native" / "facade.f90").write_text(
        "subroutine compute(x, y)\n"
        "  real(8), intent(in) :: x\n"
        "  real(8), intent(out) :: y\n"
        "  call helper(x, y)\n"
        "end subroutine compute\n"
    )
    lib = tmp_path / "libraries" / "shared" / "fortran"
    lib.mkdir(parents=True)
    (lib / "helper.f").write_text(
        "      SUBROUTINE HELPER(X, Y)\n"
        "      DOUBLE PRECISION X, Y\n"
        "      Y = X * 2.0D0\n"
        "      END\n"
    )
    (service / "nativegate.yaml").write_text(
        "name: svc\nlanguage: fortran\nlibraries:\n  - shared\n"
        "expose:\n  functions:\n    - compute\n"
    )
    return service


def test_generate_compiles_library_sources_into_the_extension(tmp_path, monkeypatch):
    # The whole gap in one assertion: before this, F2PY_SOURCES named only the
    # service's own facade, so the extension built and then failed at import
    # with `undefined symbol` for every routine the facade called into.
    from click.testing import CliRunner

    from nativegate.cli import main

    monkeypatch.chdir(tmp_path)
    service = _service_with_library(tmp_path)

    result = CliRunner().invoke(main, ["generate", "svc"])
    assert result.exit_code == 0, result.output

    cmake = (service / "CMakeLists.txt").read_text()
    # The service's own facade needs no rewriting (free-form, no INCLUDE, no
    # kind parameters), so it is compiled where it lies. The library source is
    # vendored, because it lies outside the service entirely.
    assert "native/facade.f90" in cmake
    assert "native/_expanded/helper.f" in cmake
    assert "Compiling 1 source(s) from shared" in result.output


def test_library_sources_are_vendored_under_the_service(tmp_path, monkeypatch):
    # They must be copied, not referenced in place: the Docker build context is
    # the service directory, so a path outside it could not be COPYed in.
    from click.testing import CliRunner

    from nativegate.cli import main

    monkeypatch.chdir(tmp_path)
    service = _service_with_library(tmp_path)

    CliRunner().invoke(main, ["generate", "svc"])

    assert (service / "native" / "_expanded" / "helper.f").exists()


def test_a_fortran_service_keeps_the_simple_docker_context(tmp_path, monkeypatch):
    # `libraries:` means two different things by language. C++ LINKS a target
    # outside the service, so its build context has to be the repo root. The
    # f2py sources are already vendored, so switching the context would emit a
    # `COPY libraries/...` for files that are in the service tree anyway.
    from click.testing import CliRunner

    from nativegate.cli import main

    monkeypatch.chdir(tmp_path)
    service = _service_with_library(tmp_path)
    CliRunner().invoke(main, ["generate", "svc"])

    result = CliRunner().invoke(main, ["docker", "svc"])
    assert result.exit_code == 0, result.output

    dockerfile = (service / "Dockerfile").read_text()
    assert "Build from services/svc/" in dockerfile
    assert "COPY libraries/" not in dockerfile
