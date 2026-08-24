"""T10 — structural invariants: `finite`, `total`, `no_error_flag`,
`idempotent`, `order_independent` (design-verification-layers.md section 3,
in particular 3.2's structural table and 3.5's mutator scoping / fresh
process / setup replay).

These five properties need no domain declaration beyond `state:` (T8's
`StateConfig`) and no lattice authoring beyond `ranges:` -- they fall
straight out of the IR plus T9's lattice (`lattice.build_entry_lattice`).
That is what makes them "structural" rather than "declared" (spec section
3.2's own distinction; the declared vocabulary -- `bounds`, `monotone`,
`sum_to_one`, ... -- is T11's job).

Two evaluation strategies, per spec section 3.5:

* `finite`, `total`, `no_error_flag` run **in the already-imported
  process** the caller hands in (`package`), because nothing about them
  needs isolation from prior calls -- they are checking the current call's
  own outcome, not whether a *previous* call leaked into it. Each point is
  still preceded by a fresh replay of the declared `setup` sequence (spec
  section 3.5: "every property evaluation runs after the `setup` sequence,
  replayed with the arguments recorded in `golden.json`").
* `idempotent` and `order_independent` run in a **fresh subprocess per
  sequence**, because what they are testing IS cross-call leakage --
  running them in the caller's own process could not tell "no hidden
  state" from "hidden state, but this interpreter happens to already be in
  the right state from an earlier check". See "The subprocess worker"
  below for the wire format between parent and worker.

Mutator scoping (spec section 3.5)
-----------------------------------

`idempotent` is checked for every entry point NOT in `state.mutating`;
`order_independent` uses only non-`mutating` routines as the interposed
`g`, and checks every non-`mutating` `f`. Applying either property to a
declared mutator (`pvt_set_fluid` on petro_api) would condemn the API's own
contract -- that state IS the point of the routine -- so mutators are
excluded from both checks entirely, on both sides of the pairing.

An *undeclared* mutator does not escape this: it still gets used as `g`
against every non-mutating `f`, and if it actually mutates shared state,
`order_independent` fails and says so -- see `_ORDER_INDEPENDENT_HINT`,
whose wording ("the fix ... is adding the routine to `mutating` in
review") is load-bearing per spec section 3.5's own text and is asserted
verbatim in tests.

The subprocess worker
----------------------

`idempotent`/`order_independent` need a scripted call sequence executed in
a brand-new interpreter, with the result of specific calls reported back.
Rather than reusing T2's wire protocol (`wire.py`) -- built for *bits from
native driver stdout*, one value per line, no notion of "run these N calls
in order and tell me about calls 2 and 4" -- this module defines its own,
much smaller, JSON-in/JSON-out protocol:

* **in**: a JSON file (path passed as argv) shaped
  ``{"sys_path": [...], "module": "...", "calls": [{"name": ..., "arguments":
  [...]}, ...], "observe": [<index>, ...]}`` -- `sys_path` entries are
  inserted so `module` (an extension or a plain fixture module) can be
  imported, `calls` run in order exactly as `golden.invoke` would run them,
  and `observe` names which calls' results to report back.
* **out**: one line of JSON on stdout,
  ``{"ok": true, "observations": [{"index": ..., "value": ...}, ...]}`` on
  success or ``{"ok": false, "error": "..."}`` on any exception -- printed
  once, at the end, so a stray print from imported code cannot be mistaken
  for the payload (the parent only parses the *last* non-empty stdout
  line).

JSON was chosen over reusing the wire line format because the worker's
payload is a scripted call sequence and a per-call result map, not a flat
stream of `<key, slot, value>` triples with no notion of "run twice, tell
me about the second run"; forcing that shape onto T2's protocol would have
meant inventing new slot/key conventions with no native-code counterpart to
keep them honest. Bitwise comparison of the reported values still goes
through T2's `wire.pack_float` (see `_values_equal`) -- only the envelope
around the values is new, not the comparison itself.

Every subprocess launch runs under `buildinfo.pinned_environment()` (spec
section 2.8 / section 4 rule 3), merged over the parent's own environment
so the worker can still be found on `sys.path`/`PYTHONPATH`.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from . import buildinfo, golden, lattice, wire

if TYPE_CHECKING:
    from .config import VerificationConfig
    from .ir import ModuleIR

# spec section 3.5: the byte-for-byte guidance a legitimate order_independent
# failure calls for. Quoted (paraphrased, not verbatim, since the spec gives
# guidance rather than a fixed string) in every such failure's message, and
# asserted against directly in tests.
_ORDER_INDEPENDENT_HINT = (
    "this looks like an UNDECLARED mutator: if {g!r} is genuinely supposed "
    "to change state that {f!r} reads, the fix is adding {g!r} to "
    "`state.mutating` in native2py.yaml (in review) -- not changing this "
    "check."
)


class WorkerError(RuntimeError):
    """The fresh-process worker could not be run, or crashed outright.

    Distinct from a *property* failure (a `PointFailure`): this is "the
    harness could not even ask the question at this point" -- e.g. the
    worker process itself failed to start, or produced no parseable
    output -- and is surfaced as a failure of the property being checked
    (see `_check_idempotent`/`_check_order_independent`), never silently
    skipped.
    """


# --- results -----------------------------------------------------------


@dataclass(frozen=True)
class PointFailure:
    """One lattice point (or worker run) at which a property failed."""

    point: str
    detail: str


@dataclass(frozen=True)
class PropertyOutcome:
    """One property's result for one entry-point key."""

    property: str
    function: str
    points_checked: int
    failures: tuple[PointFailure, ...] = ()

    @property
    def status(self) -> str:
        return "pass" if not self.failures else "fail"


