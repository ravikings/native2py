"""Bit-exact tests for the verification lattice (T9).

Spec: design-verification-layers.md section 3.4 -- the point-generation
formula is "part of the specification", pinned to the bit. Every float
assertion here goes through `struct.pack(">d", x).hex()` rather than `==`
on a float literal, so the test itself cannot be fooled by Python printing
two differently-rounded floats the same way.
"""

from __future__ import annotations

import struct

import pytest

from native2py.config import RangeDeclaration
from native2py.lattice import (
    DEFAULT_SWEEP_POINTS,
    EntryLattice,
    LatticePoint,
    ParameterSweep,
    build_entry_lattice,
    scatter,
    splitmix64,
    sweep,
)


def _hex(x: float) -> str:
    return struct.pack(">d", x).hex()


# --- sweep(): bit-exact against hard-coded, once-computed hex -------------


def test_sweep_default_count_and_bit_exact_values():
    # sweep(14.7, 10000.0, 33) -- the exact range/point-count petro_api's
    # pressure invariant would declare. Hex values computed once with the
    # pinned formula (lo + (i * (hi - lo)) / (n - 1)) and frozen here.
    points = sweep(14.7, 10000.0, 33)
    assert len(points) == 33

    expected_hex = {
        0: "402d666666666666",  # x_0 == lo exactly
        1: "40746bd999999999",
        2: "4083f64000000000",
        16: "40b38f5999999999",
        31: "40c2ebfacccccccd",
        32: "40c3880000000000",  # x_32 == hi exactly
    }
    for index, expected in expected_hex.items():
        assert _hex(points[index]) == expected, f"index {index}"


def test_sweep_small_n_bit_exact():
    # sweep(0.0, 1.0, 5) lands on exact quarters -- a case where the naive
    # and pinned formulas would agree, included as a sanity check that the
    # formula is not simply wrong.
    points = sweep(0.0, 1.0, 5)
    expected_hex = [
        "0000000000000000",  # 0.0
        "3fd0000000000000",  # 0.25
        "3fe0000000000000",  # 0.5
        "3fe8000000000000",  # 0.75
        "3ff0000000000000",  # 1.0
    ]
    assert [_hex(p) for p in points] == expected_hex


def test_sweep_default_n_is_33():
    assert DEFAULT_SWEEP_POINTS == 33
    assert len(sweep(0.0, 1.0)) == 33


def test_sweep_endpoints_are_exact_not_computed():
    # x_0 must be *exactly* lo and x_{n-1} *exactly* hi as float objects --
    # spec: "assigned exactly (not computed from the formula -- use the
    # literal endpoints)". Use a lo/hi/n combination where the formula's own
    # arithmetic would NOT reproduce the endpoint exactly, to prove the
    # endpoints are not merely accidentally exact.
    lo, hi, n = 14.7, 10000.0, 33
    points = sweep(lo, hi, n)
    assert points[0] is lo or points[0] == lo
    assert points[0] == lo
    assert points[-1] == hi
    # The formula's own computation of the last point, if it *had* been used
    # instead of the literal endpoint, would round differently:
    formula_last = lo + ((n - 1) * (hi - lo)) / (n - 1)
    # Demonstrate the two are not bit-for-bit guaranteed identical in
    # general by checking a case where they do differ:
    lo2, hi2, n2 = 404.9341374504143, 4323.927298825689, 21
    formula_endpoint = lo2 + ((n2 - 1) * (hi2 - lo2)) / (n2 - 1)
    assert _hex(formula_endpoint) != _hex(hi2), (
        "fixture is supposed to demonstrate a case where the raw formula "
        "would NOT reproduce the exact endpoint -- if this fails, pick a "
        "different lo/hi/n"
    )
    assert _hex(sweep(lo2, hi2, n2)[-1]) == _hex(hi2)


def test_sweep_rejects_fewer_than_two_points():
    with pytest.raises(ValueError):
        sweep(0.0, 1.0, 1)


# --- proving the test suite is sensitive to the formula's association ----


def _naive_lerp(lo: float, hi: float, n: int) -> list[float]:
    """`lo*(1-t)+hi*t` -- a common, *different* linspace association."""
    points = []
    for i in range(n):
        t = i / (n - 1)
        points.append(lo * (1 - t) + hi * t)
    return points


