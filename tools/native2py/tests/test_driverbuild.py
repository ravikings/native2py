"""T4 — driver build: compile one TU, link the extension's own built objects.

Spec `design-verification-layers.md` section 2.3, section 2.8, section 4
rules 2-3, 5, 8. See `native2py/driverbuild.py`'s module docstring for the
build-pipeline gap this module works around (native2py build's f2py/meson
invocation does not persist a `compile_commands.json` today).

Two kinds of test:

* unit tests against a fixture `compile_commands.json` (no compiler needed):
  the `-ffast-math` refusal, the driver/extension flag-divergence refusal,
  object-path resolution.
* a compiler-marked, real-service integration test: build the petro_api
  extension once (persisting its build directory), generate the driver
  (T3), build+link+run it through this module, and parse every expected
  line with T2's `wire.parse_lines`/`slots_for_entry`. A second test proves
  the "no recompile" property directly off the recorded compile argv.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from native2py import buildinfo, driverbuild, wire
from native2py.drivers.fortran import generate_driver
from native2py.ir import module_from_dict

REPO = Path(__file__).resolve().parents[3]

requires_gfortran = pytest.mark.skipif(
    shutil.which("gfortran") is None, reason="gfortran is not installed"
)


def _require_numpy():
    try:
        import numpy  # noqa: F401
    except ImportError:
        pytest.skip("numpy is not installed, so f2py cannot build the extension")


# --- fixture compile_commands.json ------------------------------------------


def _fixture_compile_commands(tmp_path: Path, extra_flags=()) -> Path:
    """A minimal, synthetic compile_commands.json for the unit tests below.

    Modeled on the real entry shape f2py's meson backend writes (see the
    module docstring's trace): `directory`, `command`, `file`, `output`.
    """
    directory = tmp_path / "bbdir"
    directory.mkdir(parents=True, exist_ok=True)
    flags = ["-fvisibility=hidden", "-O3", *extra_flags]
    entry = {
        "directory": str(directory),
        "command": "gfortran " + " ".join(flags) + " -o mod.f90.o -c ../mod.f90",
        "file": "../mod.f90",
        "output": "mod.f90.o",
    }
    path = tmp_path / "compile_commands.json"
    path.write_text(json.dumps([entry]))
    return path


def test_refuses_when_extension_flags_contain_fast_math(tmp_path):
    compile_commands = _fixture_compile_commands(tmp_path, extra_flags=["-ffast-math"])
    with pytest.raises(buildinfo.UnsafeFlagError):
        driverbuild.compile_driver_tu(
            "program p\nend program p\n",
            compile_commands,
            "mod.f90",
            tmp_path / "work",
        )


def test_refuses_when_extension_flags_contain_ofast(tmp_path):
    compile_commands = _fixture_compile_commands(tmp_path, extra_flags=["-Ofast"])
    with pytest.raises(buildinfo.UnsafeFlagError):
        driverbuild.compile_driver_tu(
            "program p\nend program p\n",
            compile_commands,
            "mod.f90",
            tmp_path / "work",
        )


def test_refuses_when_driver_flags_diverge_from_extension_flags(tmp_path):
    """The other T4-mandated negative test: a driver TU whose own flags
    would codegen differently from the extension's must be refused, never
    silently compiled — a bitwise comparison built from mismatched flags
    measures the flags, not the binding (spec section 2.8)."""
    compile_commands = _fixture_compile_commands(tmp_path)  # extension carries -O3
    with pytest.raises(driverbuild.DriverFlagMismatchError):
        driverbuild.compile_driver_tu(
            "program p\nend program p\n",
            compile_commands,
            "mod.f90",
            tmp_path / "work",
            driver_flags=["-O2"],  # differs from the extension's -O3
        )


def test_matching_driver_flags_are_accepted_without_a_compiler(tmp_path, monkeypatch):
    """Flags that DO match (same codegen subset) must pass the precondition
    check and proceed to compile — verified by making the "compiler" a stub
    script so this stays compiler-toolchain-independent."""
    compile_commands = _fixture_compile_commands(tmp_path)
    # Rewrite the fixture's compiler to a stub that just touches its -o file,
    # so this test does not require gfortran to be installed.
    stub = tmp_path / "stub_cc.sh"
    stub.write_text("#!/bin/sh\nfor a in \"$@\"; do :; done\ntouch \"${@: -1}\"\n")
    stub.chmod(0o755)
    data = json.loads(compile_commands.read_text())
    data[0]["command"] = data[0]["command"].replace("gfortran", str(stub), 1)
    compile_commands.write_text(json.dumps(data))

    obj, argv, extracted, codegen = driverbuild.compile_driver_tu(
        "program p\nend program p\n",
        compile_commands,
        "mod.f90",
        tmp_path / "work",
        driver_flags=["-fvisibility=hidden", "-O3"],
    )
    assert codegen == ["-O3"]
    assert "-ffast-math" not in extracted
    assert obj.name == "n2p_oracle_driver.o"


def test_link_objects_for_sources_reads_recorded_output_never_compiles(tmp_path):
    compile_commands = _fixture_compile_commands(tmp_path)
    objects = driverbuild.link_objects_for_sources(compile_commands, ["mod.f90"])
    assert objects == [tmp_path / "bbdir" / "mod.f90.o"]


def test_link_driver_refuses_a_missing_object(tmp_path):
    driver_obj = tmp_path / "driver.o"
    driver_obj.write_bytes(b"")
    with pytest.raises(FileNotFoundError):
        driverbuild.link_driver(
            driver_obj, [tmp_path / "does_not_exist.o"], "gfortran", tmp_path
        )


# --- the real petro_api service, end to end ---------------------------------

FORTRAN_SOURCES = [
    "petro_api.f90",
    "flash.f",
    "hydrau.f",
    "matsol.f",
    "pvtcor.f",
    "relperm.f",
    "simcor.f",
    "wellib.f",
]
EXPANDED_DIR = REPO / "services" / "petro_api" / "native" / "_expanded"
PETRO_IR = REPO / "services" / "petro_api" / ".native2py" / "ir.json"
PETRO_GOLDEN = REPO / "services" / "petro_api" / "golden.json"


def _require_petro_fixture():
    if not all((EXPANDED_DIR / s).exists() for s in FORTRAN_SOURCES):
        pytest.skip("services/petro_api/native/_expanded is not present in this checkout")
    if not PETRO_IR.exists() or not PETRO_GOLDEN.exists():
        pytest.skip("services/petro_api IR/golden.json are not present in this checkout")


@pytest.fixture(scope="module")
def petro_extension_build(tmp_path_factory):
    """Build the real petro_api f2py/meson extension ONCE per test module,
    with a persisted `--build-dir`, so its `compile_commands.json` and
    object files survive the build (see driverbuild.py's module docstring
    for why `native2py build` itself does not leave these reachable)."""
    _require_petro_fixture()
    _require_numpy()
    if shutil.which("gfortran") is None:
        pytest.skip("gfortran is not installed")

    build_dir = tmp_path_factory.mktemp("petro_ext_build")
    sources = [EXPANDED_DIR / s for s in FORTRAN_SOURCES]
    compile_commands = driverbuild.build_extension_with_compile_commands(
        sources,
        "n2p_t4_petro",
        build_dir,
        only=[
            "pvt_set_fluid",
            "solution_gor",
            "oil_fvf",
            "oil_viscosity",
            "gas_z_factor",
            "bubble_point",
            "pvt_state",
            "tubing_bhp",
            "vogel_rate",
            "last_error",
        ],
    )
    return build_dir, compile_commands


@requires_gfortran
def test_build_and_run_the_real_petro_api_driver(petro_extension_build, tmp_path):
    _require_petro_fixture()
    build_dir, compile_commands = petro_extension_build

    module = module_from_dict(json.loads(PETRO_IR.read_text()))
    document = json.loads(PETRO_GOLDEN.read_text())
    driver = generate_driver(document, module)
    assert driver.source

    result = driverbuild.build_and_run_driver(
        driver.source,
        compile_commands,
        "petro_api.f90",
        FORTRAN_SOURCES,
        tmp_path,
    )

    assert result.returncode == 0
    assert len(result.driver_sha256) == 64
    assert result.driver_sha256 == driver.driver_sha256
    assert len(result.link_target_sha256) == 64
    assert "-ffast-math" not in result.extracted_flags

    # T2 parses every expected line, in the file's key order. Keys whose
    # entry produces no wire slots at all (e.g. `pvt_set_fluid` — void, no
    # intent(out)/intent(inout) arguments) legitimately never print, so the
    # expected order for `wire.parse_lines`'s ordering check is the subset
    # of golden.json's entries that DO have a slot to print, in file order —
    # mirroring T3's own test convention (test_fortran_driver.py).
    functions_by_name = {fn.name: fn for fn in module.functions}
    expected_slots_by_key: dict[str, set] = {}
    expected_order: list[str] = []
    for key, entry in (document.get("entries") or {}).items():
        if key in driver.skipped:
            continue
        fn = functions_by_name.get(entry.get("name") or key)
        if fn is None:
            continue
        slots = {str(s) for s in wire.slots_for_entry(entry, fn)}
        expected_slots_by_key[key] = slots
        if slots:
            expected_order.append(key)

    parsed = wire.parse_lines(result.stdout.splitlines(), expected_key_order=expected_order)
    assert parsed, "the driver printed nothing"

    for key, expected_slots in expected_slots_by_key.items():
        actual_slots = {str(s) for s in parsed.get(key, {})}
        assert expected_slots <= actual_slots, (
            f"{key}: driver did not print every expected slot "
            f"(missing {expected_slots - actual_slots})"
        )


@requires_gfortran
def test_no_recompile_the_compile_argv_names_only_the_driver_tu(petro_extension_build, tmp_path):
    """Spec section 2.3 / section 4 rule 5: the driver's own build never
    recompiles a library source. Assert this directly off the recorded
    compile command rather than trusting the docstring."""
    _require_petro_fixture()
    build_dir, compile_commands = petro_extension_build

    module = module_from_dict(json.loads(PETRO_IR.read_text()))
    document = json.loads(PETRO_GOLDEN.read_text())
    driver = generate_driver(document, module)

    result = driverbuild.build_and_run_driver(
        driver.source,
        compile_commands,
        "petro_api.f90",
        FORTRAN_SOURCES,
        tmp_path,
    )

    compiled_sources = [
        arg for arg in result.compile_argv if arg.endswith(".f90") or arg.endswith(".f")
    ]
    assert compiled_sources == [str(tmp_path / "n2p_oracle_driver.f90")], (
        "the driver's compile step must name only the generated driver TU, "
        f"never a library source: {result.compile_argv}"
    )

    # And the objects linked in are the EXTENSION's own recorded outputs —
    # never a path under the driver's own work directory.
    for obj in result.linked_objects:
        assert str(build_dir) in str(obj), (
            f"linked object {obj} does not come from the extension's own build "
            f"directory {build_dir} — the oracle would be measuring a second "
            "compilation, not the extension's object code"
        )


@requires_gfortran
def test_fast_math_in_the_real_extensions_flags_is_refused(petro_extension_build, tmp_path):
    """The negative test on a real (copied) flag record, not a synthetic
    fixture — copy the real compile_commands.json and inject -ffast-math
    into the one entry the driver would build against, then assert the
    whole pipeline refuses before compiling or linking anything."""
    _require_petro_fixture()
    build_dir, compile_commands = petro_extension_build

    module = module_from_dict(json.loads(PETRO_IR.read_text()))
    document = json.loads(PETRO_GOLDEN.read_text())
    driver = generate_driver(document, module)

    sabotaged = json.loads(compile_commands.read_text())
    for entry in sabotaged:
        if entry.get("file", "").endswith("petro_api.f90"):
            entry["command"] += " -ffast-math"
    sabotaged_path = tmp_path / "sabotaged_compile_commands.json"
    sabotaged_path.write_text(json.dumps(sabotaged))

    with pytest.raises(buildinfo.UnsafeFlagError):
        driverbuild.build_and_run_driver(
            driver.source,
            sabotaged_path,
            "petro_api.f90",
            FORTRAN_SOURCES,
            tmp_path,
        )