@dataclass(frozen=True)
class StructuralInvariantResults:
    outcomes: tuple[PropertyOutcome, ...] = ()

    @property
    def passed(self) -> bool:
        return all(o.status == "pass" for o in self.outcomes)

    def failures(self) -> list[PropertyOutcome]:
        return [o for o in self.outcomes if o.status == "fail"]

    def by_property(self, name: str) -> list[PropertyOutcome]:
        return [o for o in self.outcomes if o.property == name]


# --- how the fresh-process worker imports the target --------------------


@dataclass(frozen=True)
class SubprocessTarget:
    """How `idempotent`/`order_independent`'s worker imports the module
    under test in a fresh interpreter.

    `module_name` is passed to `importlib.import_module` inside the
    worker; `sys_path` are directories inserted (at the front) before that
    import, so a built extension or a plain fixture module that is not
    already importable can still be found. `attr_path` is an optional chain
    of attribute lookups applied after the import -- f2py nests routines
    under `<extension>.<fortran module name>.<routine>` (see
    `ir.FunctionDef.fortran_module`'s docstring) rather than at the
    extension's top level, so a caller whose entry points live under a
    `module ... end module` block passes e.g. `("petro_api",)` here to reach
    them, the same way `services/petro_api/python/petro_api/__init__.py`'s
    generated flattening does.
    """

    module_name: str
    sys_path: tuple[str, ...] = ()
    attr_path: tuple[str, ...] = ()


# --- point selection ------------------------------------------------------


def _points_for(entry_lattice: "lattice.EntryLattice", points_limit: int | None):
    """Every lattice point for one entry, plus the base (recorded) point
    itself -- spec section 3.4 sweeps `[lo, hi]` inclusive, which does not
    generally include the golden.json base value, and the base value is the
    one call every one of these properties should certainly hold at.

    `points_limit` caps the number of points actually evaluated (kept in
    the same order they are generated: base, then sweeps, then corners,
    then scatter) -- a knob for keeping subprocess-per-point checks
    (`idempotent`/`order_independent`) affordable in tests; production runs
    pass `None` (no cap) so every declared point is genuinely checked, per
    spec section 3.4's "at every lattice point".
    """
    points = [
        lattice.LatticePoint(arguments=entry_lattice.base_arguments, origin="base")
    ]
    for sweep in entry_lattice.sweeps:
        points.extend(sweep.points)
    points.extend(entry_lattice.corners)
    points.extend(entry_lattice.scatter)
    if points_limit is not None:
        points = points[:points_limit]
    return points


