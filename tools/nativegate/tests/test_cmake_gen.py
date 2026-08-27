"""End-to-end: a generated C++ CMakeLists.txt really produces compile_commands.json.

Follow-up to the T1/T4/T7 verification-layers gap: `cmake_gen.py` used to
never turn on `CMAKE_EXPORT_COMPILE_COMMANDS`, so the C++/pybind11 build path
had no discoverable JSON Compilation Database — meaning buildinfo.py's
`load_compile_commands`/`extract_flags` (Layer 2's oracle, see buildinfo.py's
module docstring and design-verification-layers.md section 2) could only be
exercised against hand-written fixtures, never against a real `nativegate
build` output, the way the Fortran/meson path can be (meson writes one
unconditionally).

This test runs the real pipeline — `ngate quickstart` (which shells out
to the installed console script, same as test_quickstart.py's `_run_cli`) to
scaffold a minimal pybind11 service, then a real `cmake -S ... -B ...`
*configure* (no `--build`, so no compiler toolchain for the extension itself
needs to succeed — just CMake's own generation step) — and asserts the
resulting `compile_commands.json` exists and is parseable by buildinfo.py
with a real entry for the service's native source.

Skipped outright if `cmake` isn't on PATH, or if `pybind11` (needed for
`find_package(pybind11 CONFIG REQUIRED)` to succeed at configure time) isn't
importable from the interpreter running the tests.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nativegate import buildinfo

requires_cmake = pytest.mark.skipif(
    shutil.which("cmake") is None, reason="cmake is not installed"
)


def _pybind11_cmake_dir() -> str | None:
    try:
        import pybind11
    except ImportError:
        return None
    return pybind11.get_cmake_dir()


def _run_cli(args, cwd):
    venv_bin = Path(sys.executable).parent
    result = subprocess.run(
        [str(venv_bin / "nativegate"), *args], cwd=cwd, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@requires_cmake
def test_cmake_configure_produces_a_parseable_compile_commands_json(tmp_path):
    pybind11_cmake_dir = _pybind11_cmake_dir()
    if pybind11_cmake_dir is None:
        pytest.skip("pybind11 is not importable from this interpreter")

    repo_root = tmp_path
    (repo_root / "services").mkdir()

    header = repo_root / "widget.hpp"
    header.write_text("class Widget { public: double square(double x); };")
    impl = repo_root / "widget.cpp"
    impl.write_text(
        '#include "widget.hpp"\n\ndouble Widget::square(double x) { return x * x; }\n'
    )

    _run_cli(["quickstart", str(header)], cwd=repo_root)

    service_dir = repo_root / "services" / "widget"
    cmake_txt = (service_dir / "CMakeLists.txt").read_text()
    assert "CMAKE_EXPORT_COMPILE_COMMANDS ON" in cmake_txt

    build_dir = service_dir / "build"
    result = subprocess.run(
        [
            "cmake",
            "-S",
            str(service_dir),
            "-B",
            str(build_dir),
            f"-Dpybind11_DIR={pybind11_cmake_dir}",
            f"-DPython_EXECUTABLE={sys.executable}",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    compile_commands_path = build_dir / "compile_commands.json"
    assert compile_commands_path.exists(), (
        "cmake configure did not leave a compile_commands.json behind "
        f"(build dir contents: {sorted(p.name for p in build_dir.iterdir())})"
    )

    commands = buildinfo.load_compile_commands(compile_commands_path)
    assert commands, "compile_commands.json is empty"

    # Both the sibling .cpp implementation and the generated pybind11
    # bindings translation unit are real compile-command entries.
    widget_flags = buildinfo.extract_flags(compile_commands_path, "widget.cpp")
    assert widget_flags  # some real flag list, not asserting exact content

    bindings_flags = buildinfo.extract_flags(
        compile_commands_path, "widget_bindings.cpp"
    )
    assert bindings_flags

    # The extracted flags are usable by the rest of buildinfo.py's contract
    # (the safety gate and codegen subsetting), not just present.
    buildinfo.refuse_unsafe(widget_flags)
    buildinfo.codegen_flags(widget_flags)
