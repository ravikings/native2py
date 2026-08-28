"""nativegate CLI (design.md section 13).

MVP 1 + MVP 2 scope: C++ / pybind11 / CMake and Fortran / f2py / CMake,
both end-to-end through `generate` and `build`.
"""

from __future__ import annotations

import json
import shutil
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
    PreprocessError,
    run_c_preprocessor,
    expand_includes,
    read_source,
    resolve_kind_parameters,
    uses_kind_parameters,
)
from . import golden as golden_lib
from . import invariants as invariants_lib
from . import locking
from . import oracle as oracle_lib
from . import structural_invariants as si_lib
from .suggest import POOR, READY, WORKABLE, analyse_tree
from .generators import (
    cmake_gen,
    docker_gen,
    f2py_gen,
    middleware_gen,
    gateway_gen,
    golden_gen,
    k8s_gen,
    mcp_gen,
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
            f"nativegate generated invalid Python in {path} at line {exc.lineno}: "
            f"{exc.msg}{detail}\n"
            f"    {offending}\n"
            "This is a nativegate bug — the file was not written. Please report it "
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
            f"No service '{name}' found at {path}. Run `ngate create-service {name}` first."
        )
    return path


@click.group()
@click.version_option()
def main() -> None:
    """Automated modernization of legacy scientific computing.

    Expose native C++/C/Fortran code to Python as deployable microservices,
    without rewriting the numerics and without hand-writing bindings.
    """


@main.command()
def init() -> None:
    """Scaffold the monorepo layout (design.md section 4)."""
    for d in [SERVICES_DIR, "libraries", "tools", "infrastructure/docker", "infrastructure/kubernetes"]:
        Path(d).mkdir(parents=True, exist_ok=True)
    click.echo("Initialized nativegate monorepo layout.")


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
    click.echo(f"Next: add native code under {service_dir / 'native'}, then run `ngate expose {service_dir / 'native'}`")


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
    nativegate.yaml editing.

    Runs `create-service`, copies SOURCE into native/, auto-populates
    `expose:` with every symbol found (all classes/functions for C++, every
    routine name for Fortran), then `generate`. For anything beyond "expose
    everything in one file" — partial exposure, multi-file services, huge
    legacy Fortran templates where you only want one routine — use
    create-service + nativegate.yaml + generate directly instead; see
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
                        "`ngate build` will compile and then fail to import "
                        "(undefined symbol). Add the .cpp to "
                        f"{service_dir / 'native'} and re-run `ngate generate {service_name}`."
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

    Answers "which file do I point nativegate at first?" for a codebase you
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
            f"`ngate inspect <file>` — and if the parser line above says 'regex reader', "
            'install the AST parser first: pip install "nativegate[clang]".'
        )
        return

    console.print(f"\nStart with [bold]{best.path}[/bold]:\n")
    console.print(f"  ngate quickstart {best.path} --name {best.path.stem.lower()} --build")


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
        click.echo("  (nothing exposed — add [[nativegate::expose]] or list symbols in nativegate.yaml)")


@main.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
def expose(path: Path) -> None:
    """Mark a native source as exposed and run codegen for its service.

    PATH may be a header inside services/<name>/native/, or that native/
    directory itself. Equivalent to editing nativegate.yaml + `generate`.
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
            f"service first:\n  ngate build {name}\n"
            f"  pip install services/{name}/dist/*.whl"
        ) from exc


def _require_installed_package(name: str) -> None:
    """Fail with the build instructions rather than uvicorn's import traceback."""
    import importlib.util

    if importlib.util.find_spec(name) is None:
        raise click.ClickException(
            f"The '{name}' package is not importable. `serve` runs the built "
            f"extension, so build and install the service first:\n"
            f"  ngate build {name}\n"
            f"  pip install services/{name}/dist/*.whl"
        )


