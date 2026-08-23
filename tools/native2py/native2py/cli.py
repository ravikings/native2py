"""native2py CLI (design.md section 13).

MVP 1 + MVP 2 scope: C++ / pybind11 / CMake and Fortran / f2py / CMake,
both end-to-end through `generate` and `build`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import click
from rich import box
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from .config import ClangConfig, ConfigError, ExposeConfig, ExposeWarning, ServiceConfig
from .discovery import (
    detect_language,
    find_cpp_directives,
    find_implementation_files,
    find_native_sources,
    is_fixed_form,
    requires_preprocessing,
)
from .preprocess import (
    IncludeError,
    expand_includes,
    read_source,
    resolve_kind_parameters,
    uses_kind_parameters,
)
from . import golden as golden_lib
from . import locking
from .suggest import POOR, READY, WORKABLE, analyse_tree
from .generators import (
    cmake_gen,
    docker_gen,
    f2py_gen,
    middleware_gen,
    gateway_gen,
    golden_gen,
    k8s_gen,
    pybind_gen,
    pyproject_gen,
    python_pkg_gen,
    test_gen,
)
from .ir import (
    IRSchemaError,
    ModuleIR,
    NativeTypeError,
    module_from_dict,
    module_to_dict,
    validate as validate_ir,
)
from .parsers import cpp as cpp_parser
from .parsers.cpp_ast import ClangOptions
from .parsers import fixed_form
from .parsers import fortran as fortran_parser

SERVICES_DIR = "services"


class StepTracker:
    """Live terminal progress for a fixed sequence of steps.

    Each step shows a spinner while running, then flips to a checkmark (or a
    red X on failure) and stays on screen — so `quickstart` reads as a
    checklist filling in rather than a wall of scrolled-past log lines.
    """

    def __init__(self, steps: list[str]) -> None:
        self._console = Console()
        self._progress = Progress(
            SpinnerColumn(finished_text="[green]✔[/green]"),
            TextColumn("{task.description}"),
            console=self._console,
        )
        self._tasks = {}
        self._steps = steps

    def __enter__(self) -> "StepTracker":
        self._progress.__enter__()
        for step in self._steps:
            self._tasks[step] = self._progress.add_task(step, total=1, start=False)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._progress.__exit__(exc_type, exc, tb)

    def start(self, step: str) -> None:
        self._progress.start_task(self._tasks[step])

    def done(self, step: str) -> None:
        self._progress.update(self._tasks[step], completed=1)
        self._progress.stop_task(self._tasks[step])

    def fail(self, step: str) -> None:
        task = self._tasks[step]
        self._progress.update(task, description=f"[red]✘ {self._progress.tasks[task].description}[/red]")
        self._progress.stop_task(task)

    def log(self, message: str) -> None:
        self._console.print(f"  [dim]{message}[/dim]")


def _load_config(service_dir: Path) -> ServiceConfig:
    """ServiceConfig.load with its two new failure channels reported properly.

    `language:` is now inferred from the sources actually present, so a config
    that disagrees with native/ raises ConfigError — which reached the user as
    a raw traceback — and a service exposing nothing by default now warns.
    """
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ExposeWarning)
        try:
            config = ServiceConfig.load(service_dir)
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc

    for warning in caught:
        if issubclass(warning.category, ExposeWarning):
            click.echo(f"WARNING: {warning.message}")
    return config


def _symbol_names(module) -> list[str]:
    if module is None:
        return []
    return (
        [c.name for c in getattr(module, "classes", [])]
        + [s.name for s in getattr(module, "structs", [])]
        + [f.name for f in getattr(module, "functions", [])]
        + [
            m.name
            for c in getattr(module, "classes", [])
            for m in getattr(c, "methods", [])
        ]
    )


def _write_python(path: Path, source: str, module=None) -> None:
    """Syntax-check generated Python, then write it (D3).

    A generated file that will not parse has to fail the *build*. Before this
    gate the only thing that ever compiled generated output was one golden
    test, which is how a Fortran `&` line continuation leaked into a committed
    router.py and shipped as a SyntaxError that surfaced at container start.
    """
    try:
        compile(source, str(path), "exec")
    except SyntaxError as exc:
        offending = (exc.text or "").rstrip()
        # Name the symbol whose codegen most likely produced the bad line, so
        # the report points at the native declaration rather than at a line
        # number in a file nobody wrote by hand.
        implicated = sorted(
            {name for name in _symbol_names(module) if name and name in offending}
        )
        detail = f" (symbol: {', '.join(implicated)})" if implicated else ""
        raise click.ClickException(
            f"native2py generated invalid Python in {path} at line {exc.lineno}: "
            f"{exc.msg}{detail}\n"
            f"    {offending}\n"
            "This is a native2py bug — the file was not written. Please report it "
            "with the native declaration above."
        ) from exc
    path.write_text(source)


def _validate_module(module) -> None:
    """Refuse to generate from an IR that cannot produce working Python (B4)."""
    problems = validate_ir(module)
    if not problems:
        return
    lines = "\n".join(f"  - {p.symbol}: {p.message}" for p in problems)
    raise click.ClickException(
        f"Cannot generate a Python package for '{module.name}':\n{lines}"
    )


def _service_dir(name: str) -> Path:
    path = Path(SERVICES_DIR) / name
    if not path.exists():
        raise click.ClickException(
            f"No service '{name}' found at {path}. Run `native2py create-service {name}` first."
        )
    return path


@click.group()
@click.version_option()
def main() -> None:
    """Expose native C++/C/Fortran code to Python as deployable microservices."""


@main.command()
def init() -> None:
    """Scaffold the monorepo layout (design.md section 4)."""
    for d in [SERVICES_DIR, "libraries", "tools", "infrastructure/docker", "infrastructure/kubernetes"]:
        Path(d).mkdir(parents=True, exist_ok=True)
    click.echo("Initialized native2py monorepo layout.")


@main.command("create-service")
@click.argument("name")
@click.option("--language", type=click.Choice(["cpp", "fortran"]), default="cpp")
@click.option(
    "--force",
    is_flag=True,
    help="Delete and recreate services/<name> if it already exists. Removes any native code already placed there.",
)
def create_service(name: str, language: str, force: bool) -> None:
    """Scaffold a new services/<name> directory (design.md section 4)."""
    service_dir = _scaffold_service(name, language, force)
    click.echo(f"Created service '{name}' ({language}) at {service_dir}")
    click.echo(f"Next: add native code under {service_dir / 'native'}, then run `native2py expose {service_dir / 'native'}`")


@main.command()
@click.argument("source", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--name", help="Service name. Defaults to SOURCE's filename without extension.")
@click.option(
    "--force",
    is_flag=True,
    help="Delete and recreate services/<name> if it already exists.",
)
@click.option("--build/--no-build", default=False, help="Also compile the extension after generating.")
def quickstart(source: Path, name: str | None, force: bool, build: bool) -> None:
    """One-shot: scaffold a service from a single C++/Fortran file, expose
    everything found in it, and generate the Python package — no manual
    native2py.yaml editing.

    Runs `create-service`, copies SOURCE into native/, auto-populates
    `expose:` with every symbol found (all classes/functions for C++, every
    routine name for Fortran), then `generate`. For anything beyond "expose
    everything in one file" — partial exposure, multi-file services, huge
    legacy Fortran templates where you only want one routine — use
    create-service + native2py.yaml + generate directly instead; see
    the Fortran guide in docs/.
    """
    language = detect_language(source)
    if language is None:
        raise click.ClickException(
            f"Could not detect a native language for {source} "
            "(expected .hpp/.cpp for C++ or .f90/.f95/.f03 for Fortran)."
        )

    service_name = name or source.stem
    steps = ["Scaffold service", "Copy native source", "Generate bindings & package"]
    if build:
        steps.append("Build wheel")

    with StepTracker(steps) as tracker:
        tracker.start("Scaffold service")
        try:
            service_dir = _scaffold_service(service_name, language, force)
        except Exception:
            tracker.fail("Scaffold service")
            raise
        tracker.log(f"services/{service_name}")
        tracker.done("Scaffold service")

        tracker.start("Copy native source")
        native_copy = service_dir / "native" / source.name
        # Copied byte-for-byte: a latin-1 legacy deck must not pick up
        # U+FFFD replacement characters on its way into the service.
        native_copy.write_bytes(source.read_bytes())
        tracker.log(f"{source} -> {native_copy}")

        config = _load_config(service_dir)
        if language == "cpp":
            # Empty expose: lists mean "expose everything found" for C++ — see ExposeConfig.is_exposed.
            # A header with declarations only (no bodies) needs its matching
            # .cpp — without it the extension still *builds* on macOS (undefined
            # symbols are deferred to load time) but fails with a confusing
            # dlopen error on first import. Pull in same-stem implementation
            # files automatically, the same pairing calculator.hpp/calculator.cpp uses.
            if source.suffix in (".hpp", ".hh", ".h"):
                impl_files = find_implementation_files(source)
                for impl_file in impl_files:
                    impl_copy = service_dir / "native" / impl_file.name
                    impl_copy.write_bytes(impl_file.read_bytes())
                    tracker.log(f"{impl_file} -> {impl_copy}")
                if not impl_files:
                    tracker.log(
                        f"[yellow]WARNING[/yellow]: no implementation file found for {source.name} — "
                        "looked beside it and in any sibling src/ directory. If its "
                        "methods are declared but not defined inline, "
                        "`native2py build` will compile and then fail to import "
                        "(undefined symbol). Add the .cpp to "
                        f"{service_dir / 'native'} and re-run `native2py generate {service_name}`."
                    )
        else:
            routines = fortran_parser.list_routine_names(native_copy)
            if not routines:
                tracker.fail("Copy native source")
                raise click.ClickException(f"No Fortran function/subroutine declarations found in {source}.")
            config.expose.functions = routines
            config.save(service_dir)
            tracker.log(f"Exposing Fortran routines: {', '.join(routines)}")
        tracker.done("Copy native source")

        tracker.start("Generate bindings & package")
        try:
            _generate_service(service_dir, config)
        except Exception:
            tracker.fail("Generate bindings & package")
            raise
        tracker.log(f"services/{service_name}/python/{service_name}")
        tracker.done("Generate bindings & package")

        if build:
            tracker.start("Build wheel")
            try:
                _run([sys.executable, "-m", "pip", "wheel", ".", "-w", "dist"], cwd=service_dir, log=tracker.log)
            except SystemExit:
                tracker.fail("Build wheel")
                raise
            tracker.log(f"services/{service_name}/dist/")
            tracker.done("Build wheel")

    click.echo(f"\nDone — services/{service_name} is ready.")


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
def detect(path: Path) -> None:
    """Detect the native language of a file or directory."""
    if path.is_file():
        lang = detect_language(path)
        click.echo(f"{path}: {lang or 'unknown'}")
        return

    for lang in ("cpp", "fortran"):
        sources = find_native_sources(path, lang)
        if sources:
            click.echo(f"{lang}:")
            for src in sources:
                click.echo(f"  {src}")


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--parser",
    type=click.Choice(list(cpp_parser.BACKENDS)),
    default="auto",
    help="C++ only: 'clang' for the AST parser, 'regex' for the fallback reader.",
)
@click.option(
    "--include",
    "includes",
    multiple=True,
    help="C++ only: extra include directory for the AST parse (repeatable).",
)
@click.option("--std", default="c++17", show_default=True, help="C++ only: language standard.")
@click.option("--all", "show_all", is_flag=True, help="Show every file, not just the top 15.")
def suggest(path: Path, parser: str, includes: tuple[str, ...], show_all: bool, std: str) -> None:
    """Rank the native sources under PATH by how cleanly they would bind.

    Answers "which file do I point native2py at first?" for a codebase you
    did not write. Parses every header it finds and sorts by how much binds
    versus how much is skipped, preferring self-contained files — a good
    first service is one with no dependencies on the rest of the tree, not
    necessarily the biggest one.
    """
    console = Console()
    if parser == "auto":
        console.print(f"[dim]parser: {cpp_parser.backend_description(parser)}[/dim]")

    with console.status(f"Parsing native sources under {path}..."):
        candidates = analyse_tree(
            path, backend=parser, options=ClangOptions(std=std, include_paths=tuple(includes))
        )

    if not candidates:
        raise click.ClickException(f"No C++ or Fortran sources found under {path}.")

    marks = {
        READY: "[green]✔[/green]",
        WORKABLE: "[yellow]~[/yellow]",
        POOR: "[red]✘[/red]",
    }
    table = Table(box=box.SIMPLE, header_style="bold")
    table.add_column("")
    table.add_column("file", no_wrap=True)
    table.add_column("binds")
    table.add_column("skipped", justify="right")
    table.add_column("notes", style="dim", overflow="fold")

    shown = candidates if show_all else candidates[:15]
    for c in shown:
        # c.path is already relative to the cwd (find_native_sources builds it
        # from the PATH argument as given), same as the "Start with" command
        # below — stripping the root here would print a path missing the
        # root it was found under, not one runnable as-is.
        table.add_row(
            marks[c.verdict],
            str(c.path),
            c.binds_summary(),
            str(c.skipped) if c.skipped else "",
            c.notes(),
        )
    console.print(table)
    if len(candidates) > len(shown):
        console.print(f"[dim]... and {len(candidates) - len(shown)} more (--all to show).[/dim]")

    best = candidates[0]
    if best.verdict == POOR:
        console.print(
            "[yellow]Nothing here binds cleanly.[/yellow] Check the skipped reasons with "
            f"`native2py inspect <file>` — and if the parser line above says 'regex reader', "
            'install the AST parser first: pip install "native2py[clang]".'
        )
        return

    console.print(f"\nStart with [bold]{best.path}[/bold]:\n")
    console.print(f"  native2py quickstart {best.path} --name {best.path.stem.lower()} --build")


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--function",
    "functions",
    multiple=True,
    help="Fortran only: name of a function/subroutine to extract (repeatable). Required for Fortran.",
)
@click.option(
    "--parser",
    type=click.Choice(list(cpp_parser.BACKENDS)),
    default="auto",
    help="C++ only: 'clang' for the AST parser, 'regex' for the fallback reader.",
)
@click.option(
    "--include",
    "includes",
    multiple=True,
    help="C++ only: extra include directory for the AST parse (repeatable).",
)
@click.option(
    "--std",
    default="c++17",
    show_default=True,
    help="C++ only: language standard for the AST parse.",
)
def inspect(
    path: Path,
    functions: tuple[str, ...],
    parser: str,
    includes: tuple[str, ...],
    std: str,
) -> None:
    """Parse a native source file and print the resulting IR."""
    lang = detect_language(path)
    if lang == "cpp":
        try:
            click.echo(f"parser: {cpp_parser.backend_description(parser)}")
            module = cpp_parser.parse_header(
                path,
                ExposeConfig(),
                backend=parser,
                options=ClangOptions(std=std, include_paths=tuple(includes)),
            )
        except cpp_parser.ParserUnavailable as exc:
            raise click.ClickException(str(exc)) from exc
    elif lang == "fortran":
        if not functions:
            raise click.ClickException(
                "Fortran sources need at least one --function <name> to target "
                "(large legacy files are parsed one routine at a time)."
            )
        module = fortran_parser.parse_source(path, ExposeConfig(functions=list(functions)))
    else:
        raise click.ClickException(f"Unrecognized native source: {path} (detected language: {lang}).")

    click.echo(f"module: {module.name} ({module.language})")
    for cls in module.classes:
        click.echo(f"  class {cls.name}")
        for m in cls.methods:
            params = ", ".join(f"{p.name}: {p.type}" for p in m.parameters)
            click.echo(f"    {m.name}({params}) -> {m.returns}")
    for struct in module.structs:
        click.echo(f"  struct {struct.name}")
        for f in struct.fields:
            click.echo(f"    {f.name}: {f.type}")
    for fn in module.functions:
        params = ", ".join(f"{p.name}: {p.type}" for p in fn.parameters)
        click.echo(f"  function {fn.name}({params}) -> {fn.returns}")
    _report_skipped(path, module)
    if module.is_empty():
        click.echo("  (nothing exposed — add [[native2py::expose]] or list symbols in native2py.yaml)")


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
def expose(path: Path) -> None:
    """Mark a native source as exposed and run codegen for its service.

    PATH may be a header inside services/<name>/native/, or that native/
    directory itself. Equivalent to editing native2py.yaml + `generate`.
    """
    service_dir = _resolve_service_dir(path)
    config = _load_config(service_dir)
    _generate_service(service_dir, config)
    click.echo(f"Exposed native API from {path} for service '{config.name}'.")


@main.command()
@click.argument("name")
def generate(name: str) -> None:
    """Re-run binding/package/test generation for services/<name>."""
    service_dir = _service_dir(name)
    config = _load_config(service_dir)
    _generate_service(service_dir, config)
    click.echo(f"Generated bindings, CMake, Python package, and tests for '{name}'.")


@main.command()
@click.argument("name")
def build(name: str) -> None:
    """Build the service's Python wheel (pip wheel .)."""
    service_dir = _service_dir(name)
    _run([sys.executable, "-m", "pip", "wheel", ".", "-w", "dist"], cwd=service_dir)


