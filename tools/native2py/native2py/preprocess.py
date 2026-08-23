"""Fortran INCLUDE expansion.

Legacy F77 keeps its COMMON blocks and PARAMETERs in .INC files pulled in
with `INCLUDE 'PETRO.INC'` inside each routine body. f2py accepts an
--include-paths flag and *appears* to handle these, but the wrapper it
generates for a routine containing an in-body INCLUDE is silently broken:
arguments never reach the Fortran side, so calls return uninitialized
memory instead of an error. Verified against libraries/petro:

    INCLUDE 'PETRO.INC' present   -> probe1(35.0) == 1.09e-314   (garbage)
    same IMPLICIT rules, no INCLUDE -> probe1(35.0) == 70.0      (correct)
    INCLUDE expanded textually    -> probe1(35.0) == 70.0        (correct)

So native2py expands INCLUDEs itself and hands f2py a flat source file.
This is a correctness requirement, not an optimization — without it the
bindings compile, import, and return plausible-looking wrong numbers.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

_INCLUDE_RE = re.compile(r"^\s*INCLUDE\s+['\"]([^'\"]+)['\"]", re.IGNORECASE)

MAX_INCLUDE_DEPTH = 25


class IncludeError(ValueError):
    """An INCLUDE could not be resolved against the configured search path."""


class EncodingError(ValueError):
    """A source file could not be decoded with the encoding the user named."""


class SourceEncodingWarning(UserWarning):
    """A source file was not valid UTF-8 and was decoded as latin-1 instead."""


# Comment styles for the marker lines expand_includes writes around an inlined
# file. A `C` in column 1 is a comment in FIXED-form Fortran only; in free-form
# it is a syntax error, so free-form callers need `!`.
COMMENT_STYLES = {"fixed": "C     ", "free": "! "}

DEFAULT_FALLBACK_ENCODING = "latin-1"


def read_source(path: Path, encoding: str | None = None) -> str:
    """Decode `path` honestly.

    UTF-8 is tried first. If it fails, the file is decoded as latin-1 — which
    never fails, and is the right guess for decks with VMS/mainframe lineage —
    and the fallback is reported as a `SourceEncodingWarning` rather than
    swallowed. Passing `encoding` states the real encoding; a decode failure
    with an explicit encoding is an error, never a silent substitution.

    Undecodable bytes are never replaced with U+FFFD.
    """
    raw = path.read_bytes()
    if encoding is not None:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError) as exc:
            raise EncodingError(
                f"{path}: cannot decode as '{encoding}': {exc}. "
                "Set the correct encoding for this source."
            ) from exc

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        text = raw.decode(DEFAULT_FALLBACK_ENCODING)
        warnings.warn(
            f"{path}: not valid UTF-8 ({exc.reason} at byte {exc.start}); "
            f"decoded as {DEFAULT_FALLBACK_ENCODING} instead. If that is wrong, "
            "state the real encoding explicitly.",
            SourceEncodingWarning,
            stacklevel=2,
        )
        return text


def _resolve(name: str, search_paths: list[Path]) -> Path | None:
    for directory in search_paths:
        candidate = directory / name
        if candidate.is_file():
            return candidate
        # Legacy sources are inconsistent about case ('petro.inc' vs 'PETRO.INC'),
        # which matters on case-sensitive filesystems even when it worked on the
        # original VMS/Windows host.
        for existing in directory.iterdir() if directory.is_dir() else []:
            if existing.is_file() and existing.name.lower() == name.lower():
                return existing
    return None


def expand_includes(
    source_path: Path,
    search_paths: list[Path] | None = None,
    _depth: int = 0,
    _stack: tuple[str, ...] = (),
    *,
    comment_style: str = "fixed",
    encoding: str | None = None,
) -> str:
    """Return the source of `source_path` with every INCLUDE inlined.

    `comment_style` selects the marker-line comment character: "fixed" (the
    default, `C` in column 1) for fixed-form sources, "free" (`!`) for
    free-form, where a column-1 `C` is a syntax error.
    """
    if comment_style not in COMMENT_STYLES:
        raise ValueError(
            f"Unknown comment_style {comment_style!r}; expected one of "
            f"{sorted(COMMENT_STYLES)}."
        )
    marker = COMMENT_STYLES[comment_style]

    if _depth > MAX_INCLUDE_DEPTH:
        raise IncludeError(
            f"INCLUDE nesting deeper than {MAX_INCLUDE_DEPTH} while expanding "
            f"{source_path} (cycle? chain: {' -> '.join(_stack)})"
        )

    search_paths = search_paths or []
    # An INCLUDE is resolved relative to the including file first, matching
    # the behaviour every Fortran compiler implements.
    local_paths = [source_path.parent, *search_paths]

    lines: list[str] = []
    for line in read_source(source_path, encoding).splitlines():
        match = _INCLUDE_RE.match(line)
        if not match:
            lines.append(line)
            continue

        include_name = match.group(1)
        resolved = _resolve(include_name, local_paths)
        if resolved is None:
            searched = ", ".join(str(p) for p in local_paths)
            raise IncludeError(
                f"{source_path.name}: cannot resolve INCLUDE '{include_name}'. "
                f"Searched: {searched}. Add the directory to `include_paths:` in native2py.yaml."
            )

        lines.append(f"{marker}--- native2py: expanded INCLUDE '{include_name}' ---")
        lines.append(
            expand_includes(
                resolved,
                search_paths,
                _depth + 1,
                _stack + (source_path.name,),
                comment_style=comment_style,
                encoding=encoding,
            )
        )
        lines.append(f"{marker}--- native2py: end INCLUDE '{include_name}' ---")

    return "\n".join(lines)


def find_include_files(root: Path) -> list[Path]:
    """Locate .INC/.inc files under `root`, for reporting and dependency tracking."""
    return sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".inc",)
    )


# --- kind-parameter resolution ------------------------------------------
#
# f2py builds a wrapper file (`-f2pywrappers2.f90`) that redeclares each
# routine's arguments, but it does NOT carry the module's PARAMETER
# declarations across into that wrapper. So the standard modern idiom
#
#     integer, parameter :: dp = kind(1.0d0)
#     real(dp), intent(in) :: x
#
# compiles as `real(dp)` in a scope where `dp` does not exist. gfortran then
# reports "Parameter 'dp' has not been declared", silently degrades the
# argument to REAL(4), and the build dies ~200 lines later with a type
# mismatch that names neither `dp` nor the line that caused it.
#
# Substituting the resolved literal (`real(8)`) makes the same source build
# unchanged. This is done on a generated copy — the original file is never
# modified.

_KIND_PARAM_RE = re.compile(
    r"^\s*integer\s*(?:\([^)]*\))?\s*,\s*parameter\s*::\s*(?P<assignments>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# kind(1.0d0) / selected_real_kind(15, 307) / 8
_KIND_VALUE_RE = re.compile(
    r"^(?P<name>\w+)\s*=\s*(?P<expr>.+?)\s*$",
)


def _kind_literal(expr: str) -> str | None:
    """Resolve a kind-defining expression to its literal value."""
    text = expr.strip().lower().replace(" ", "")

    if text.isdigit():
        return text

    match = re.fullmatch(r"kind\((.+)\)", text)
    if match:
        literal = match.group(1)
        if "d" in literal:
            return "8"
        if re.fullmatch(r"[\d.]+(e[-+]?\d+)?", literal):
            return "4"
        return None

    match = re.fullmatch(r"selected_real_kind\((?P<args>.*)\)", text)
    if match:
        args = [a for a in match.group("args").split(",") if a]
        try:
            precision = int(re.sub(r"^p=", "", args[0])) if args else 6
        except ValueError:
            return None
        return "8" if precision > 6 else "4"

    match = re.fullmatch(r"selected_int_kind\((?P<args>.*)\)", text)
    if match:
        return None  # integer kinds are not substituted; f2py handles them

    return None


def resolve_kind_parameters(source: str) -> str:
    """Replace `real(dp)` with `real(8)` for kind parameters defined in-file.

    Only substitutes names it can resolve to a literal; anything else is left
    exactly as written so an unresolvable kind still fails loudly at compile
    time rather than being silently rewritten to the wrong width.
    """
    kinds: dict[str, str] = {}
    for decl in _KIND_PARAM_RE.finditer(source):
        for assignment in decl.group("assignments").split(","):
            # A kind expression can itself contain a comma
            # (selected_real_kind(15, 307)); only split on top-level ones.
            match = _KIND_VALUE_RE.match(assignment.strip())
            if not match:
                continue
            literal = _kind_literal(match.group("expr"))
            if literal:
                kinds[match.group("name").lower()] = literal

    # Recover comma-split kind expressions like selected_real_kind(15, 307).
    for decl in _KIND_PARAM_RE.finditer(source):
        for match in re.finditer(
            r"(?P<name>\w+)\s*=\s*(?P<expr>selected_real_kind\s*\([^)]*\)|kind\s*\([^)]*\))",
            decl.group("assignments"),
            re.IGNORECASE,
        ):
            literal = _kind_literal(match.group("expr"))
            if literal:
                kinds[match.group("name").lower()] = literal

    if not kinds:
        return source

    def substitute(match: re.Match) -> str:
        base, kind_name = match.group("base"), match.group("kind")
        literal = kinds.get(kind_name.lower())
        return f"{base}({literal})" if literal else match.group(0)

    return re.sub(
        r"\b(?P<base>real|complex|integer|logical)\s*\(\s*(?:kind\s*=\s*)?(?P<kind>[a-z_]\w*)\s*\)",
        substitute,
        source,
        flags=re.IGNORECASE,
    )


def uses_kind_parameters(source: str) -> bool:
    """True if `source` would be changed by resolve_kind_parameters."""
    return resolve_kind_parameters(source) != source
