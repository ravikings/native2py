"""Tests for T11's declared-invariant runner (declared_invariants.py).

Spec: design-verification-layers.md section 3.2 (declared vocabulary,
`monotone` = raw float64 `>=`/`<=`, no tolerance) and section 3.6 (bisection
rule, byte-identical counterexample message). These use plain Python fixture
functions -- no compiler, no native build -- since the properties operate on
a `ParameterSweep` (from lattice.py, T9) and any callable.
"""

from __future__ import annotations

import math

import pytest

from nativegate.config import (
    BoundsProperty,
    MonotoneProperty,
    ScalesLinearlyInProperty,
    SymmetricInProperty,
)
from nativegate.declared_invariants import (
    NotImplementedInvariant,
    bisect_bracket,
    evaluate_bounds,
    evaluate_monotone,
    evaluate_property,
)
from nativegate.lattice import LatticePoint, ParameterSweep, sweep as sweep_fn

LO, HI, N = 14.7, 10000.0, 33


def _pressure_sweep() -> ParameterSweep:
    xs = sweep_fn(LO, HI, N)
    points = tuple(
        LatticePoint(arguments=(x,), origin="sweep", parameter="pressure", index=i)
        for i, x in enumerate(xs)
    )
    return ParameterSweep(parameter="pressure", lo=LO, hi=HI, points=points)


# --- bounds: byte-exact failure message -------------------------------------


def test_bounds_violation_message_byte_exact():
    sw = _pressure_sweep()

    def oil_fvf(p):
        return 1.2 - 0.25 * p / 10000.0

    prop = BoundsProperty(min=1.0)
    report = evaluate_bounds(
        oil_fvf, sw, prop, "oil_fvf", position=0, base_arguments=(sw.points[0].arguments[0],)
    )

    assert not report.passed
    assert report.points_checked == 33
    assert report.message == (
        "invariant `oil_fvf: bounds{min: 1.0}` failed\n"
        "  first failure : pressure = 8127.756249999999   -> 0.99680609375\n"
        "  last passing  : pressure = 7815.715624999999   -> 1.004607109375\n"
        "  bracket       : the property breaks between "
        "7999.99999999976 and 8000.000000000044\n"
        "  lattice       : pressure ∈ [14.7, 10000], 33 points, index 26"
    )
    # "proved" must never appear anywhere in output.
    assert "proved" not in report.message


def test_bounds_pass_says_checked_at_n_points_never_proved():
    sw = _pressure_sweep()

    def oil_fvf(p):
        return 1.5  # always within [1.0, inf)

    prop = BoundsProperty(min=1.0)
    report = evaluate_bounds(
        oil_fvf, sw, prop, "oil_fvf", position=0, base_arguments=(sw.points[0].arguments[0],)
    )
    assert report.passed
    assert report.points_checked == 33
    assert report.message == "invariant `oil_fvf: bounds{min: 1.0}` checked at 33 points"
    assert "proved" not in report.message


# --- monotone: failure and pass ---------------------------------------------


def test_monotone_failure_message_byte_exact():
    sw = _pressure_sweep()

    def solution_gor(p):
        if 8000 < p < 8500:
            return 1.0  # a dip that breaks nondecreasing
        return p / 1000.0

    prop = MonotoneProperty(parameter="pressure", direction="nondecreasing")
    report = evaluate_monotone(
        solution_gor, sw, prop, "solution_gor", position=0, base_arguments=(sw.points[0].arguments[0],)
    )

    assert not report.passed
    assert report.message == (
        "invariant `solution_gor: monotone{in: pressure, direction: nondecreasing}` failed\n"
        "  first failure : pressure = 8127.756249999999   -> 1\n"
        "  last passing  : pressure = 7815.715624999999   -> 7.815715624999999\n"
        "  bracket       : the property breaks between "
        "7999.99999999976 and 8000.000000000044\n"
        "  lattice       : pressure ∈ [14.7, 10000], 33 points, index 26"
    )
    assert "proved" not in report.message


def test_monotone_pass():
    sw = _pressure_sweep()

    def solution_gor(p):
        return p  # strictly increasing -> nondecreasing holds everywhere

    prop = MonotoneProperty(parameter="pressure", direction="nondecreasing")
    report = evaluate_monotone(
        solution_gor, sw, prop, "solution_gor", position=0, base_arguments=(sw.points[0].arguments[0],)
    )
    assert report.passed
    assert report.points_checked == 33
    assert report.message == (
        "invariant `solution_gor: monotone{in: pressure, direction: nondecreasing}` "
        "checked at 33 points"
    )


def test_monotone_no_tolerance_field_exists():
    # config.py's own guarantee (spec 3.2): monotone carries no tolerance,
    # so none can sneak in through config. Verified directly on the dataclass.
    prop = MonotoneProperty(parameter="pressure", direction="nondecreasing")
    assert not hasattr(prop, "tolerance")


# --- bisection termination rules --------------------------------------------


def test_bisect_stops_at_adjacent_floats_before_the_cap():
    # A narrow bracket needs far fewer than 40 halvings to hit float64
    # exhaustion (adjacent floats), so it must stop there, not loop to 40.
    a = 1.0
    b = 1.0 + 2.0**-20
    threshold = 1.0 + 2.0**-21

    def holds(x: float) -> bool:
        return x < threshold

    final_a, final_b, steps = bisect_bracket(a, b, holds)
    assert steps < 40
    # a and b are adjacent floats: no float exists strictly between them.
    assert math.nextafter(final_a, final_b) == final_b


def test_bisect_honors_the_40_step_cap():
    # A bracket wide enough (in relative terms) that full float64 collapse
    # needs on the order of 52 steps (a double's mantissa) -- so 40 steps is
    # not enough to reach adjacent floats, and the cap must be what stops it.
    a, b = 0.0, 1.0
    threshold = math.pi / 4  # irrational: no rational midpoint ever lands on it

    def holds(x: float) -> bool:
        return x < threshold

    final_a, final_b, steps = bisect_bracket(a, b, holds)
    assert steps == 40
    assert math.nextafter(final_a, final_b) != final_b  # not yet adjacent


def test_bisect_precondition_bracket_always_valid():
    a, b = 0.0, 1.0
    threshold = 0.3

    def holds(x: float) -> bool:
        return x < threshold

    final_a, final_b, _steps = bisect_bracket(a, b, holds)
    assert holds(final_a)
    assert not holds(final_b)


# --- stubs: unimplemented vocabulary words are hard errors ------------------


def test_symmetric_in_is_a_hard_error_not_a_silent_pass():
    sw = _pressure_sweep()
    prop = SymmetricInProperty(parameters=["a", "b"])
    with pytest.raises(NotImplementedInvariant):
        evaluate_property(
            lambda *a: 0.0, sw, prop, "some_fn", position=0, base_arguments=(sw.points[0].arguments[0],)
        )


def test_scales_linearly_in_is_a_hard_error_not_a_silent_pass():
    sw = _pressure_sweep()
    prop = ScalesLinearlyInProperty(parameter="pressure")
    with pytest.raises(NotImplementedInvariant):
        evaluate_property(
            lambda *a: 0.0, sw, prop, "some_fn", position=0, base_arguments=(sw.points[0].arguments[0],)
        )