@main.group()
def golden() -> None:
    """Numerical regression: record what the bindings return, then check it.

    "It builds and imports" is not the acceptance criterion for re-hosting
    decades-old engineering code — "the answers did not change" is. `record`
    captures every bound entry point's output for a fixed, reproducible set of
    inputs into services/<name>/golden.json; `verify` replays it. The
    generated tests/test_golden.py does the same thing inside the service's
    own suite, so CI catches drift without any extra wiring.
    """


def _import_service_package(name: str):
    """Import the service's built package, or explain how to get one.

    Recording runs against the *compiled* extension, not the source: the whole
    point is to catch a compiler, flag or code change that moves a number.
    """
    import importlib

    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise click.ClickException(
            f"Could not import the '{name}' package ({exc}). Golden values are "
            "recorded against the built extension, so build and install the "
            f"service first:\n  native2py build {name}\n"
            f"  pip install services/{name}/dist/*.whl"
        ) from exc


def _load_service_ir(service_dir: Path, name: str) -> ModuleIR:
    ir_path = service_dir / IR_PATH
    if not ir_path.exists():
        raise click.ClickException(
            f"No {ir_path} — run `native2py generate {name}` first."
        )
    try:
        return module_from_dict(json.loads(ir_path.read_text()))
    except IRSchemaError as exc:
        # A version this native2py cannot honestly read has to stop the command,
        # not surface as a traceback from inside `golden record`.
        raise click.ClickException(f"{ir_path}: {exc}") from exc


