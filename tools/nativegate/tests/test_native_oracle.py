"""The native oracle: is the re-hosted Python answer the *right* answer?

A golden file proves "unchanged since someone recorded it". That is a
different claim from "correct", and it is the weaker one: record a wrong
number and it will defend that wrong number forever. The only thing that
establishes correctness for a re-hosted engineering code is the code it was
re-hosted *from* — so this compiles the original sources with a plain
compiler, runs them, and compares what they print against what the generated
Python bindings return.

Three independent paths are built here from the same unmodified sources in
`libraries/petro`:

1. **Fortran, natively.** A driver program compiled by `gfortran` against
   `services/petro_api/native/petro_api.f90` and the seven fixed-form F77
   decks. No nativegate in the loop at all — this is what the library did
   before anyone tried to re-host it.
2. **C++, natively.** A driver compiled by `clang++` against
   `libraries/petro/cpp`, whose `FluidModel` reaches the same F77 routines
   through `FortranBridge.hpp`. Plus `cpp/examples/smoke.cpp` itself, which is
   the artifact the original findings were eyeballed against.
3. **Python.** The f2py extension, built the way nativegate builds it —
   through `preprocess.expand_includes` and
   `preprocess.resolve_kind_parameters`, which is where a re-host can quietly
   change a type, an intent or an argument order.

Then: (1) == (3), (2) == (3), and the values committed in
`services/petro_api/golden.json` == (1). That last one is what makes the
golden file evidence rather than an assertion — it says the committed
baseline is the native binary's answer, not merely a number some run
produced.

Running it needs a toolchain. Every fixture below skips with a specific
reason when one is missing, so a machine without `gfortran` gets a clean skip
rather than a red suite. See `libraries/petro/ORACLE.md` for how to run it
and what to do when it fails.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nativegate import golden
from nativegate.preprocess import expand_includes, resolve_kind_parameters

REPO = Path(__file__).resolve().parents[3]
LIBRARY = REPO / "libraries" / "petro"
FORTRAN_DIR = LIBRARY / "fortran"
INCLUDE_DIR = FORTRAN_DIR / "include"
CPP_DIR = LIBRARY / "cpp"
FACADE = REPO / "services" / "petro_api" / "native" / "petro_api.f90"
GOLDEN_JSON = REPO / "services" / "petro_api" / "golden.json"

# One fluid, one pressure, stated once and used by all three paths. A 35 API
# oil with 0.65 gravity gas at 180 F on Vazquez-Beggs, evaluated at 2000 psia
# — inside every correlation's stated range, and the same case the recorded
# golden file uses, so the two artifacts corroborate each other instead of
# testing different things.
API_GRAVITY = 35.0
GAS_GRAVITY = 0.65
TEMPERATURE_F = 180.0
CORRELATION = 2  # CORR_VAZQUEZ_BEGGS
PRESSURE = 2000.0
TARGET_GOR = 1000.0

# The oracle compares two builds on one machine, so the same libm serves
# both and only compiler and optimisation flags differ between them. That is
# tighter than the cross-platform case golden.json has to survive, but there
# is no reason to invent a second number: holding the oracle to the golden
# file's own default keeps one story about what "the same answer" means.
RTOL = golden.DEFAULT_RTOL
ATOL = golden.DEFAULT_ATOL

# props(1..9) of pvt_state, in the order the F90 facade documents.
PVT_STATE_FIELDS = (
    "bo", "bg", "bw", "rs", "mu_oil", "mu_gas", "rho_oil", "rho_gas", "z_factor",
)


# --- toolchain and source availability -----------------------------------


def _require(tool: str) -> str:
    found = shutil.which(tool)
    if found is None:
        pytest.skip(
            f"{tool} is not on PATH, so the native oracle cannot compile the "
            "original sources to compare against. Install a toolchain (see "
            "libraries/petro/ORACLE.md) to run this test."
        )
    return found


def _require_numpy():
    try:
        import numpy  # noqa: F401
    except ImportError:
        pytest.skip("numpy is not installed, so f2py cannot build the Python path")


def _require_library():
    missing = [p for p in (FORTRAN_DIR, CPP_DIR, FACADE) if not p.exists()]
    if missing:
        pytest.skip(
            "libraries/petro is not present in this checkout "
            f"(missing {', '.join(str(p) for p in missing)})"
        )


def _run(command, cwd: Path, what: str) -> str:
    completed = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        # A build failure is not a skip. The toolchain is present; something
        # about the sources or the generated wrapper is wrong, and that is
        # exactly what this test exists to notice.
        raise AssertionError(
            f"{what} failed ({completed.returncode}):\n"
            f"  $ {' '.join(str(c) for c in command)}\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return completed.stdout


def _parse_named_values(text: str) -> dict:
    """`name value` lines from a driver, as floats."""
    values = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s+([-+0-9.EeDd]+)\s*$", line)
        if match:
            values[match.group(1)] = float(match.group(2).replace("D", "E"))
    return values


# --- the three builds ----------------------------------------------------


FORTRAN_DRIVER = f"""
program oracle
    use petro_api
    implicit none
    ! No `dp` of its own: petro_api exports one, and a second would be a
    ! name clash rather than a definition.
    double precision :: props(9)
    integer :: i
    character(len=8) :: names(9)
    data names /'bo','bg','bw','rs','mu_oil','mu_gas','rho_oil','rho_gas','z_factor'/

    call pvt_set_fluid({API_GRAVITY}d0, {GAS_GRAVITY}d0, {TEMPERATURE_F}d0, {CORRELATION})
    write (*,'(A,1X,ES24.17)') 'solution_gor ', solution_gor({PRESSURE}d0)
    write (*,'(A,1X,ES24.17)') 'oil_fvf      ', oil_fvf({PRESSURE}d0)
    write (*,'(A,1X,ES24.17)') 'oil_viscosity', oil_viscosity({PRESSURE}d0)
    write (*,'(A,1X,ES24.17)') 'gas_z_factor ', gas_z_factor({PRESSURE}d0)
    write (*,'(A,1X,ES24.17)') 'bubble_point ', bubble_point({TARGET_GOR}d0)
    write (*,'(A,1X,ES24.17)') 'vogel_rate   ', vogel_rate(4000.0d0, 2500.0d0, 6000.0d0)
    write (*,'(A,1X,ES24.17)') 'tubing_bhp   ', tubing_bhp(250.0d0, 2000.0d0, &
        350.0d0, 1200.0d0, 0.2010d0, 0.0006d0, 8000.0d0, 8400.0d0, 20)

    props = 0.0d0
    call pvt_state({PRESSURE}d0, props, 9)
    do i = 1, 9
        write (*,'(A,1X,ES24.17)') 'state_'//trim(names(i)), props(i)
    end do