def _load_service_ir(service_dir: Path, name: str) -> ModuleIR:
    ir_path = service_dir / IR_PATH
    if not ir_path.exists():
        raise click.ClickException(
            f"No {ir_path} — run `ngate generate {name}` first."
        )
    try:
        return module_from_dict(json.loads(ir_path.read_text()))
    except IRSchemaError as exc:
        # A version this nativegate cannot honestly read has to stop the command,
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

    # Hash the sources this service actually compiles. Left to its default,
    # `provenance()` records an empty `sources` map, which the generated
    # golden test rejects — so a service built once could never build again,
    # and the remedy the failure names (re-record) produced the same empty
    # map. Resolved through oracle's helper so `oracle_check` recomputes an
    # identical set rather than reporting a phantom source change.
    sources_dir, source_names = oracle_lib.compiled_sources(service_dir, module.language)
    document = golden_lib.build_document(
        module,
        entries,
        skips,
        rtol=rtol,
        atol=atol,
        environment=golden_lib.provenance(
            sources=[sources_dir / n for n in source_names]
        ),
    )

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
    _golden_verify(name)


def _golden_verify(name: str) -> None:
    service_dir = _service_dir(name)
    path = service_dir / golden_lib.GOLDEN_FILENAME
    if not path.exists():
        raise click.ClickException(
            f"No {path} — record one first with `ngate golden record {name}`."
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
            f"intended, re-record with `ngate golden record {name} --force`."
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


def _package_namespace_for(module: ModuleIR, package):
    """The object `golden.invoke()`/T10/T11's runners call attributes on --
    same rule as `oracle._package_namespace`: f2py nests everything under
    the enclosing `module X` block, if there is one."""
    if module.fortran_module:
        return getattr(package, module.fortran_module)
    return package


def _subprocess_target_for(name: str, module: ModuleIR) -> si_lib.SubprocessTarget:
    """How T10's fresh-process worker re-imports the *installed* package --
    no `sys_path` entry needed (unlike oracle's freshly-built extension,
    which lives in a temp build dir), since `_import_service_package`
    already required this be importable."""
    attr_path = (module.fortran_module,) if module.fortran_module else ()
    return si_lib.SubprocessTarget(module_name=name, sys_path=(), attr_path=attr_path)


def _invariants_verify(name: str) -> None:
    """Shared by `invariants verify` and the `verify` aggregate (spec section
    2.6: `ngate verify` runs invariants "if `invariants.json` exists" --
    read here as "if `state:`/`invariants:`/`ranges:` are declared", since
    those declarations are the source of truth and the file is a result."""
    service_dir = _service_dir(name)
    module = _load_service_ir(service_dir, name)
    try:
        config = ServiceConfig.load(service_dir)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        config.verification.validate_against_ir(module)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    golden_path = service_dir / golden_lib.GOLDEN_FILENAME
    if not golden_path.exists():
        raise click.ClickException(
            f"No {golden_path} — record one first with `ngate golden record {name}` "
            "(declared invariants are checked against golden.json's recorded arguments)."
        )
    document = golden_lib.read(golden_path)
    package = _import_service_package(name)
    target = _subprocess_target_for(name, module)

    try:
        result = invariants_lib.verify_invariants(
            module, document, config.verification, _package_namespace_for(module, package), target
        )
    except invariants_lib.EmptyCheckedError as exc:
        raise click.ClickException(str(exc)) from exc

    path = service_dir / invariants_lib.INVARIANTS_FILENAME
    invariants_lib.write(path, result.document)

    checked, uncovered = invariants_lib.coverage(result.document)
    click.echo(f"{checked} function(s) checked, {uncovered} uncovered ({path}):")
    for key, entry in result.document["checked"].items():
        click.echo(
            f"  {key}: {entry['status']} ({len(entry['properties'])} propert(y/ies), "
            f"{entry['points']} point(s))"
        )
    for key, reason in result.document["uncovered"].items():
        click.echo(f"  [uncovered] {key}: {reason}")

    if not result.passed:
        click.echo(f"{len(result.failure_messages)} invariant failure(s):")
        for message in result.failure_messages:
            click.echo(f"  - {message}")
        raise click.ClickException(f"Declared/structural invariants failed for '{name}'.")


@main.group()
def invariants() -> None:
    """Layer 3: are the declared properties true over the swept lattice?

    `verify` runs T10's structural properties (`finite`, `total`,
    `no_error_flag`, `idempotent`, `order_independent`) and T11's declared
    properties (`bounds`, `monotone`, `sum_to_one`) over golden.json's
    recorded entries, swept per nativegate.yaml's `ranges:`, and writes
    services/<name>/invariants.json (design-verification-layers.md section
    3.7).
    """


@invariants.command("verify")
@click.argument("name")
def invariants_verify(name: str) -> None:
    """Check services/<name>'s declared invariants and write invariants.json."""
    _invariants_verify(name)


def _toolchain_present() -> bool:
    """Whether *a* compiler this repo could build a driver with is on PATH --
    `oracle check` needs one (design-verification-layers.md section 5: "needs
    a compiler"); `golden`/`invariants` do not."""
    return any(shutil.which(tool) for tool in ("gfortran", "clang++", "g++"))


@main.command()
@click.argument("name")
def verify(name: str) -> None:
    """Run every verification layer for services/<name>, reporting each
    separately.

    Order (design-verification-layers.md section 2.6 / section 5's CI
    ordering, restated verbatim): **oracle check first** — a faithful
    binding is a precondition for the other two layers meaning anything —
    **then golden, then invariants**. `oracle check` needs a compiler and is
    skipped (visibly, not silently) if none is on PATH; `golden verify` and
    `invariants verify` need only the installed wheel. A failure in one
    layer is reported by name and does not stop the others from running, so
    one layer's failure can never mask another's.
    """
    service_dir = _service_dir(name)
    failed_layers: list[str] = []

    # 1. oracle check --------------------------------------------------------
    if not _toolchain_present():
        click.echo("oracle: skipped, no toolchain")
    else:
        try:
            report = oracle_lib.oracle_check(name, service_dir=service_dir)
        except oracle_lib.OracleError as exc:
            click.echo(f"oracle: FAILED — {exc}")
            failed_layers.append("oracle")
        else:
            for line in report.failures:
                click.echo(f"  - {line}")
            if report.passed:
                click.echo(
                    f"oracle: passed ({report.covered} covered, {len(report.skipped)} skipped)"
                )
            else:
                click.echo(f"oracle: FAILED ({len(report.failures)} failure(s))")
                failed_layers.append("oracle")

    # 2. golden ---------------------------------------------------------------
    try:
        _golden_verify(name)
    except click.ClickException as exc:
        click.echo(f"golden: FAILED — {exc.message}")
        failed_layers.append("golden")
    else:
        click.echo("golden: passed")

    # 3. invariants -------------------------------------------------------------
    config = None
    config_invalid = False
    try:
        config = ServiceConfig.load(service_dir)
    except FileNotFoundError:
        pass
    except ConfigError as exc:
        # A malformed nativegate.yaml must be reported as this layer's
        # failure, same as every other invariants error below — never let it
        # propagate as a raw traceback that aborts golden/oracle's already
        # -reported results too.
        click.echo(f"invariants: FAILED — {exc}")
        failed_layers.append("invariants")
        config_invalid = True

    if config_invalid:
        pass
    elif config is None or config.verification.is_empty:
        click.echo("invariants: skipped, no state/invariants/ranges declared")
    else:
        try:
            _invariants_verify(name)
        except click.ClickException as exc:
            click.echo(f"invariants: FAILED — {exc.message}")
            failed_layers.append("invariants")
        else:
            click.echo("invariants: passed")

    if failed_layers:
        raise click.ClickException(
            f"verification failed for '{name}': {', '.join(failed_layers)} layer(s) failed."
        )


@main.group()
def oracle() -> None:
    """Layer 2: is the binding faithful to the legacy binary, not just unchanged?

    `check` generates a native driver from the entries recorded in
    golden.json (never from a fresh sample plan — see design-verification-
    layers.md section 2.2), builds and runs it, executes the same calls
    through the Python binding in the same build, and compares every
    observable value bitwise. It regenerates and recompiles the driver every
    time — there is no committed file to go stale, and none is needed to
    fail a build (design-verification-layers.md section 2.6).
    """


@oracle.command("check")
@click.argument("name")
def oracle_check(name: str) -> None:
    """Generate, build and run the oracle driver for services/<name>, and
    compare it bitwise against the Python binding, in this build."""
    service_dir = _service_dir(name)
    try:
        report = oracle_lib.oracle_check(name, service_dir=service_dir)
    except oracle_lib.OracleError as exc:
        raise click.ClickException(str(exc)) from exc

    if report.failures:
        click.echo(f"{len(report.failures)} oracle failure(s):")
        for line in report.failures:
            click.echo(f"  - {line}")

    click.echo(
        f"{report.covered} covered, {len(report.skipped)} skipped"
        + (
            " (" + ", ".join(f"{k}: {v}" for k, v in report.skipped.items()) + ")"
            if report.skipped
            else ""
        )
    )

    if report.historical_diff_refused:
        click.echo(report.historical_diff_refused)
    elif report.historical_diff:
        click.echo(f"{len(report.historical_diff)} bit(s) differ from the recorded oracle.json:")
        for line in report.historical_diff:
            click.echo(f"  - {line}")

    if not report.passed:
        raise click.ClickException(
            f"The oracle disagrees with the Python binding for '{name}' — "
            "the binding is not faithful to the native code it was "
            "generated from, or the check covered nothing."
        )


@oracle.command("record")
@click.argument("name")
def oracle_record(name: str) -> None:
    """Run a full oracle check for services/<name> and, on pass, write
    oracle.json — provenance, not the gate (design-verification-layers.md
    section 2.6). A future `check` in the same build image additionally
    diffs against it bitwise; anywhere else, the CLI refuses that historical
    comparison rather than falling back to a tolerance."""
    service_dir = _service_dir(name)
    try:
        document = oracle_lib.oracle_record(name, service_dir=service_dir)
    except oracle_lib.OracleError as exc:
        raise click.ClickException(str(exc)) from exc

    path = service_dir / oracle_lib.ORACLE_FILENAME
    covered, skipped = len(document["entries"]), len(document["skipped"])
    click.echo(f"Recorded {covered} entry point(s) to {path}.")
    if skipped:
        click.echo(f"{skipped} entry point(s) not covered:")
        for key, reason in document["skipped"].items():
            click.echo(f"  - {key}: {reason}")


@oracle.command("show")
@click.argument("name")
def oracle_show(name: str) -> None:
    """Print services/<name>'s golden.json entries, their expected wire
    slots, and the skips — without building, compiling, or running anything."""
    service_dir = _service_dir(name)
    try:
        report = oracle_lib.oracle_show(name, service_dir=service_dir)
    except oracle_lib.OracleError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"{len(report.entries)} entries, {len(report.skipped)} skipped:")
    for entry in report.entries:
        args = ", ".join(repr(a) for a in entry.arguments)
        if entry.slots is None:
            click.echo(f"  {entry.key}({args}) -> [no matching function in the IR]")
        else:
            click.echo(f"  {entry.key}({args}) -> {', '.join(entry.slots) or '(no observable slots)'}")
    for key, reason in report.skipped.items():
        click.echo(f"  [skipped] {key}: {reason}")