@golden.command("record")
@click.argument("name")
@click.option("--rtol", default=golden_lib.DEFAULT_RTOL, show_default=True, help="Relative tolerance stored in the golden file.")
@click.option("--atol", default=golden_lib.DEFAULT_ATOL, show_default=True, help="Absolute tolerance stored in the golden file.")
@click.option("--force", is_flag=True, help="Overwrite an existing golden file whose values differ.")
def golden_record(name: str, rtol: float, atol: float, force: bool) -> None:
    """Record golden values for services/<name> (needs the built package installed)."""
    service_dir = _service_dir(name)
    module = _load_service_ir(service_dir, name)
    package = _import_service_package(name)

    path = service_dir / golden_lib.GOLDEN_FILENAME
    existing = golden_lib.read(path) if path.exists() else None

    # Hand-edited inputs are kept: an engineer who replaced the generated
    # `1.0` with a real reservoir pressure should not lose it on the next
    # re-record.
    entries, skips = golden_lib.run(module, package, existing)
    document = golden_lib.build_document(module, entries, skips, rtol=rtol, atol=atol)

    if existing is not None and not force:
        results = {key: entry["result"] for key, entry in entries.items()}
        differences = golden_lib.compare(existing, results)
        if differences:
            # Re-recording silently is how a regression gets committed as the
            # new truth. Make the operator say they meant it.
            click.echo(f"{len(differences)} value(s) differ from the recorded golden file:")
            for line in differences[:20]:
                click.echo(f"  - {line}")
            raise click.ClickException(
                "Refusing to overwrite. Investigate first; re-record with --force "
                "once you are satisfied the change is intended."
            )

    golden_lib.write(path, document)
    recorded, skipped = golden_lib.coverage(document)
    click.echo(f"Recorded {recorded} entry point(s) to {path}.")
    if skipped:
        click.echo(f"{skipped} entry point(s) not covered:")
        for key, reason in document["skipped"].items():
            click.echo(f"  - {key}: {reason}")


