"""T11 — evaluate declared invariant properties over T9's lattice.

Spec: design-verification-layers.md section 3.2 (declared vocabulary,
`monotone` compares raw float64 with `>=`/`<=`, no tolerance) and section 3.6
(counterexample reporting: first failure, then a pinned deterministic
bisection, in a byte-identical message format). Also section 6: the word
"proved" must never appear in output; the vocabulary is "checked at N
points".

This module evaluates the three properties this pass implements —
`bounds`, `monotone`, `sum_to_one` — against a lattice built by
:mod:`nativegate.lattice`. `symmetric_in` and `scales_linearly_in` are declared
vocabulary words (config.py's INVARIANT_VOCABULARY) that have no evaluator
here yet; per the task, an unimplemented vocabulary word must be a loud,
hard error, never a silently-passing no-op -- see `NotImplementedInvariant`
and `evaluate_property`'s dispatch, which raises rather than skips.

T10 (setup replay as its own module) had not landed when this was written.
Rather than duplicate a full runner, `replay_setup` below implements only
what this module needs: replay `state.setup`'s entries, in `nativegate.yaml`
order, using the arguments `golden.json` recorded for each -- directly
against `config.StateConfig` and `golden.invoke`, with no dependency on a
T10 module that does not exist yet.

Ambiguities in spec section 3.6, resolved here and flagged in the task
report (search this file for "AMBIGUITY" to find each decision):

* The example message's numeric formatting is inconsistent between fields
  (`min: 1.0` keeps a trailing `.0`; the lattice line's `10000` does not),
  and is not itself specified as a formula anywhere in the document. This
  module picks ONE deterministic rule (`_fmt`, below) and applies it to
  every computed/swept value, and a second rule for property-label
  thresholds (which echoes the declared YAML value's float form). Both are
  fixed and produce byte-identical output across runs for the same failure,
  which is the property section 3.6 actually requires ("the message is
  byte-identical across runs and can itself be asserted in tests") --
  matching the example's exact whitespace was not achievable from the text
  alone since the example is not accompanied by a format string.
* The example's `bracket` line ends in a physical unit ("... 8437.50 psia").
  No unit metadata exists anywhere in `config.py`'s dataclasses (checked:
  `RangeDeclaration` carries only `lo`/`hi`). This implementation omits the
  unit suffix rather than inventing a units feature the spec does not
  describe elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from .config import (
    BoundsProperty,
    InvariantProperty,
    MonotoneProperty,
    ScalesLinearlyInProperty,
    StateConfig,
    SumToOneProperty,
    SymmetricInProperty,
)
from .lattice import LatticePoint, ParameterSweep

MAX_BISECTION_STEPS = 40


class NotImplementedInvariant(NotImplementedError):
    """A declared vocabulary word with no evaluator this pass.

    Raised, never swallowed: "an unimplemented vocabulary word must be a
    hard error, never a silent pass" (task T11, item 1).
    """


class InvariantSetupError(RuntimeError):
    """`state.setup` names a function with no golden.json entry to replay."""


# --- value formatting (see module docstring: "Ambiguities") ----------------


def _fmt(x: float) -> str:
    """Deterministic formatting for a *computed/swept* value.

    Integral floats print without a trailing `.0` (`10000.0` -> `"10000"`),
    matching the lattice line's range endpoints in the spec's own example;
    everything else uses `repr()`, which is Python's shortest round-tripping
    representation and therefore stable and exact.
    """
    x = float(x)
    if x == int(x) and abs(x) < 1e16:
        return str(int(x))
    return repr(x)


def _fmt_threshold(x: float) -> str:
    """Formatting for a property's declared threshold (e.g. `bounds.min`).

    Echoes the float form the spec's example uses for `min: 1.0` -- always
    showing the value as a float literal, trailing `.0` included -- since
    this is restating a YAML-declared number, not a swept/computed one.
    """
    return repr(float(x))


def _label(prop: InvariantProperty) -> str:
    """The `<word>{...}` fragment inside the message's first line."""
    if isinstance(prop, BoundsProperty):
        parts = []
        if prop.min is not None:
            parts.append(f"min: {_fmt_threshold(prop.min)}")
        if prop.max is not None:
            parts.append(f"max: {_fmt_threshold(prop.max)}")
        return f"bounds{{{', '.join(parts)}}}"
    if isinstance(prop, MonotoneProperty):
        return f"monotone{{in: {prop.parameter}, direction: {prop.direction}}}"
    if isinstance(prop, SumToOneProperty):
        return f"sum_to_one{{{', '.join(prop.fields)}}}"
    raise AssertionError(f"unreachable: no label for {prop!r}")