@main.command()
@click.argument("name")
@click.option("--host", default="127.0.0.1", show_default=True, help="Interface to bind.")
@click.option("--port", default=8000, show_default=True, help="Port to bind.")
@click.option("--reload", is_flag=True, help="Restart on source changes (development only).")
def serve(name: str, host: str, port: int, reload: bool) -> None:
    """Run services/<name>'s FastAPI app with uvicorn.

    A convenience wrapper, not a deployment: it runs a single uvicorn process
    against the *installed* package. The generated Dockerfile runs gunicorn
    with uvicorn workers, and that is what should serve real traffic. The
    default bind is loopback, because a generated service is not hardened for
    an untrusted network (see docs/production-readiness.md).
    """
    _service_dir(name)
    _require_installed_package(name)
    import importlib.util

    if importlib.util.find_spec("uvicorn") is None:
        raise click.ClickException(
            "uvicorn is not installed in this interpreter. Install the "
            f"service's own dependencies:\n"
            f"  pip install services/{name}/dist/*.whl"
        )

    cmd = [sys.executable, "-m", "uvicorn", f"{name}.service:app",
           "--host", host, "--port", str(port)]
    if reload:
        cmd.append("--reload")
    _run(cmd, cwd=Path.cwd())


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
    # `libraries:` means two different things by language, and the Dockerfile
    # only has to care about one of them. C++ LINKS a CMake target that lives
    # outside the service, so the build context has to be the repo root. The
    # f2py path has no target to link: `generate` already copied those sources
    # into native/_expanded/, so everything the build needs is under the
    # service directory and the simpler context still works. Passing the
    # libraries through here would switch the context and emit a
    # `COPY libraries/...` for sources that are already vendored.
    libraries = [] if config.language == "fortran" else _validated_libraries(config)
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
            f"build time, so two builds can differ. Run `ngate lock {name}`."
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
    click.echo(f"  Re-run `ngate docker {name}` so the Dockerfile installs from it.")


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
        mcp_source = mcp_gen.generate_mcp_py(name, list(services))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    # One MCP endpoint covering every mounted service, built from the same
    # prefixed routers the gateway app mounts.
    _write_python(package_dir / mcp_gen.MCP_FILENAME, mcp_source)
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
        "# Run `ngate expose` then `ngate generate` to populate this package.\n"
    )
    (service_dir / "pyproject.toml").write_text(pyproject_gen.generate_pyproject(name, language))
    return service_dir