def _describe_point(point: "lattice.LatticePoint") -> str:
    if point.origin == "sweep":
        return f"{point.parameter}[{point.index}] = {point.arguments!r} (sweep)"
    return f"{point.origin}: {point.arguments!r}"


def build_lattices(
    document: dict,
    functions_by_name: dict,
    verification: "VerificationConfig",
    *,
    n: int = lattice.DEFAULT_SWEEP_POINTS,
) -> dict[str, "lattice.EntryLattice"]:
    """T9's per-entry lattice for every golden.json entry whose function is
    in `functions_by_name` and whose recorded argument count matches the
    function's non-`intent(out)` parameter count.

    An entry that does not match (arity mismatch, or names a function not
    present in this module's IR) is silently excluded here -- diagnosing
    *that* is golden's/the IR's job, not this module's; it simply is not
    counted for structural checking, the same way an uncovered parameter
    is not swept.
    """
    lattices: dict[str, "lattice.EntryLattice"] = {}
    for key, entry in document.get("entries", {}).items():
        name = entry.get("name") or key
        fn = functions_by_name.get(name)
        if fn is None:
            continue
        parameter_names = [p.name for p in fn.parameters if p.intent != "out"]
        arguments = entry.get("arguments") or []
        if len(parameter_names) != len(arguments):
            continue
        try:
            lattices[key] = lattice.build_entry_lattice(
                entry, parameter_names, verification.ranges, n=n
            )
        except ValueError:
            continue
    return lattices


# --- setup replay (in-process) --------------------------------------------


def _replay_setup(package, document: dict, setup_names: Sequence[str]) -> None:
    """Replay `state.setup`, in order, with golden.json's recorded
    arguments for each -- spec section 3.5: "every property evaluation runs
    after the `setup` sequence, replayed with the arguments recorded in
    `golden.json`"."""
    for name in setup_names:
        entry = document.get("entries", {}).get(name)
        if entry is None:
            raise KeyError(
                f"state.setup names {name!r}, which has no golden.json entry "
                "to replay it with"
            )
        golden.invoke(entry, package)


def _call_at_point(package, entry: dict, point: "lattice.LatticePoint"):
    """Call `entry`'s routine with `point`'s arguments substituted for the
    recorded ones, in-process, returning the JSON-able result. Reuses
    `golden.invoke` (rather than re-deriving f2py/pybind11 calling
    conventions here) so method/static/function/constructor dispatch and
    array materialisation stay in exactly one place."""
    synthetic = {**entry, "arguments": list(point.arguments)}
    result, _effects = golden.invoke(synthetic, package)
    return result


def _is_finite(value) -> bool:
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_finite(v) for v in value)
    return True


# --- finite / total / no_error_flag (in-process) --------------------------


def _check_finite_and_total(
    document: dict,
    functions_by_name: dict,
    verification: "VerificationConfig",
    package,
    lattices: dict[str, "lattice.EntryLattice"],
    points_limit: int | None,
) -> list[PropertyOutcome]:
    outcomes: list[PropertyOutcome] = []
    for key, entry in document.get("entries", {}).items():
        name = entry.get("name") or key
        entry_lattice = lattices.get(key)
        if functions_by_name.get(name) is None or entry_lattice is None:
            continue
        finite_failures: list[PointFailure] = []
        total_failures: list[PointFailure] = []
        checked = 0
        for point in _points_for(entry_lattice, points_limit):
            checked += 1
            desc = _describe_point(point)
            try:
                _replay_setup(package, document, verification.state.setup)
                result = _call_at_point(package, entry, point)
            except Exception as exc:  # noqa: BLE001 - recorded as a failure, not swallowed
                total_failures.append(
                    PointFailure(desc, f"{type(exc).__name__}: {exc}")
                )
                continue
            if not _is_finite(result):
                finite_failures.append(
                    PointFailure(desc, f"non-finite result: {result!r}")
                )
        outcomes.append(PropertyOutcome("finite", key, checked, tuple(finite_failures)))
        outcomes.append(PropertyOutcome("total", key, checked, tuple(total_failures)))
    return outcomes


