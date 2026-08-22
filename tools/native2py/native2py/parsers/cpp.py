"""C++ parser front door — picks a backend and hides the difference.

Two backends produce the identical `ir.ModuleIR`:

* `cpp_ast` — a real Clang AST parse (libclang). Runs the preprocessor, so
  `#include`s, macros and `#ifdef`s are resolved; understands templates,
  typedefs, overloads, inheritance and incompleteness properly. This is the
  parser design.md section 7 specifies, and the default whenever libclang is
  importable.
* `cpp_regex` — the original token/brace reader. No preprocessor, no template
  support, and it can only see what one header spells literally. Kept as the
  fallback for machines without libclang, and selectable explicitly for
  reproducing its behaviour.

Selection order, first match wins:

1. the `backend=` argument,
2. `$NATIVE2PY_CPP_PARSER` (`auto` | `clang` | `regex`),
3. `parser:` in native2py.yaml, passed through by the CLI,
4. `auto` — Clang if available, regex otherwise.

Asking for `clang` explicitly and not having it is an error, not a silent
downgrade: a build that quietly loses macro-defined symbols because a wheel
was missing is exactly the failure this module exists to prevent.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import ExposeConfig
from ..ir import ModuleIR
from . import cpp_ast, cpp_regex
from .cpp_ast import ClangOptions

BACKENDS = ("auto", "clang", "regex")
_ENV_VAR = "NATIVE2PY_CPP_PARSER"


class ParserUnavailable(RuntimeError):
    """Raised when the requested C++ backend cannot run on this machine."""


def resolve_backend(backend: str | None = None) -> str:
    """Return "clang" or "regex" for a possibly-"auto" request."""
    choice = (backend or os.environ.get(_ENV_VAR) or "auto").lower()
    if choice not in BACKENDS:
        raise ParserUnavailable(
            f"Unknown C++ parser backend '{choice}'; expected one of {', '.join(BACKENDS)}."
        )

    if choice == "regex":
        return "regex"

    if choice == "clang":
        if not cpp_ast.is_available():
            raise ParserUnavailable(
                "The Clang parser was requested but libclang is not usable: "
                f"{cpp_ast.unavailable_reason()}. Install it with "
                '`pip install "native2py[clang]"`, point NATIVE2PY_LIBCLANG at a '
                "libclang shared library, or set parser: regex in native2py.yaml."
            )
        return "clang"

    return "clang" if cpp_ast.is_available() else "regex"


def backend_description(backend: str | None = None) -> str:
    """One line naming the backend in use, for `native2py inspect`/`generate`."""
    try:
        resolved = resolve_backend(backend)
    except ParserUnavailable as exc:
        return str(exc)
    if resolved == "clang":
        return f"clang AST ({cpp_ast.clang_version()})"
    if (backend or os.environ.get(_ENV_VAR) or "").lower() == "regex":
        return "regex reader (selected explicitly)"
    return f"regex reader (libclang unavailable: {cpp_ast.unavailable_reason()})"


def parse_header(
    path: Path,
    expose: ExposeConfig,
    extra_known_records: frozenset[str] = frozenset(),
    *,
    backend: str | None = None,
    options: ClangOptions | None = None,
) -> ModuleIR:
    """Parse one C++ header into a ModuleIR using the selected backend."""
    if resolve_backend(backend) == "clang":
        return cpp_ast.parse_header(path, expose, extra_known_records, options)
    return cpp_regex.parse_header(path, expose, extra_known_records)


def defined_record_names(
    path: Path,
    *,
    backend: str | None = None,
    options: ClangOptions | None = None,
) -> frozenset[str]:
    """Records defined (not merely forward-declared) in this header."""
    if resolve_backend(backend) == "clang":
        return cpp_ast.defined_record_names(path, options)
    return cpp_regex.defined_record_names(path)