@golden.command("verify")
@click.argument("name")
def golden_verify(name: str) -> None:
    """Replay services/<name>'s golden file against the installed package."""
    service_dir = _service_dir(name)
    path = service_dir / golden_lib.GOLDEN_FILENAME
    if not path.exists():
        raise click.ClickException(
            f"No {path} — record one first with `native2py golden record {name}`."
        )

    document = golden_lib.read(path)
    package = _import_service_package(name)
    results, errors = golden_lib.replay(document, package)
    differences = golden_lib.compare(document, results, errors)

    if differences:
        click.echo(f"{len(differences)} numerical difference(s):")
        for line in differences:
            click.echo(f"  - {line}")
        raise click.ClickException(
            "The bindings no longer return the recorded values. If the change is "
            f"intended, re-record with `native2py golden record {name} --force`."
        )

    recorded, skipped = golden_lib.coverage(document)
    click.echo(f"{recorded} entry point(s) unchanged ({skipped} not covered).")


@golden.command("show")
@click.argument("name")
def golden_show(name: str) -> None:
    """Print what the golden file covers, and what it does not."""
    service_dir = _service_dir(name)
    path = service_dir / golden_lib.GOLDEN_FILENAME
    if not path.exists():
        raise click.ClickException(f"No {path} — record one first.")

    document = golden_lib.read(path)
    recorded, skipped = golden_lib.coverage(document)
    tolerance = document.get("tolerance") or {}
    click.echo(
        f"{path}: {recorded} recorded, {skipped} not covered "
        f"(rtol={tolerance.get('rtol')}, atol={tolerance.get('atol')})"
    )
    for key, entry in (document.get("entries") or {}).items():
        click.echo(f"  {key}({', '.join(repr(a) for a in entry['arguments'])}) -> {entry['result']!r}")
    for key, reason in (document.get("skipped") or {}).items():
        click.echo(f"  [skipped] {key}: {reason}")


@main.command()
@click.argument("name")
def test(name: str) -> None:
    """Run the service's test suite."""
    service_dir = _service_dir(name)
    _run([sys.executable, "-m", "pytest", "tests/"], cwd=service_dir)


@main.command()
@click.argument("name")
@click.option("--build/--no-build", default=False, help="Also run `docker build` after generating the Dockerfile.")
def docker(name: str, build: bool) -> None:
    """Generate (and optionally build) the service's Dockerfile."""
    service_dir = _service_dir(name)
    config = _load_config(service_dir)
    libraries = _validated_libraries(config)
    # Generated from what is actually there: a lock the user has not created
    # would produce a Dockerfile with a COPY that fails, and silently ignoring
    # one they HAVE created would quietly drop the pinning they asked for.
    locked = (service_dir / locking.LOCK_FILENAME).exists()
    dockerfile = docker_gen.generate_dockerfile(
        name, config.language, name, libraries=libraries, locked=locked
    )
    (service_dir / "Dockerfile").write_text(dockerfile)
    click.echo(f"Wrote {service_dir / 'Dockerfile'}")
    if locked:
        click.echo("  Dependencies install from requirements.lock (--require-hashes).")
    else:
        click.echo(
            f"  NOTE: no {locking.LOCK_FILENAME} — pip will resolve dependencies at "
            f"build time, so two builds can differ. Run `native2py lock {name}`."
        )

    if libraries:
        # Shared libraries live outside the service dir, so the build context
        # must be the repo root for COPY to reach them.
        click.echo(f"Build context: repo root (links libraries: {', '.join(libraries)})")
        if build:
            _run(
                ["docker", "build", "-f", str(service_dir / "Dockerfile"), "-t", f"{name}:latest", "."],
                cwd=Path("."),
            )
    elif build:
        _run(["docker", "build", "-t", f"{name}:latest", "."], cwd=service_dir)