def _resolve_service_dir(path: Path) -> Path:
    for parent in [path] + list(path.parents):
        if (parent / "nativegate.yaml").exists():
            return parent
        if parent.name == "native" and (parent.parent / "nativegate.yaml").exists():
            return parent.parent
    raise click.ClickException(
        f"Could not find a nativegate.yaml above {path}. Run `ngate create-service` first."
    )


def _generate_service(service_dir: Path, config: ServiceConfig) -> None:
    if config.language == "cpp":
        _generate_cpp_service(service_dir, config)
    elif config.language == "fortran":
        _generate_fortran_service(service_dir, config)
    else:
        raise click.ClickException(f"Unsupported language '{config.language}'.")


IR_PATH = Path(".nativegate") / "ir.json"


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
    # The MCP view of the same router, mounted by service.py at /mcp. Written
    # before service.py only for readability; neither imports at generate time.
    _write_python(
        package_dir / mcp_gen.MCP_FILENAME,
        mcp_gen.generate_mcp_py(config.name),
        module,
    )
    _write_python(
        package_dir / "service.py", python_pkg_gen.generate_service_py(config.name), module
    )

    tests_dir = service_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    _write_python(
        tests_dir / "test_mcp.py",
        mcp_gen.generate_mcp_smoke_test(config.name),
        module,
    )
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


