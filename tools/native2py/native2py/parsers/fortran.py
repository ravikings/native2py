"""Fortran parser front door — picks a backend and hides the difference.

Two backends produce the identical `ir.ModuleIR`:

* `fortran_fparser` — a real fparser2 parse tree. The whole file is parsed
  once by a standard-conforming Fortran parser, so routine boundaries,
  declaration attributes, `result(...)` clauses, module nesting and
  continuation lines are read off the tree instead of being reconstructed by
  pattern. This is the parser design.md section 8 ultimately specifies.
* `fortran_regex` — the original line/regex reader, with `fixed_form.py` as
  its fixed-form half. No grammar: each exposed name drives one targeted
  search. Kept as the fallback for machines without fparser, and selectable
  explicitly for reproducing its behaviour.

Selection order, first match wins:

1. the `backend=` argument,
2. `$NATIVE2PY_FORTRAN_PARSER` (`auto` | `fparser2` | `regex`),
3. `parser:` in native2py.yaml, passed through by the CLI,
4. `auto`.

`auto` currently resolves to `regex`. It stays there until the differential
parity harness (tests/test_fortran_fparser.py) is green across the whole
legacy corpus on a machine that is not the author's; flipping the default is
a separate, deliberate change, not a side effect of a wheel appearing on the
box. Everything else about the seam is live today, so `NATIVE2PY_FORTRAN_PARSER=fparser2`
is all it takes to run the tree parser.

Asking for `fparser2` explicitly and not having it is an error, not a silent
downgrade — the same rule the C++ side enforces. A build that quietly loses
symbols because a wheel was missing is exactly the failure this module exists
to prevent.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import ExposeConfig
from ..ir import ModuleIR
from . import fortran_fparser, fortran_regex

# Re-exported so callers that reach for the free-form continuation joiner (the
# CLI's source-rewriting paths, and a good number of tests) keep working after
# the implementation moved into fortran_regex.
from .fortran_regex import normalize_free_form  # noqa: F401

BACKENDS = ("auto", "fparser2", "regex")
_ENV_VAR = "NATIVE2PY_FORTRAN_PARSER"

# `auto` -> this. See the module docstring: not `fparser2` until parity is
# proven on the corpus by someone other than the author.
_AUTO_BACKEND = "regex"


class ParserUnavailable(RuntimeError):
    """Raised when the requested Fortran backend cannot run on this machine."""


def resolve_backend(backend: str | None = None) -> str:
    """Return "fparser2" or "regex" for a possibly-"auto" request."""
    choice = (backend or os.environ.get(_ENV_VAR) or "auto").lower()
    if choice not in BACKENDS:
        raise ParserUnavailable(
            f"Unknown Fortran parser backend '{choice}'; expected one of {', '.join(BACKENDS)}."
        )

    if choice == "regex":
        return "regex"

    if choice == "fparser2":
        if not fortran_fparser.is_available():
            raise ParserUnavailable(
                "The fparser2 parser was requested but fparser is not usable: "
                f"{fortran_fparser.unavailable_reason()}. Install it with "
                '`pip install "native2py[fparser]"`, or set parser: regex in '
                "native2py.yaml."
            )
        return "fparser2"

    if _AUTO_BACKEND == "fparser2" and fortran_fparser.is_available():
        return "fparser2"
    return "regex"


def backend_description(backend: str | None = None) -> str:
    """One line naming the backend in use, for `native2py inspect`/`generate`."""
    try:
        resolved = resolve_backend(backend)
    except ParserUnavailable as exc:
        return str(exc)
    if resolved == "fparser2":
        return f"fparser2 parse tree ({fortran_fparser.fparser_version()})"
    requested = (backend or os.environ.get(_ENV_VAR) or "").lower()
    if requested == "regex":
        return "regex reader (selected explicitly)"
    if not fortran_fparser.is_available():
        return f"regex reader (fparser unavailable: {fortran_fparser.unavailable_reason()})"
    return "regex reader (default; fparser2 available, select it with parser: fparser2)"


def parse_source(
    path: Path,
    expose: ExposeConfig,
    include_paths: list[Path] | None = None,
    *,
    backend: str | None = None,
) -> ModuleIR:
    """Parse one Fortran source into a ModuleIR using the selected backend."""
    if resolve_backend(backend) == "fparser2":
        return fortran_fparser.parse_source(path, expose, include_paths)
    return fortran_regex.parse_source(path, expose, include_paths)


def list_routine_names(path: Path, *, backend: str | None = None) -> list[str]:
    """Every function/subroutine name in the file, in source order."""
    if resolve_backend(backend) == "fparser2":
        return fortran_fparser.list_routine_names(path)
    return fortran_regex.list_routine_names(path)
