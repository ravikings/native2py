"""Numerical regression harness: prove the answers did not change.

For a migration of decades-old PVT, well and reservoir code, "it builds and
imports" is not the acceptance criterion — *the numbers are the same* is. The
things that silently change them are exactly the things native2py touches:
a regenerate that picks a different overload, a compiler or flag change, a
`-ffast-math` creeping into a CMake preset, a parser upgrade that maps a type
differently, a refactor of the native code underneath.

So: record every bound entry point's output for a fixed set of inputs once,
commit that file, and compare on every build.

Design constraints that shaped this:

* **Inputs must be reproducible from the IR alone.** They are derived from
  each parameter's position and type, so re-recording on another machine
  produces the same call — no random data, no timestamps in the file.
* **Comparison is float-aware, and the tolerance is argued for.** See
  "Choosing a tolerance" below. Every entry carries its own tolerance in the
  file, so the number a given routine is held to is visible in review rather
  than hidden in a global default.
* **The environment is recorded with the values.** A golden failure asks one
  question first — "did the toolchain change?" — and answering it after the
  fact is impossible if the file only holds numbers. See "Provenance".
* **In-place outputs are recorded too.** Fortran routines with `intent(inout)`
  array arguments return `None` and write their answer into the caller's
  array. Recording only the return value would record `None` and prove
  nothing, so arguments that the call modified are recorded as well.
* **Call order is preserved.** Legacy Fortran keeps state in COMMON blocks:
  an initialisation routine sets up what the next call reads. Entries are
  therefore recorded and replayed in the order the IR exposes them, not
  sorted — sorting them alphabetically silently changed the answers.
* **Coverage is visible.** Entry points that cannot be called with generated
  inputs are recorded as skipped, with the reason, so a golden file that
  covers three of forty symbols cannot be mistaken for a clean bill of health.

Choosing a tolerance
--------------------

The default was 1e-12 relative with a zero absolute floor. That is roughly
4,500 ULP of double precision, which sounds generous and is not, because the
things it has to survive are not rounding of a single operation:

* **libm differs between platforms.** `pow`, `exp`, `log10` and `asin` are not
  correctly rounded in any mainstream libm. Apple's, glibc's and musl's
  implementations disagree by a few ULP on the same argument, and these
  correlations are nothing *but* chained transcendentals — Standing and
  Vazquez-Beggs are products of `10**x` and `p**1.2048`.
* **The chain amplifies.** The relative error of `10**x` is about
  `|x·ln 10|` times the relative error of `x`; with `x ≈ 3` that is a factor
  of seven per exponentiation, and there are several per correlation.
* **FMA contraction is an optimisation-flag decision.** `-O2` on one target
  and `-O3` on another fuse a different set of multiply-adds, each fusion
  worth up to a ULP, and none of that is visible in the source.
* **Iteration terminates on a threshold.** A ULP of difference in a residual
  can put a Newton or fixed-point loop on the other side of its convergence
  test, so the two platforms take a different number of iterations. The
  resulting difference is set by *the routine's own convergence tolerance*,
  not by machine epsilon, and no global default can know what that is.

So the default is 1e-9 relative. That is ~4.5 million ULP of headroom, which
covers every effect above with room to spare — and it is still six to seven
orders of magnitude tighter than any change that could be called a different
answer. These correlations agree with laboratory data to about ±10%; a
changed coefficient, a dropped unit conversion, a swapped argument pair, a
different overload, or `-ffast-math` all move a result by 1e-3 relative or
more. 1e-9 cannot hide any of them.

The absolute floor is 1e-12 rather than 0.0 so that a quantity which is
legitimately zero, or denormal-small, does not fail on a difference that is
physically meaningless. Every physical quantity in this domain is far above
it: the smallest, gas compressibility, is ~1e-5 /psi.

Where a routine's own algorithm is looser than 1e-9, the *entry* says so
(`entries[key]["tolerance"]`) with a reason. That is the honest form: it
records that `tubing_bhp` is a marching fixed-point solve that stops at 1e-4
relative on segment midpoint pressure, rather than silently loosening every
entry in the file to accommodate the worst one.

Provenance
----------

A golden failure has two possible causes and they need completely different
responses: the code changed (a regression — fix it), or the toolchain changed
(a different compiler, libm or flag — investigate, then re-record
deliberately). Recording only the numbers makes that undecidable after the
fact, and the documented remedy — re-record — destroys the evidence. So the
file records the platform, the interpreter, numpy, the Fortran and C++
compiler identifications, native2py's own version, and the SHA-256 of every
native source the values came from. On a mismatch the comparison prints
recorded against current, so the first question is answered before anyone
opens a terminal.

None of it varies run to run on the same machine: no timestamps, no paths, no
hostnames, no user names. The file is diffed in review.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .ir import ClassDef, ModuleIR, StructDef

GOLDEN_FILENAME = "golden.json"

# 2: added per-entry tolerances, the provenance block and argument_effects.
FORMAT_VERSION = 2

# See "Choosing a tolerance" above — this number is argued for, not picked.
DEFAULT_RTOL = 1e-9
DEFAULT_ATOL = 1e-12


# --- deterministic sample inputs ----------------------------------------

# Values are picked per parameter *position* so a signature always gets the
# same call, and are spread across the range rather than repeated so an
# argument-order regression (a swapped pair) actually changes an answer.
_FLOATS = (1.0, 2.5, 0.75, 10.0, 3.25, 0.5, 7.5, 100.0)
_INTS = (1, 2, 3, 4, 5, 6, 7, 8)
_STRINGS = ("alpha", "beta", "gamma", "delta")


def _scalar(kind: str, index: int):
    if kind == "float":
        return _FLOATS[index % len(_FLOATS)]
    if kind == "int":
        return _INTS[index % len(_INTS)]
    if kind == "bool":
        return index % 2 == 0
    if kind == "str":
        return _STRINGS[index % len(_STRINGS)]
    return None


class Unsupported(Exception):
    """No deterministic input can be built for this signature."""


def sample_arguments(parameters, structs: dict, offset: int = 0) -> list:
    """Concrete arguments for one call, as plain Python values."""
    arguments = []
    for index, param in enumerate(parameters, start=offset):
        if param.type in structs:
            arguments.append(
                {
                    "__struct__": param.type,
                    "fields": _struct_sample(structs[param.type], structs, index),
                }
            )
            continue
        value = _scalar(param.type, index)
        if value is None:
            raise Unsupported(
                f"parameter '{param.name}' has type '{param.type}', which the "
                "golden harness cannot generate an input for"
            )
        if not param.is_array:
            arguments.append(value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            # Distinct elements, so a routine that only reads element 0 (or
            # reverses the array) produces a different answer than one that
            # sums it.
            arguments.append([value, value + 1, value + 2])
        else:
            arguments.append([value, value, value])
    return arguments


def _struct_sample(struct: StructDef, structs: dict, index: int) -> dict:
    fields = {}
    for offset, field in enumerate(struct.fields):
        if field.type in structs:
            fields[field.name] = {
                "__struct__": field.type,
                "fields": _struct_sample(structs[field.type], structs, index + offset),
            }
            continue
        value = _scalar(field.type, index + offset)
        if value is None:
            raise Unsupported(
                f"field '{struct.name}.{field.name}' has type '{field.type}'"
            )
        fields[field.name] = value
    return fields


# --- the call plan ------------------------------------------------------


@dataclass
class Call:
    """One recorded entry point: what to call, and with what."""

    key: str  # "Class.method" or "function"
    kind: str  # "method" | "static" | "function"
    class_name: str | None
    name: str
    constructor_arguments: list
    arguments: list


@dataclass
class Skip:
    key: str
    reason: str


def plan(module: ModuleIR) -> tuple[list[Call], list[Skip]]:
    """Every entry point worth recording, and every one that cannot be.

    Derived from the IR only, so `record` and `verify` agree without either
    reading the other's file.
    """
    structs = {struct.name: struct for struct in module.structs}
    calls: list[Call] = []
    skips: list[Skip] = []

    for cls in module.classes:
        ctor_args, ctor_error = _constructor_arguments(cls, structs)
        for method in cls.methods:
            key = f"{cls.name}.{method.name}"
            if not method.is_static and ctor_error is not None:
                skips.append(Skip(key, ctor_error))
                continue
            try:
                arguments = sample_arguments(method.parameters, structs)
            except Unsupported as exc:
                skips.append(Skip(key, str(exc)))
                continue
            calls.append(
                Call(
                    key=key,
                    kind="static" if method.is_static else "method",
                    class_name=cls.name,
                    name=method.name,
                    constructor_arguments=[] if method.is_static else ctor_args,
                    arguments=arguments,
                )
            )

    for fn in module.functions:
        # An intent(out) argument is supplied by Fortran, not the caller.
        inputs = [p for p in fn.parameters if p.intent != "out"]
        try:
            arguments = sample_arguments(inputs, structs)
        except Unsupported as exc:
            skips.append(Skip(fn.name, str(exc)))
            continue
        calls.append(
            Call(
                key=fn.name,
                kind="function",
                class_name=None,
                name=fn.name,
                constructor_arguments=[],
                arguments=arguments,
            )
        )

    return calls, skips


def _constructor_arguments(cls: ClassDef, structs: dict) -> tuple[list, str | None]:
    if cls.has_default_constructor:
        return [], None
    if not cls.constructors:
        return [], (
            "no public constructor callable from Python (abstract, or all "
            "constructors are non-public)"
        )
    try:
        return sample_arguments(min(cls.constructors, key=len), structs), None
    except Unsupported as exc:
        return [], f"constructor is not callable with generated inputs: {exc}"


# --- running the plan ---------------------------------------------------


def _materialise(value, package):
    """Turn a recorded argument into the object the native call wants.

    A numeric list becomes a numpy array when numpy is importable. f2py binds
    an `intent(inout)` array as a genuine in-place output: it rejects a plain
    list outright, and even where it accepts a sequence it writes into a copy
    the caller never sees. Passing an array is what makes those outputs
    observable at all. pybind11's sequence casters accept an array, so this is
    safe for C++ too.
    """
    if isinstance(value, dict) and "__struct__" in value:
        native = getattr(package, value["__struct__"])()
        for name, field in value["fields"].items():
            setattr(native, name, _materialise(field, package))
        return native
    if isinstance(value, list) and value and all(_is_number(v) for v in value):
        numpy = _numpy()
        if numpy is not None:
            dtype = "int64" if all(isinstance(v, int) for v in value) else "float64"
            return numpy.array(value, dtype=dtype)
    return value


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _numpy():
    try:
        import numpy
    except ImportError:  # pragma: no cover - numpy is present wherever we build
        return None
    return numpy


def _jsonable(value):
    """Whatever a native call returned, as plain JSON data.

    A struct comes back as a pybind11 object and a Fortran array as a numpy
    array; both have to become data before they can be compared to a file.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "tolist"):  # numpy array
        return _jsonable(value.tolist())
    fields = {
        name: getattr(value, name)
        for name in dir(value)
        if not name.startswith("_") and not callable(getattr(value, name))
    }
    return {name: _jsonable(field) for name, field in fields.items()}