@main.command("lock")
@click.argument("name")
def lock(name: str) -> None:
    """Pin this service's Python dependencies by version and SHA-256.

    Writes requirements.lock, which the generated Dockerfile installs with
    --require-hashes. Without it, `docker build` resolves fastapi/uvicorn/numpy
    from PyPI every time, so an image rebuilt a week later ships different code
    with nothing recording the change.

    Resolution targets the IMAGE — Python 3.12 on Linux, both architectures —
    not the machine running this command.
    """
    service_dir = _service_dir(name)
    config = _load_config(service_dir)

    requirements = list(
        docker_gen._LANGUAGE_RUNTIME_PYTHON_DEPS.get(
            config.language, docker_gen._LANGUAGE_RUNTIME_PYTHON_DEPS["cpp"]
        )
    )
    click.echo(f"Resolving {len(requirements)} direct dependencies for the image...")
    try:
        target = locking.write_lock(service_dir, name, requirements)
    except locking.LockError as exc:
        raise click.ClickException(str(exc)) from exc

    pinned = sum(1 for line in target.read_text().splitlines() if "==" in line)
    click.echo(f"Wrote {target} ({pinned} packages pinned by hash)")
    click.echo(f"  Re-run `native2py docker {name}` so the Dockerfile installs from it.")


@main.command("k8s")
@click.argument("name")
@click.option("--image", default=None, help="Image reference to deploy. Defaults to <name>:latest.")
@click.option("--replicas", default=2, show_default=True, help="Initial replica count.")
@click.option(
    "--output",
    "output",
    default=None,
    help="Where to write the manifests. Defaults to infrastructure/kubernetes/<name>.yaml.",
)
def k8s(name: str, image: str | None, replicas: int, output: str | None) -> None:
    """Generate Kubernetes manifests for services/<name>.

    Replicas, resources and the image are starting points. The security
    context and the probe wiring are not — see generators/k8s_gen.py.
    """
    service_dir = _service_dir(name)
    config = _load_config(service_dir)

    manifests = k8s_gen.generate_k8s_manifests(
        name,
        config.language,
        image,
        auth=config.api.auth,
        replicas=replicas,
    )
    target = Path(output) if output else Path("infrastructure/kubernetes") / f"{name}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(manifests)

    click.echo(f"Wrote {target}")
    click.echo(f"  readiness -> /readyz (drains on SIGTERM), liveness -> /healthz")
    if config.language == "fortran":
        click.echo(
            "  WEB_CONCURRENCY=1 — COMMON blocks are per-process. Scale with "
            "replicas, not workers."
        )
    if config.api.auth == "api_key":
        click.echo(
            f"  Create the Secret first, or the pod will refuse to start:\n"
            f"    kubectl create secret generic {name}-api-keys "
            '--from-literal=keys="$(openssl rand -hex 32)"'
        )


@main.command()
@click.argument("name")
@click.option(
    "--service",
    "services",
    multiple=True,
    required=True,
    help="Service to mount into the gateway (repeatable). Each is mounted under /<service-name>.",
)
def gateway(name: str, services: tuple[str, ...]) -> None:
    """Generate a composed gateway app that serves several services on one URL.

    Each service keeps its own wheel, version, and build — the gateway just
    depends on them and mounts their routers under a path prefix. Use this
    when you want one deployable and one URL; use a real API gateway /
    Kubernetes Ingress in front of separate images instead when you need
    independent scaling. See docs/deployment-topologies.md.
    """
    for service_name in services:
        _service_dir(service_name)  # fail early if any service doesn't exist

    # The distribution name may contain hyphens ("platform-api"), but the
    # importable package must be a valid Python identifier so uvicorn's
    # "<module>.app:app" target resolves.
    package_name = name.replace("-", "_")

    gateway_dir = Path("gateways") / name
    package_dir = gateway_dir / package_name
    package_dir.mkdir(parents=True, exist_ok=True)

    (package_dir / "__init__.py").write_text("")
    # The gateway is its own FastAPI app, so it needs its own middleware —
    # a mounted router runs under THIS app, not under the service's. Its auth
    # mode is the strictest of the services it mounts: composing an
    # authenticated service into an open gateway would publish it unprotected.
    auths = {_load_config(_service_dir(s)).api.auth for s in services}
    gateway_auth = "api_key" if "api_key" in auths else "none"
    _write_python(
        package_dir / middleware_gen.MIDDLEWARE_FILENAME,
        middleware_gen.generate_middleware_py(package_name, auth=gateway_auth),
    )
    try:
        app_source = gateway_gen.generate_gateway_app(name, list(services), package_name)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _write_python(package_dir / "app.py", app_source)
    (gateway_dir / "pyproject.toml").write_text(
        gateway_gen.generate_gateway_pyproject(name, package_name, list(services))
    )

    click.echo(f"Generated gateway '{name}' at {gateway_dir}")
    click.echo(f"Mounts: {', '.join(f'/{s}' for s in services)}")
    if gateway_auth == "api_key":
        click.echo(
            "Auth: api_key (inherited from a mounted service). Set "
            f"{middleware_gen.ENV_API_KEYS} or the gateway will refuse to start."
        )
    click.echo(f"Run with: uvicorn {package_name}.app:app")


@main.command()
@click.argument("name")
def clean(name: str) -> None:
    """Remove build artifacts for services/<name>."""
    import shutil

    service_dir = _service_dir(name)
    for pattern in ["dist", "build", "*.egg-info", "**/__pycache__", ".pytest_cache", "native/_expanded"]:
        for match in service_dir.glob(pattern):
            if match.is_dir():
                shutil.rmtree(match, ignore_errors=True)
            else:
                match.unlink(missing_ok=True)
    click.echo(f"Cleaned build artifacts for '{name}'.")


def _scaffold_service(name: str, language: str, force: bool) -> Path:
    import shutil

    service_dir = Path(SERVICES_DIR) / name
    if service_dir.exists():
        if not force:
            raise click.ClickException(
                f"{service_dir} already exists. Re-run with --force to delete and recreate it."
            )
        click.echo(f"--force: removing existing {service_dir}")
        shutil.rmtree(service_dir)

    (service_dir / "native").mkdir(parents=True)
    (service_dir / "bindings" / "generated").mkdir(parents=True)
    (service_dir / "python" / name).mkdir(parents=True)
    (service_dir / "tests").mkdir(parents=True)

    ServiceConfig(name=name, language=language).save(service_dir)
    (service_dir / "python" / name / "__init__.py").write_text(
        "# Run `native2py expose` then `native2py generate` to populate this package.\n"
    )
    (service_dir / "pyproject.toml").write_text(pyproject_gen.generate_pyproject(name, language))
    return service_dir


def _resolve_service_dir(path: Path) -> Path:
    for parent in [path] + list(path.parents):
        if (parent / "native2py.yaml").exists():
            return parent
        if parent.name == "native" and (parent.parent / "native2py.yaml").exists():
            return parent.parent
    raise click.ClickException(
        f"Could not find a native2py.yaml above {path}. Run `native2py create-service` first."
    )


