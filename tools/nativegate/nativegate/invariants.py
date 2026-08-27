"""T12 -- `invariants.json`: the layer-3 artifact and its aggregate gate.

Spec: design-verification-layers.md section 3.7 (file schema, `uncovered`),
section 2.6 (CI ordering: oracle check -> golden -> invariants), section 5
(where each layer runs).

This module is the closing task of the verification-layers effort: it does
not implement new checking logic (T10's structural properties and T11's
declared properties already do that), it *aggregates* them into one result
artifact per entry, and enforces the one rule none of the earlier tasks were
positioned to enforce on their own -- "an invariants file that silently
checks nothing must not look like a pass" (section 3.7, quoted in
`EmptyCheckedError`).

Ambiguities resolved here (T10/T11 were silent on them because they operate
one property/one sweep at a time, not on the whole file):

* **Which sweep(s) `bounds`/`sum_to_one` run against.** Neither carries an
  `in:` parameter (unlike `monotone`/`scales_linearly_in`), so section 3.4's
  lattice does not say which of an entry's several 1-D sweeps a parameter-
  less property is evaluated over. This module evaluates them over *every*
  swept parameter of the entry -- each of the entry's `ParameterSweep`s in
  turn -- rather than inventing a "the first one" or "the base point only"
  reading, on the theory that a `bounds` claim is supposed to hold
  everywhere the lattice looks, not only along one arbitrarily-chosen axis.
  An entry with no swept parameter at all (nothing in `ranges:` matches its
  numeric arguments) cannot evaluate the property and is recorded under
  `uncovered` instead of silently skipped.
* **`uncovered`'s scope.** Section 3.4 says "a swept parameter with no
  declared range is an error ... recorded under `uncovered`" for the
  *lattice* in general, not only for parameters a declared invariant
  mentions. This module therefore records EVERY entry with an unswept
  numeric parameter under `uncovered` (via `EntryLattice.unswept_parameters`,
  T9), whether or not that entry carries a declared invariant at all --
  this is what surfaces `tubing_bhp`'s undeclared `rate` range even though
  no `invariants: tubing_bhp: ...` block exists in `nativegate.yaml`.
* **`corners`/`scatter`/`n` default from `VerificationConfig.lattice`.**
  T8's `VerificationConfig` now parses a `lattice:` block (`n`,
  `scatter: {seed, count}`, `corners:`) -- see `config.LatticeConfig`. Every
  `n=`/`scatter_seed=`/`scatter_count=`/`corners=` keyword below defaults to
  `None`, meaning "take it from `verification.lattice`"; an explicit
  argument (still accepted, for tests and any other direct caller) always
  wins over the declared config. `corners` is reported in `invariants.json`
  as a mapping of function name to its declared corner tuples (matching
  `LatticeConfig.corners`'s own shape) rather than the flat list this module
  used before the config surface existed, since a flat list cannot say which
  function each corner belongs to once more than one function declares
  corners.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from . import golden, lattice, structural_invariants as si
from .config import (
    BoundsProperty,
    MonotoneProperty,
    ScalesLinearlyInProperty,
    SumToOneProperty,
    SymmetricInProperty,
    VerificationConfig,
)
from .declared_invariants import NotImplementedInvariant, PropertyReport, evaluate_property, _label
from .lattice import EntryLattice, ParameterSweep

if TYPE_CHECKING:
    from .ir import ModuleIR

INVARIANTS_FILENAME = "invariants.json"
FORMAT_VERSION = 1


class InvariantsError(RuntimeError):
    """Base for a hard failure of the invariants runner itself."""


class EmptyCheckedError(InvariantsError):
    """`checked` ended up empty -- section 3.7: "an invariants file that
    silently checks nothing must not look like a pass." Never swallowed."""


class _UncoveredError(Exception):
    """Internal: a declared property could not be evaluated. Turned into an
    `uncovered` reason string, never raised past this module."""


