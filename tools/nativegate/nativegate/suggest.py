"""Rank the native sources in a tree by how cleanly they would bind.

The information needed to pick a starting file already exists — `inspect`
prints it — but only one file at a time, for someone who already knows which
files are candidates. Someone meeting a C++ codebase for the first time does
not, and the cost of guessing wrong is a service that half-binds and fails
later. This module runs the parse across a whole tree and sorts the results,
so the answer to "which file do I point at" is the first row of a table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import ExposeConfig
from .discovery import find_implementation_files, find_native_sources
from .ir import ModuleIR
from .parsers import cpp as cpp_parser
from .parsers.cpp_ast import ClangOptions
from .parsers import fortran as fortran_parser

# Verdicts, in ranking order. READY files bind everything they declare;
# WORKABLE ones bind more than they skip; POOR ones bind nothing useful.
READY = "ready"
WORKABLE = "workable"
POOR = "poor"

_QUOTED_INCLUDE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.MULTILINE)

# Headers-only extensions: these are what you point nativegate at. A bare .cpp
# is an implementation file, not an interface, so it is not offered as a
# candidate unless nothing else exists.
_HEADER_SUFFIXES = {".hpp", ".hh", ".h"}


@dataclass
class Candidate:
    """One native source, and how well it would bind."""

    path: Path
    language: str
    verdict: str
    bound: int = 0
    skipped: int = 0
    classes: int = 0
    structs: int = 0
    functions: int = 0
    methods: int = 0
    impl_files: list[Path] = field(default_factory=list)
    local_includes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def sort_key(self) -> tuple:
        # Dependencies dominate, ahead of skip count: one skipped array method
        # out of twenty costs you that method, while one #include of another
        # subsystem's header costs you that whole subsystem's build. Only
        # outright unusable files (POOR) sort below a dependency-heavy one.
        return (
            1 if self.verdict == POOR else 0,
            len(self.local_includes),
            self.skipped,
            -self.bound,
            str(self.path),
        )

    def binds_summary(self) -> str:
        parts = []
        if self.functions:
            parts.append(f"{self.functions} fn")
        if self.classes:
            parts.append(f"{self.classes} class")
        if self.methods:
            parts.append(f"{self.methods} method")
        if self.structs:
            parts.append(f"{self.structs} struct")
        return ", ".join(parts) or "nothing"

    def notes(self) -> str:
        if self.error:
            return self.error
        notes = []
        if self.impl_files:
            notes.append(", ".join(f.name for f in self.impl_files))
        elif self.language == "cpp" and (self.methods or self.functions):
            notes.append("no .cpp found")
        if self.local_includes:
            shown = sorted(self.local_includes)
            extra = ""
            if len(shown) > 2:
                shown, extra = shown[:2], f" +{len(self.local_includes) - 2}"
            notes.append("needs " + ", ".join(shown) + extra)
        return "; ".join(notes)


def _local_includes(source: Path, impl_files: list[Path]) -> list[str]:
    """Quoted #includes that resolve to a real neighbouring header.

    Scans the implementation files too: a header can look self-contained
    while its .cpp pulls in a bridge to another subsystem, and that
    dependency is exactly what makes a file a poor first choice.
    """
    found: set[str] = set()
    for path in [source, *impl_files]:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for name in _QUOTED_INCLUDE.findall(text):
            included = Path(name).name
            if Path(included).stem == source.stem:
                continue  # the .cpp including its own header
            # Only count it if it actually exists nearby — a missing include
            # is a build problem, not a dependency worth ranking on.
            if (path.parent / name).exists() or (source.parent / included).exists():
                found.add(included)
    return sorted(found)


def _classify(module: ModuleIR, local_includes: list[str]) -> tuple[str, dict]:
    methods = sum(len(cls.methods) for cls in module.classes)
    counts = {
        "classes": len(module.classes),
        "structs": len(module.structs),
        "functions": len(module.functions),
        "methods": methods,
    }
    bound = methods + len(module.functions) + len(module.structs)
    skipped = len(module.skipped)

    if bound == 0 or skipped >= bound:
        verdict = POOR
    elif skipped == 0 and not local_includes:
        # Turnkey: everything it declares binds, and it drags nothing else in.
        verdict = READY
    else:
        verdict = WORKABLE
    return verdict, {"bound": bound, "skipped": skipped, **counts}


def _analyse_cpp(source: Path, backend: str, options: ClangOptions) -> Candidate:
    impl_files = find_implementation_files(source)
    try:
        module = cpp_parser.parse_header(
            source, ExposeConfig(), backend=backend, options=options
        )
    except Exception as exc:  # a header that will not parse is a real answer
        return Candidate(
            path=source,
            language="cpp",
            verdict=POOR,
            impl_files=impl_files,
            error=f"parse failed: {type(exc).__name__}",
        )

    local_includes = _local_includes(source, impl_files)
    verdict, counts = _classify(module, local_includes)
    return Candidate(
        path=source,
        language="cpp",
        verdict=verdict,
        impl_files=impl_files,
        local_includes=local_includes,
        **counts,
    )


def _analyse_fortran(source: Path) -> Candidate:
    try:
        routines = fortran_parser.list_routine_names(source)
    except Exception as exc:
        return Candidate(
            path=source,
            language="fortran",
            verdict=POOR,
            error=f"parse failed: {type(exc).__name__}",
        )
    return Candidate(
        path=source,
        language="fortran",
        verdict=READY if routines else POOR,
        bound=len(routines),
        functions=len(routines),
    )


def analyse_tree(
    root: Path,
    backend: str = "auto",
    options: ClangOptions | None = None,
) -> list[Candidate]:
    """Parse every native source under `root`, best starting point first."""
    options = options or ClangOptions()
    candidates: list[Candidate] = []

    cpp_sources = find_native_sources(root, "cpp")
    headers = [p for p in cpp_sources if p.suffix.lower() in _HEADER_SUFFIXES]
    # Prefer headers; fall back to .cpp only for projects that have no headers
    # at all, where the .cpp really is the interface.
    for source in headers or cpp_sources:
        candidates.append(_analyse_cpp(source, backend, options))

    for source in find_native_sources(root, "fortran"):
        candidates.append(_analyse_fortran(source))

    return sorted(candidates, key=lambda c: c.sort_key)