def _generate_service(service_dir: Path, config: ServiceConfig) -> None:
    if config.language == "cpp":
        _generate_cpp_service(service_dir, config)
    elif config.language == "fortran":
        _generate_fortran_service(service_dir, config)
    else:
        raise click.ClickException(f"Unsupported language '{config.language}'.")


IR_PATH = Path(".native2py") / "ir.json"


def _restore_scaffold(service_dir: Path, config: ServiceConfig) -> None:
    """Re-create scaffold files that `create-service` wrote, if they are gone.

    Only ever writes what is *missing*: pyproject.toml is generated, but it is
    also the file you legitimately hand-edit (a pinned dependency, a version
    bump), so regenerating over it would silently discard that. Without this,
    deleting one generated file left `generate` unable to restore it and
    `create-service --force` — which deletes native/ — as the only way back.
    """
    pyproject = service_dir / "pyproject.toml"
    if not pyproject.exists():
        pyproject.write_text(
            pyproject_gen.generate_pyproject(config.name, config.language)
        )
        click.echo(f"Restored missing {pyproject}")


def _warn_if_numpy_is_undeclared(service_dir: Path, module) -> None:
    """Say so when a binding needs numpy and pyproject.toml does not declare it.

    A raw `T*` is bound through pybind11/numpy.h, which needs numpy's headers
    to build and numpy to import. pyproject.toml is written once at scaffold
    time — before any header has been parsed — and is explicitly a file you may
    hand-edit, so `generate` will not rewrite it. Telling you exactly what to
    add beats either clobbering your edits or letting the build fail with a
    missing-header error three layers down in CMake.
    """
    if not cmake_gen.uses_numpy_buffers(module):
        return
    pyproject = service_dir / "pyproject.toml"
    if pyproject.exists() and '"numpy"' in pyproject.read_text():
        return
    click.echo(
        "WARNING: this service binds a raw pointer argument as a numpy buffer, "
        "which needs numpy at build and run time, but pyproject.toml does not "
        'declare it. Add "numpy" to [build-system] requires and to '
        "[project] dependencies."
    )


def _write_package(service_dir: Path, config: ServiceConfig, module) -> None:
    _validate_module(module)
    _restore_scaffold(service_dir, config)
    _warn_if_numpy_is_undeclared(service_dir, module)

    # A machine-readable record of exactly what was bound. `golden record`
    # reads it instead of re-parsing: recording happens against an installed
    # wheel, on a machine that may have neither the headers nor libclang.
    ir_path = service_dir / IR_PATH
    ir_path.parent.mkdir(parents=True, exist_ok=True)
    ir_path.write_text(json.dumps(module_to_dict(module), indent=2) + "\n")

    package_dir = service_dir / "python" / config.name
    package_dir.mkdir(parents=True, exist_ok=True)
    # Every generated .py is compiled before it is written (D3): an unparsable
    # file must fail the build here, never the container start.
    _write_python(
        package_dir / "__init__.py", python_pkg_gen.generate_init_py(module), module
    )
    _write_python(
        package_dir / "router.py",
        python_pkg_gen.generate_router_py(module, config.name),
        module,
    )
    _write_python(
        package_dir / middleware_gen.MIDDLEWARE_FILENAME,
        middleware_gen.generate_middleware_py(config.name, auth=config.api.auth),
        module,
    )
    _write_python(
        package_dir / "service.py", python_pkg_gen.generate_service_py(config.name), module
    )

    tests_dir = service_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    _write_python(
        tests_dir / "test_python_api.py",
        test_gen.generate_python_api_test(module, config.name),
        module,
    )
    # The regression test ships even before anything is recorded: it skips
    # with the command to record, which is how you find out the harness
    # exists. A golden file that nobody knows to create protects nothing.
    _write_python(
        tests_dir / "test_golden.py",
        golden_gen.generate_golden_test(
            config.name, config.name, golden_lib.GOLDEN_FILENAME
        ),
        module,
    )


LIBRARIES_DIR = "libraries"


def _validated_libraries(config: ServiceConfig) -> list[str]:
    """Check each declared shared library exists and is buildable before we
    emit an add_subdirectory() that would otherwise fail deep inside CMake
    with a much less obvious message."""
    for lib in config.libraries:
        lib_dir = Path(LIBRARIES_DIR) / lib
        if not lib_dir.is_dir():
            raise click.ClickException(
                f"Service '{config.name}' declares library '{lib}' but "
                f"{lib_dir} does not exist."
            )
        if not (lib_dir / "CMakeLists.txt").exists():
            raise click.ClickException(
                f"{lib_dir} has no CMakeLists.txt — a shared native library must "
                "define its own CMake target (e.g. add_library(common_cpp STATIC ...) "
                "with POSITION_INDEPENDENT_CODE ON)."
            )
    return list(config.libraries)


def _report_skipped(source: Path, module) -> None:
    """Print declarations the parser recognised but could not bind.

    Silence here is the failure mode that costs the most time: a header with
    thirty methods generates a module with twenty-six and nothing says which
    four are missing or why, so the gap is discovered from an AttributeError
    in Python much later.
    """
    for message in getattr(module, "diagnostics", []):
        # The AST parser recovers from errors by inventing types (an unknown
        # return type becomes `int`), so a header that does not compile can
        # still produce plausible-looking bindings. Say so.
        click.echo(f"  {source.name}: compiler error: {message}")

    if not module.skipped:
        return
    click.echo(f"  {source.name}: skipped {len(module.skipped)} declaration(s):")
    for entry in module.skipped:
        click.echo(f"    - {entry.name}: {entry.reason}")


def _has_undefined_methods(module: ModuleIR) -> bool:
    """True if anything bound needs a definition from a separate .cpp.

    Structs are pure data and constructors of header-only classes may well be
    inline, so only methods and free functions count.
    """
    return any(cls.methods for cls in module.classes) or bool(module.functions)


def _validated_include_paths(config: ServiceConfig) -> list[str]:
    """Extra header search directories, repo-root-relative.

    For C++ these become `target_include_directories` entries so a source can
    `#include` a header living outside the service's own native/ directory.
    (Fortran uses the same config key for INCLUDE resolution.)
    """
    for p in config.include_paths:
        if not Path(p).is_dir():
            raise click.ClickException(
                f"include_paths entry '{p}' is not a directory (relative to the repo root)."
            )
    return list(config.include_paths)


