#!/usr/bin/env python3
"""Differential IR harness — parse the real corpus under a *named* backend and
snapshot the resulting IR.

Why this exists
---------------
native2py is about to replace a parser front end (ROADMAP 1.4b / 3.2). The only
question that matters during that swap is not "does it still build" but **"did
any answer change, and which one"**. A test suite made of hand-written fixtures
cannot answer it: the fixtures were written against the parser being replaced.
So the harness parses the *libraries the repo actually ships* —
`libraries/petro` (30-year-old F77 decks plus a modern C++ facade) and
`libraries/geometry` — under an explicitly named backend, and writes the IR to
a committed JSON snapshot.

Two properties make it a gate rather than a report:

* **The backend is always named.** Nothing here ever says "whatever parser is
  installed". `cpp:clang` and `cpp:regex` are separate, separately snapshotted
  backends, and a Fortran backend name is picked up the same way the moment
  `parsers/fortran.py` grows the same `BACKENDS` / `resolve_backend` seam
  `parsers/cpp.py` already has — see `available_backends()`. There is no
  Fortran-specific code in this file to rewrite when that lands.
* **The diff is per symbol.** "files differ" is useless when a parser swap
  touches 200 declarations. `diff` reports, for every source file, which
  symbols only one backend found and, for the symbols both found, exactly
  which field disagrees.

Usage (standalone — this is the mode the parser swap runs in)::

    python tests/corpus/harness.py list
    python tests/corpus/harness.py snapshot --update            # all backends
    python tests/corpus/harness.py snapshot --update -b cpp:clang
    python tests/corpus/harness.py check                        # live vs committed
    python tests/corpus/harness.py diff cpp:clang cpp:regex     # backend vs backend
    python tests/corpus/harness.py diff cpp:clang cpp:regex --from-snapshots

`diff` parses live by default, so it works for a backend nobody has snapshotted
yet — which is the situation on the first day of a parser swap.

As a pytest run: `tests/corpus/test_corpus.py` walks the same corpus and asserts
the live parse still equals the committed snapshot, per file, with the same
per-symbol report as the failure message.

Snapshot format
---------------
`snapshots/<backend-key>/<repo-relative source path>.json`, sorted keys, two
space indent, LF. Paths inside are repo-relative POSIX; nothing machine- or
time-dependent is stored, and the writing native2py's own version is stripped
(it would churn every snapshot on a version bump while telling you nothing
about the parser). `schema_version` is kept, because a change to it *is* a
change to the contract these snapshots describe.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
SNAPSHOT_DIR = HARNESS_DIR / "snapshots"
# tests/corpus -> tests -> tools/native2py -> tools -> repo root
REPO_ROOT = HARNESS_DIR.parents[3]

if __package__ in (None, ""):  # standalone: make `native2py` importable
    sys.path.insert(0, str(HARNESS_DIR.parents[1]))

from native2py.config import ExposeConfig  # noqa: E402
from native2py.ir import ModuleIR, module_to_dict  # noqa: E402
from native2py.parsers.cpp_ast import ClangOptions  # noqa: E402


# --- the corpus ---------------------------------------------------------
#
# Repo-relative globs, not a hard-coded file list: a header added to
# libraries/petro joins the corpus by existing, and `snapshot --update` is then
# the only thing standing between it and a committed snapshot. Directories that
# are not part of the parseable API surface (examples/, decks/) stay out.

CPP_GLOBS = (
    "libraries/petro/cpp/include/*.hpp",
    "libraries/geometry/*.hpp",
)
FORTRAN_GLOBS = (
    "libraries/petro/fortran/*.f",
    "libraries/petro/fortran/modern/*.f90",
)

# Headers in libraries/petro/cpp/include #include each other by bare name.
CPP_INCLUDE_PATHS = ("libraries/petro/cpp/include",)
# PETRO.INC / GRID.INC live here; the fixed-form decks INCLUDE them.
FORTRAN_INCLUDE_PATHS = ("libraries/petro/fortran/include",)


def corpus_sources(language: str) -> list[Path]:
    """Repo-relative paths of every corpus source for `language`, sorted."""
    globs = {"cpp": CPP_GLOBS, "fortran": FORTRAN_GLOBS}[language]
    found: set[Path] = set()
    for pattern in globs:
        found.update(p.relative_to(REPO_ROOT) for p in REPO_ROOT.glob(pattern))
    return sorted(found)


# --- backends -----------------------------------------------------------


class BackendUnavailable(RuntimeError):
    """The named backend cannot run here (missing libclang, missing fparser)."""


@dataclasses.dataclass(frozen=True, order=True)
class Backend:
    """One named parser front end, e.g. cpp:clang."""

    language: str
    name: str

    @property
    def key(self) -> str:
        return f"{self.language}:{self.name}"

    @property
    def slug(self) -> str:
        """Filesystem-safe form of `key`, used as the snapshot directory."""
        return f"{self.language}-{self.name}"

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.key


LANGUAGES = ("cpp", "fortran")


def _front_door(language: str):
    """The parsers.<language> front-door module, or None if it will not import.

    Tolerant on purpose: this repo is worked on by several people at once, and
    a Fortran front door mid-refactor must not make the C++ half of the harness
    unrunnable.
    """
    import importlib

    try:
        return importlib.import_module(f"native2py.parsers.{language}")
    except Exception:
        return None


def available_backends(language: str | None = None) -> list[Backend]:
    """Every named backend that can actually run on this machine.

    Discovered through the front door's `BACKENDS` tuple, never hard-coded:
    `cpp.py` publishes ("auto", "clang", "regex") and `fortran.py` is getting
    the identical seam, so a new backend name becomes a new snapshot directory
    with no change here. "auto" is deliberately excluded — a snapshot taken
    under "whichever parser happened to be installed" is not evidence.
    """
    languages = LANGUAGES if language is None else (language,)
    backends: list[Backend] = []
    for lang in languages:
        module = _front_door(lang)
        if module is None:
            continue
        names = [n for n in getattr(module, "BACKENDS", ()) if n != "auto"]
        if not names:
            # A front door with no seam yet (or one that never grows one) still
            # has exactly one behaviour; name it rather than skip the language.
            names = ["default"]
        for name in names:
            backend = Backend(lang, name)
            if _resolvable(module, name):
                backends.append(backend)
    return backends


def _resolvable(module, name: str) -> bool:
    resolve = getattr(module, "resolve_backend", None)
    if resolve is None:
        return True
    try:
        resolve(name)
    except Exception:
        return False
    return True


def parse_backend_key(key: str) -> Backend:
    if ":" not in key:
        raise SystemExit(
            f"Backend '{key}' is not of the form <language>:<name> "
            f"(known: {', '.join(b.key for b in available_backends())})."
        )
    language, name = key.split(":", 1)
    if language not in LANGUAGES:
        raise SystemExit(f"Unknown language '{language}' in backend '{key}'.")
    return Backend(language, name)


def _require_front_door(backend: Backend):
    module = _front_door(backend.language)
    if module is None:
        raise BackendUnavailable(
            f"native2py.parsers.{backend.language} will not import, so backend "
            f"'{backend.key}' cannot run."
        )
    if not _resolvable(module, backend.name):
        raise BackendUnavailable(
            f"Backend '{backend.key}' is not usable on this machine "
            f"(parsers.{backend.language}.resolve_backend refused it)."
        )
    return module


def parse(backend: Backend, relative_source: Path) -> ModuleIR:
    """Parse one corpus source under one named backend."""
    module = _require_front_door(backend)
    path = REPO_ROOT / relative_source
    name = None if backend.name == "default" else backend.name

    if backend.language == "cpp":
        options = ClangOptions(
            std="c++17",
            include_paths=tuple(str(REPO_ROOT / p) for p in CPP_INCLUDE_PATHS),
        )
        return module.parse_header(
            path, ExposeConfig(), backend=name, options=options
        )

    # Fortran has no expose-everything mode by design (a 20,000-line deck must
    # not cost a full parse), so the harness asks the front door what is in the
    # file and exposes all of it — the corpus equivalent of "everything".
    routines = module.list_routine_names(path)
    expose = ExposeConfig(functions=routines)
    include_paths = [REPO_ROOT / p for p in FORTRAN_INCLUDE_PATHS]
    try:
        return module.parse_source(
            path, expose, include_paths=include_paths, backend=name
        )
    except TypeError:
        # Front door without the backend seam yet: same call, no backend kwarg.
        if name not in (None, "default"):
            raise
        return module.parse_source(path, expose, include_paths=include_paths)


# --- snapshots ----------------------------------------------------------


def _repo_relative(text: str) -> str:
    root = str(REPO_ROOT)
    return text.replace(root + os.sep, "").replace(root, "<repo>").replace(os.sep, "/")


def snapshot_document(module: ModuleIR, relative_source: Path) -> dict:
    """The IR as it is committed: normalized, machine-independent, sorted."""
    data = module_to_dict(module)
    # The writing tool's own version is not a property of the parse, and would
    # rewrite every snapshot on an unrelated release.
    data.pop("native2py_version", None)
    # Every string, not just source_file: Clang names an anonymous record
    # "(unnamed struct at /abs/path/FortranBridge.hpp:115:8)", and a diagnostic
    # quotes the include that failed. An absolute path in a committed snapshot
    # makes it un-shareable between two machines, which would defeat the point.
    data = _normalize_strings(data)
    data["source_file"] = relative_source.as_posix()
    return data


def _normalize_strings(value):
    if isinstance(value, dict):
        return {k: _normalize_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_strings(v) for v in value]
    if isinstance(value, str):
        return _repo_relative(value)
    return value


def snapshot_path(backend: Backend, relative_source: Path) -> Path:
    return SNAPSHOT_DIR / backend.slug / f"{relative_source.as_posix()}.json"


def dump(document: dict) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def write_snapshot(backend: Backend, relative_source: Path, document: dict) -> Path:
    path = snapshot_path(backend, relative_source)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(document))
    return path


def read_snapshot(backend: Backend, relative_source: Path) -> dict | None:
    path = snapshot_path(backend, relative_source)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def is_snapshotted(backend: Backend) -> bool:
    """True once someone has committed snapshots for this backend.

    A backend that exists but has never been snapshotted is not a failure — it
    is the normal state of a front end on the day it is introduced. `check` and
    the pytest run say so and move on; `snapshot --update -b <key>` adopts it.
    """
    return (SNAPSHOT_DIR / backend.slug).is_dir()


def live_documents(backend: Backend) -> dict[Path, dict]:
    """Parse the whole corpus for `backend` and return normalized documents."""
    return {
        source: snapshot_document(parse(backend, source), source)
        for source in corpus_sources(backend.language)
    }


def snapshot_documents(backend: Backend) -> dict[Path, dict]:
    documents = {}
    for source in corpus_sources(backend.language):
        document = read_snapshot(backend, source)
        if document is not None:
            documents[source] = document
    return documents


# --- per-symbol diff ----------------------------------------------------
#
# The whole point of the harness. A JSON-blob comparison tells you a file
# changed; during a parser swap you need to know that `FluidModel.viscosity`
# lost its `is_const`, and that is what this produces.


def flatten(document: dict) -> dict[str, dict]:
    """One entry per addressable symbol, keyed by a stable human-readable path."""
    flat: dict[str, dict] = {}

    def params(items) -> list[dict]:
        return [dict(p) for p in items or []]

    for cls in document.get("classes") or []:
        key = f"class {cls['name']}"
        flat[key] = {
            "namespace": cls.get("namespace"),
            "bases": list(cls.get("bases") or []),
            "has_default_constructor": cls.get("has_default_constructor"),
            "constructors": [params(c) for c in cls.get("constructors") or []],
            "fields": params(cls.get("fields")),
        }
        for method in cls.get("methods") or []:
            flat[f"{key}.{method['name']}()"] = {
                "returns": method.get("returns"),
                "is_static": method.get("is_static"),
                "is_const": method.get("is_const"),
                "is_overloaded": method.get("is_overloaded"),
                "parameters": params(method.get("parameters")),
            }

    for struct in document.get("structs") or []:
        flat[f"struct {struct['name']}"] = {
            "namespace": struct.get("namespace"),
            "has_default_constructor": struct.get("has_default_constructor"),
            "fields": params(struct.get("fields")),
        }

    for fn in document.get("functions") or []:
        flat[f"function {fn['name']}()"] = {
            "namespace": fn.get("namespace"),
            "returns": fn.get("returns"),
            "is_subroutine": fn.get("is_subroutine"),
            "is_overloaded": fn.get("is_overloaded"),
            "fortran_module": fn.get("fortran_module"),
            "parameters": params(fn.get("parameters")),
        }

    # Skips and diagnostics are part of the contract too: a backend that stops
    # reporting why it could not bind something has regressed, even though the
    # bound surface is unchanged.
    for skip in document.get("skipped") or []:
        flat[f"skipped {skip['name']}"] = {"reason": skip.get("reason")}
    for index, message in enumerate(document.get("diagnostics") or []):
        flat[f"diagnostic #{index + 1}"] = {"message": message}

    module_level = {
        key: document.get(key)
        for key in ("name", "language", "fortran_module", "schema_version")
    }
    flat["<module>"] = module_level
    return flat


def _field_diffs(left, right, prefix: str = "") -> list[str]:
    """Leaf-level differences between two comparable JSON values."""
    if isinstance(left, dict) and isinstance(right, dict):
        lines = []
        for key in sorted(set(left) | set(right)):
            lines += _field_diffs(
                left.get(key), right.get(key), f"{prefix}.{key}" if prefix else key
            )
        return lines
    if isinstance(left, list) and isinstance(right, list):
        lines = []
        if len(left) != len(right):
            return [f"{prefix or 'value'}: {len(left)} item(s) vs {len(right)}"]
        for index, (a, b) in enumerate(zip(left, right)):
            lines += _field_diffs(a, b, f"{prefix}[{index}]")
        return lines
    if left != right:
        return [f"{prefix or 'value'}: {left!r} vs {right!r}"]
    return []


@dataclasses.dataclass
class FileDiff:
    source: Path
    only_left: list[str] = dataclasses.field(default_factory=list)
    only_right: list[str] = dataclasses.field(default_factory=list)
    changed: dict[str, list[str]] = dataclasses.field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.only_left or self.only_right or self.changed)

    def symbol_count(self) -> int:
        return len(self.only_left) + len(self.only_right) + len(self.changed)


def diff_documents(source: Path, left: dict | None, right: dict | None) -> FileDiff:
    result = FileDiff(source)
    left_flat = flatten(left) if left is not None else {}
    right_flat = flatten(right) if right is not None else {}
    result.only_left = sorted(set(left_flat) - set(right_flat))
    result.only_right = sorted(set(right_flat) - set(left_flat))
    for key in sorted(set(left_flat) & set(right_flat)):
        lines = _field_diffs(left_flat[key], right_flat[key])
        if lines:
            result.changed[key] = lines
    return result


def render_diff(
    diffs: list[FileDiff], left_label: str, right_label: str, max_lines: int = 6
) -> str:
    out = [f"IR differences: {left_label} (<) vs {right_label} (>)", ""]
    total = 0
    for file_diff in diffs:
        if not file_diff:
            continue
        total += file_diff.symbol_count()
        out.append(f"{file_diff.source.as_posix()}")
        for key in file_diff.only_left:
            out.append(f"  < only in {left_label}: {key}")
        for key in file_diff.only_right:
            out.append(f"  > only in {right_label}: {key}")
        for key, lines in file_diff.changed.items():
            out.append(f"  ~ {key}")
            for line in lines[:max_lines]:
                out.append(f"      {line}")
            if len(lines) > max_lines:
                out.append(f"      ... and {len(lines) - max_lines} more field(s)")
        out.append("")
    if total == 0:
        return f"No IR differences between {left_label} and {right_label}."
    out.append(f"{total} differing symbol(s) across {sum(1 for d in diffs if d)} file(s).")
    return "\n".join(out)


def compare_backends(
    left: Backend, right: Backend, from_snapshots: bool = False
) -> list[FileDiff]:
    if left.language != right.language:
        raise SystemExit(
            f"Cannot diff {left.key} against {right.key}: different languages, so "
            "they never parse the same sources."
        )
    load = snapshot_documents if from_snapshots else live_documents
    left_docs, right_docs = load(left), load(right)
    sources = sorted(set(left_docs) | set(right_docs))
    return [
        diff_documents(source, left_docs.get(source), right_docs.get(source))
        for source in sources
    ]


# --- commands -----------------------------------------------------------


def _selected(keys: list[str] | None) -> list[Backend]:
    if not keys:
        return available_backends()
    return [parse_backend_key(k) for k in keys]


def cmd_list(args) -> int:
    print(f"repo root: {REPO_ROOT}")
    for language in LANGUAGES:
        sources = corpus_sources(language)
        module = _front_door(language)
        names = (
            [b.key for b in available_backends(language)]
            if module is not None
            else ["<front door will not import>"]
        )
        print(f"\n{language}: {len(sources)} source(s), backends: {', '.join(names) or 'none'}")
        for source in sources:
            print(f"  {source.as_posix()}")
    return 0


def cmd_snapshot(args) -> int:
    status = 0
    for backend in _selected(args.backend):
        # Without --update this is a verification run, so a backend nobody has
        # adopted yet is "not snapshotted", not "8 files out of date".
        if not args.update and not args.backend and not is_snapshotted(backend):
            print(f"{backend.key}: SKIP — no committed snapshots yet.")
            continue
        try:
            documents = live_documents(backend)
        except BackendUnavailable as exc:
            print(f"{backend.key}: SKIP — {exc}")
            continue
        stale = []
        for source, document in documents.items():
            existing = read_snapshot(backend, source)
            if existing == document:
                continue
            if args.update:
                write_snapshot(backend, source, document)
            else:
                stale.append(source)
        if args.update:
            print(f"{backend.key}: wrote {len(documents)} snapshot(s).")
        elif stale:
            status = 1
            print(f"{backend.key}: {len(stale)} snapshot(s) out of date:")
            for source in stale:
                print(f"  {source.as_posix()}")
        else:
            print(f"{backend.key}: {len(documents)} snapshot(s) up to date.")
    return status


def cmd_check(args) -> int:
    status = 0
    for backend in _selected(args.backend):
        if not is_snapshotted(backend) and not args.backend:
            print(f"{backend.key}: SKIP — no committed snapshots yet.")
            continue
        try:
            documents = live_documents(backend)
        except BackendUnavailable as exc:
            print(f"{backend.key}: SKIP — {exc}")
            continue
        diffs = []
        missing = []
        for source, document in documents.items():
            committed = read_snapshot(backend, source)
            if committed is None:
                missing.append(source)
                continue
            diffs.append(diff_documents(source, committed, document))
        if missing:
            status = 1
            print(f"{backend.key}: no committed snapshot for:")
            for source in missing:
                print(f"  {source.as_posix()}")
        if any(diffs):
            status = 1
            print(render_diff(diffs, f"{backend.key} (committed)", f"{backend.key} (live)"))
        elif not missing:
            print(f"{backend.key}: {len(documents)} file(s) match the committed snapshots.")
    return status


def cmd_diff(args) -> int:
    left, right = parse_backend_key(args.left), parse_backend_key(args.right)
    try:
        diffs = compare_backends(left, right, from_snapshots=args.from_snapshots)
    except BackendUnavailable as exc:
        print(f"cannot diff: {exc}")
        return 2
    print(render_diff(diffs, left.key, right.key))
    # A difference is information, not a failure: `regex` really is worse than
    # `clang`, and the harness exists to show by how much. Only --fail-on-diff
    # turns it into an exit code, for the parser swap's own CI gate.
    return 1 if (args.fail_on_diff and any(diffs)) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tests/corpus/harness.py", description=__doc__.split("\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Show the corpus and the backends available here.")

    snap = sub.add_parser("snapshot", help="Write (or verify) committed snapshots.")
    snap.add_argument("-b", "--backend", action="append", help="e.g. cpp:clang (repeatable)")
    snap.add_argument("--update", action="store_true", help="Rewrite snapshots on disk.")

    check = sub.add_parser("check", help="Live parse vs committed snapshots.")
    check.add_argument("-b", "--backend", action="append")

    diff = sub.add_parser("diff", help="Per-symbol diff between two named backends.")
    diff.add_argument("left")
    diff.add_argument("right")
    diff.add_argument(
        "--from-snapshots",
        action="store_true",
        help="Compare committed snapshots instead of parsing live.",
    )
    diff.add_argument(
        "--fail-on-diff", action="store_true", help="Exit non-zero if anything differs."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {
        "list": cmd_list,
        "snapshot": cmd_snapshot,
        "check": cmd_check,
        "diff": cmd_diff,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