def run(module: ModuleIR, package, existing: dict | None = None) -> tuple[dict, list[Skip]]:
    """Call every planned entry point on an imported package.

    Generated inputs are a starting point, not the goal: `1.0` for an API
    gravity is outside every PVT correlation's valid range, and a baseline
    taken there pins the behaviour of an extrapolation rather than of the
    correlation. So arguments already present in `existing` are reused
    verbatim — edit them in golden.json to values your engineers recognise,
    re-record, and those inputs survive every future re-record.

    Returns {key: entry} plus the skips. An entry point that raises is
    recorded as a skip rather than aborting the run: one bad symbol should not
    cost you the other thirty-nine.
    """
    calls, skips = plan(module)
    previous = (existing or {}).get("entries") or {}
    entries: dict = {}

    for call in calls:
        spec = call_spec(call)
        kept = previous.get(call.key)
        if kept is not None:
            spec["arguments"] = kept.get("arguments", spec["arguments"])
            spec["constructor_arguments"] = kept.get(
                "constructor_arguments", spec["constructor_arguments"]
            )
        try:
            result, effects = invoke(spec, package)
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            skips.append(Skip(call.key, f"{type(exc).__name__}: {exc}"))
            continue
        entry = {**spec, "result": result}
        if effects:
            entry["argument_effects"] = effects
        entries[call.key] = entry

    return entries, skips