def _clang_options(clang: ClangConfig) -> ClangOptions:
    """native2py.yaml's `clang:` block as parser options."""
    return ClangOptions(
        std=clang.std,
        include_paths=tuple(clang.include_paths),
        defines=tuple(clang.defines),
        extra_args=tuple(clang.extra_args),
    )


def _generate_cpp_service(service_dir: Path, config: ServiceConfig) -> None:
    headers = find_native_sources(service_dir / "native", "cpp")
    headers = [h for h in headers if h.suffix in (".hpp", ".hh", ".h")]
    if not headers:
        raise click.ClickException(f"No C++ headers found under {service_dir / 'native'}.")

    # All headers merge into ONE module. A Python extension can only carry a
    # single PYBIND11_MODULE init symbol, so a per-header bindings file would
    # leave every header but one orphaned — which is what happened before:
    # engine.hpp's bindings were generated, never compiled, and `Engine`
    # vanished from the package with no warning.
    merged = ModuleIR(
        name=config.name,
        language="cpp",
        source_file=str(service_dir / "native"),
    )
    contributing_headers: list[str] = []
    origin: dict[str, str] = {}

    # A type forward-declared in one header may be defined in a sibling — all
    # of a service's headers compile into one extension, so it's complete by
    # the time pybind11 sees it. Collect the definitions first so those
    # symbols aren't wrongly skipped as incomplete.
    try:
        backend = cpp_parser.resolve_backend(config.parser)
    except cpp_parser.ParserUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    options = _clang_options(config.clang)
    click.echo(f"Parsing C++ with {cpp_parser.backend_description(config.parser)}.")

    sibling_records = frozenset().union(
        *(
            cpp_parser.defined_record_names(h, backend=backend, options=options)
            for h in headers
        )
    )

    for header in headers:
        try:
            module = cpp_parser.parse_header(
                header,
                config.expose,
                sibling_records,
                backend=backend,
                options=options,
            )
        except NativeTypeError as exc:
            raise click.ClickException(str(exc)) from exc

        _report_skipped(header, module)

        if module.is_empty():
            continue

        for symbol in [*module.classes, *module.structs, *module.functions]:
            previous = origin.get(symbol.name)
            if previous is not None:
                raise click.ClickException(
                    f"'{symbol.name}' is defined in both {previous} and {header.name}. "
                    "native2py binds all headers of a service into one extension, so "
                    "symbol names must be unique across them — rename one, or expose "
                    "only one of the headers via `expose:` in native2py.yaml."
                )
            origin[symbol.name] = header.name

        merged.classes.extend(module.classes)
        merged.structs.extend(module.structs)
        merged.functions.extend(module.functions)
        merged.skipped.extend(module.skipped)
        contributing_headers.append(header.name)

    if merged.is_empty():
        raise click.ClickException(
            f"No exposable symbols found in {service_dir / 'native'}. "
            "Check `expose:` in native2py.yaml, or that the headers declare "
            "classes/functions native2py can parse."
        )

    if len(contributing_headers) > 1:
        click.echo(f"Merged {len(contributing_headers)} headers: {', '.join(contributing_headers)}")

    bindings_dir = service_dir / "bindings" / "generated"
    bindings_dir.mkdir(parents=True, exist_ok=True)
    # Everything here is generated, so stale files from a previous run (a
    # renamed service, or the old one-file-per-header scheme) are dead weight
    # that CMake no longer references — clear them rather than leave misleading
    # source lying around.
    for stale in bindings_dir.glob("*_bindings.cpp"):
        stale.unlink()
    bindings_path = bindings_dir / f"{config.name}_bindings.cpp"
    bindings_path.write_text(pybind_gen.generate_bindings(merged, contributing_headers))

    native_dir = service_dir / "native"
    impl_files = sorted(
        p for p in native_dir.iterdir() if p.suffix in (".cpp", ".cc", ".cxx")
    )
    if not impl_files and _has_undefined_methods(merged):
        # Nothing links these declarations. On macOS the extension still
        # *builds* — undefined symbols in a module bundle are resolved lazily
        # at dlopen — so the failure surfaces much later as
        # "symbol not found in flat namespace '__ZN9Simulator10advance_toEd'",
        # which points at the linker rather than the missing file.
        click.echo(
            f"WARNING: no .cpp/.cc/.cxx implementation files in {native_dir}, but the "
            "headers declare methods without inline bodies. `native2py build` will "
            "succeed and then fail on first import with an undefined-symbol error. "
            "Add the implementation file(s) and re-run `native2py generate "
            f"{config.name}`."
        )

    native_sources = [f"native/{s.name}" for s in impl_files]
    cmake_path = service_dir / "CMakeLists.txt"
    cmake_path.write_text(
        cmake_gen.generate_cmake(
            merged,
            config.name,
            native_sources + [f"bindings/generated/{bindings_path.name}"],
            libraries=_validated_libraries(config),
            include_paths=_validated_include_paths(config),
        )
    )

    _write_package(service_dir, config, merged)