def _row(label: str, body: str) -> str:
    """One `  <label padded to 14> : <body>` line (spec section 3.6's block)."""
    return f"  {label:<14}: {body}"


# --- bisection (spec section 3.6, pinned) -----------------------------------


def bisect_bracket(
    a: float,
    b: float,
    holds: Callable[[float], bool],
    max_steps: int = MAX_BISECTION_STEPS,
) -> tuple[float, float, int]:
    """Deterministically bisect `[a, b]` toward the pass/fail boundary.

    Precondition: `holds(a)` is True (a passes) and `holds(b)` is False (b
    fails) -- the bracket the spec calls "the last passing lattice point and
    the first failing one". Per spec: "the midpoint is `(a + b) / 2` in
    float64; the side keeping the bracket valid is retained; iteration stops
    when `(a + b) / 2` equals either endpoint (float64 exhaustion) or after
    40 steps, whichever is first."

    Returns `(a, b, steps_taken)` -- the tightest bracket found, still
    satisfying `holds(a)` and not `holds(b)`.
    """
    steps = 0
    while steps < max_steps:
        mid = (a + b) / 2
        if mid == a or mid == b:
            break
        steps += 1
        if holds(mid):
            a = mid
        else:
            b = mid
    return a, b, steps


# --- reporting ---------------------------------------------------------


@dataclass
class PropertyReport:
    function: str
    property: InvariantProperty
    passed: bool
    points_checked: int
    message: str


def _pass_message(function: str, prop: InvariantProperty, points_checked: int) -> str:
    # Section 6: "The file should say 'checked at 33 points', and the CLI
    # should never print the word 'proved'."
    return f"invariant `{function}: {_label(prop)}` checked at {points_checked} points"


def _failure_message(
    *,
    function: str,
    prop: InvariantProperty,
    parameter: str,
    first_failure_x: float,
    first_failure_value,
    last_passing_x: float,
    last_passing_value,
    bracket_a: float,
    bracket_b: float,
    lattice_lo: float,
    lattice_hi: float,
    lattice_n: int,
    lattice_index: int,
) -> str:
    """Byte-identical block per spec section 3.6::

        invariant `oil_fvf: bounds{min: 1.0}` failed
          first failure : pressure = 8437.50   -> 0.9994
          last passing  : pressure = 8125.00   -> 1.0002
          bracket       : the property breaks between 8125.00 and 8437.50 psia
          lattice       : pressure in [14.7, 10000], 33 points, index 27

    (rendered here with the same four labelled rows; see the module
    docstring for the two formatting/units ambiguities this resolves.)
    """
    lines = [
        f"invariant `{function}: {_label(prop)}` failed",
        _row(
            "first failure",
            f"{parameter} = {_fmt(first_failure_x)}   -> {_fmt(first_failure_value)}",
        ),
        _row(
            "last passing",
            f"{parameter} = {_fmt(last_passing_x)}   -> {_fmt(last_passing_value)}",
        ),
        _row(
            "bracket",
            f"the property breaks between {_fmt(bracket_a)} and {_fmt(bracket_b)}",
        ),
        _row(
            "lattice",
            f"{parameter} ∈ [{_fmt(lattice_lo)}, {_fmt(lattice_hi)}], "
            f"{lattice_n} points, index {lattice_index}",
        ),
    ]
    return "\n".join(lines)