def test_naive_association_differs_from_pinned_formula():
    # If sweep() were "simplified" to lo*(1-t)+hi*t, this test would fail --
    # which is the point: it demonstrates the bit-exact tests above are
    # actually exercising the pinned association, not merely checking *some*
    # linspace-shaped output that any reasonable implementation would also
    # produce.
    lo, hi, n = 14.7, 10000.0, 33
    pinned = sweep(lo, hi, n)
    naive = _naive_lerp(lo, hi, n)
    differing_interior_indices = [
        i for i in range(1, n - 1) if _hex(pinned[i]) != _hex(naive[i])
    ]
    assert differing_interior_indices, (
        "the pinned formula and the naive lo*(1-t)+hi*t association produced "
        "bit-identical output at every interior point for this lo/hi/n -- "
        "that would mean this test fixture cannot actually distinguish the "
        "two, defeating its purpose"
    )
    # Endpoints agree (both are assigned/land on lo, hi); it is specifically
    # the interior points where the associations diverge.
    assert _hex(pinned[0]) == _hex(naive[0]) == _hex(lo)
    assert _hex(pinned[-1]) == _hex(naive[-1]) == _hex(hi)


# --- splitmix64: reference vectors -----------------------------------------


def test_splitmix64_reference_vectors_seed_zero():
    # Canonical splitmix64 (Vigna, http://prng.di.unimi.it/splitmix64.c)
    # reference stream for seed 0 -- the first five 64-bit outputs.
    outputs = splitmix64(0, 5)
    expected = [
        0xE220A8397B1DCDAF,
        0x6E789E6AA1B965F4,
        0x06C45D188009454F,
        0xF88BB8A8724C81EC,
        0x1B39896A51A8749B,
    ]
    assert outputs == expected


def test_splitmix64_is_deterministic_and_seed_sensitive():
    assert splitmix64(0, 3) == splitmix64(0, 3)
    assert splitmix64(0, 3) != splitmix64(1, 3)


def test_splitmix64_rejects_negative_count():
    with pytest.raises(ValueError):
        splitmix64(0, -1)


# --- scatter() ---------------------------------------------------------


def test_scatter_is_deterministic_and_in_range():
    ranges = {"pressure": RangeDeclaration(lo=14.7, hi=10000.0)}
    first = scatter(seed=42, count=10, ranges=ranges)
    second = scatter(seed=42, count=10, ranges=ranges)
    assert first == second
    values = first["pressure"]
    assert len(values) == 10
    assert all(14.7 <= v <= 10000.0 for v in values)


def test_scatter_never_uses_python_random_module(monkeypatch):
    # Poison the stdlib random module's primitives; scatter() must not call
    # any of them.
    import random

    def _boom(*args, **kwargs):
        raise AssertionError("scatter() must not use the `random` module")

    monkeypatch.setattr(random, "random", _boom)
    monkeypatch.setattr(random, "uniform", _boom)
    monkeypatch.setattr(random, "seed", _boom)
    ranges = {"pressure": RangeDeclaration(lo=0.0, hi=1.0)}
    scatter(seed=1, count=5, ranges=ranges)  # must not raise


def test_scatter_seed_changes_output():
    ranges = {"pressure": RangeDeclaration(lo=0.0, hi=1.0)}
    a = scatter(seed=1, count=5, ranges=ranges)
    b = scatter(seed=2, count=5, ranges=ranges)
    assert a != b


def test_scatter_per_range_independent_of_other_ranges():
    # Adding another declared range must not perturb the samples already
    # drawn for an existing one (see the module's documented design
    # decision: each range gets its own splitmix64 sub-stream).
    one_range = {"pressure": RangeDeclaration(lo=0.0, hi=1.0)}
    two_ranges = {
        "pressure": RangeDeclaration(lo=0.0, hi=1.0),
        "temperature": RangeDeclaration(lo=32.0, hi=212.0),
    }
    a = scatter(seed=7, count=4, ranges=one_range)
    b = scatter(seed=7, count=4, ranges=two_ranges)
    assert a["pressure"] == b["pressure"]


# --- build_entry_lattice(): per-entry sweep construction --------------


def _golden_entry(**arguments_kwargs):
    return {"name": "solution_gor", "arguments": [2000.0]}


def test_build_entry_lattice_base_point_is_golden_arguments_unchanged():
    entry = {"name": "solution_gor", "arguments": [2000.0]}
    ranges = {"pressure": RangeDeclaration(lo=14.7, hi=10000.0)}
    lattice = build_entry_lattice(entry, ["pressure"], ranges, n=5)
    assert lattice.base_arguments == (2000.0,)