end program oracle
"""

CPP_DRIVER = f"""
#include "FluidModel.hpp"
#include <stdio.h>

int main()
{{
    FluidModel fluid({API_GRAVITY}, {GAS_GRAVITY}, {TEMPERATURE_F}, {CORRELATION});
    printf("solution_gor %.17g\\n", fluid.solution_gor({PRESSURE}));
    printf("oil_fvf %.17g\\n",      fluid.oil_fvf({PRESSURE}));
    printf("oil_viscosity %.17g\\n", fluid.oil_viscosity({PRESSURE}));
    printf("gas_z_factor %.17g\\n", fluid.z_factor({PRESSURE}));
    printf("bubble_point %.17g\\n", fluid.bubble_point());
    PvtState s = fluid.properties_at({PRESSURE});
    printf("state_bo %.17g\\n", s.bo);
    printf("state_bg %.17g\\n", s.bg);
    printf("state_bw %.17g\\n", s.bw);
    printf("state_rs %.17g\\n", s.rs);
    printf("state_mu_oil %.17g\\n", s.mu_oil);
    printf("state_mu_gas %.17g\\n", s.mu_gas);
    printf("state_rho_oil %.17g\\n", s.rho_oil);
    printf("state_rho_gas %.17g\\n", s.rho_gas);
    printf("state_z_factor %.17g\\n", s.z_factor);
    return 0;
}}
"""


@pytest.fixture(scope="session")
def workspace(tmp_path_factory) -> Path:
    """The F77 decks, INCLUDEs expanded, ready for both compilers."""
    _require_library()
    _require("gfortran")
    directory = tmp_path_factory.mktemp("native_oracle")
    for deck in sorted(FORTRAN_DIR.glob("*.f")):
        (directory / deck.name).write_text(expand_includes(deck, [INCLUDE_DIR]))
    return directory


@pytest.fixture(scope="session")
def fortran_objects(workspace: Path) -> list:
    """The F77 decks as ordinary object files — no nativegate anywhere."""
    gfortran = _require("gfortran")
    decks = sorted(p.name for p in workspace.glob("*.f"))
    _run([gfortran, "-c", "-O2", *decks], workspace, "compiling the F77 decks")
    return [workspace / f"{Path(d).stem}.o" for d in decks]


@pytest.fixture(scope="session")
def fortran_native(workspace: Path, fortran_objects) -> dict:
    """What the original Fortran prints, compiled by gfortran directly.

    Note that this compiles `native/petro_api.f90` — the *unmodified* facade,
    `real(dp)` and all. Only the f2py path needs it rewritten, and building
    the oracle from the rewritten copy would be comparing nativegate's output
    against itself.
    """
    gfortran = _require("gfortran")
    (workspace / "petro_api.f90").write_text(FACADE.read_text())
    (workspace / "oracle_driver.f90").write_text(FORTRAN_DRIVER)
    _run(
        [gfortran, "-O2", "-o", "oracle_driver", "petro_api.f90",
         "oracle_driver.f90", *[o.name for o in fortran_objects]],
        workspace,
        "linking the Fortran oracle driver",
    )
    return _parse_named_values(
        _run([str(workspace / "oracle_driver")], workspace, "running the Fortran oracle driver")
    )


@pytest.fixture(scope="session")
def cpp_native(workspace: Path, fortran_objects) -> dict:
    """What the original C++ prints, compiled by clang++ (or g++) directly.

    `FluidModel` calls into the same F77 routines through `FortranBridge.hpp`,
    so this is a second independent caller of the same arithmetic — and the
    one the 1994 C++ layer's users actually saw.
    """
    compiler = shutil.which("clang++") or _require("g++")
    gfortran = _require("gfortran")
    (workspace / "cpp_driver.cpp").write_text(CPP_DRIVER)
    runtime = Path(
        subprocess.run(
            [gfortran, "-print-file-name=libgfortran.dylib"
             if sys.platform == "darwin" else "-print-file-name=libgfortran.so"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
    ).parent
    _run(
        [compiler, "-O2", "-w", f"-I{CPP_DIR / 'include'}", "cpp_driver.cpp",
         str(CPP_DIR / "src" / "FluidModel.cpp"), *[o.name for o in fortran_objects],
         f"-L{runtime}", "-lgfortran", "-o", "cpp_driver"],
        workspace,
        "linking the C++ oracle driver",
    )
    return _parse_named_values(
        _run([str(workspace / "cpp_driver")], workspace, "running the C++ oracle driver")
    )


@pytest.fixture(scope="session")
def python_bindings(workspace: Path):
    """The generated Python path: f2py, built the way nativegate builds it."""
    _require_numpy()
    _require("gfortran")
    build = workspace / "python"
    build.mkdir(exist_ok=True)
    for deck in sorted(workspace.glob("*.f")):
        shutil.copy(deck, build / deck.name)
    # The two source rewrites nativegate performs, and nothing else. If either
    # of them changes a number, this test is where it shows up.
    (build / "petro_api.f90").write_text(
        resolve_kind_parameters(expand_includes(FACADE, [INCLUDE_DIR]))
    )
    # `--backend meson` for the same reason generators/f2py_gen.py passes it:
    # f2py's default backend depends on the Python version, and the distutils
    # one is broken against modern setuptools. The oracle only means something
    # if it builds the way a generated service builds, so the flag belongs on
    # both or neither.
    _run(
        [sys.executable, "-m", "numpy.f2py", "-c", "--backend", "meson",
         "-m", "petro_oracle",
         "petro_api.f90", *[p.name for p in sorted(build.glob("*.f"))]],
        build,
        "building the f2py extension",
    )

    sys.path.insert(0, str(build))
    try:
        import petro_oracle  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    return petro_oracle.petro_api


@pytest.fixture(scope="session")
def python_values(python_bindings) -> dict:
    """The same quantities, through the generated bindings."""
    import numpy

    package = python_bindings
    package.pvt_set_fluid(API_GRAVITY, GAS_GRAVITY, TEMPERATURE_F, CORRELATION)
    values = {
        "solution_gor": package.solution_gor(PRESSURE),
        "oil_fvf": package.oil_fvf(PRESSURE),
        "oil_viscosity": package.oil_viscosity(PRESSURE),
        "gas_z_factor": package.gas_z_factor(PRESSURE),
        "bubble_point": package.bubble_point(TARGET_GOR),
        "vogel_rate": package.vogel_rate(4000.0, 2500.0, 6000.0),
        "tubing_bhp": package.tubing_bhp(
            250.0, 2000.0, 350.0, 1200.0, 0.2010, 0.0006, 8000.0, 8400.0, 20
        ),
    }
    props = numpy.zeros(9)
    package.pvt_state(PRESSURE, props, 9)
    values.update({f"state_{name}": props[i] for i, name in enumerate(PVT_STATE_FIELDS)})
    return values


# --- the comparisons -----------------------------------------------------


def _agree(name: str, expected: float, actual: float, left: str, right: str):
    assert expected == pytest.approx(actual, rel=RTOL, abs=ATOL), (
        f"{name}: {left} says {expected!r}, {right} says {actual!r} "
        f"(rtol={RTOL}, atol={ATOL})"
    )


FORTRAN_QUANTITIES = (
    "solution_gor", "oil_fvf", "oil_viscosity", "gas_z_factor",
    "bubble_point", "vogel_rate", "tubing_bhp",
)


@pytest.mark.parametrize("quantity", FORTRAN_QUANTITIES)
def test_python_matches_the_native_fortran_binary(quantity, fortran_native, python_values):
    assert quantity in fortran_native, f"the Fortran driver printed no {quantity}"
    _agree(
        quantity, fortran_native[quantity], python_values[quantity],
        "the gfortran binary", "the generated Python bindings",
    )


@pytest.mark.parametrize("field", PVT_STATE_FIELDS)
def test_pvt_state_array_matches_the_native_fortran_binary(field, fortran_native, python_values):
    # pvt_state writes its answer into the caller's array and returns None.
    # This is the one entry point where "the return value is unchanged" would
    # be a comparison of None against None.
    key = f"state_{field}"
    _agree(
        key, fortran_native[key], python_values[key],
        "the gfortran binary", "the generated Python bindings",
    )


CPP_QUANTITIES = (
    "solution_gor", "oil_fvf", "oil_viscosity", "gas_z_factor", "bubble_point",
)


@pytest.mark.parametrize("quantity", CPP_QUANTITIES)
def test_python_matches_the_native_cpp_binary(quantity, cpp_native, python_values):
    # bubble_point is comparable only because PVTINI caches
    # `PB = PVTBUB(1000.0D0)` (pvtcor.f:75) and `FluidModel::bubble_point()`
    # returns that cached value — so the C++ answer is the facade's
    # `bubble_point(1000.0)` and TARGET_GOR is 1000 for that reason.
    _agree(
        quantity, cpp_native[quantity], python_values[quantity],
        "the clang++ binary", "the generated Python bindings",
    )


@pytest.mark.parametrize("field", PVT_STATE_FIELDS)
def test_pvt_state_array_matches_the_native_cpp_binary(field, cpp_native, python_values):
    key = f"state_{field}"
    _agree(
        key, cpp_native[key], python_values[key],
        "the clang++ binary", "the generated Python bindings",
    )


def test_the_two_native_languages_agree_with_each_other(fortran_native, cpp_native):
    # If they disagree, the disagreement is in the legacy library, not in
    # nativegate — and every comparison above is measuring the wrong thing.
    shared = sorted(set(fortran_native) & set(cpp_native))
    assert shared, "the two drivers have no quantity in common"
    for key in shared:
        _agree(key, fortran_native[key], cpp_native[key],
               "the gfortran binary", "the clang++ binary")


def test_the_committed_golden_file_matches_the_native_binary(fortran_native):
    """The load-bearing one.

    `golden.json` is the file CI compares against forever. It is worth
    exactly as much as the run that recorded it — so this checks the
    committed numbers against a freshly compiled native binary, which is what
    turns "someone recorded this once" into "you can re-derive it".
    """
    if not GOLDEN_JSON.exists():
        pytest.skip(
            f"no {GOLDEN_JSON} recorded yet — "
            "run `ngate golden record petro_api` against a built extension"
        )
    document = json.loads(GOLDEN_JSON.read_text())
    entries = document.get("entries") or {}

    checked = []
    differences = []
    for quantity in FORTRAN_QUANTITIES:
        entry = entries.get(quantity)
        if entry is None or entry.get("result") is None:
            continue
        rtol, atol = golden.entry_tolerance(document, entry)
        native = fortran_native[quantity]
        recorded = entry["result"]
        checked.append(quantity)
        if recorded != pytest.approx(native, rel=rtol, abs=atol):
            differences.append(
                f"{quantity}: golden.json records {recorded!r}, the native "
                f"binary computes {native!r} (rtol={rtol})"
            )

    # And the in-place array output, which is where a recorded `null` would
    # have hidden nine wrong numbers.
    state = entries.get("pvt_state") or {}
    for index, recorded_array in (state.get("argument_effects") or {}).items():
        rtol, atol = golden.entry_tolerance(document, state)
        for position, field in enumerate(PVT_STATE_FIELDS):
            checked.append(f"pvt_state.{field}")
            native = fortran_native[f"state_{field}"]
            if recorded_array[position] != pytest.approx(native, rel=rtol, abs=atol):
                differences.append(
                    f"pvt_state props({position + 1}) [{field}]: golden.json "
                    f"records {recorded_array[position]!r}, the native binary "
                    f"computes {native!r}"
                )

    assert not differences, (
        "the committed golden values are not what the native binary computes:\n  "
        + "\n  ".join(differences)
    )
    assert len(checked) >= 10, (
        "the oracle only cross-checked "
        f"{len(checked)} recorded value(s) — that is not a meaningful audit"
    )


def test_the_shipped_smoke_example_still_reproduces_its_documented_values(
    workspace, fortran_objects, python_values
):
    """`cpp/examples/smoke.cpp` is the artifact the original findings quote.

    It prints to two decimals, so this cannot be a tight numerical check —
    but it is the thing a reviewer will run by hand, and it exercises the
    whole C++ layer (Simulator, WellModel, DeckReader) rather than just the
    correlations. If it stops building or stops agreeing, the documented
    evidence in FINDINGS.md has gone stale.
    """
    example = CPP_DIR / "examples" / "smoke.cpp"
    if not example.exists():
        pytest.skip(f"{example} is not present in this checkout")
    compiler = shutil.which("clang++") or _require("g++")
    gfortran = _require("gfortran")
    runtime = Path(
        subprocess.run(
            [gfortran, "-print-file-name=libgfortran.dylib"
             if sys.platform == "darwin" else "-print-file-name=libgfortran.so"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
    ).parent
    _run(
        [compiler, "-O2", "-w", f"-I{CPP_DIR / 'include'}", str(example),
         *[str(p) for p in sorted((CPP_DIR / "src").glob("*.cpp"))],
         *[o.name for o in fortran_objects], f"-L{runtime}", "-lgfortran",
         "-o", "smoke"],
        workspace,
        "linking cpp/examples/smoke.cpp",
    )
    output = _run([str(workspace / "smoke")], workspace, "running smoke")

    printed = dict(
        re.findall(r"^(\S+)\s*=\s*([-+0-9.eE]+)", output, flags=re.MULTILINE)
    )
    assert printed, f"smoke.cpp printed nothing parseable:\n{output}"

    # Two decimals printed, so compare at the precision it actually offers.
    assert float(printed["Rs(2000)"]) == pytest.approx(
        python_values["solution_gor"], abs=5e-3
    )
    assert float(printed["Bo(2000)"]) == pytest.approx(python_values["oil_fvf"], abs=5e-5)
    assert float(printed["Z(2000)"]) == pytest.approx(
        python_values["gas_z_factor"], abs=5e-5
    )
    assert float(printed["IPR@2500"]) == pytest.approx(
        python_values["vogel_rate"], abs=5e-3
    )


def test_the_comparison_would_notice_a_wrong_answer(fortran_native):
    # An oracle that cannot fail is decoration. A part-per-million error in a
    # solution GOR — far below anything an engineer would see in a report —
    # has to be caught, or none of the agreements above mean anything.
    reference = fortran_native["solution_gor"]
    with pytest.raises(AssertionError):
        _agree("solution_gor", reference, reference * (1.0 + 1e-6), "native", "python")


def test_the_oracle_skips_rather_than_fails_without_a_toolchain(monkeypatch):
    """The skip path is itself a promise, so it is tested.

    A test that turns into a hard failure on a laptop without gfortran gets
    deleted from CI within a week, and then nothing checks the numbers at all.
    """
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(pytest.skip.Exception) as raised:
        _require("gfortran")
    assert "gfortran" in str(raised.value)
    assert "ORACLE.md" in str(raised.value)


def test_the_oracle_uses_inputs_inside_the_correlations_valid_range():
    # A 1.0 API gravity is not a fluid. Pinning behaviour at inputs no
    # correlation is defined for records an extrapolation, and an
    # extrapolation is exactly where two libms are most likely to disagree.
    assert 10.0 <= API_GRAVITY <= 60.0
    assert 0.55 <= GAS_GRAVITY <= 1.2
    assert 60.0 <= TEMPERATURE_F <= 300.0
    assert 14.7 <= PRESSURE <= 10000.0
    assert CORRELATION in (1, 2, 3)