# --- property evaluators -------------------------------------------------


def _replaced(arguments: tuple, position: int, value) -> tuple:
    return arguments[:position] + (value,) + arguments[position + 1 :]


def _call_at(fn: Callable, sweep: ParameterSweep, position: int, base_arguments: tuple, x: float):
    return fn(*_replaced(base_arguments, position, x))


def _bounds_holds(prop: BoundsProperty, value: float) -> bool:
    if prop.min is not None and not (value >= prop.min):
        return False
    if prop.max is not None and not (value <= prop.max):
        return False
    return True


def evaluate_bounds(
    fn: Callable,
    sweep: ParameterSweep,
    prop: BoundsProperty,
    function_name: str,
    *,
    position: int,
    base_arguments: tuple,
) -> PropertyReport:
    """Check `bounds` at every point of one parameter's sweep, in order."""
    values = [fn(*point.arguments) for point in sweep.points]
    passes = [_bounds_holds(prop, v) for v in values]

    if all(passes):
        return PropertyReport(
            function=function_name,
            property=prop,
            passed=True,
            points_checked=len(sweep.points),
            message=_pass_message(function_name, prop, len(sweep.points)),
        )

    first_fail_index = passes.index(False)
    if first_fail_index == 0:
        # No passing point precedes the failure: there is nothing to bracket.
        # Report the bare failure (still never the word "proved").
        message = (
            f"invariant `{function_name}: {_label(prop)}` failed\n"
            + _row(
                "first failure",
                f"{sweep.parameter} = {_fmt(sweep.points[0].arguments[position])}"
                f"   -> {_fmt(values[0])}",
            )
            + "\n"
            + _row(
                "lattice",
                f"{sweep.parameter} ∈ [{_fmt(sweep.lo)}, {_fmt(sweep.hi)}], "
                f"{len(sweep.points)} points, index 0 (no passing point precedes it)",
            )
        )
        return PropertyReport(
            function=function_name,
            property=prop,
            passed=False,
            points_checked=len(sweep.points),
            message=message,
        )

    last_pass_index = first_fail_index - 1
    a = sweep.points[last_pass_index].arguments[position]
    b = sweep.points[first_fail_index].arguments[position]

    def holds(x: float) -> bool:
        return _bounds_holds(prop, _call_at(fn, sweep, position, base_arguments, x))

    bracket_a, bracket_b, _steps = bisect_bracket(a, b, holds)

    message = _failure_message(
        function=function_name,
        prop=prop,
        parameter=sweep.parameter,
        first_failure_x=b,
        first_failure_value=values[first_fail_index],
        last_passing_x=a,
        last_passing_value=values[last_pass_index],
        bracket_a=bracket_a,
        bracket_b=bracket_b,
        lattice_lo=sweep.lo,
        lattice_hi=sweep.hi,
        lattice_n=len(sweep.points),
        lattice_index=first_fail_index,
    )
    return PropertyReport(
        function=function_name,
        property=prop,
        passed=False,
        points_checked=len(sweep.points),
        message=message,
    )


def _monotone_holds(prop: MonotoneProperty, previous: float, current: float) -> bool:
    # Spec section 3.2: "raw float64 values with >=/<=, no tolerance."
    if prop.direction == "nondecreasing":
        return current >= previous
    return current <= previous