def _check_no_error_flag(
    document: dict,
    functions_by_name: dict,
    verification: "VerificationConfig",
    package,
    lattices: dict[str, "lattice.EntryLattice"],
    points_limit: int | None,
) -> list[PropertyOutcome]:
    error_flag = verification.state.error_flag
    if not error_flag:
        return []
    error_flag_entry_key = None
    for key, entry in document.get("entries", {}).items():
        if (entry.get("name") or key) == error_flag:
            error_flag_entry_key = key
            break
    if error_flag_entry_key is None:
        raise KeyError(
            f"state.error_flag names {error_flag!r}, which has no golden.json entry"
        )
    error_flag_entry = document["entries"][error_flag_entry_key]

    outcomes: list[PropertyOutcome] = []
    for key, entry in document.get("entries", {}).items():
        name = entry.get("name") or key
        if name == error_flag:
            continue  # the accessor is not checked against itself
        entry_lattice = lattices.get(key)
        if functions_by_name.get(name) is None or entry_lattice is None:
            continue
        failures: list[PointFailure] = []
        checked = 0
        for point in _points_for(entry_lattice, points_limit):
            checked += 1
            desc = _describe_point(point)
            try:
                _replay_setup(package, document, verification.state.setup)
                _call_at_point(package, entry, point)
                flag_value, _ = golden.invoke(error_flag_entry, package)
            except Exception as exc:  # noqa: BLE001
                failures.append(PointFailure(desc, f"{type(exc).__name__}: {exc}"))
                continue
            if flag_value:
                failures.append(
                    PointFailure(
                        desc,
                        f"{error_flag}() reported {flag_value!r} (truthy/nonzero) "
                        f"after calling {name}({point.arguments!r})",
                    )
                )
        outcomes.append(PropertyOutcome("no_error_flag", key, checked, tuple(failures)))
    return outcomes


# --- the fresh-process worker (idempotent / order_independent) -----------


def _setup_calls(document: dict, verification: "VerificationConfig") -> list[dict]:
    calls = []
    for name in verification.state.setup:
        entry = document.get("entries", {}).get(name)
        if entry is None:
            raise KeyError(
                f"state.setup names {name!r}, which has no golden.json entry "
                "to replay it with"
            )
        calls.append({"name": name, "arguments": list(entry["arguments"])})
    return calls