def call_spec(call: Call) -> dict:
    return {
        "kind": call.kind,
        "class": call.class_name,
        "name": call.name,
        "constructor_arguments": call.constructor_arguments,
        "arguments": call.arguments,
    }


def _invoke(entry: dict, package):
    """Execute one recorded call against an imported package."""
    return invoke(entry, package)[0]


def invoke(entry: dict, package) -> tuple:
    """Execute one recorded call; return (result, argument_effects).

    `argument_effects` maps argument index (as a string, because it has to
    survive JSON) to the argument's value *after* the call, for arguments the
    call modified in place. A Fortran routine whose whole answer is written
    into an `intent(inout)` array returns `None`; without this the golden file
    would record `None` and assert nothing about the nine PVT properties the
    call actually produced.
    """
    arguments = [_materialise(a, package) for a in entry["arguments"]]
    if entry["kind"] == "function":
        target = getattr(package, entry["name"])
    elif entry["kind"] == "static":
        target = getattr(getattr(package, entry["class"]), entry["name"])
    else:
        cls = getattr(package, entry["class"])
        instance = cls(
            *[_materialise(a, package) for a in entry["constructor_arguments"]]
        )
        target = getattr(instance, entry["name"])

    result = _jsonable(target(*arguments))

    effects = {}
    for index, (before, after) in enumerate(zip(entry["arguments"], arguments)):
        if not isinstance(before, list):
            continue
        now = _jsonable(after)
        if now != before:
            effects[str(index)] = now
    return result, effects