def evaluate_monotone(
    fn: Callable,
    sweep: ParameterSweep,
    prop: MonotoneProperty,
    function_name: str,
    *,
    position: int,
    base_arguments: tuple,
) -> PropertyReport:
    """Check `monotone` between every consecutive pair of a sweep, in order."""
    values = [fn(*point.arguments) for point in sweep.points]

    first_fail_index = None
    for i in range(1, len(values)):
        if not _monotone_holds(prop, values[i - 1], values[i]):
            first_fail_index = i
            break

    if first_fail_index is None:
        return PropertyReport(
            function=function_name,
            property=prop,
            passed=True,
            points_checked=len(sweep.points),
            message=_pass_message(function_name, prop, len(sweep.points)),
        )

    last_pass_index = first_fail_index - 1
    a = sweep.points[last_pass_index].arguments[position]
    b = sweep.points[first_fail_index].arguments[position]
    anchor_value = values[last_pass_index]

    def holds(x: float) -> bool:
        # "Holds" here means: monotonicity from the anchor (last known-good
        # point) to `x` still holds -- i.e. the candidate `x` has not (yet)
        # reproduced the violation. This keeps the same bisect_bracket
        # contract (holds(a) True, holds(b) False) used for `bounds`.
        candidate_value = _call_at(fn, sweep, position, base_arguments, x)
        return _monotone_holds(prop, anchor_value, candidate_value)

    bracket_a, bracket_b, _steps = bisect_bracket(a, b, holds)

    message = _failure_message(
        function=function_name,
        prop=prop,
        parameter=sweep.parameter,
        first_failure_x=b,
        first_failure_value=values[first_fail_index],
        last_passing_x=a,
        last_passing_value=values[last_pass_index],
        bracket_a=bracket_a,
        bracket_b=bracket_b,
        lattice_lo=sweep.lo,
        lattice_hi=sweep.hi,
        lattice_n=len(sweep.points),
        lattice_index=first_fail_index,
    )
    return PropertyReport(
        function=function_name,
        property=prop,
        passed=False,
        points_checked=len(sweep.points),
        message=message,
    )


def _sum_to_one_holds(prop: SumToOneProperty, result: dict) -> bool:
    total = sum(result[field] for field in prop.fields)
    return abs(total - 1.0) <= prop.tolerance


def evaluate_sum_to_one(
    fn: Callable,
    sweep: ParameterSweep,
    prop: SumToOneProperty,
    function_name: str,
    *,
    position: int,
    base_arguments: tuple,
) -> PropertyReport:
    """Check `sum_to_one` at every point of one parameter's sweep, in order.

    `fn` is expected to return a mapping with (at least) `prop.fields` as
    keys -- the multi-field result `sum_to_one` is declared over (spec
    section 3.2's `saturations: sum_to_one: [sw, so, sg]` example).
    """
    results = [fn(*point.arguments) for point in sweep.points]
    sums = [sum(r[field] for r in [result] for field in prop.fields) for result in results]
    passes = [_sum_to_one_holds(prop, r) for r in results]

    if all(passes):
        return PropertyReport(
            function=function_name,
            property=prop,
            passed=True,
            points_checked=len(sweep.points),
            message=_pass_message(function_name, prop, len(sweep.points)),
        )

    first_fail_index = passes.index(False)
    if first_fail_index == 0:
        message = (
            f"invariant `{function_name}: {_label(prop)}` failed\n"
            + _row(
                "first failure",
                f"{sweep.parameter} = {_fmt(sweep.points[0].arguments[position])}"
                f"   -> {_fmt(sums[0])}",
            )
            + "\n"
            + _row(
                "lattice",
                f"{sweep.parameter} ∈ [{_fmt(sweep.lo)}, {_fmt(sweep.hi)}], "
                f"{len(sweep.points)} points, index 0 (no passing point precedes it)",
            )
        )
        return PropertyReport(
            function=function_name,
            property=prop,
            passed=False,
            points_checked=len(sweep.points),
            message=message,
        )

    last_pass_index = first_fail_index - 1
    a = sweep.points[last_pass_index].arguments[position]
    b = sweep.points[first_fail_index].arguments[position]

    def holds(x: float) -> bool:
        result = _call_at(fn, sweep, position, base_arguments, x)
        return _sum_to_one_holds(prop, result)

    bracket_a, bracket_b, _steps = bisect_bracket(a, b, holds)

    message = _failure_message(
        function=function_name,
        prop=prop,
        parameter=sweep.parameter,
        first_failure_x=b,
        first_failure_value=sums[first_fail_index],
        last_passing_x=a,
        last_passing_value=sums[last_pass_index],
        bracket_a=bracket_a,
        bracket_b=bracket_b,
        lattice_lo=sweep.lo,
        lattice_hi=sweep.hi,
        lattice_n=len(sweep.points),
        lattice_index=first_fail_index,
    )
    return PropertyReport(
        function=function_name,
        property=prop,
        passed=False,
        points_checked=len(sweep.points),
        message=message,
    )


