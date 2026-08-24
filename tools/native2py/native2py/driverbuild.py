"""T4 — turn a generated oracle driver source into a runnable binary.

Spec `design-verification-layers.md` section 2.3 ("one set of object code,
not two compilations of one source"), section 2.8 (hard preconditions),
section 4 rules 2-3, 5, 8. Read those, plus `buildinfo.py` (T1) and
`drivers/fortran.py` (T3), before changing anything here.

What this module does NOT do, on purpose:

* It never recompiles a library source. The whole bitwise claim in section
  2.3 rests on the driver and the extension executing the *identical*
  machine code for the library routines — a second compilation of the same
  `.f90`/`.f` files, even with identical flags, is a second body of object
  code the compiler is free to have inlined or contracted differently.
  `link_objects_for_sources` only ever *reads* a `compile_commands.json`
  entry's already-recorded `output` path; it does not invoke a compiler.
* It never widens the safety gate into a warning. `buildinfo.refuse_unsafe`
  is called before anything is compiled, and a mismatch between the driver's
  own codegen-affecting flags and the extension's is `DriverFlagMismatchError`
  — both hard errors (spec section 2.8, section 4 rule 2).
* It never guesses at threading. Every subprocess this module runs — the
  driver TU compile is a single-threaded compiler invocation, so that one is
  unaffected, but the *driver executable* — runs under
  `buildinfo.pinned_environment()` (spec section 4 rule 3).

What "the built library objects" means here, concretely: for the Fortran/
f2py-via-meson path this repository actually uses (`services/petro_api`),
`compile_commands.json`'s per-source `output` field is the `.o` file meson's
ninja backend wrote for that source — already `-fPIC` (meson builds every
source of a shared-library/extension target position-independent), and
linking `-fPIC` objects into a plain executable is legal everywhere this
runs (spec section 2.3's stated consequence). This module links exactly
those files, in the order the caller names their sources, plus the freshly
compiled driver object — nothing else.

**A build-pipeline gap this module used to work around, now fixed for the
Fortran path:** `native2py build <name>` runs `pip wheel .`, which drives
scikit-build-core → CMake → the generated `add_custom_command` that shells
out to `python -m numpy.f2py -c --backend meson ...` (see
`generators/f2py_gen.py`). That command now passes an explicit
`--build-dir "${CMAKE_CURRENT_SOURCE_DIR}/.native2py/build"`, so f2py no
longer falls back to `tempfile.mkdtemp()` for its meson build directory (see
`numpy/f2py/f2py2e.py:run_compile`) and the build tree survives at a fixed,
documented location: `services/<name>/.native2py/build/bbdir/compile_commands.json`,
with the corresponding `.o` files alongside it. After a real
`native2py build petro_api`, that `compile_commands.json` is directly
loadable by `buildinfo.load_compile_commands` and usable by this module's
`link_objects_for_sources`/`build_and_run_driver` without any workaround.
The analogous C++/`cmake_gen.py` gap (`CMAKE_EXPORT_COMPILE_COMMANDS`
pointing into a throwaway CMake build tree rather than a pinned one) is a
separate, still-open gap — out of scope here.
`build_extension_with_compile_commands` below is kept as a
test/CI-isolation helper: it reproduces the same f2py invocation
`CMakeLists.txt` now generates (including the `--build-dir` flag) but lets a
test hand it its own throwaway `build_dir` instead of writing into a real
service's `.native2py/` tree, so tests don't mutate checked-out fixture
directories. It performs the library's one and only compilation — the
driver then links its outputs — so it does not violate "never recompile the
library sources"; it exists purely for test isolation now, not to route
around a missing location.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from . import buildinfo

__all__ = [
    "DriverBuildResult",
    "DriverFlagMismatchError",
    "DriverRunError",
    "link_objects_for_sources",
    "compile_driver_tu",
    "link_driver",
    "run_driver",
    "build_and_run_driver",
    "build_extension_with_compile_commands",
    "build_cxx_extension_with_compile_commands",
]


class DriverFlagMismatchError(RuntimeError):
    """The driver TU's codegen-affecting flags differ from the extension's.

    Spec section 2.8: "The driver translation unit's flags differ from the
    extension's extracted flags in any way that affects code generation." A
    hard refusal, not a warning — a divergence here means a subsequent
    bitwise mismatch would measure the flags, not the binding.
    """


class DriverRunError(RuntimeError):
    """The compiled driver exited non-zero. Message includes stderr (T4 §3)."""


@dataclass
class DriverBuildResult:
    """Provenance and output of one driver build+run (T4 §4)."""

    driver_sha256: str
    link_target_sha256: str
    extracted_flags: list[str]
    codegen_flags: list[str]
    compile_argv: list[str]
    link_argv: list[str]
    stdout: str
    stderr: str
    returncode: int
    executable: Path
    driver_object: Path
    linked_objects: list[Path] = field(default_factory=list)


# --- locating the extension's own built objects (never recompiled) --------


def link_objects_for_sources(
    compile_commands_path: Path, source_names: Sequence[str]
) -> list[Path]:
    """The extension's own built object files for `source_names`, in order.

    Reads each source's `compile_commands.json` entry and resolves its
    recorded `output` path against `directory` — the object file the
    extension's *own* build already produced. This function never invokes a
    compiler; if an entry has no `output` field (a `command`-form entry that
    used `-o` instead — `buildinfo.flags_for_source` strips that pair rather
    than exposing it, so this reads the raw entry directly), that source's
    object cannot be located and a `KeyError` says so by name rather than
    silently skipping it out of the link line.
    """
    commands = buildinfo.load_compile_commands(Path(compile_commands_path))
    objects: list[Path] = []
    for name in source_names:
        entry = buildinfo.find_entry(commands, name)
        directory = Path(entry.get("directory") or Path(compile_commands_path).parent)
        output = entry.get("output")
        if not output:
            output = _output_from_argv(entry)
        if not output:
            raise KeyError(
                f"compile command for {name!r} has no 'output' field and no "
                "'-o' argument — cannot locate its built object file"
            )
        objects.append((directory / output).resolve())
    return objects


def _output_from_argv(entry: dict) -> str | None:
    # buildinfo.entry_argv is the single shared implementation of "turn a
    # compile_commands.json entry's arguments/command into an argv list" —
    # this module used to keep its own private copy; see buildinfo.py's
    # entry_argv docstring.
    argv = buildinfo.entry_argv(entry)
    for i, token in enumerate(argv):
        if token == "-o" and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _compiler_executable(entry: dict) -> str:
    """argv[0] of the entry's own compile command — the same compiler, not
    whatever happens to be first on PATH."""
    return buildinfo.entry_argv(entry)[0]


def _module_search_flags(flags: Sequence[str], base_dir: Path) -> list[str]:
    """`-I`/`-J` tokens from the extension's flags, as search-only, absolute `-I`s.

    The driver's `use <module>` needs to find the `.mod` file the
    extension's own compile wrote (gfortran's `-J<dir>` sets both the write
    location and a search location; `-Idir` is already search-only). This is
    a *build-mechanics* necessity, not a codegen-affecting flag — it does
    not change what machine code the compiler emits for a given AST, only
    where it looks up module interfaces — so it is deliberately excluded
    from `buildinfo.codegen_flags` and from the driver/extension divergence
    check, and is added on top of the codegen subset instead.

    `compile_commands.json` entries record paths relative to their own
    `directory` field, and this module compiles the driver in a different
    working directory (its own `work_dir`), so every path is resolved
    against `base_dir` (the extension entry's `directory`) before use —
    otherwise a relative `-Ifoo.so.p` silently resolves against the wrong
    cwd and the `.mod` file is "not found" even though it exists.
    """
    out: list[str] = []
    for flag in flags:
        if flag.startswith("-J"):
            path = flag[2:]
            out.append("-I" + str((base_dir / path).resolve()))
        elif flag.startswith("-I"):
            path = flag[2:]
            out.append("-I" + str((base_dir / path).resolve()))
    return out


# --- compiling the driver TU only ------------------------------------------

# Source suffix for the driver TU, by language — the only thing that used to
# be hardcoded to Fortran here. Everything else (which compiler to invoke,
# which flags to use) already came from the extension's own
# compile_commands.json entry, so it was already language-agnostic.
_DRIVER_SUFFIX = {"fortran": ".f90", "cpp": ".cpp"}


def compile_driver_tu(
    driver_source: str,
    compile_commands_path: Path,
    extension_source_name: str,
    work_dir: Path,
    *,
    driver_flags: Sequence[str] | None = None,
    language: str = "fortran",
) -> tuple[Path, list[str], list[str], list[str]]:
    """Compile ONLY the driver translation unit.

    Never touches a library source. Returns
    `(driver_object_path, compile_argv, extracted_flags, codegen_flags)`.

    `language` selects the driver TU's source suffix/filename —
    `"fortran"` (default, `.f90`, unchanged behavior) or `"cpp"` (`.cpp`).
    It does NOT select which compiler runs: the compiler is always
    `argv[0]` of the extension's own compile_commands.json entry for
    `extension_source_name` — the same compiler (and, via
    `driver_flags`/`use_codegen` below, the same codegen-affecting flags)
    that built the extension, whether that happens to be gfortran, clang++,
    or g++. This is what keeps the driver and the extension's objects one
    body of machine code (spec section 2.3) regardless of source language.

    Preconditions enforced here (spec section 2.8), both hard errors:

    * `buildinfo.refuse_unsafe` on the extension's full extracted flags.
    * if `driver_flags` is given, its codegen-affecting subset must equal
      the extension's codegen-affecting subset exactly (order-sensitive) —
      otherwise `DriverFlagMismatchError`. When omitted, the extension's own
      codegen flags are used for the driver, so this can never diverge by
      construction; `driver_flags` exists so a caller (and this module's own
      negative test) can demonstrate the refusal.
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    suffix = _DRIVER_SUFFIX.get(language)
    if suffix is None:
        raise ValueError(
            f"unknown driver language {language!r} — expected one of "
            f"{sorted(_DRIVER_SUFFIX)}"
        )

    commands = buildinfo.load_compile_commands(Path(compile_commands_path))
    ext_entry = buildinfo.find_entry(commands, extension_source_name)
    extracted_flags = buildinfo.flags_for_source(commands, extension_source_name)

    buildinfo.refuse_unsafe(extracted_flags)

    ext_codegen = buildinfo.codegen_flags(extracted_flags)

    if driver_flags is not None:
        given_codegen = buildinfo.codegen_flags(list(driver_flags))
        if given_codegen != ext_codegen:
            raise DriverFlagMismatchError(
                "refusing to build the oracle driver: its codegen-affecting "
                f"flags {given_codegen} differ from the extension's own "
                f"{ext_codegen} (design-verification-layers.md section 2.8) "
                "— a bitwise comparison built from mismatched flags would "
                "measure the flags, not the binding"
            )
        use_codegen = given_codegen
    else:
        use_codegen = ext_codegen

    compiler = _compiler_executable(ext_entry)
    ext_directory = Path(ext_entry.get("directory") or Path(compile_commands_path).parent)
    search_flags = _module_search_flags(extracted_flags, ext_directory)

    driver_path = work_dir / f"n2p_oracle_driver{suffix}"
    driver_path.write_text(driver_source)

    obj_path = work_dir / "n2p_oracle_driver.o"
    argv = [compiler, *use_codegen, *search_flags, "-c", str(driver_path), "-o", str(obj_path)]

    completed = subprocess.run(argv, cwd=work_dir, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise DriverRunError(
            f"compiling the oracle driver TU failed ({completed.returncode}):\n"
            f"  $ {' '.join(argv)}\n{completed.stdout}\n{completed.stderr}"
        )

    return obj_path, argv, extracted_flags, use_codegen


# --- linking ----------------------------------------------------------------


def link_driver(
    driver_object: Path,
    link_objects: Sequence[Path],
    compiler: str,
    work_dir: Path,
    *,
    extra_link_args: Sequence[str] = (),
) -> tuple[Path, list[str]]:
    """Link the driver object against the extension's own built objects.

    Nothing here compiles anything — every path in `link_objects` must
    already exist (`link_objects_for_sources`'s job) — so this function
    cannot accidentally become a second compilation of library sources.
    """
    work_dir = Path(work_dir)
    for obj in link_objects:
        if not Path(obj).exists():
            raise FileNotFoundError(
                f"link object {obj} does not exist — it must be the extension's "
                "own build output, never something this module compiles"
            )
    exe_path = work_dir / ("n2p_oracle_driver.exe" if os.name == "nt" else "n2p_oracle_driver")
    argv = [
        compiler,
        str(driver_object),
        *[str(p) for p in link_objects],
        *extra_link_args,
        "-o",
        str(exe_path),
    ]
    completed = subprocess.run(argv, cwd=work_dir, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise DriverRunError(
            f"linking the oracle driver failed ({completed.returncode}):\n"
            f"  $ {' '.join(argv)}\n{completed.stdout}\n{completed.stderr}"
        )
    return exe_path, argv


# --- running -----------------------------------------------------------------


def run_driver(executable: Path, work_dir: Path) -> subprocess.CompletedProcess:
    """Run the driver under `buildinfo.pinned_environment()`, capture both
    streams. Non-zero exit is a hard failure whose message includes stderr
    (T4 §3) — raised by the caller, not here, so callers that want the raw
    `CompletedProcess` (e.g. to also assert stdout parses) can still get it.
    """
    env = dict(os.environ)
    env.update(buildinfo.pinned_environment())
    return subprocess.run(
        [str(executable)], cwd=work_dir, capture_output=True, text=True, check=False, env=env
    )


# --- the whole T4 pipeline in one call --------------------------------------


def build_and_run_driver(
    driver_source: str,
    compile_commands_path: Path,
    extension_source_name: str,
    link_source_names: Sequence[str],
    work_dir: Path,
    *,
    driver_flags: Sequence[str] | None = None,
    extra_link_args: Sequence[str] = (),
    language: str = "fortran",
) -> DriverBuildResult:
    """Compile the driver TU, link it against the extension's own built
    objects, run it under the pinned environment, and report provenance.

    `language` — `"fortran"` (default) or `"cpp"` — selects the driver TU's
    source suffix; see `compile_driver_tu` for what it does and does not
    control.

    Raises `buildinfo.UnsafeFlagError` (fast-math in the extension's flags),
    `DriverFlagMismatchError` (driver/extension codegen flags diverge), or
    `DriverRunError` (compile, link, or a non-zero run — every message
    includes the captured stderr).
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    driver_object, compile_argv, extracted_flags, used_codegen = compile_driver_tu(
        driver_source,
        compile_commands_path,
        extension_source_name,
        work_dir,
        driver_flags=driver_flags,
        language=language,
    )

    link_objects = link_objects_for_sources(compile_commands_path, link_source_names)

    commands = buildinfo.load_compile_commands(Path(compile_commands_path))
    ext_entry = buildinfo.find_entry(commands, extension_source_name)
    compiler = _compiler_executable(ext_entry)

    executable, link_argv = link_driver(
        driver_object, link_objects, compiler, work_dir, extra_link_args=extra_link_args
    )

    completed = run_driver(executable, work_dir)
    if completed.returncode != 0:
        raise DriverRunError(
            f"the oracle driver exited {completed.returncode}:\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )

    driver_sha256 = hashlib.sha256(driver_source.encode("utf-8")).hexdigest()
    link_target_sha256 = buildinfo.link_target_hash(link_objects)

    return DriverBuildResult(
        driver_sha256=driver_sha256,
        link_target_sha256=link_target_sha256,
        extracted_flags=extracted_flags,
        codegen_flags=used_codegen,
        compile_argv=compile_argv,
        link_argv=link_argv,
        stdout=completed.stdout,
        stderr=completed.stderr,
        returncode=completed.returncode,
        executable=executable,
        driver_object=driver_object,
        linked_objects=link_objects,
    )


# --- test/CI fixture helper: produce a *locatable* build ---------------------


def build_extension_with_compile_commands(
    sources: Sequence[Path],
    module_name: str,
    build_dir: Path,
    *,
    only: Sequence[str] | None = None,
) -> Path:
    """Build the f2py/meson extension the way `CMakeLists.txt` does, but with
    an explicit `--build-dir` so `compile_commands.json` and the object files
    persist afterwards (see the module docstring's "build-pipeline gap").

    This performs the library sources' ONE compilation — the same one a real
    `native2py build` would have performed, had its generated CMake pinned a
    location — so a driver subsequently linking the resulting objects is not
    looking at a second body of machine code. It is not part of T4's own
    compile/link/run contract (`build_and_run_driver` above never calls
    this); it exists so tests and CI can hand `build_and_run_driver` a real,
    locatable extension build without reaching into a throwaway temp
    directory numpy.f2py orphaned.

    Returns the path to `<build_dir>/bbdir/compile_commands.json`.
    """
    build_dir = Path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable,
        "-m",
        "numpy.f2py",
        "-c",
        "--backend",
        "meson",
        "--build-dir",
        str(build_dir),
        "-m",
        module_name,
        *[str(s) for s in sources],
    ]
    if only:
        argv.extend(["only:", *only, ":"])
    # f2py's meson backend both (a) writes its generated wrapper sources
    # into the current working directory before copying them into
    # build_dir (so cwd cannot BE build_dir — that copy collides with
    # itself, "are the same file") and (b) moves the finished extension to
    # cwd when the build completes ("move exec to root"). Use a cwd that is
    # a sibling of build_dir, still under this function's caller-supplied
    # directory, so nothing lands in the repository root.
    run_cwd = build_dir.parent / f"{build_dir.name}-f2py-cwd"
    run_cwd.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(argv, cwd=run_cwd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise DriverRunError(
            f"building the f2py extension failed ({completed.returncode}):\n"
            f"  $ {' '.join(argv)}\n{completed.stdout}\n{completed.stderr}"
        )
    compile_commands = build_dir / "bbdir" / "compile_commands.json"
    if not compile_commands.exists():
        raise FileNotFoundError(
            f"expected {compile_commands} after building the extension, found nothing "
            "(meson's build directory layout may have changed)"
        )
    return compile_commands


def build_cxx_extension_with_compile_commands(
    module,
    header_names: str | list[str],
    native_sources: Sequence[Path],
    module_name: str,
    build_dir: Path,
) -> Path:
    """The C++/pybind11 analogue of `build_extension_with_compile_commands`.

    Generates a pybind11 bindings TU (`generators.pybind_gen`) and a
    `CMakeLists.txt` (`generators.cmake_gen` — already emits
    `CMAKE_EXPORT_COMPILE_COMMANDS ON`, see that module's docstring) for
    `module`/`native_sources`, then configures and builds them with an
    explicit `-B build_dir`, so — unlike a real `native2py build`'s `pip
    wheel .` (which drives scikit-build-core into a throwaway, pip-managed
    build tree; see this module's docstring for the analogous, still-open
    f2py-era gap on the CMake side) — `compile_commands.json` and the
    compiled `.o`/extension files persist at a known location afterwards.

    This performs `native_sources`' ONE compilation; a driver subsequently
    linking the resulting objects is not looking at a second body of
    machine code. Like its Fortran counterpart, it exists purely for
    test/CI isolation — `build_and_run_driver` never calls this itself.

    Returns the path to `<build_dir>/compile_commands.json`. The built
    extension module itself lands directly under `build_dir` (CMake's
    default `pybind11_add_module` output location for a single-config
    generator) — see `_cxx_extension_search_dir`.
    """
    from .generators import cmake_gen, pybind_gen

    try:
        import pybind11
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise DriverRunError(
            "building the C++/pybind11 extension requires the 'pybind11' "
            "package to be importable (for its CMake package config dir)"
        ) from exc

    build_dir = Path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)

    src_dir = build_dir.parent / f"{build_dir.name}-src"
    native_dir = src_dir / "native"
    native_dir.mkdir(parents=True, exist_ok=True)

    for src in native_sources:
        src = Path(src)
        (native_dir / src.name).write_bytes(src.read_bytes())

    # The implementation sources #include their own header(s) by name, so
    # those headers must live alongside them in native_dir too — copied from
    # wherever the caller's sources actually are (the same directory as
    # native_sources, since that is the service's own native/ tree).
    if native_sources:
        source_dir = Path(native_sources[0]).parent
        for header_name in ([header_names] if isinstance(header_names, str) else header_names):
            header_src = source_dir / header_name
            if header_src.exists():
                (native_dir / header_name).write_bytes(header_src.read_bytes())

    bindings_source = pybind_gen.generate_bindings(module, header_names)
    bindings_name = f"{module.name}_bindings.cpp"
    (native_dir / bindings_name).write_text(bindings_source)

    cmake_sources = [f"native/{Path(s).name}" for s in native_sources] + [
        f"native/{bindings_name}"
    ]
    cmake_text = cmake_gen.generate_cmake(module, module_name, cmake_sources)
    (src_dir / "CMakeLists.txt").write_text(cmake_text)

    configure_argv = [
        "cmake",
        "-S",
        str(src_dir),
        "-B",
        str(build_dir),
        f"-Dpybind11_DIR={pybind11.get_cmake_dir()}",
        f"-DPython_EXECUTABLE={sys.executable}",
    ]
    completed = subprocess.run(configure_argv, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise DriverRunError(
            f"configuring the C++/pybind11 extension failed ({completed.returncode}):\n"
            f"  $ {' '.join(configure_argv)}\n{completed.stdout}\n{completed.stderr}"
        )

    build_argv = ["cmake", "--build", str(build_dir)]
    completed = subprocess.run(build_argv, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise DriverRunError(
            f"building the C++/pybind11 extension failed ({completed.returncode}):\n"
            f"  $ {' '.join(build_argv)}\n{completed.stdout}\n{completed.stderr}"
        )

    compile_commands = build_dir / "compile_commands.json"
    if not compile_commands.exists():
        raise FileNotFoundError(
            f"expected {compile_commands} after building the extension, found nothing "
            "(CMake's build directory layout may have changed)"
        )
    return compile_commands


def _cxx_extension_search_dir(build_dir: Path) -> Path:
    """Where the built pybind11 extension `.so`/`.pyd` lands.

    CMake's default `LIBRARY_OUTPUT_DIRECTORY` for a `pybind11_add_module`
    target, with no `install()` step run and a single-config generator
    (Unix Makefiles/Ninja — what `cmake`'s platform default resolves to on
    the platforms this runs on), is the build directory itself.
    """
    return Path(build_dir)