def test_build_entry_lattice_sweeps_one_parameter_holding_others_fixed():
    # Two-argument entry, only one parameter ("pressure") has a declared
    # range: the other argument must stay pinned at its golden.json value
    # across every sweep point.
    entry = {"name": "two_arg_fn", "arguments": [2000.0, 42.0]}
    ranges = {"pressure": RangeDeclaration(lo=14.7, hi=10000.0)}
    lattice = build_entry_lattice(entry, ["pressure", "other"], ranges, n=5)

    assert len(lattice.sweeps) == 1
    swept = lattice.sweeps[0]
    assert swept.parameter == "pressure"
    assert swept.lo == 14.7
    assert swept.hi == 10000.0
    assert len(swept.points) == 5

    for point in swept.points:
        assert point.arguments[1] == 42.0  # "other" held at its golden value
        assert point.origin == "sweep"
        assert point.parameter == "pressure"

    # Endpoints of the sweep are the base entry's pressure value replaced
    # with the declared range's exact lo/hi.
    assert swept.points[0].arguments == (14.7, 42.0)
    assert swept.points[-1].arguments == (10000.0, 42.0)
    assert [p.index for p in swept.points] == [0, 1, 2, 3, 4]

    # A parameter with no declared range is reported, not silently dropped.
    assert lattice.unswept_parameters == ("other",)


def test_build_entry_lattice_corners_pass_through_verbatim():
    entry = {"name": "solution_gor", "arguments": [2000.0]}
    ranges = {"pressure": RangeDeclaration(lo=14.7, hi=10000.0)}
    corners = [(14.7,), (10000.0,), (2500.0,)]
    lattice = build_entry_lattice(entry, ["pressure"], ranges, n=5, corners=corners)

    assert len(lattice.corners) == 3
    assert [c.arguments for c in lattice.corners] == [(14.7,), (10000.0,), (2500.0,)]
    assert all(c.origin == "corner" for c in lattice.corners)


def test_build_entry_lattice_scatter_uses_declared_seed():
    entry = {"name": "solution_gor", "arguments": [2000.0]}
    ranges = {"pressure": RangeDeclaration(lo=14.7, hi=10000.0)}
    lattice = build_entry_lattice(
        entry, ["pressure"], ranges, n=5, scatter_seed=1234, scatter_count=6
    )
    assert len(lattice.scatter) == 6
    assert all(p.origin == "scatter" for p in lattice.scatter)
    for point in lattice.scatter:
        (pressure,) = point.arguments
        assert 14.7 <= pressure <= 10000.0

    # Reproducing with the same seed reproduces the same scatter points.
    lattice_again = build_entry_lattice(
        entry, ["pressure"], ranges, n=5, scatter_seed=1234, scatter_count=6
    )
    assert [p.arguments for p in lattice.scatter] == [
        p.arguments for p in lattice_again.scatter
    ]


def test_build_entry_lattice_scatter_requires_seed():
    entry = {"name": "solution_gor", "arguments": [2000.0]}
    ranges = {"pressure": RangeDeclaration(lo=14.7, hi=10000.0)}
    with pytest.raises(ValueError):
        build_entry_lattice(entry, ["pressure"], ranges, n=5, scatter_count=3)


def test_build_entry_lattice_no_range_means_no_sweep_no_error():
    # "there is no default range -- a swept parameter with no declared
    # range is an error, not a guess, recorded under uncovered" (spec
    # section 3.4): this function's contract is to not sweep it and report
    # it back, leaving the "is this an error" decision (and the
    # invariants.json `uncovered` bookkeeping) to the caller (T12).
    entry = {"name": "solution_gor", "arguments": [2000.0]}
    lattice = build_entry_lattice(entry, ["pressure"], ranges={}, n=5)
    assert lattice.sweeps == ()
    assert lattice.unswept_parameters == ("pressure",)


def test_build_entry_lattice_non_numeric_arguments_are_never_swept():
    # An array or struct argument must not be treated as a sweepable scalar
    # even if its name happens to collide with a declared range.
    entry = {"name": "fn", "arguments": [[1.0, 2.0, 3.0]]}
    ranges = {"data": RangeDeclaration(lo=0.0, hi=1.0)}
    lattice = build_entry_lattice(entry, ["data"], ranges, n=5)
    assert lattice.sweeps == ()
    assert lattice.unswept_parameters == ()  # not "rangeless", just not a number


def test_build_entry_lattice_mismatched_parameter_names_raises():
    entry = {"name": "fn", "arguments": [1.0, 2.0]}
    with pytest.raises(ValueError):
        build_entry_lattice(entry, ["only_one"], ranges={})