def _generate_fortran_service(service_dir: Path, config: ServiceConfig) -> None:
    if not config.expose.functions:
        raise click.ClickException(
            "Fortran services require an `expose.functions:` list in native2py.yaml "
            "naming exactly the routines to bind — there is no expose-everything "
            "fallback, so large legacy templates only pay for what they use. "
            "Add e.g.:\n  expose:\n    functions:\n      - calculate_pressure"
        )

    sources = find_native_sources(service_dir / "native", "fortran")
    if not sources:
        raise click.ClickException(f"No Fortran sources found under {service_dir / 'native'}.")

    include_paths = [Path(p) for p in config.include_paths]
    for p in include_paths:
        if not p.is_dir():
            raise click.ClickException(
                f"include_paths entry '{p}' is not a directory (relative to the repo root)."
            )

    # A requested routine may live in any one of several native/*.f90 files
    # (common for large template libraries split across many files); search
    # each source until every requested name is found, rather than requiring
    # the caller to say which file it's in.
    remaining = list(config.expose.functions)
    module = None
    intents_by_source: dict[Path, dict[str, dict[str, tuple[str, bool]]]] = {}

    for source in sources:
        if not remaining:
            break
        found_expose = ExposeConfig(functions=[n for n in remaining if _routine_in_file(source, n)])
        if not found_expose.functions:
            continue

        try:
            parsed = fortran_parser.parse_source(
                source, found_expose, include_paths=include_paths
            )
        except IncludeError as exc:
            raise click.ClickException(str(exc)) from exc
        if module is None:
            module = parsed
        else:
            # Routines from different Fortran modules (or from a module and
            # from bare fixed-form decks) can coexist: FunctionDef carries its
            # own enclosing module, and generate_init_py re-exports each from
            # wherever f2py actually put it.
            module.functions.extend(parsed.functions)
            module.skipped.extend(parsed.skipped)

        # Remember which routines came from which file: the inferred intents
        # have to be written back into that file's expanded copy as Cf2py
        # directives, or f2py never learns about them.
        intents_by_source[source] = {
            fn.name: {p.name: (p.intent, p.is_array) for p in fn.parameters}
            for fn in parsed.functions
        }

        for name in found_expose.functions:
            remaining.remove(name)

    if remaining:
        raise click.ClickException(
            f"Could not find routine(s) {', '.join(remaining)} in any file under {service_dir / 'native'}."
        )

    assert module is not None
    module.name = config.name

    # f2py silently mis-wraps routines containing an in-body INCLUDE — the
    # extension builds and imports but arguments never arrive, so calls
    # return uninitialized memory. Hand it pre-expanded source instead.
    # See preprocess.expand_includes for the verification of this.
    # Free-form sources get the same treatment for a different reason: f2py's
    # generated wrapper does not inherit the module's kind PARAMETERs, so
    # `real(dp)` has to be resolved to `real(8)` before it reaches f2py.
    # See preprocess.resolve_kind_parameters.
    # A source needing the C preprocessor (an uppercase .F90/.F suffix, or cpp
    # directives in the text) is parsed here as if every #ifdef branch were
    # live, because neither Fortran parser has a preprocessor. That silently
    # binds whichever branch happens to lex, so say so rather than let it pass.
    for source in sources:
        directives = find_cpp_directives(read_source(source))
        if requires_preprocessing(source):
            click.echo(
                f"WARNING: {source.name} needs the C preprocessor"
                + (f" (found {', '.join('#' + d for d in directives[:4])})" if directives else "")
                + ". native2py has no preprocessor, so conditional code is read "
                "as though every branch is active. Pre-process it yourself and "
                "point native2py at the output if the branches differ."
            )

    fixed_form_sources = [s for s in sources if is_fixed_form(s)]
    # A6: an INCLUDE in a .f90 has exactly the same consequence as one in a
    # fixed-form deck — f2py mis-wraps the routine and calls return
    # uninitialized memory — so free-form sources get expanded too.
    free_form_include_sources = [
        s for s in sources if s not in fixed_form_sources and _has_include(s)
    ]
    kind_param_sources = [
        s
        for s in sources
        if s not in fixed_form_sources
        and s not in free_form_include_sources
        and uses_kind_parameters(read_source(s))
    ]

    rewritten = fixed_form_sources + free_form_include_sources + kind_param_sources
    if rewritten:
        expanded_dir = service_dir / "native" / "_expanded"
        expanded_dir.mkdir(parents=True, exist_ok=True)
        native_sources = []
        for source in sources:
            if source in fixed_form_sources:
                target = expanded_dir / source.name
                expanded = expand_includes(source, include_paths)
                expanded = fixed_form.inject_intent_directives(
                    expanded, intents_by_source.get(source, {})
                )
                target.write_text(expanded)
                native_sources.append(f"native/_expanded/{source.name}")
            elif source in free_form_include_sources:
                target = expanded_dir / source.name
                expanded = expand_includes(
                    source, include_paths, comment_style="free"
                )
                # An expanded INCLUDE routinely carries the kind PARAMETERs the
                # body needs, so resolve them on the same copy.
                target.write_text(resolve_kind_parameters(expanded))
                native_sources.append(f"native/_expanded/{source.name}")
            elif source in kind_param_sources:
                target = expanded_dir / source.name
                target.write_text(resolve_kind_parameters(read_source(source)))
                native_sources.append(f"native/_expanded/{source.name}")
            else:
                native_sources.append(f"native/{source.name}")
        if fixed_form_sources:
            click.echo(
                f"Expanded INCLUDEs for {len(fixed_form_sources)} fixed-form source(s) "
                f"-> {expanded_dir}"
            )
        if free_form_include_sources:
            click.echo(
                f"Expanded INCLUDEs for {len(free_form_include_sources)} free-form "
                f"source(s) -> {expanded_dir}"
            )
        if kind_param_sources:
            click.echo(
                f"Resolved kind parameters (real(dp) -> real(8)) for "
                f"{len(kind_param_sources)} source(s) -> {expanded_dir}"
            )
    else:
        native_sources = [f"native/{s.name}" for s in sources]

    cmake_path = service_dir / "CMakeLists.txt"
    cmake_path.write_text(
        f2py_gen.generate_cmake(
            module,
            config.name,
            native_sources,
            only_routines=list(config.expose.functions),
        )
    )

    _write_package(service_dir, config, module)


import re as _re

_INCLUDE_LINE_RE = _re.compile(r"^\s*INCLUDE\s+['\"][^'\"]+['\"]", _re.IGNORECASE | _re.MULTILINE)


def _has_include(source: Path) -> bool:
    return _INCLUDE_LINE_RE.search(read_source(source)) is not None


def _routine_in_file(source: Path, name: str) -> bool:
    import re

    # The prefix can be several words ("DOUBLE PRECISION FUNCTION PVTRS"),
    # and fixed-form continuation means the "(" may not be on this line, so
    # don't require it.
    pattern = re.compile(
        rf"^[ \t]*(?:[A-Za-z0-9_*]+[ \t]+)*?(?:function|subroutine)[ \t]+{re.escape(name)}\b",
        re.IGNORECASE | re.MULTILINE,
    )
    return pattern.search(read_source(source)) is not None


def _run(cmd: list[str], cwd: Path, log=click.echo) -> None:
    log(f"$ {' '.join(cmd)}  (in {cwd})")
    result = subprocess.run(cmd, cwd=cwd)
    if result.returncode != 0:
        sys.exit(result.returncode)


if __name__ == "__main__":
    main()