@dataclass
class InvariantsResult:
    document: dict
    passed: bool
    failure_messages: list[str] = field(default_factory=list)


# --- picking which sweep(s) a declared property runs against ---------------


def _resolve_n(n: int | None, verification: VerificationConfig) -> int:
    """`n`'s "explicit argument, else declared config, else the module
    default" resolution rule -- shared by `build_invariants_document` and
    `verify_invariants` so the two entry points can never silently disagree
    on what an unset `n` means for the same `VerificationConfig`."""
    if n is not None:
        return n
    return verification.lattice.n if verification.lattice.n is not None else lattice.DEFAULT_SWEEP_POINTS


def _sweeps_for_property(
    prop, entry_lattice: EntryLattice, parameter_names: Sequence[str]
) -> list[tuple[ParameterSweep, int]]:
    """`[(sweep, argument_position), ...]` to evaluate `prop` against, or
    raise `_UncoveredError` with the reason it cannot be evaluated at all.

    See the module docstring's "Ambiguities resolved here" for why
    `bounds`/`sum_to_one` (no `in:`) run against every swept parameter while
    `monotone`/`scales_linearly_in` (declared `in:`) run against exactly one.
    """
    if isinstance(prop, (MonotoneProperty, ScalesLinearlyInProperty)):
        parameter = prop.parameter
        sweep_obj = next((s for s in entry_lattice.sweeps if s.parameter == parameter), None)
        if sweep_obj is None:
            raise _UncoveredError(f"no range declared for parameter '{parameter}'")
        return [(sweep_obj, parameter_names.index(parameter))]

    if isinstance(prop, SymmetricInProperty):
        # No evaluator exists yet (declared_invariants.evaluate_property
        # raises NotImplementedInvariant for this word regardless of which
        # sweep is chosen) -- pick a sweep only so evaluate_property has one
        # to raise against; if there is none, report the range gap instead
        # of the unimplemented-word gap, since that is the more actionable
        # of the two.
        if not entry_lattice.sweeps:
            raise _UncoveredError(
                "no swept parameter declared to evaluate `symmetric_in` against"
            )
        sweep_obj = entry_lattice.sweeps[0]
        return [(sweep_obj, parameter_names.index(sweep_obj.parameter))]

    # bounds / sum_to_one: no declared `in:` -- evaluated over every swept
    # parameter of the entry (see module docstring).
    if not entry_lattice.sweeps:
        missing = entry_lattice.unswept_parameters
        reason = (
            f"no range declared for parameter(s) {', '.join(missing)}"
            if missing
            else "no swept parameter available to evaluate this property against"
        )
        raise _UncoveredError(reason)
    return [(s, parameter_names.index(s.parameter)) for s in entry_lattice.sweeps]


def _callable_for(package, entry: dict):
    """A plain positional callable `declared_invariants.evaluate_property`
    can call directly -- reuses `golden.invoke` (materialisation, method/
    static/function dispatch) rather than re-deriving calling conventions."""

    def call(*args):
        synthetic = {**entry, "arguments": list(args)}
        result, _effects = golden.invoke(synthetic, package)
        return result

    return call


def _entries_by_function_name(document: dict) -> dict[str, tuple[str, dict]]:
    """First golden.json entry per function name, paired with its own key.

    Returning the `(key, entry)` pair (rather than just `entry`) lets callers
    look up the matching lattice by the *same* golden key that picked this
    entry, so a function with more than one recorded entry (e.g. one golden
    entry per constructed instance) always evaluates a property against one
    consistent (entry, lattice) pair instead of silently mixing one entry's
    call arguments with a different entry's swept ranges.
    """
    result: dict[str, tuple[str, dict]] = {}
    for key, entry in document.get("entries", {}).items():
        result.setdefault(entry.get("name") or key, (key, entry))
    return result


def _limited(sweep_obj: ParameterSweep, points_limit: int | None) -> ParameterSweep:
    if points_limit is None:
        return sweep_obj
    return ParameterSweep(
        parameter=sweep_obj.parameter,
        lo=sweep_obj.lo,
        hi=sweep_obj.hi,
        points=sweep_obj.points[:points_limit],
    )