def _fortran_library_inputs(config: ServiceConfig) -> tuple[list[Path], list[Path]]:
    """Fortran sources and INCLUDE directories from this service's `libraries:`.

    THE GAP THIS CLOSES

    `libraries:` was C++-only. The C++ path links a shared library through
    CMake `add_subdirectory` + `target_link_libraries`; f2py has no equivalent,
    because `f2py -c` takes a list of SOURCES and compiles them itself. So a
    Fortran service could name a library and get nothing: `services/petro_api`
    bound the ten routines of its F90 facade, built an extension, and failed at
    import with `undefined symbol: iprvog_` — the seven F77 decks the facade
    calls into were never compiled.

    The fix is the mechanism nativegate already uses everywhere else: the
    library's sources are expanded into `native/_expanded/` alongside the
    service's own and handed to f2py as ordinary sources. The original tree
    stays read-only, and the Docker build context stays the service directory,
    because everything the build needs has been copied under it.

    INCLUDE directories are discovered rather than demanded. A legacy tree
    keeps its COMMON blocks in `include/*.INC` next to the decks; requiring the
    user to also spell that out in `include_paths:` would be asking them to
    restate something the library already says. Explicit `include_paths:` still
    win — these are added to them, not instead.
    """
    sources: list[Path] = []
    include_dirs: list[Path] = []

    for name in config.libraries:
        lib_dir = Path(LIBRARIES_DIR) / name
        if not lib_dir.is_dir():
            raise click.ClickException(
                f"Service '{config.name}' declares library '{name}' but "
                f"{lib_dir} does not exist."
            )

        # Deliberately NOT requiring a CMakeLists.txt the way the C++ path
        # does: that file declares a CMake target, and f2py never uses one.
        found = sorted(
            path
            for path in lib_dir.rglob("*")
            if path.suffix.lower() in (".f", ".f90", ".f77", ".for")
            and "_expanded" not in path.parts
        )
        if not found:
            raise click.ClickException(
                f"Service '{config.name}' is Fortran and declares library "
                f"'{name}', but {lib_dir} contains no Fortran sources. (A C++ "
                "library cannot be linked into an f2py extension.)"
            )
        sources += found

        include_dirs += sorted(
            {
                path.parent
                for path in lib_dir.rglob("*")
                if path.suffix.lower() == ".inc"
            }
        )

    return sources, include_dirs


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
    """nativegate.yaml's `clang:` block as parser options."""
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
                    "nativegate binds all headers of a service into one extension, so "
                    "symbol names must be unique across them — rename one, or expose "
                    "only one of the headers via `expose:` in nativegate.yaml."
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
            "Check `expose:` in nativegate.yaml, or that the headers declare "
            "classes/functions nativegate can parse."
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
            "headers declare methods without inline bodies. `ngate build` will "
            "succeed and then fail on first import with an undefined-symbol error. "
            "Add the implementation file(s) and re-run `ngate generate "
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