def replay(document: dict, package, effects: dict | None = None) -> tuple[dict, dict]:
    """Re-run the calls recorded in a golden document.

    The *file* drives this, not the IR: a golden run has to be able to say
    "this entry point used to exist and no longer does", which is impossible
    if the plan is regenerated from the current code every time.

    Returns ({key: result}, {key: error message}). Pass a dict as `effects`
    to also collect the in-place argument outputs of each call, and hand the
    same dict to `compare` so they are checked.
    """
    results: dict = {}
    errors: dict = {}
    for key, entry in (document.get("entries") or {}).items():
        try:
            result, produced = invoke(entry, package)
        except Exception as exc:  # noqa: BLE001
            errors[key] = f"{type(exc).__name__}: {exc}"
            continue
        results[key] = result
        if effects is not None:
            effects[key] = produced
    return results, errors


# --- provenance ---------------------------------------------------------


@lru_cache(maxsize=None)
def _tool_version(command: str) -> str | None:
    """The first line of `<command> --version`, or None if it is not here.

    One line, because the rest is a copyright notice that changes with the
    calendar and would make the file diff for no reason.
    """
    executable = shutil.which(command)
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError:  # pragma: no cover - which() said it was there
        return None
    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    return output[0].strip() if output else None


def _numpy_version() -> str | None:
    numpy = _numpy()
    return None if numpy is None else numpy.__version__


def _native2py_version() -> str:
    try:
        from . import __version__
    except ImportError:  # pragma: no cover
        return "unknown"
    return str(__version__)