# --- declared-invariant pass, aggregated over a whole entry -----------------


def run_declared_invariants(
    document: dict,
    functions_by_name: dict,
    verification: VerificationConfig,
    package,
    lattices: dict[str, EntryLattice],
    *,
    points_limit: int | None = None,
) -> tuple[dict[str, list[tuple[object, list[PropertyReport]]]], dict[str, list[str]]]:
    """Every declared property in `verification.invariants`, evaluated.

    Returns `(reports, uncovered)`:
    * `reports[function_name]` is `[(prop, [PropertyReport, ...]), ...]` --
      one `PropertyReport` per sweep the property ran against (see
      `_sweeps_for_property`).
    * `uncovered[function_name]` is every reason a declared property for
      that function could not be evaluated at all (an undeclared range, or
      `NotImplementedInvariant`'s vocabulary gap) -- never silently dropped.
    """
    entries_by_function = _entries_by_function_name(document)

    reports: dict[str, list[tuple[object, list[PropertyReport]]]] = {}
    uncovered: dict[str, list[str]] = {}

    for fn_name, properties in verification.invariants.items():
        key_and_entry = entries_by_function.get(fn_name)
        fn = functions_by_name.get(fn_name)
        entry = key_and_entry[1] if key_and_entry is not None else None
        entry_lattice = lattices.get(key_and_entry[0]) if key_and_entry is not None else None
        if entry is None or fn is None or entry_lattice is None:
            uncovered.setdefault(fn_name, []).append(
                "no golden.json entry (or no matching lattice) to evaluate "
                "declared invariants against"
            )
            continue

        parameter_names = [p.name for p in fn.parameters if p.intent != "out"]
        call = _callable_for(package, entry)

        for prop in properties:
            try:
                sweeps = _sweeps_for_property(prop, entry_lattice, parameter_names)
            except _UncoveredError as exc:
                uncovered.setdefault(fn_name, []).append(str(exc))
                continue

            try:
                sub_reports = [
                    evaluate_property(
                        call,
                        _limited(sweep_obj, points_limit),
                        prop,
                        fn_name,
                        position=position,
                        base_arguments=entry_lattice.base_arguments,
                    )
                    for sweep_obj, position in sweeps
                ]
            except NotImplementedInvariant as exc:
                uncovered.setdefault(fn_name, []).append(str(exc))
                continue

            reports.setdefault(fn_name, []).append((prop, sub_reports))

    return reports, uncovered


# --- assembling the file (spec section 3.7) ---------------------------------


