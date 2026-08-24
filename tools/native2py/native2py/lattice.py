"""The verification lattice: fixed, declared points to check properties over.

Layer 3 (design-verification-layers.md section 3) trades "one recorded point"
for "a swept surface" without reaching for randomness or a full cartesian
explosion. The whole guarantee of the layer rests on the lattice being the
*same* points on every machine forever, so this module is deliberately pure
(no native calls, no `random`, no `numpy.random` global state) and every
formula it uses is pinned to the bit in the spec:

* `sweep()` implements section 3.4's point-generation formula literally,
  in the exact operation association written there. A mathematically
  equivalent reassociation (e.g. precomputing the step and multiplying) is
  *not* interchangeable — see "Why the formula's association matters" below
  and the test that demonstrates it.
* `scatter()` implements splitmix64 itself (it is ~10 lines and completely
  specified) rather than depending on `random`, whose algorithm and stream
  are not specified to be stable across Python versions.
* `build_entry_lattice()` is the per-entry assembly described in section
  3.4 item 1: every argument held at the value `golden.json` recorded for
  that entry, one declared-range parameter swept at a time.

Why the formula's association matters
--------------------------------------

`x_i = lo + (i * (hi - lo)) / (n - 1)` and the mathematically-equivalent-in-
real-arithmetic `lo * (1 - t) + hi * t` (with `t = i / (n - 1)`) are *not*
the same function in float64: multiplication and division do not associate
or distribute exactly under rounding, so they disagree in the low bits at
most interior points (see `test_lattice.py::test_naive_association_differs`
for a hard-coded example). If a reviewer ever "simplified" `sweep()` to the
naive form, every declared `monotone` check checked against slightly
different points and the change would pass silently — nothing here would
tell them it happened, because nothing before this file's test suite pins
the bits. That is why the formula is copied here character-for-character
rather than re-derived.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping, Sequence

from .config import RangeDeclaration

# Spec section 3.4 item 1: "sweep one parameter across N points (default 33)
# inclusive of both endpoints".
DEFAULT_SWEEP_POINTS = 33

# splitmix64 constants (Vigna, http://prng.di.unimi.it/splitmix64.c) --
# these are the published constants, not tunables.
_MASK64 = (1 << 64) - 1
_GOLDEN_GAMMA = 0x9E3779B97F4A7C15
_MIX1 = 0xBF58476D1CE4E5B9
_MIX2 = 0x94D049BB133111EB


# --- the sweep formula, pinned to the bit --------------------------------


def sweep(lo: float, hi: float, n: int = DEFAULT_SWEEP_POINTS) -> list[float]:
    """`n` points across `[lo, hi]`, inclusive, per spec section 3.4.

    ``x_i = lo + (i * (hi - lo)) / (n - 1)``, each operation performed in
    float64 in exactly that association (no precomputed step, no
    reassociation) -- see the module docstring for why that is load-bearing.
    `x_0` and `x_{n-1}` are assigned the literal endpoints `lo`/`hi` rather
    than computed from the formula, so they are exact even where the
    formula's rounding would not reproduce them (e.g. `n - 1` not dividing
    `hi - lo` evenly is irrelevant to the endpoints, but the spec is explicit
    that they are assigned, not computed, so this does not depend on that
    working out).
    """
    if n < 2:
        raise ValueError(f"sweep() needs n >= 2 (need both endpoints), got n={n}")
    lo = float(lo)
    hi = float(hi)
    points: list[float] = []
    last = n - 1
    for i in range(n):
        if i == 0:
            points.append(lo)
        elif i == last:
            points.append(hi)
        else:
            points.append(lo + (i * (hi - lo)) / last)
    return points


# --- splitmix64 ------------------------------------------------------------


def _splitmix64_next(state: int) -> tuple[int, int]:
    """One splitmix64 step: advance `state`, return `(new_state, output)`.

    Reference: http://prng.di.unimi.it/splitmix64.c

        uint64_t next() {
            uint64_t z = (x += 0x9e3779b97f4a7c15);
            z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9;
            z = (z ^ (z >> 27)) * 0x94d049bb133111eb;
            return z ^ (z >> 31);
        }

    Python integers are unbounded, so every arithmetic step is masked to
    64 bits explicitly to reproduce the C implementation's wraparound.
    """
    state = (state + _GOLDEN_GAMMA) & _MASK64
    z = state
    z = ((z ^ (z >> 30)) * _MIX1) & _MASK64
    z = ((z ^ (z >> 27)) * _MIX2) & _MASK64
    z = z ^ (z >> 31)
    return state, z


def splitmix64(seed: int, count: int) -> list[int]:
    """`count` successive splitmix64 outputs (as uint64) seeded with `seed`.

    Exposed on its own (not just through `scatter()`) so it can be tested
    directly against the algorithm's published reference vectors.
    """
    if count < 0:
        raise ValueError(f"splitmix64() needs count >= 0, got {count}")
    state = int(seed) & _MASK64
    outputs = []
    for _ in range(count):
        state, z = _splitmix64_next(state)
        outputs.append(z)
    return outputs


def _unit_interval(word: int) -> float:
    """A uint64 splitmix64 output mapped to `[0.0, 1.0)`.

    2**53 significant bits is exactly a float64 mantissa's worth of
    precision, which is what `random.Random.random()` also uses -- taking
    the top 53 bits rather than dividing the full 64-bit word keeps the
    mapping exact-in-float64 (a plain `word / 2**64` also works, but loses
    the same low bits anyway, and this is the conventional construction).
    """
    return (word >> 11) * (1.0 / (1 << 53))


def scatter(
    seed: int, count: int, ranges: Mapping[str, RangeDeclaration]
) -> dict[str, tuple[float, ...]]:
    """`count` deterministic pseudo-random samples in each named range.

    Spec section 3.4 item 3: "splitmix64 with the seed written into the
    file, mapped to each range, for a fixed count. Reproducible, and honest
    about being a plain pseudo-random sample" -- explicitly *not*
    low-discrepancy.

    Design decision (not pinned by the spec, flagged here): each range gets
    its own independent sub-stream, seeded by mixing the declared `seed`
    with the range's name, so that adding or removing a range does not
    perturb the samples drawn for every other range (which sharing one
    running generator across ranges, in dict-iteration order, would do).
    The per-range sub-seed is itself derived with `splitmix64`, so nothing
    but splitmix64 and the declared seed is ever used as a source of
    randomness. The mapping from a splitmix64 word to `[lo, hi]` is
    `lo + u * (hi - lo)` with `u` the word's top 53 bits as a `[0, 1)`
    float -- also not pinned by the spec beyond "mapped to each range", and
    called out for the same reason.

    Returns `{parameter_name: (sample_0, ..., sample_{count-1})}`.
    """
    if count < 0:
        raise ValueError(f"scatter() needs count >= 0, got {count}")
    seed = int(seed) & _MASK64
    result: dict[str, tuple[float, ...]] = {}
    for name in ranges:
        # Derive a sub-seed for this range's sub-stream from (seed, name),
        # so the sub-stream is a pure function of the declared seed and the
        # parameter name -- not of iteration order or of how many other
        # ranges exist.
        # A raw `int.from_bytes(name) & _MASK64` only keeps the name's low 64
        # bits, so any two names sharing an 8-byte suffix (e.g.
        # "differential_pressure" and "static_pressure", both ending in
        # "pressure") would collide and silently draw identical sub-streams.
        # Hash the full name first so every byte of it affects the sub-seed.
        name_hash = int.from_bytes(
            hashlib.sha256(name.encode("utf-8")).digest()[:8], "big", signed=False
        )
        sub_seed = splitmix64((seed ^ name_hash) & _MASK64, 1)[0]
        words = splitmix64(sub_seed, count)
        lo, hi = ranges[name]
        result[name] = tuple(lo + _unit_interval(w) * (hi - lo) for w in words)
    return result


# --- per-entry lattice construction (spec section 3.4 item 1) --------------


@dataclass(frozen=True)
class LatticePoint:
    """One point to evaluate: a full positional argument tuple for a call.

    `origin` and the sweep-specific fields are provenance for the runners
    that consume this (T10's structural checks, T11's declared-property
    checks and its bisection reporting, which needs to know a point's
    position within its sweep to report "index 27" the way section 3.6's
    message format does).
    """

    arguments: tuple
    origin: str  # "sweep" | "corner" | "scatter"
    parameter: str | None = None  # the swept parameter, for origin="sweep"
    index: int | None = None  # position within that parameter's sweep


@dataclass(frozen=True)
class ParameterSweep:
    """The lattice for one swept parameter, plus enough to report a
    counterexample against it (spec section 3.6: "lattice: pressure in
    [14.7, 10000], 33 points, index 27")."""

    parameter: str
    lo: float
    hi: float
    points: tuple[LatticePoint, ...]


@dataclass(frozen=True)
class EntryLattice:
    """Every lattice point declared for one golden.json entry.

    `base_arguments` is the entry's own recorded `arguments` -- unmodified,
    per spec section 3.4 item 1 ("hold all arguments at the values recorded
    in golden.json for that entry"). Every point in `sweeps`, `corners` and
    `scatter` is a full argument tuple of the same arity, differing from
    `base_arguments` only in the swept parameter's position (sweeps,
    scatter) or not at all (corners, which are passed through verbatim --
    they are whatever the human declared, not derived from the base point).
    """

    key: str
    base_arguments: tuple
    sweeps: tuple[ParameterSweep, ...]
    corners: tuple[LatticePoint, ...]
    scatter: tuple[LatticePoint, ...]
    # Parameters that a declared range exists for but that do not appear
    # (by name) in `parameter_names`, or vice versa -- surfaced so a caller
    # can feed them into invariants.json's `uncovered` block (spec section
    # 3.7); this module does not decide what "uncovered" means for the
    # file, it only refuses to silently drop a parameter with no range.
    unswept_parameters: tuple[str, ...]


def build_entry_lattice(
    entry: Mapping,
    parameter_names: Sequence[str],
    ranges: Mapping[str, RangeDeclaration],
    *,
    n: int = DEFAULT_SWEEP_POINTS,
    corners: Sequence[Sequence] = (),
    scatter_seed: int | None = None,
    scatter_count: int = 0,
) -> EntryLattice:
    """The lattice for one golden.json entry.

    `entry` is one value from a golden document's `"entries"` mapping (i.e.
    `document["entries"][key]`) -- its `"arguments"` are the base point,
    held fixed while one parameter at a time is swept. `parameter_names` is
    the *positional* parameter name for each element of `entry["arguments"]`
    -- golden.json itself is positional (it mirrors the Python-visible call,
    same as the oracle's wire format, spec section 2.5), so the caller (which
    has the parsed IR) supplies the names.

    Only parameters that are (a) plain numbers in the base arguments (not
    arrays or structs -- sections 3.2-3.4 describe scalar sweeps) and (b)
    named in `ranges` are swept; per spec section 3.4, "there is no default
    range -- a swept parameter with no declared range is an error, not a
    guess". This function does not raise for a rangeless numeric parameter
    -- it simply does not sweep it, and reports it back in
    `unswept_parameters` so a caller can record it as `uncovered`.

    `corners` are full argument tuples, passed through completely
    unmodified (spec section 3.4 item 2) -- this function does not validate
    their arity or clamp them to any range.
    """
    base_arguments = tuple(entry["arguments"])
    if len(parameter_names) != len(base_arguments):
        raise ValueError(
            f"entry {entry.get('name', '?')!r} has {len(base_arguments)} "
            f"argument(s) but {len(parameter_names)} parameter name(s) were "
            "given"
        )

    sweeps: list[ParameterSweep] = []
    unswept: list[str] = []
    for position, name in enumerate(parameter_names):
        value = base_arguments[position]
        if not _is_plain_number(value):
            continue  # arrays/structs are not swept by this lattice kind
        range_declaration = ranges.get(name)
        if range_declaration is None:
            unswept.append(name)
            continue
        lo, hi = range_declaration
        swept_values = sweep(lo, hi, n)
        points = tuple(
            LatticePoint(
                arguments=_replaced(base_arguments, position, x),
                origin="sweep",
                parameter=name,
                index=index,
            )
            for index, x in enumerate(swept_values)
        )
        sweeps.append(ParameterSweep(parameter=name, lo=lo, hi=hi, points=points))

    corner_points = tuple(
        LatticePoint(arguments=tuple(corner), origin="corner") for corner in corners
    )

    scatter_points: tuple[LatticePoint, ...] = ()
    if scatter_count > 0:
        if scatter_seed is None:
            raise ValueError("scatter_count > 0 requires a scatter_seed")
        swept_names = {s.parameter for s in sweeps}
        scatter_ranges = {
            name: ranges[name] for name in parameter_names if name in swept_names
        }
        samples = scatter(scatter_seed, scatter_count, scatter_ranges)
        name_to_position = {name: i for i, name in enumerate(parameter_names)}
        built = []
        for draw in range(scatter_count):
            arguments = base_arguments
            for name, values in samples.items():
                arguments = _replaced(arguments, name_to_position[name], values[draw])
            built.append(LatticePoint(arguments=arguments, origin="scatter", index=draw))
        scatter_points = tuple(built)

    return EntryLattice(
        key=entry.get("name", ""),
        base_arguments=base_arguments,
        sweeps=tuple(sweeps),
        corners=corner_points,
        scatter=scatter_points,
        unswept_parameters=tuple(unswept),
    )


def _is_plain_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _replaced(arguments: tuple, position: int, value) -> tuple:
    return arguments[:position] + (value,) + arguments[position + 1 :]