def _run_worker_sequence(
    calls: list[dict],
    observe: list[int],
    target: SubprocessTarget,
    *,
    python_executable: str | None = None,
    timeout: float = 120.0,
) -> dict:
    """Run `calls` (a scripted call sequence) in a fresh subprocess,
    reporting the results of the calls at `observe` positions. See the
    module docstring's "The subprocess worker" for the wire format."""
    python_executable = python_executable or sys.executable
    with tempfile.TemporaryDirectory() as tmp:
        spec_path = Path(tmp) / "spec.json"
        spec_path.write_text(
            json.dumps(
                {
                    "sys_path": list(target.sys_path),
                    "module": target.module_name,
                    "attr_path": list(target.attr_path),
                    "calls": calls,
                    "observe": observe,
                }
            )
        )
        env = {**os.environ, **buildinfo.pinned_environment()}
        try:
            result = subprocess.run(
                [python_executable, "-m", "native2py.structural_invariants", "worker", str(spec_path)],
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkerError(
                f"structural invariants worker timed out after {timeout}s"
            ) from exc

    if result.returncode not in (0, 1):
        raise WorkerError(
            f"structural invariants worker crashed (exit {result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    stdout = result.stdout.strip()
    if not stdout:
        raise WorkerError(
            f"structural invariants worker produced no output (exit "
            f"{result.returncode}):\nstderr:\n{result.stderr}"
        )
    try:
        return json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError as exc:
        raise WorkerError(
            f"structural invariants worker produced unparseable output: {stdout!r}"
        ) from exc


def _values_equal(a, b) -> bool:
    """Bitwise-equal for floats (via `wire.pack_float`, per spec section
    3.2/4 rule 1 -- "bits, not decimals"), recursively for lists, exact `==`
    otherwise (int/bool/str/None -- there is no bit channel for these, but
    the same no-tolerance principle applies: exact equality or nothing)."""
    if isinstance(a, float) or isinstance(b, float):
        if not (isinstance(a, float) and isinstance(b, float)):
            return a == b
        return wire.pack_float(a) == wire.pack_float(b)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_values_equal(x, y) for x, y in zip(a, b))
    return a == b


def _check_idempotent(
    document: dict,
    verification: "VerificationConfig",
    target: SubprocessTarget,
    lattices: dict[str, "lattice.EntryLattice"],
    points_limit: int | None,
    python_executable: str | None,
) -> list[PropertyOutcome]:
    mutating = set(verification.state.mutating)
    setup_calls = _setup_calls(document, verification)
    base_index = len(setup_calls)
    outcomes: list[PropertyOutcome] = []

    for key, entry in document.get("entries", {}).items():
        name = entry.get("name") or key
        entry_lattice = lattices.get(key)
        if name in mutating or entry_lattice is None:
            continue
        failures: list[PointFailure] = []
        checked = 0
        for point in _points_for(entry_lattice, points_limit):
            checked += 1
            desc = _describe_point(point)
            calls = setup_calls + [
                {"name": name, "arguments": list(point.arguments)},
                {"name": name, "arguments": list(point.arguments)},
            ]
            try:
                payload = _run_worker_sequence(
                    calls, [base_index, base_index + 1], target,
                    python_executable=python_executable,
                )
            except WorkerError as exc:
                failures.append(PointFailure(desc, str(exc)))
                continue
            if not payload.get("ok"):
                failures.append(PointFailure(desc, payload.get("error", "worker failed")))
                continue
            values = {o["index"]: o["value"] for o in payload["observations"]}
            first, second = values.get(base_index), values.get(base_index + 1)
            if not _values_equal(first, second):
                failures.append(
                    PointFailure(
                        desc,
                        f"{name}({point.arguments!r}) called twice in a row, in a "
                        f"fresh process, returned different bits: first={first!r} "
                        f"second={second!r}",
                    )
                )
        outcomes.append(PropertyOutcome("idempotent", key, checked, tuple(failures)))
    return outcomes


def _check_order_independent(
    document: dict,
    verification: "VerificationConfig",
    target: SubprocessTarget,
    lattices: dict[str, "lattice.EntryLattice"],
    points_limit: int | None,
    python_executable: str | None,
) -> list[PropertyOutcome]:
    mutating = set(verification.state.mutating)
    setup_calls = _setup_calls(document, verification)
    base_index = len(setup_calls)

    entries = document.get("entries", {})
    non_mutating: list[tuple[str, dict]] = [
        (entry.get("name") or key, entry)
        for key, entry in entries.items()
        if (entry.get("name") or key) not in mutating
    ]

    outcomes: list[PropertyOutcome] = []
    for key, entry in entries.items():
        f_name = entry.get("name") or key
        entry_lattice = lattices.get(key)
        if f_name in mutating or entry_lattice is None:
            continue
        failures: list[PointFailure] = []
        checked = 0
        for point in _points_for(entry_lattice, points_limit):
            checked += 1
            desc = _describe_point(point)

            baseline_calls = setup_calls + [
                {"name": f_name, "arguments": list(point.arguments)},
                {"name": f_name, "arguments": list(point.arguments)},
            ]
            try:
                baseline_payload = _run_worker_sequence(
                    baseline_calls, [base_index + 1], target,
                    python_executable=python_executable,
                )
            except WorkerError as exc:
                failures.append(PointFailure(desc, f"baseline: {exc}"))
                continue
            if not baseline_payload.get("ok"):
                failures.append(
                    PointFailure(desc, f"baseline: {baseline_payload.get('error')}")
                )
                continue
            baseline_values = {o["index"]: o["value"] for o in baseline_payload["observations"]}
            baseline_second = baseline_values.get(base_index + 1)

            for g_name, g_entry in non_mutating:
                g_arguments = list(g_entry["arguments"])
                perturbed_calls = setup_calls + [
                    {"name": f_name, "arguments": list(point.arguments)},
                    {"name": g_name, "arguments": g_arguments},
                    {"name": f_name, "arguments": list(point.arguments)},
                ]
                second_f_index = base_index + 2
                try:
                    payload = _run_worker_sequence(
                        perturbed_calls, [second_f_index], target,
                        python_executable=python_executable,
                    )
                except WorkerError as exc:
                    failures.append(PointFailure(desc, f"g={g_name}: {exc}"))
                    continue
                if not payload.get("ok"):
                    failures.append(
                        PointFailure(desc, f"g={g_name}: {payload.get('error')}")
                    )
                    continue
                values = {o["index"]: o["value"] for o in payload["observations"]}
                perturbed_second = values.get(second_f_index)
                if not _values_equal(baseline_second, perturbed_second):
                    hint = _ORDER_INDEPENDENT_HINT.format(g=g_name, f=f_name)
                    failures.append(
                        PointFailure(
                            desc,
                            f"{f_name}({point.arguments!r}); {g_name}({g_arguments!r}); "
                            f"{f_name}({point.arguments!r}) != "
                            f"{f_name}({point.arguments!r}); {f_name}({point.arguments!r}) "
                            f"(baseline second call={baseline_second!r}, "
                            f"perturbed second call={perturbed_second!r}) -- {hint}",
                        )
                    )
        outcomes.append(PropertyOutcome("order_independent", key, checked, tuple(failures)))
    return outcomes


# --- the public entry point ------------------------------------------------


def run_structural_invariants(
    module: "ModuleIR",
    document: dict,
    verification: "VerificationConfig",
    package,
    target: SubprocessTarget,
    *,
    lattices: dict[str, "lattice.EntryLattice"] | None = None,
    n: int = lattice.DEFAULT_SWEEP_POINTS,
    points_limit: int | None = None,
    python_executable: str | None = None,
) -> StructuralInvariantResults:
    """Every structural property (spec section 3.2's table) for every
    golden.json entry whose function is in `module` and has a lattice.

    `package` is the already-imported extension, used for the in-process
    checks (`finite`/`total`/`no_error_flag`); `target` tells the fresh-
    process worker how to import the (same, or equivalent) module for
    `idempotent`/`order_independent` -- kept separate from `package`
    because the in-process checks need a live object and the subprocess
    checks need an *importable name*, and the two are not always the same
    thing (a package already imported under a temporary name, e.g.).

    `points_limit` caps the number of lattice points evaluated per
    property per function -- primarily for `idempotent`/`order_independent`,
    whose cost is one-or-more fresh subprocess launches *per point*; pass
    `None` (the default) for full spec-mandated coverage ("at every lattice
    point"), or a small integer to keep test runtime bounded.
    """
    functions_by_name = {fn.name: fn for fn in module.functions}
    if lattices is None:
        lattices = build_lattices(document, functions_by_name, verification, n=n)

    outcomes: list[PropertyOutcome] = []
    outcomes += _check_finite_and_total(
        document, functions_by_name, verification, package, lattices, points_limit
    )
    outcomes += _check_no_error_flag(
        document, functions_by_name, verification, package, lattices, points_limit
    )
    outcomes += _check_idempotent(
        document, verification, target, lattices, points_limit, python_executable
    )
    outcomes += _check_order_independent(
        document, verification, target, lattices, points_limit, python_executable
    )
    return StructuralInvariantResults(tuple(outcomes))


# --- the worker process entry point ----------------------------------------


def _worker_main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "worker":
        sys.stderr.write(
            "usage: python -m native2py.structural_invariants worker <spec.json>\n"
        )
        return 2

    spec = json.loads(Path(argv[1]).read_text())
    for path in spec.get("sys_path", []):
        if path not in sys.path:
            sys.path.insert(0, path)

    try:
        package = importlib.import_module(spec["module"])
        for attr in spec.get("attr_path", []):
            package = getattr(package, attr)
        observe = set(spec.get("observe", []))
        observations = []
        for index, call in enumerate(spec["calls"]):
            arguments = [golden._materialise(a, package) for a in call["arguments"]]
            fn = getattr(package, call["name"])
            result = golden._jsonable(fn(*arguments))
            if index in observe:
                observations.append({"index": index, "value": result})
        payload = {"ok": True, "observations": observations}
    except Exception as exc:  # noqa: BLE001 - reported to the parent, not raised here
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(_worker_main(sys.argv[1:]))