def build_invariants_document(
    module: "ModuleIR",
    document: dict,
    verification: VerificationConfig,
    structural_results: si.StructuralInvariantResults,
    declared_reports: dict[str, list[tuple[object, list[PropertyReport]]]],
    declared_uncovered: dict[str, list[str]],
    lattices: dict[str, EntryLattice],
    *,
    n: int | None = None,
    scatter_seed: int | None = None,
    scatter_count: int | None = None,
    corners: dict[str, list] | None = None,
    environment: dict | None = None,
) -> dict:
    """The `invariants.json` contents, per spec section 3.7's schema.

    `n`/`scatter_seed`/`scatter_count`/`corners` each default to `None`,
    meaning "read it from `verification.lattice`" (see the module
    docstring) -- an explicit argument always overrides the declared
    config.
    """
    n = _resolve_n(n, verification)
    if scatter_seed is None:
        scatter_seed = verification.lattice.scatter.seed
    if scatter_count is None:
        scatter_count = verification.lattice.scatter.count
    if corners is None:
        corners = verification.lattice.corners
    checked: dict[str, dict] = {}
    uncovered: dict[str, str] = {}

    # First entry wins per function name -- must match `_entries_by_function_name`'s
    # convention exactly, or the `uncovered` check below (which needs the
    # matching lattice) picks a *different* entry than the one
    # `run_declared_invariants` actually evaluated properties against,
    # producing an invariants.json where a function's checked properties and
    # its uncovered reasons come from two different recorded calls.
    entry_key_by_function: dict[str, str] = {}
    for key, entry in document.get("entries", {}).items():
        entry_key_by_function.setdefault(entry.get("name") or key, key)

    # Structural properties, grouped by function name.
    structural_by_function: dict[str, list] = {}
    for outcome in structural_results.outcomes:
        structural_by_function.setdefault(outcome.function, []).append(outcome)

    # Every function with a built lattice is a candidate for `uncovered`
    # (an unswept numeric parameter is a property of the *lattice*, spec
    # section 3.4, independent of whether any property is actually declared
    # for that function) -- so this must not be limited to functions that
    # already have a structural/declared outcome, or a function with zero
    # declared invariants and an undeclared range (e.g. petro_api's
    # `tubing_bhp`) would never be reported.
    lattice_functions = {
        (entry.get("name") or key)
        for key, entry in document.get("entries", {}).items()
        if key in lattices
    }
    all_functions = (
        set(structural_by_function)
        | set(declared_reports)
        | set(declared_uncovered)
        | lattice_functions
    )

    for fn_name in all_functions:
        properties: list[str] = []
        points = 0
        passed = True

        for outcome in structural_by_function.get(fn_name, []):
            properties.append(outcome.property)
            points = max(points, outcome.points_checked)
            if outcome.status != "pass":
                passed = False

        for prop, sub_reports in declared_reports.get(fn_name, []):
            properties.append(_label(prop))
            points = max(points, sum(r.points_checked for r in sub_reports))
            if any(not r.passed for r in sub_reports):
                passed = False

        if properties:
            checked[fn_name] = {
                "properties": properties,
                "points": points,
                "status": "pass" if passed else "fail",
            }

        # An unswept numeric parameter on this entry's lattice (spec section
        # 3.4: "a swept parameter with no declared range is an error, not a
        # guess, recorded under uncovered") -- true independent of whether
        # any invariant is declared for this function at all.
        reasons: list[str] = []
        entry_key = entry_key_by_function.get(fn_name)
        entry_lattice = lattices.get(entry_key) if entry_key is not None else None
        if entry_lattice is not None and entry_lattice.unswept_parameters:
            reasons.append(
                "no range declared for parameter(s) "
                + ", ".join(entry_lattice.unswept_parameters)
            )
        reasons.extend(declared_uncovered.get(fn_name, []))
        if reasons:
            uncovered[fn_name] = "; ".join(reasons)

    return {
        "format": FORMAT_VERSION,
        "service": module.name,
        "provenance": environment if environment is not None else golden.provenance(),
        "lattice": {
            "points_per_sweep": n,
            "ranges": {name: [rng.lo, rng.hi] for name, rng in verification.ranges.items()},
            "corners": {
                fn_name: [list(corner) for corner in fn_corners]
                for fn_name, fn_corners in (corners or {}).items()
            },
            "scatter": {"count": scatter_count, "seed": scatter_seed},
        },
        "state": {
            "setup": list(verification.state.setup),
            "mutating": list(verification.state.mutating),
            "error_flag": verification.state.error_flag,
        },
        # Insertion order follows the entries actually checked -- sorted by
        # name for a stable, reviewable diff (unlike golden.json, call order
        # carries no meaning here: nothing in this file replays a sequence).
        "checked": dict(sorted(checked.items())),
        "uncovered": dict(sorted(uncovered.items())),
    }


# --- the public entry point --------------------------------------------------