def _with_shims(text: str, shims: list[str], source: Path) -> str:
    """Insert generated flattening shims into a module's `_expanded` copy.

    Before `end module`, because the shim constructs the derived type and that
    type is only visible from inside the module that defines it. This edits
    the GENERATED copy only — the original tree stays read-only, same rule as
    every other transform under `_expanded/`.
    """
    if not shims:
        return text
    import re as _re

    match = None
    for match in _re.finditer(r"(?im)^\s*end\s+module\b.*$", text):
        pass  # keep the last one
    if match is None:
        raise click.ClickException(
            f"{source.name}: routines here need a flattening shim, but no "
            "`end module` line was found to anchor it. This is a nativegate "
            "bug — please report it with the source layout."
        )
    insertion = "\n" + "\n\n".join(shims) + "\n\n"
    return text[: match.start()] + insertion + text[match.start() :]


def _generate_fortran_service(service_dir: Path, config: ServiceConfig) -> None:
    if not config.expose.functions:
        raise click.ClickException(
            "Fortran services require an `expose.functions:` list in nativegate.yaml "
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

    # Preprocessed Fortran (.F90/.F, or #ifdef in a lowercase file) is not
    # Fortran yet — it is INPUT to the C preprocessor, and parsing it directly
    # reads every conditional branch as simultaneously live. It used to be
    # warned about and mis-read; it is now preprocessed with gfortran's own
    # `-cpp -E` into native/_expanded/ BEFORE anything parses it, so discovery,
    # intent inference and f2py all see the code gfortran would have compiled.
    preprocessed_set: set[Path] = set()
    needs_cpp = [s for s in sources if requires_preprocessing(s)]
    if needs_cpp:
        expanded_dir = service_dir / "native" / "_expanded"
        expanded_dir.mkdir(parents=True, exist_ok=True)
        replaced: list[Path] = []
        for source in sources:
            if source not in needs_cpp:
                replaced.append(source)
                continue
            target = expanded_dir / (source.stem + source.suffix.lower())
            try:
                target.write_text(
                    run_c_preprocessor(
                        source, include_paths, config.fortran_defines
                    )
                )
            except PreprocessError as exc:
                raise click.ClickException(str(exc)) from exc
            replaced.append(target)
            preprocessed_set.add(target)
        sources = replaced
        click.echo(
            f"Preprocessed {len(needs_cpp)} source(s) with gfortran -cpp -E "
            f"-> {expanded_dir}"
            + (
                f" (defines: {', '.join(config.fortran_defines)})"
                if config.fortran_defines
                else ""
            )
        )

    # Sources from `libraries:` are compiled INTO the extension — f2py links no
    # external target — so they join the service's own before anything below
    # classifies or expands them. See _fortran_library_inputs.
    library_sources, library_includes = _fortran_library_inputs(config)
    include_paths += [d for d in library_includes if d not in include_paths]

    # A library file whose name matches one the service already has is the
    # facade-was-copied-in case: services/petro_api/native/petro_api.f90 is the
    # same routine as libraries/petro/fortran/modern/petro_api.f90. Compiling
    # both would be a duplicate-symbol link error, so the service's copy wins —
    # it is the one that was parsed and whose intents were inferred.
    own_names = {source.name for source in sources}
    shadowed = [lib for lib in library_sources if lib.name in own_names]
    library_sources = [lib for lib in library_sources if lib.name not in own_names]
    for lib in shadowed:
        click.echo(f"Using native/{lib.name} instead of {lib} (same file name).")

    by_name: dict[str, Path] = {}
    for lib in library_sources:
        clash = by_name.get(lib.name)
        if clash is not None:
            raise click.ClickException(
                f"Two library sources are both named '{lib.name}' ({clash} and "
                f"{lib}). They would collide in native/_expanded/ and produce "
                "duplicate symbols. Rename one, or expose only one library."
            )
        by_name[lib.name] = lib

    if library_sources:
        click.echo(
            f"Compiling {len(library_sources)} source(s) from "
            f"{', '.join(config.libraries)} into the extension."
        )

    # A requested routine may live in any one of several native/*.f90 files
    # (common for large template libraries split across many files); search
    # each source until every requested name is found, rather than requiring
    # the caller to say which file it's in.
    remaining = list(config.expose.functions)
    module = None
    intents_by_source: dict[Path, dict[str, dict[str, tuple[str, bool]]]] = {}
    shims_by_source: dict[Path, list[str]] = {}

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
        shims_by_source[source] = list(parsed.fortran_shims)
        if module is None:
            module = parsed
        else:
            module.fortran_shims.extend(parsed.fortran_shims)
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
    # From here on `sources` means "everything that gets compiled", not "the
    # files searched for exposed routines" — the routine search above is
    # deliberately limited to the service's own native/ directory.
    sources = sources + library_sources

    for source in sources:
        directives = find_cpp_directives(read_source(source))
        if requires_preprocessing(source):
            click.echo(
                f"WARNING: {source.name} needs the C preprocessor"
                + (f" (found {', '.join('#' + d for d in directives[:4])})" if directives else "")
                + ". nativegate has no preprocessor, so conditional code is read "
                "as though every branch is active. Pre-process it yourself and "
                "point nativegate at the output if the branches differ."
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
    # A source whose routines needed a flattening shim must be copied so the
    # shim has somewhere to live — even when nothing else forces a rewrite.
    shim_only_sources = [
        s
        for s in sources
        if shims_by_source.get(s)
        and s not in fixed_form_sources
        and s not in free_form_include_sources
        and s not in kind_param_sources
    ]

    rewritten = (
        fixed_form_sources
        + free_form_include_sources
        + kind_param_sources
        + shim_only_sources
    )
    # A library source is always copied under the service, even when it needs
    # no rewriting: it lives outside native/, so referencing it as
    # `native/<name>` would name a file that is not there — and the Docker
    # build context is the service directory, so a path outside it could not be
    # COPYed in anyway.
    library_set = set(library_sources)
    if rewritten or library_sources or preprocessed_set:
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
                target.write_text(
                    _with_shims(
                        resolve_kind_parameters(read_source(source)),
                        shims_by_source.get(source, []),
                        source,
                    )
                )
                native_sources.append(f"native/_expanded/{source.name}")
            elif source in shim_only_sources:
                target = expanded_dir / source.name
                target.write_text(
                    _with_shims(
                        read_source(source), shims_by_source[source], source
                    )
                )
                native_sources.append(f"native/_expanded/{source.name}")
            elif source in library_set:
                target = expanded_dir / source.name
                target.write_text(read_source(source))
                native_sources.append(f"native/_expanded/{source.name}")
            elif source in preprocessed_set:
                # Already written under _expanded/ by the preprocessor pass.
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
            # The shim name where one exists: f2py must wrap the flattened
            # entry point, not the original whose derived argument it cannot
            # express.
            only_routines=[fn.cpp_name or fn.name for fn in module.functions],
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