def evaluate_property(
    fn: Callable,
    sweep: ParameterSweep,
    prop: InvariantProperty,
    function_name: str,
    *,
    position: int,
    base_arguments: tuple,
) -> PropertyReport:
    """Dispatch to the right evaluator, or raise for an unimplemented word.

    `symmetric_in` and `scales_linearly_in` are in config.py's closed
    vocabulary but have no evaluator this pass: per T11 item 1, that must be
    a loud error, never a silently-passing no-op.
    """
    if isinstance(prop, BoundsProperty):
        return evaluate_bounds(
            fn, sweep, prop, function_name, position=position, base_arguments=base_arguments
        )
    if isinstance(prop, MonotoneProperty):
        return evaluate_monotone(
            fn, sweep, prop, function_name, position=position, base_arguments=base_arguments
        )
    if isinstance(prop, SumToOneProperty):
        return evaluate_sum_to_one(
            fn, sweep, prop, function_name, position=position, base_arguments=base_arguments
        )
    if isinstance(prop, (SymmetricInProperty, ScalesLinearlyInProperty)):
        word = "symmetric_in" if isinstance(prop, SymmetricInProperty) else "scales_linearly_in"
        raise NotImplementedInvariant(
            f"invariants.{function_name}: `{word}` is a declared vocabulary word "
            "with no evaluator implemented yet (T11 stub). This is a hard "
            "error, not a skip -- an unimplemented property must never look "
            "like a pass."
        )
    raise AssertionError(f"unreachable: no evaluator for {prop!r}")


# --- minimal setup replay (T10 had not landed; see module docstring) -------


def replay_setup(state: StateConfig, golden_document: dict, package) -> None:
    """Replay `state.setup`'s entries, in `nativegate.yaml` order.

    Spec section 3.5: "Every property evaluation runs after the setup
    sequence, replayed with the arguments recorded in golden.json." This
    finds each setup function's entry by its recorded `name` (golden.json's
    entries are keyed by an arbitrary key, but each entry carries the
    function name it calls) and replays it via `golden.invoke`, which is the
    same call path golden itself uses.
    """
    from . import golden  # local: avoid a hard import cycle at module load

    entries = golden_document.get("entries") or {}
    entries_by_function: dict = {}
    for entry in entries.values():
        entries_by_function.setdefault(entry.get("name"), []).append(entry)

    # A function name can appear more than once in `state.setup` (e.g. an
    # init-then-update sequence); each occurrence consumes the *next*
    # golden.json entry recorded for that function, in file order, rather
    # than always replaying the first one.
    next_index: dict = {}
    for fn_name in state.setup:
        candidates = entries_by_function.get(fn_name) or []
        idx = next_index.get(fn_name, 0)
        if idx >= len(candidates):
            raise InvariantSetupError(
                f"state.setup names {fn_name!r}, which has no golden.json "
                "entry to replay -- setup must be replayed with recorded "
                "arguments, and none were recorded for this function "
                f"(occurrence {idx + 1} of {fn_name!r} in state.setup)."
            )
        golden.invoke(candidates[idx], package)
        next_index[fn_name] = idx + 1