def verify_invariants(
    module: "ModuleIR",
    document: dict,
    verification: VerificationConfig,
    package,
    target: si.SubprocessTarget,
    *,
    n: int | None = None,
    points_limit: int | None = None,
    scatter_seed: int | None = None,
    scatter_count: int | None = None,
    corners: dict[str, list] | None = None,
    environment: dict | None = None,
    python_executable: str | None = None,
) -> InvariantsResult:
    """Run structural (T10) + declared (T11) properties and assemble the
    `invariants.json` document (spec section 3.7).

    `n`/`scatter_seed`/`scatter_count`/`corners` default to `None`, meaning
    "read it from `verification.lattice`" -- see the module docstring and
    `config.LatticeConfig`. An explicit argument always overrides the
    declared config. `n` in particular also controls how many points
    `si.build_lattices` (and therefore every structural/declared check)
    actually evaluates, not just what is reported in the document.

    Raises `EmptyCheckedError` if the resulting `checked` block is empty --
    per spec, that must never be reported as a pass. Otherwise returns an
    `InvariantsResult` whose `.passed` reflects every checked entry's status
    and whose `.document` is ready to be written with `write()`.
    """
    n = _resolve_n(n, verification)

    # `si.build_lattices` reads corners/scatter straight off
    # `verification.lattice` -- it has no override parameters of its own. If
    # this call's explicit `scatter_seed`/`scatter_count`/`corners` differ
    # from what's declared, build the lattices against an overridden copy of
    # `verification` so the points actually checked below match what
    # `build_invariants_document` (called with these same explicit
    # arguments, further down) reports as having been checked -- otherwise
    # the report would describe a sweep this call never actually ran.
    lattice_verification = verification
    if scatter_seed is not None or scatter_count is not None or corners is not None:
        effective_scatter = replace(
            verification.lattice.scatter,
            seed=verification.lattice.scatter.seed if scatter_seed is None else scatter_seed,
            count=verification.lattice.scatter.count if scatter_count is None else scatter_count,
        )
        effective_lattice = replace(
            verification.lattice,
            scatter=effective_scatter,
            corners=verification.lattice.corners if corners is None else corners,
        )
        lattice_verification = replace(verification, lattice=effective_lattice)

    functions_by_name = {fn.name: fn for fn in module.functions}
    lattices = si.build_lattices(document, functions_by_name, lattice_verification, n=n)

    structural_results = si.run_structural_invariants(
        module,
        document,
        verification,
        package,
        target,
        lattices=lattices,
        n=n,
        points_limit=points_limit,
        python_executable=python_executable,
    )
    declared_reports, declared_uncovered = run_declared_invariants(
        document, functions_by_name, verification, package, lattices, points_limit=points_limit
    )

    invariants_document = build_invariants_document(
        module,
        document,
        verification,
        structural_results,
        declared_reports,
        declared_uncovered,
        lattices,
        n=n,
        scatter_seed=scatter_seed,
        scatter_count=scatter_count,
        corners=corners,
        environment=environment,
    )

    if not invariants_document["checked"]:
        raise EmptyCheckedError(
            "invariants run checked nothing -- an invariants file that "
            "silently checks nothing must not look like a pass "
            "(design-verification-layers.md section 3.7). Declare `ranges:` "
            "for at least one numeric parameter, or `state`/`invariants` in "
            "nativegate.yaml, before running `ngate invariants verify`."
        )

    passed = all(v["status"] == "pass" for v in invariants_document["checked"].values())

    failure_messages: list[str] = []
    for outcome in structural_results.failures():
        for point_failure in outcome.failures:
            failure_messages.append(
                f"{outcome.property} `{outcome.function}`: {point_failure.detail}"
            )
    for reports in declared_reports.values():
        for _prop, sub_reports in reports:
            for r in sub_reports:
                if not r.passed:
                    failure_messages.append(r.message)

    return InvariantsResult(
        document=invariants_document, passed=passed, failure_messages=failure_messages
    )


# --- file I/O (matching golden.write()'s conventions exactly) --------------


def write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n")


def read(path: Path) -> dict:
    return json.loads(path.read_text())


def coverage(document: dict) -> tuple[int, int]:
    """(checked, uncovered) counts -- mirrors `golden.coverage()`."""
    return len(document.get("checked") or {}), len(document.get("uncovered") or {})