def source_digests(paths, root: Path | None = None) -> dict:
    """SHA-256 of each native source, keyed by a path that means something.

    The digest is what makes "the code did not change" checkable rather than
    asserted: a golden failure whose source digests are unchanged is a
    toolchain difference, and one whose digests moved is a code change whose
    diff you can go and read.
    """
    digests = {}
    for path in sorted(Path(p) for p in paths):
        if not path.is_file():
            continue
        key = str(path.relative_to(root)) if root else path.name
        digests[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def provenance(sources=None, root: Path | None = None) -> dict:
    """What produced these numbers.

    Everything here is a property of the machine and the sources, never of
    the moment — the file is diffed in review, so a timestamp would make
    every re-record look like a change.
    """
    return {
        "platform": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "numpy": _numpy_version(),
        "fortran_compiler": _tool_version("gfortran"),
        "cxx_compiler": _tool_version("clang++") or _tool_version("g++"),
        "native2py": _native2py_version(),
        "sources": source_digests(sources or [], root),
    }


# Ordered so the reported diff reads from "most likely to move a number" down.
_PROVENANCE_ORDER = (
    "sources",
    "fortran_compiler",
    "cxx_compiler",
    "platform",
    "machine",
    "numpy",
    "python",
    "python_implementation",
    "native2py",
)


def provenance_differences(recorded: dict | None, current: dict | None) -> list[str]:
    """Human-readable "recorded X, now Y" lines for each field that moved."""
    if not recorded or not current:
        return []
    lines = []
    keys = [k for k in _PROVENANCE_ORDER if k in recorded or k in current]
    keys += sorted((set(recorded) | set(current)) - set(_PROVENANCE_ORDER))
    for key in dict.fromkeys(keys):
        was, now = recorded.get(key), current.get(key)
        if was == now:
            continue
        if key == "sources":
            for name in sorted(set(was or {}) | set(now or {})):
                a, b = (was or {}).get(name), (now or {}).get(name)
                if a != b:
                    lines.append(
                        f"  source {name}: recorded {a or '(absent)'}, now {b or '(absent)'}"
                    )
            continue
        lines.append(f"  {key}: recorded {was!r}, now {now!r}")
    return lines


# --- the file -----------------------------------------------------------


def build_document(
    module: ModuleIR,
    entries: dict,
    skips: list[Skip],
    rtol: float = DEFAULT_RTOL,
    atol: float = DEFAULT_ATOL,
    tolerances: dict | None = None,
    environment: dict | None = None,
) -> dict:
    """The golden file's contents.

    Deliberately free of anything that varies run to run — no timestamps, no
    paths, no machine identity — so the only reason for a diff in review is a
    changed answer.

    `tolerances` maps an entry key to `{"rtol": ..., "atol": ..., "reason":
    ...}` for entries whose own algorithm cannot be held to the default; every
    entry gets an explicit tolerance either way, so what a routine is held to
    is visible in the file rather than implied by a default in the code.
    """
    tolerances = tolerances or {}
    entries = {
        key: {
            **entry,
            "tolerance": {
                "rtol": rtol,
                "atol": atol,
                **{k: v for k, v in tolerances.get(key, {}).items()},
            },
        }
        for key, entry in entries.items()
    }
    return {
        "format": FORMAT_VERSION,
        "service": module.name,
        "language": module.language,
        # The fallback for an entry that has no tolerance of its own — a
        # format-1 file, or one hand-written before this field existed.
        "tolerance": {"rtol": rtol, "atol": atol},
        "provenance": environment if environment is not None else provenance(),
        # Insertion order, NOT sorted: call order is part of the fixture.
        # Legacy F77 keeps state in COMMON blocks — PVTINI initialises what
        # PVTRS and PVTBO then read — so replaying alphabetically returns
        # different (and wrong) numbers than recording did. Sorting these keys
        # made `verify` fail immediately after `record`, against an unchanged
        # build.
        "entries": entries,
        "skipped": {skip.key: skip.reason for skip in sorted(skips, key=lambda s: s.key)},
    }


def write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n")


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def coverage(document: dict) -> tuple[int, int]:
    """(recorded, skipped) — so a golden file covering three of forty symbols
    cannot be mistaken for a clean bill of health."""
    return len(document.get("entries") or {}), len(document.get("skipped") or {})


# --- comparison ---------------------------------------------------------


def _close(recorded, current, rtol: float, atol: float) -> bool:
    if isinstance(recorded, bool) or isinstance(current, bool):
        return recorded == current
    if isinstance(recorded, (int, float)) and isinstance(current, (int, float)):
        if math.isnan(recorded) and math.isnan(current):
            # NaN != NaN, but a routine that returned NaN before and returns
            # NaN now has not regressed — it is the *change* that matters.
            return True
        return math.isclose(recorded, current, rel_tol=rtol, abs_tol=atol)
    if isinstance(recorded, list) and isinstance(current, list):
        return len(recorded) == len(current) and all(
            _close(r, c, rtol, atol) for r, c in zip(recorded, current)
        )
    if isinstance(recorded, dict) and isinstance(current, dict):
        return recorded.keys() == current.keys() and all(
            _close(recorded[k], current[k], rtol, atol) for k in recorded
        )
    return recorded == current


def entry_tolerance(document: dict, entry: dict) -> tuple[float, float]:
    """The (rtol, atol) one entry is held to.

    The entry's own tolerance wins; the document-level one is the fallback for
    a format-1 file, and the module defaults are the fallback for that.
    """
    fallback = document.get("tolerance") or {}
    own = entry.get("tolerance") or {}
    return (
        own.get("rtol", fallback.get("rtol", DEFAULT_RTOL)),
        own.get("atol", fallback.get("atol", DEFAULT_ATOL)),
    )


def compare(
    document: dict,
    results: dict,
    errors: dict | None = None,
    effects: dict | None = None,
    environment: dict | None = None,
) -> list[str]:
    """Differences between a recorded golden document and a fresh replay.

    Returns human-readable lines; empty means the answers are unchanged. A
    call that now raises, or an entry point that disappeared, counts as a
    difference: an API that silently lost a symbol between regenerates is
    exactly the drift this is here to catch.

    Pass `environment` (from `provenance()`) to have any toolchain difference
    reported alongside the numbers. It is never a difference on its own — a
    new compiler that returns the same answers is not a regression — but when
    the answers *have* moved it is the first thing anyone needs to know, and
    re-recording would have destroyed it.
    """
    errors = errors or {}

    differences = []
    for key, entry in (document.get("entries") or {}).items():
        rtol, atol = entry_tolerance(document, entry)
        if key in errors:
            differences.append(f"{key}: recorded, but this build cannot call it — {errors[key]}")
            continue
        if key not in results:
            differences.append(f"{key}: recorded but missing from this build")
            continue
        expected, actual = entry.get("result"), results[key]
        if not _close(expected, actual, rtol, atol):
            differences.append(
                f"{key}: expected {expected!r}, got {actual!r} (rtol={rtol}, atol={atol})"
            )
        if effects is None:
            continue
        # An in-place output that stopped being written is a silent wrong
        # answer, not a missing feature: the caller's array simply keeps
        # whatever it held before the call.
        recorded_effects = entry.get("argument_effects") or {}
        produced = effects.get(key) or {}
        for index, expected_effect in recorded_effects.items():
            if index not in produced:
                differences.append(
                    f"{key}: argument {index} was written in place when recorded, "
                    "and this build does not write it"
                )
            elif not _close(expected_effect, produced[index], rtol, atol):
                differences.append(
                    f"{key}: argument {index} after the call: expected "
                    f"{expected_effect!r}, got {produced[index]!r}"
                )

    if differences and environment is not None:
        moved = provenance_differences(document.get("provenance"), environment)
        if moved:
            differences.append(
                "the toolchain also changed since these values were recorded — "
                "a legitimate platform difference looks exactly like a regression, "
                "so rule this out before re-recording:\n" + "\n".join(moved)
            )
        else:
            differences.append(
                "the toolchain is unchanged since these values were recorded "
                "(platform, compilers, numpy and native source digests all match), "
                "so this is a change in behaviour, not a platform difference."
            )

    return differences
