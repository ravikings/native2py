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
* **Comparison is float-aware.** Exact equality would fail on any platform
  with a different libm; a relative tolerance says what "unchanged" means and
  the tolerance is recorded alongside the values.
* **Call order is preserved.** Legacy Fortran keeps state in COMMON blocks:
  an initialisation routine sets up what the next call reads. Entries are
  therefore recorded and replayed in the order the IR exposes them, not
  sorted — sorting them alphabetically silently changed the answers.
* **Coverage is visible.** Entry points that cannot be called with generated
  inputs are recorded as skipped, with the reason, so a golden file that
  covers three of forty symbols cannot be mistaken for a clean bill of health.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from .ir import ClassDef, ModuleIR, StructDef

GOLDEN_FILENAME = "golden.json"
FORMAT_VERSION = 1

DEFAULT_RTOL = 1e-12
DEFAULT_ATOL = 0.0


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
    """Turn a recorded argument into the object the native call wants."""
    if isinstance(value, dict) and "__struct__" in value:
        native = getattr(package, value["__struct__"])()
        for name, field in value["fields"].items():
            setattr(native, name, _materialise(field, package))
        return native
    return value


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
            entries[call.key] = {**spec, "result": _invoke(spec, package)}
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            skips.append(Skip(call.key, f"{type(exc).__name__}: {exc}"))

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
    return _jsonable(target(*arguments))


def replay(document: dict, package) -> tuple[dict, dict]:
    """Re-run the calls recorded in a golden document.

    The *file* drives this, not the IR: a golden run has to be able to say
    "this entry point used to exist and no longer does", which is impossible
    if the plan is regenerated from the current code every time.

    Returns ({key: result}, {key: error message}).
    """
    results: dict = {}
    errors: dict = {}
    for key, entry in (document.get("entries") or {}).items():
        try:
            results[key] = _invoke(entry, package)
        except Exception as exc:  # noqa: BLE001
            errors[key] = f"{type(exc).__name__}: {exc}"
    return results, errors


# --- the file -----------------------------------------------------------


def build_document(
    module: ModuleIR,
    entries: dict,
    skips: list[Skip],
    rtol: float = DEFAULT_RTOL,
    atol: float = DEFAULT_ATOL,
) -> dict:
    """The golden file's contents.

    Deliberately free of anything that varies run to run — no timestamps, no
    paths, no machine identity — so the only reason for a diff in review is a
    changed answer.
    """
    return {
        "format": FORMAT_VERSION,
        "service": module.name,
        "language": module.language,
        "tolerance": {"rtol": rtol, "atol": atol},
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


def compare(document: dict, results: dict, errors: dict | None = None) -> list[str]:
    """Differences between a recorded golden document and a fresh replay.

    Returns human-readable lines; empty means the answers are unchanged. A
    call that now raises, or an entry point that disappeared, counts as a
    difference: an API that silently lost a symbol between regenerates is
    exactly the drift this is here to catch.
    """
    tolerance = document.get("tolerance") or {}
    rtol = tolerance.get("rtol", DEFAULT_RTOL)
    atol = tolerance.get("atol", DEFAULT_ATOL)
    errors = errors or {}

    differences = []
    for key, entry in (document.get("entries") or {}).items():
        if key in errors:
            differences.append(f"{key}: recorded, but this build cannot call it — {errors[key]}")
            continue
        if key not in results:
            differences.append(f"{key}: recorded but missing from this build")
            continue
        expected, actual = entry.get("result"), results[key]
        if not _close(expected, actual, rtol, atol):
            differences.append(f"{key}: expected {expected!r}, got {actual!r}")

    return differences
