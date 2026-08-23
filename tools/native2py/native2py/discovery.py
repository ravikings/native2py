"""Language detection and source discovery (design.md section 5, 13)."""

from __future__ import annotations

import re
from pathlib import Path

# Fixed-form (.f/.for/.f77) vs free-form (.f90+) matters beyond discovery:
# they have different comment, continuation, and column rules, so the parser
# needs to know which dialect it's reading.
_FIXED_FORM_SUFFIXES = {".f", ".for", ".f77"}
_FREE_FORM_SUFFIXES = {".f90", ".f95", ".f03", ".f08"}

_EXTENSIONS = {
    ".hpp": "cpp",
    ".hh": "cpp",
    ".h": "cpp",
    ".cpp": "cpp",
    ".cc": "cpp",
    **{s: "fortran" for s in _FIXED_FORM_SUFFIXES},
    **{s: "fortran" for s in _FREE_FORM_SUFFIXES},
}


def detect_language(path: Path) -> str | None:
    return _EXTENSIONS.get(path.suffix.lower())


def is_fixed_form(path: Path) -> bool:
    """True for FORTRAN 77 fixed-form source (comment in col 1, continuation in col 6)."""
    return path.suffix.lower() in _FIXED_FORM_SUFFIXES


# --- C-preprocessed Fortran (.F90/.F/.FOR) ------------------------------
#
# By convention an UPPERCASE Fortran suffix means the file is run through the
# C preprocessor before the compiler sees it. native2py has no preprocessor,
# so parsing such a file treats every #ifdef branch as live — the wrong code
# is bound and nothing says so. detect_language deliberately still maps these
# by lowercased suffix; these predicates let the caller refuse or warn.

_PREPROCESSED_SUFFIXES = {".F", ".FOR", ".F77", ".F90", ".F95", ".F03", ".F08", ".FPP"}

_CPP_DIRECTIVE_RE = re.compile(
    r"^\s*#\s*(include|define|undef|if|ifdef|ifndef|elif|else|endif|error|pragma|line)\b",
    re.MULTILINE,
)


def has_uppercase_preprocessed_suffix(path: Path) -> bool:
    """True for the `.F90`/`.F`/`.FOR` spelling that means "run cpp first"."""
    return path.suffix in _PREPROCESSED_SUFFIXES


def find_cpp_directives(source: str) -> list[str]:
    """Report which C-preprocessor directives appear in `source`.

    Returns the directive names in order of first appearance ("ifdef",
    "include", ...), so a caller can say exactly what it cannot handle.
    """
    found: list[str] = []
    for match in _CPP_DIRECTIVE_RE.finditer(source):
        name = match.group(1).lower()
        if name not in found:
            found.append(name)
    return found


def requires_preprocessing(path: Path, source: str | None = None) -> bool:
    """True if `path` needs the C preprocessor before it can be parsed correctly.

    Either the uppercase suffix convention or the actual presence of cpp
    directives is enough — lowercase `.f90` files with `#ifdef` in them are
    common, and mis-parse identically.
    """
    if detect_language(path) != "fortran":
        return False
    if has_uppercase_preprocessed_suffix(path):
        return True
    if source is None:
        try:
            source = path.read_text(errors="replace")
        except OSError:
            return False
    return bool(find_cpp_directives(source))


# native2py writes INCLUDE-expanded copies under native/_expanded/. They must
# never be picked up as inputs too, or the same routines get compiled twice
# and the link fails with "redefinition of f2py_rout_...".
GENERATED_DIRS = frozenset({"_expanded"})


_IMPL_SUFFIXES = (".cpp", ".cc", ".cxx")

# A header under one of these directories is assumed to belong to a project
# that splits declarations from definitions, so its .cpp lives elsewhere.
_HEADER_DIR_NAMES = frozenset({"include", "inc", "headers"})
_IMPL_DIR_NAMES = ("src", "source", "sources", "lib")


def find_implementation_files(header: Path) -> list[Path]:
    """Locate the .cpp/.cc/.cxx files that define what `header` declares.

    Two layouts are handled. The simple one puts calculator.hpp and
    calculator.cpp side by side. The conventional one — which every real C++
    project of any size uses — splits them into include/ and src/, and
    matching only on `header.with_suffix('.cpp')` silently misses it: the
    service builds and then dies on first import with an undefined-symbol
    error, because the declarations were bound but nothing defined them.
    """
    found: list[Path] = []
    seen: set[Path] = set()

    def add(candidate: Path) -> None:
        if candidate.is_file() and candidate not in seen:
            seen.add(candidate)
            found.append(candidate)

    # Side-by-side layout.
    for suffix in _IMPL_SUFFIXES:
        add(header.with_suffix(suffix))

    # Split layout: walk up to the nearest include/ ancestor, then look for
    # src/ beside it. Nearest wins — for include/pkg/sub/Foo.hpp the relevant
    # project root is the one holding include/, not any outer checkout.
    for ancestor in header.parents:
        if ancestor.name.lower() not in _HEADER_DIR_NAMES:
            continue
        for impl_dir_name in _IMPL_DIR_NAMES:
            impl_dir = ancestor.parent / impl_dir_name
            if not impl_dir.is_dir():
                continue
            for suffix in _IMPL_SUFFIXES:
                # rglob, not glob: src/ is often subdivided to mirror include/.
                for match in sorted(impl_dir.rglob(f"{header.stem}{suffix}")):
                    add(match)
        break

    return found


def find_native_sources(root: Path, language: str) -> list[Path]:
    suffixes = [ext for ext, lang in _EXTENSIONS.items() if lang == language]
    found: list[Path] = []
    for suffix in suffixes:
        found.extend(
            p
            for p in sorted(root.rglob(f"*{suffix}"))
            if not GENERATED_DIRS.intersection(p.parts)
        )
    return found
