"""Loader for a service's nativegate.yaml (design.md section 9)."""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Union

import yaml

if TYPE_CHECKING:
    from .ir import ModuleIR

CONFIG_FILENAME = "nativegate.yaml"

SUPPORTED_LANGUAGES = ("cpp", "fortran")

# Closed vocabulary of declared invariant properties (design-verification-layers.md
# section 3.3). This set is exhaustive and deliberately NOT extensible via an
# eval'd expression string or any other escape hatch: "if a property does not
# fit the vocabulary, the vocabulary grows in a reviewed commit" (spec verbatim).
# Never add a generic/expression form here — that is a spec change, not a task
# decision, and the spec is emphatic that this must never happen.
INVARIANT_VOCABULARY = (
    "bounds",
    "monotone",
    "sum_to_one",
    "symmetric_in",
    "scales_linearly_in",
)

MONOTONE_DIRECTIONS = ("nondecreasing", "nonincreasing")


class ConfigError(ValueError):
    """nativegate.yaml is missing something nativegate refuses to guess."""


class ExposeWarning(UserWarning):
    """An empty `expose:` block was read as "bind everything" without an opt-in."""


@dataclass
class ExposeConfig:
    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    # Explicit opt-in to "bind the whole public surface": `expose: all` or
    # `expose: {all: true}` in nativegate.yaml. None means "not stated" — which
    # is still permissive for direct construction (the parsers rely on that,
    # falling back to `[[nativegate::expose]]` annotations), but ServiceConfig.load
    # warns about it so the intent isn't left implicit in a yaml file.
    all: bool | None = None

    def is_exposed(self, name: str) -> bool:
        if self.all:
            return True
        if not self.classes and not self.functions:
            # Nothing named: permissive unless the user explicitly said `all: false`.
            return self.all is not False
        return name in self.classes or name in self.functions

    @property
    def is_empty(self) -> bool:
        return not self.classes and not self.functions


@dataclass
class ClangConfig:
    """Compiler flags the C++ AST parser needs to read this service's headers.

    A real front end has to be told what a compiler would be told: where the
    other headers live, which macros the build defines, which standard the
    code is written against. Getting these wrong doesn't fail loudly — clang
    recovers from an unknown type by pretending it was `int` — so nativegate
    reports every parse error it sees rather than binding the wreckage.
    """

    std: str = "c++17"
    include_paths: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)
    # Names of extern "C" functions whose bare `T*` arguments are SCALARS
    # passed by reference — the Fortran-linkage convention. Opt-in per
    # function, never inferred: `double* x` is also how C passes an array
    # whose length travels through COMMON or a PARAMETER, and binding an
    # array as one scalar hands the callee a pointer it reads past. FLASH2's
    # XLIQ(NCMAX) looks exactly like PVTRS's scalar `p` in a C prototype —
    # only the person who read the Fortran knows which is which, so they say
    # so here. "*" asserts it for every extern "C" function in the service.
    scalar_ref_functions: list[str] = field(default_factory=list)


@dataclass
class ApiConfig:
    """How the generated HTTP API authenticates callers.

    Only the *mode* lives here. Keys never do: `nativegate.yaml` is committed,
    and a credential in a committed file is a credential in every clone and
    every image layer. The generated middleware reads keys from
    `NATIVEGATE_API_KEYS` at startup, and refuses to start if the mode requires
    them and none are present — failing closed, because a service that
    silently drops authentication is discovered by an attacker rather than by
    whoever deployed it.
    """

    # "none" (open — logged loudly at startup) or "api_key".
    auth: str = "none"


@dataclass
class BoundsProperty:
    """`bounds: {min: ..., max: ...}` — at least one of min/max is required."""

    min: float | None = None
    max: float | None = None


@dataclass
class MonotoneProperty:
    """`monotone: {in: <param>, direction: nondecreasing|nonincreasing}`.

    Compared with raw `>=`/`<=` at run time, no tolerance (spec §3.4) — this
    dataclass carries no tolerance field, and none may be added.
    """

    parameter: str
    direction: str


@dataclass
class SumToOneProperty:
    """`sum_to_one: [a, b, c]` with an optional sibling `tolerance:`."""

    fields: list[str] = field(default_factory=list)
    tolerance: float = 0.0


@dataclass
class SymmetricInProperty:
    """`symmetric_in: [a, b]` — the result is unchanged under permuting these
    named parameters."""

    parameters: list[str] = field(default_factory=list)


@dataclass
class ScalesLinearlyInProperty:
    """`scales_linearly_in: {in: <param>}` — the result scales linearly with
    the named parameter, all others held fixed."""

    parameter: str


# The typed union every declared property parses into. Runners (T9/T10/T11)
# switch on the concrete type rather than on a string tag, so a new vocabulary
# word cannot be half-added (a string literal with no matching dataclass).
InvariantProperty = Union[
    BoundsProperty,
    MonotoneProperty,
    SumToOneProperty,
    SymmetricInProperty,
    ScalesLinearlyInProperty,
]


@dataclass
class StateConfig:
    """`state:` — declared mutators (design-verification-layers.md §3.5).

    Structural purity checks (`idempotent`, `order_independent`) apply to
    every entry point NOT listed in `mutating`; applying them universally
    would condemn `petro_api`'s own contract (`pvt_set_fluid` mutates COMMON
    state by design). Declaring this here is what makes an *undeclared*
    mutator mechanically detectable instead of a matter of trust.

    Design decision (not literal spec text, design-verification-layers.md
    §3.5 does not say this explicitly -- flagged here rather than decided
    silently): the declared `error_flag` accessor is ALSO implicitly
    excluded from `idempotent` and from being chosen as the interposed `g`
    routine in `order_independent`, the same way `mutating` routines are.
    `error_flag` names a clear-on-read accessor (e.g. petro_api's
    `last_error`/`PVTERR`, which returns the stored error code and then
    resets it) -- calling it twice legitimately returns different bits on
    the second call, which would make `idempotent` fail not because of a
    bug but because clear-on-read is not idempotent by construction. This
    is the same "the check would condemn the API's own contract" reasoning
    already applied to `mutating` above, just for a different kind of
    intentional statefulness. See `structural_invariants.py`'s exemption
    logic for where this is enforced.
    """

    setup: list[str] = field(default_factory=list)
    mutating: list[str] = field(default_factory=list)
    error_flag: str | None = None


@dataclass
class RangeDeclaration:
    """`ranges: {pressure: [lo, hi]}` — one entry, already validated (lo <
    hi, both finite)."""

    lo: float
    hi: float

    def __iter__(self):
        return iter((self.lo, self.hi))


@dataclass
class ScatterDeclaration:
    """`lattice.scatter: {seed: ..., count: ...}` (spec §3.4 item 3).

    `seed` is required once `count` > 0 -- `lattice.build_entry_lattice`
    raises `ValueError` for a positive count with no seed, and this loader
    catches the same mistake earlier, at config-load time, with a message
    that names the file.
    """

    seed: int | None = None
    count: int = 0


@dataclass
class LatticeConfig:
    """`lattice:` — declares the YAML surface for `lattice.py`'s (T9)
    sweep point count, scatter seed/count, and per-function corners (spec
    §3.4 items 1-3).

    Design decision (not literal spec text, flagged here rather than
    decided silently): `lattice.py`'s `build_entry_lattice`/`scatter`
    already implement corners and scatter sampling and accept them as bare
    keyword arguments, but until this task there was no `nativegate.yaml`
    surface to *declare* them -- only explicit Python callers (tests) could
    populate them. This dataclass is that surface. `corners` is keyed by
    exposed function name, each value a list of full positional argument
    tuples passed through to `lattice.build_entry_lattice`'s `corners=`
    verbatim (see that function's docstring: "passed through completely
    unmodified... this function does not validate their arity or clamp them
    to any range") -- this loader does not validate arity either, only that
    the function name is exposed.
    """

    n: int | None = None
    scatter: ScatterDeclaration = field(default_factory=ScatterDeclaration)
    corners: dict[str, list[tuple]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return self.n is None and self.scatter.count == 0 and self.scatter.seed is None and not self.corners


@dataclass
class VerificationConfig:
    """Parsed `state:` / `invariants:` / `ranges:` / `lattice:` blocks
    (design-verification-layers.md §3.2-3.5).

    This is the single place declared invariant semantics live; T9/T10/T11's
    lattice and runners consume these typed objects rather than re-parsing
    YAML themselves.
    """

    state: StateConfig = field(default_factory=StateConfig)
    invariants: dict[str, list[InvariantProperty]] = field(default_factory=dict)
    ranges: dict[str, RangeDeclaration] = field(default_factory=dict)
    lattice: LatticeConfig = field(default_factory=LatticeConfig)

    @property
    def is_empty(self) -> bool:
        return (
            not self.state.setup
            and not self.state.mutating
            and self.state.error_flag is None
            and not self.invariants
            and not self.ranges
            and self.lattice.is_empty
        )

    def has_range(self, parameter: str) -> bool:
        """False for a swept parameter with NO declared range.

        Spec §3.4: "there is no default range — a swept parameter with no
        declared range is an error, not a guess, recorded under `uncovered`."
        That `uncovered` bookkeeping is T12's job; this method exists so a
        later runner can distinguish "absent" cleanly, which is this task's
        job (parse-time, `ranges:` must not silently default to anything).
        """
        return parameter in self.ranges

    def validate_against_ir(self, module: "ModuleIR") -> None:
        """Second-phase validation once a parsed signature is available.

        `ServiceConfig.load` validates everything checkable from the YAML
        and the `expose:` block alone (closed vocabulary, enum values,
        required sub-keys, functions named in `expose:`). Whether
        `monotone.in` actually names a *parameter* of that function needs the
        parsed signature (`ModuleIR`), which `ServiceConfig.load` does not
        build (it would give every config load a parser dependency it
        doesn't otherwise have). Callers that have already parsed the
        service's IR (the CLI, T9/T10/T11 runners, or a test that parses the
        real source with `parsers.fortran`) call this explicitly.
        """
        functions_by_name = {fn.name: fn for fn in module.functions}
        for fn_name, properties in self.invariants.items():
            fn = functions_by_name.get(fn_name)
            if fn is None:
                continue  # not this module; another IR may cover it
            param_names = {p.name for p in fn.parameters}
            for prop in properties:
                if isinstance(prop, MonotoneProperty) and prop.parameter not in param_names:
                    raise ConfigError(
                        f"invariants.{fn_name}: monotone.in '{prop.parameter}' is not "
                        f"a parameter of '{fn_name}' (parameters: "
                        f"{', '.join(sorted(param_names)) or '(none)'})."
                    )
                if isinstance(prop, ScalesLinearlyInProperty) and prop.parameter not in param_names:
                    raise ConfigError(
                        f"invariants.{fn_name}: scales_linearly_in.in "
                        f"'{prop.parameter}' is not a parameter of '{fn_name}' "
                        f"(parameters: {', '.join(sorted(param_names)) or '(none)'})."
                    )
                if isinstance(prop, SymmetricInProperty):
                    unknown = [p for p in prop.parameters if p not in param_names]
                    if unknown:
                        raise ConfigError(
                            f"invariants.{fn_name}: symmetric_in names "
                            f"{unknown!r}, not parameters of '{fn_name}' "
                            f"(parameters: {', '.join(sorted(param_names)) or '(none)'})."
                        )


def _load_verification(
    data, config_path: Path, known_functions: set[str] | None
) -> VerificationConfig:
    """Read `state:`, `invariants:`, `ranges:` — all three optional blocks.

    `known_functions` is the service's `expose.functions` list: everything
    `state:`/`invariants:` may name. Parameter-level checks (`monotone.in`)
    are NOT done here — see `VerificationConfig.validate_against_ir`.
    """
    state = _load_state(data.get("state"), config_path, known_functions)
    invariants = _load_invariants(data.get("invariants"), config_path, known_functions)
    ranges = _load_ranges(data.get("ranges"), config_path)
    lattice_config = _load_lattice(data.get("lattice"), config_path, known_functions)
    return VerificationConfig(
        state=state, invariants=invariants, ranges=ranges, lattice=lattice_config
    )


def _load_state(
    state_data, config_path: Path, known_functions: set[str] | None
) -> StateConfig:
    state_data = state_data or {}
    if not isinstance(state_data, dict):
        raise ConfigError(
            f"{config_path}: `state:` must be a block, got {type(state_data).__name__}."
        )
    _reject_unknown_keys(state_data, {"setup", "mutating", "error_flag"}, "state:", config_path)

    setup = _require_known_functions(
        list(state_data.get("setup") or []), "state.setup", config_path, known_functions
    )
    mutating = _require_known_functions(
        list(state_data.get("mutating") or []), "state.mutating", config_path, known_functions
    )
    error_flag = state_data.get("error_flag")
    if error_flag is not None:
        error_flag = str(error_flag)
        _require_known_functions([error_flag], "state.error_flag", config_path, known_functions)
    return StateConfig(setup=setup, mutating=mutating, error_flag=error_flag)


def _reject_unknown_keys(data: dict, allowed: set[str], where: str, config_path: Path) -> None:
    """Closed-key check shared by every `{where}:` block in this module.

    Every declared block (`state:`, `bounds`, `monotone`,
    `scales_linearly_in`, `lattice:`, `lattice.scatter:`, ...) rejects any
    key outside its own fixed set the same way -- factored here so the
    wording only needs to be right, and only needs updating, in one place.
    """
    unknown_keys = set(data) - allowed
    if unknown_keys:
        raise ConfigError(
            f"{config_path}: `{where}` has unrecognised key(s) "
            f"{sorted(unknown_keys)}. Only {sorted(allowed)} are understood."
        )


def _require_known_functions(
    names: list[str], where: str, config_path: Path, known_functions: set[str] | None
) -> list[str]:
    if known_functions is None:
        return list(names)
    for name in names:
        if name not in known_functions:
            raise ConfigError(
                f"{config_path}: `{where}` names '{name}', which is not in "
                f"`expose.functions:`. Only exposed functions may be "
                "declared here."
            )
    return list(names)


def _load_invariants(
    invariants_data, config_path: Path, known_functions: set[str] | None
) -> dict[str, list[InvariantProperty]]:
    invariants_data = invariants_data or {}
    if not isinstance(invariants_data, dict):
        raise ConfigError(
            f"{config_path}: `invariants:` must be a mapping of function name "
            f"to a list of properties, got {type(invariants_data).__name__}."
        )

    result: dict[str, list[InvariantProperty]] = {}
    for fn_name, entries in invariants_data.items():
        if known_functions is not None and fn_name not in known_functions:
            raise ConfigError(
                f"{config_path}: `invariants.{fn_name}` names a function not "
                "in `expose.functions:`. Only exposed functions may carry "
                "declared invariants."
            )
        if not isinstance(entries, list) or not entries:
            raise ConfigError(
                f"{config_path}: `invariants.{fn_name}` must be a non-empty "
                f"list of property declarations, got {entries!r}."
            )
        properties: list[InvariantProperty] = []
        for entry in entries:
            properties.append(
                _load_property(entry, fn_name, config_path)
            )
        result[fn_name] = properties
    return result


def _load_property(entry, fn_name: str, config_path: Path) -> InvariantProperty:
    """Parse one `- word: {...}` entry against the CLOSED vocabulary.

    No branch here ever evaluates a string as code, and none may be added:
    design-verification-layers.md §3.3 is emphatic that the vocabulary is
    fixed and any pressure to add an eval-based escape hatch is a spec
    change, not a task decision.
    """
    if not isinstance(entry, dict) or not entry:
        raise ConfigError(
            f"{config_path}: invariants.{fn_name} entry {entry!r} must be a "
            "mapping naming exactly one property "
            f"({', '.join(INVARIANT_VOCABULARY)})."
        )
    vocabulary_keys = [k for k in entry if k in INVARIANT_VOCABULARY]
    if len(vocabulary_keys) != 1:
        raise ConfigError(
            f"{config_path}: invariants.{fn_name} entry {entry!r} must name "
            f"exactly one property from the closed vocabulary "
            f"({', '.join(INVARIANT_VOCABULARY)}). No expression/eval form "
            "exists; if this genuinely does not fit, that is a spec change, "
            "not something to work around here."
        )
    word = vocabulary_keys[0]
    payload = entry[word]
    # `sum_to_one` is the one form with a sibling key in the same mapping
    # entry (spec §3.2's example: `- sum_to_one: [sw, so, sg]` /
    # `  tolerance: 1e-12`). Every other form must be the entry's only key.
    extra_keys = set(entry) - {word} - ({"tolerance"} if word == "sum_to_one" else set())
    if extra_keys:
        raise ConfigError(
            f"{config_path}: invariants.{fn_name} entry {entry!r} has "
            f"unexpected key(s) {sorted(extra_keys)} alongside '{word}'."
        )
    where = f"invariants.{fn_name}.{word}"

    if word == "bounds":
        return _load_bounds(payload, where, config_path)
    if word == "monotone":
        return _load_monotone(payload, where, config_path)
    if word == "sum_to_one":
        return _load_sum_to_one(payload, entry, where, config_path)
    if word == "symmetric_in":
        return _load_symmetric_in(payload, where, config_path)
    if word == "scales_linearly_in":
        return _load_scales_linearly_in(payload, where, config_path)
    raise AssertionError(f"unreachable: {word!r} passed the vocabulary check")


def _load_bounds(payload, where: str, config_path: Path) -> BoundsProperty:
    if not isinstance(payload, dict):
        raise ConfigError(f"{config_path}: `{where}` must be a mapping, got {payload!r}.")
    minimum = payload.get("min")
    maximum = payload.get("max")
    if minimum is None and maximum is None:
        raise ConfigError(
            f"{config_path}: `{where}` needs at least one of `min`/`max`."
        )
    _reject_unknown_keys(payload, {"min", "max"}, where, config_path)
    return BoundsProperty(
        min=float(minimum) if minimum is not None else None,
        max=float(maximum) if maximum is not None else None,
    )


def _load_monotone(payload, where: str, config_path: Path) -> MonotoneProperty:
    if not isinstance(payload, dict):
        raise ConfigError(f"{config_path}: `{where}` must be a mapping, got {payload!r}.")
    _reject_unknown_keys(payload, {"in", "direction"}, where, config_path)
    parameter = payload.get("in")
    direction = payload.get("direction")
    if not parameter or not isinstance(parameter, str):
        raise ConfigError(f"{config_path}: `{where}.in` must name a parameter (a string).")
    if direction not in MONOTONE_DIRECTIONS:
        raise ConfigError(
            f"{config_path}: `{where}.direction` must be one of "
            f"{MONOTONE_DIRECTIONS}, got {direction!r}."
        )
    return MonotoneProperty(parameter=parameter, direction=direction)


def _load_sum_to_one(payload, raw_entry: dict, where: str, config_path: Path) -> SumToOneProperty:
    # `sum_to_one:` takes a bare list; an optional sibling `tolerance:` key
    # sits next to it in the same mapping entry (per the spec's example:
    # `- sum_to_one: [sw, so, sg]` / `  tolerance: 1e-12`), not nested inside
    # the payload, so it is read from raw_entry rather than payload.
    if not isinstance(payload, list) or not payload:
        raise ConfigError(f"{config_path}: `{where}` must be a non-empty list of field names.")
    fields = [str(f) for f in payload]
    if len(set(fields)) != len(fields):
        raise ConfigError(f"{config_path}: `{where}` lists a field more than once: {fields!r}.")
    tolerance_raw = raw_entry.get("tolerance", 0.0)
    try:
        tolerance = float(tolerance_raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{config_path}: `{where}` tolerance must be numeric, got {tolerance_raw!r}.")
    if tolerance < 0 or not math.isfinite(tolerance):
        raise ConfigError(f"{config_path}: `{where}` tolerance must be finite and >= 0.")
    return SumToOneProperty(fields=fields, tolerance=tolerance)


def _load_symmetric_in(payload, where: str, config_path: Path) -> SymmetricInProperty:
    if not isinstance(payload, list) or len(payload) < 2:
        raise ConfigError(
            f"{config_path}: `{where}` must be a list of at least two parameter names."
        )
    parameters = [str(p) for p in payload]
    return SymmetricInProperty(parameters=parameters)


def _load_scales_linearly_in(payload, where: str, config_path: Path) -> ScalesLinearlyInProperty:
    if isinstance(payload, str):
        parameter = payload
    elif isinstance(payload, dict):
        _reject_unknown_keys(payload, {"in"}, where, config_path)
        parameter = payload.get("in")
    else:
        raise ConfigError(
            f"{config_path}: `{where}` must be a parameter name or `{{in: <param>}}`, "
            f"got {payload!r}."
        )
    if not parameter or not isinstance(parameter, str):
        raise ConfigError(f"{config_path}: `{where}.in` must name a parameter (a string).")
    return ScalesLinearlyInProperty(parameter=parameter)


def _load_ranges(ranges_data, config_path: Path) -> dict[str, RangeDeclaration]:
    ranges_data = ranges_data or {}
    if not isinstance(ranges_data, dict):
        raise ConfigError(
            f"{config_path}: `ranges:` must be a mapping of parameter name to "
            f"[lo, hi], got {type(ranges_data).__name__}."
        )
    result: dict[str, RangeDeclaration] = {}
    for parameter, bounds in ranges_data.items():
        if (
            not isinstance(bounds, (list, tuple))
            or len(bounds) != 2
        ):
            raise ConfigError(
                f"{config_path}: `ranges.{parameter}` must be `[lo, hi]`, got {bounds!r}."
            )
        try:
            lo, hi = float(bounds[0]), float(bounds[1])
        except (TypeError, ValueError):
            raise ConfigError(f"{config_path}: `ranges.{parameter}` values must be numeric, got {bounds!r}.")
        if not math.isfinite(lo) or not math.isfinite(hi):
            raise ConfigError(f"{config_path}: `ranges.{parameter}` must be finite, got {bounds!r}.")
        if not lo < hi:
            raise ConfigError(
                f"{config_path}: `ranges.{parameter}` must have lo < hi, got [{lo}, {hi}]."
            )
        result[str(parameter)] = RangeDeclaration(lo=lo, hi=hi)
    return result


def _load_lattice(
    lattice_data, config_path: Path, known_functions: set[str] | None
) -> LatticeConfig:
    """Read the `lattice:` block: `n`, `scatter: {seed, count}`, `corners:`.

    See `LatticeConfig`'s docstring for why this exists. Matches the rest of
    this module's style: closed set of recognised keys, explicit errors
    naming what's wrong, functions referenced in `corners:` must be exposed
    (mirroring `_require_known_functions`'s treatment of `state:`/
    `invariants:`).
    """
    lattice_data = lattice_data or {}
    if not isinstance(lattice_data, dict):
        raise ConfigError(
            f"{config_path}: `lattice:` must be a block, got {type(lattice_data).__name__}."
        )
    _reject_unknown_keys(lattice_data, {"n", "scatter", "corners"}, "lattice:", config_path)

    n = None
    if "n" in lattice_data and lattice_data["n"] is not None:
        n_raw = lattice_data["n"]
        if isinstance(n_raw, bool) or not isinstance(n_raw, int):
            raise ConfigError(f"{config_path}: `lattice.n` must be an integer, got {n_raw!r}.")
        if n_raw < 2:
            raise ConfigError(
                f"{config_path}: `lattice.n` must be >= 2 (need both endpoints), got {n_raw!r}."
            )
        n = n_raw

    scatter = _load_scatter(lattice_data.get("scatter"), config_path)
    corners = _load_corners(lattice_data.get("corners"), config_path, known_functions)

    return LatticeConfig(n=n, scatter=scatter, corners=corners)


def _load_scatter(scatter_data, config_path: Path) -> ScatterDeclaration:
    scatter_data = scatter_data or {}
    if not isinstance(scatter_data, dict):
        raise ConfigError(
            f"{config_path}: `lattice.scatter:` must be a block, got "
            f"{type(scatter_data).__name__}."
        )
    _reject_unknown_keys(scatter_data, {"seed", "count"}, "lattice.scatter:", config_path)

    seed_raw = scatter_data.get("seed")
    seed = None
    if seed_raw is not None:
        if isinstance(seed_raw, bool) or not isinstance(seed_raw, int):
            raise ConfigError(
                f"{config_path}: `lattice.scatter.seed` must be an integer, got {seed_raw!r}."
            )
        seed = seed_raw

    count_raw = scatter_data.get("count", 0)
    if isinstance(count_raw, bool) or not isinstance(count_raw, int):
        raise ConfigError(
            f"{config_path}: `lattice.scatter.count` must be an integer, got {count_raw!r}."
        )
    if count_raw < 0:
        raise ConfigError(
            f"{config_path}: `lattice.scatter.count` must be >= 0, got {count_raw!r}."
        )
    if count_raw > 0 and seed is None:
        raise ConfigError(
            f"{config_path}: `lattice.scatter.count` is {count_raw!r} but "
            "`lattice.scatter.seed` is missing. A positive count needs a "
            "seed to be reproducible -- there is no default."
        )
    return ScatterDeclaration(seed=seed, count=count_raw)


def _load_corners(
    corners_data, config_path: Path, known_functions: set[str] | None
) -> dict[str, list[tuple]]:
    corners_data = corners_data or {}
    if not isinstance(corners_data, dict):
        raise ConfigError(
            f"{config_path}: `lattice.corners:` must be a mapping of function "
            f"name to a list of argument tuples, got {type(corners_data).__name__}."
        )
    result: dict[str, list[tuple]] = {}
    for fn_name, entries in corners_data.items():
        _require_known_functions(
            [fn_name], "lattice.corners", config_path, known_functions
        )
        if not isinstance(entries, list) or not entries:
            raise ConfigError(
                f"{config_path}: `lattice.corners.{fn_name}` must be a "
                f"non-empty list of argument tuples, got {entries!r}."
            )
        tuples: list[tuple] = []
        for entry in entries:
            if not isinstance(entry, (list, tuple)):
                raise ConfigError(
                    f"{config_path}: `lattice.corners.{fn_name}` entry {entry!r} "
                    "must be a list of positional argument values (one full "
                    "call's worth), passed through verbatim."
                )
            tuples.append(tuple(entry))
        result[str(fn_name)] = tuples
    return result


def _dump_property(prop: InvariantProperty) -> dict:
    """Serialize one typed property back to its `nativegate.yaml` shape.

    Inverse of `_load_property`, used by `ServiceConfig.save` so a
    load/validate/save round trip is lossless.
    """
    if isinstance(prop, BoundsProperty):
        payload = {}
        if prop.min is not None:
            payload["min"] = prop.min
        if prop.max is not None:
            payload["max"] = prop.max
        return {"bounds": payload}
    if isinstance(prop, MonotoneProperty):
        return {"monotone": {"in": prop.parameter, "direction": prop.direction}}
    if isinstance(prop, SumToOneProperty):
        entry = {"sum_to_one": list(prop.fields)}
        if prop.tolerance:
            entry["tolerance"] = prop.tolerance
        return entry
    if isinstance(prop, SymmetricInProperty):
        return {"symmetric_in": list(prop.parameters)}
    if isinstance(prop, ScalesLinearlyInProperty):
        return {"scales_linearly_in": {"in": prop.parameter}}
    raise AssertionError(f"unreachable: no dump form for {prop!r}")


def _dump_verification(verification: VerificationConfig) -> dict:
    data: dict = {}
    state = verification.state
    if state.setup or state.mutating or state.error_flag is not None:
        state_block = {}
        if state.setup:
            state_block["setup"] = list(state.setup)
        if state.mutating:
            state_block["mutating"] = list(state.mutating)
        if state.error_flag is not None:
            state_block["error_flag"] = state.error_flag
        data["state"] = state_block
    if verification.invariants:
        data["invariants"] = {
            fn_name: [_dump_property(p) for p in properties]
            for fn_name, properties in verification.invariants.items()
        }
    if verification.ranges:
        data["ranges"] = {
            name: [rng.lo, rng.hi] for name, rng in verification.ranges.items()
        }
    lattice_config = verification.lattice
    if not lattice_config.is_empty:
        lattice_block: dict = {}
        if lattice_config.n is not None:
            lattice_block["n"] = lattice_config.n
        if lattice_config.scatter.count or lattice_config.scatter.seed is not None:
            lattice_block["scatter"] = {
                "seed": lattice_config.scatter.seed,
                "count": lattice_config.scatter.count,
            }
        if lattice_config.corners:
            lattice_block["corners"] = {
                fn_name: [list(corner) for corner in corners]
                for fn_name, corners in lattice_config.corners.items()
            }
        data["lattice"] = lattice_block
    return data


@dataclass
class ServiceConfig:
    name: str
    language: str
    expose: ExposeConfig = field(default_factory=ExposeConfig)
    # "auto" (Clang AST when libclang is importable, else the regex reader),
    # "clang" (require the AST parser), or "regex" (force the fallback).
    parser: str = "auto"
    clang: ClangConfig = field(default_factory=ClangConfig)
    # Shared native libraries under libraries/ that this service links
    # against (design.md section 4). Each entry is a directory name, e.g.
    # "common-cpp" -> libraries/common-cpp/ with its own CMakeLists.txt.
    libraries: list[str] = field(default_factory=list)
    # Directories searched for Fortran INCLUDE files (.INC), relative to the
    # repo root. Legacy F77 keeps COMMON blocks and IMPLICIT statements there.
    include_paths: list[str] = field(default_factory=list)
    # Authentication for the generated HTTP API. See ApiConfig.
    api: ApiConfig = field(default_factory=ApiConfig)
    # -D flags for preprocessed Fortran (.F90/.F, or #ifdef in lowercase
    # files), applied when nativegate runs gfortran's preprocessor to produce
    # the `_expanded` copy. Same reason clang.defines exists for C++: the
    # branches differ, and only the build knows which one is live.
    fortran_defines: list[str] = field(default_factory=list)
    # `state:`/`invariants:`/`ranges:` — layer 3 declarations
    # (design-verification-layers.md §3.2-3.5). Optional: a service with no
    # invariants.json coverage simply has an empty VerificationConfig.
    verification: VerificationConfig = field(default_factory=VerificationConfig)

    @classmethod
    def load(cls, service_dir: Path) -> "ServiceConfig":
        config_path = service_dir / CONFIG_FILENAME
        if not config_path.exists():
            raise FileNotFoundError(
                f"No {CONFIG_FILENAME} found in {service_dir}. "
                "Run `ngate create-service` first."
            )
        data = yaml.safe_load(config_path.read_text()) or {}
        expose = _load_expose(data.get("expose"), config_path)
        clang_data = data.get("clang") or {}
        # `None` means "no restriction": `expose: all` or an empty `expose:`
        # block (the historical permissive default) both bind whatever the
        # parser finds, so there is no fixed name list to check `state:`/
        # `invariants:` references against. An explicit `expose.functions:`
        # list IS that fixed list, and is enforced.
        known_functions = None if (expose.all or expose.is_empty) else set(expose.functions)
        return cls(
            name=data.get("name", service_dir.name),
            language=_load_language(data.get("language"), service_dir, config_path),
            expose=expose,
            parser=str(data.get("parser") or "auto"),
            clang=ClangConfig(
                std=str(clang_data.get("std") or "c++17"),
                include_paths=list(clang_data.get("include_paths") or []),
                defines=list(clang_data.get("defines") or []),
                extra_args=list(clang_data.get("extra_args") or []),
                scalar_ref_functions=list(clang_data.get("scalar_ref_functions") or []),
            ),
            libraries=list(data.get("libraries") or []),
            include_paths=list(data.get("include_paths") or []),
            api=_load_api(data.get("api"), config_path),
            fortran_defines=list((data.get("fortran") or {}).get("defines") or []),
            verification=_load_verification(data, config_path, known_functions),
        )

    def save(self, service_dir: Path) -> None:
        data = {
            "name": self.name,
            "language": self.language,
            "expose": {
                "classes": self.expose.classes,
                "functions": self.expose.functions,
            },
        }
        if self.expose.all is not None:
            data["expose"]["all"] = self.expose.all
        if self.parser != "auto":
            data["parser"] = self.parser
        clang = {
            key: value
            for key, value in {
                "std": self.clang.std if self.clang.std != "c++17" else None,
                "include_paths": self.clang.include_paths,
                "defines": self.clang.defines,
                "extra_args": self.clang.extra_args,
            }.items()
            if value
        }
        if clang:
            data["clang"] = clang
        if self.libraries:
            data["libraries"] = self.libraries
        if self.include_paths:
            data["include_paths"] = self.include_paths
        # Only written when it differs from the default, so an existing
        # nativegate.yaml does not grow a key that says nothing.
        if self.api.auth != "none":
            data["api"] = {"auth": self.api.auth}
        verification = _dump_verification(self.verification)
        if verification:
            data.update(verification)
        (service_dir / CONFIG_FILENAME).write_text(yaml.dump(data, sort_keys=False))


def _load_api(api_data, config_path: Path) -> ApiConfig:
    """Read the `api:` block.

    An unrecognised auth mode is an error, not a fallback to `none`. Quietly
    treating `api: {auth: apikey}` (a plausible typo) as "no authentication"
    would produce exactly the silently-open service this setting exists to
    prevent.
    """
    api_data = api_data or {}
    if not isinstance(api_data, dict):
        raise ConfigError(
            f"{config_path}: `api:` must be a block, got {type(api_data).__name__}."
        )
    auth = str(api_data.get("auth") or "none").strip().lower()
    if auth not in ("none", "api_key"):
        raise ConfigError(
            f"{config_path}: `api.auth: {auth}` is not understood. Use "
            "`none` or `api_key`. Refusing to default to `none`, which would "
            "leave the service open on a typo."
        )
    return ApiConfig(auth=auth)


def _load_expose(expose_data, config_path: Path) -> ExposeConfig:
    """Read an `expose:` block, requiring an explicit opt-in for "bind everything".

    `expose: all` (or `expose: {all: true}`) states the intent. An empty block
    keeps the historical permissive behaviour — the C++ parsers depend on it,
    and on `[[nativegate::expose]]` annotations in the source — but says so out
    loud rather than binding a whole header on an unstated default.
    """
    if isinstance(expose_data, str):
        if expose_data.strip().lower() != "all":
            raise ConfigError(
                f"{config_path}: `expose: {expose_data}` is not understood. Use "
                "`expose: all`, or a block with `classes:`/`functions:` lists."
            )
        return ExposeConfig(all=True)

    # Checked before the `or {}` below, which cannot tell `False` from an
    # omitted key — `False or {}` is `{}`. That coercion turned an explicit
    # `expose: false` ("bind nothing") into "not stated", which then took the
    # permissive branch and bound the entire native API: the exact opposite of
    # what was written, with only a warning suggesting the user say `expose:
    # all`.
    if isinstance(expose_data, bool):
        return ExposeConfig(all=expose_data)

    expose_data = expose_data or {}
    if not isinstance(expose_data, dict):
        raise ConfigError(
            f"{config_path}: `expose:` must be a mapping or the word `all`, "
            f"not {type(expose_data).__name__}."
        )

    all_value = expose_data.get("all")
    if all_value is not None and not isinstance(all_value, bool):
        raise ConfigError(
            f"{config_path}: `expose.all` must be true or false, got {all_value!r}."
        )

    expose = ExposeConfig(
        classes=list(expose_data.get("classes") or []),
        functions=list(expose_data.get("functions") or []),
        all=all_value,
    )
    if expose.is_empty and expose.all is None:
        warnings.warn(
            f"{config_path}: `expose:` is empty, so every symbol nativegate finds "
            "will be bound unless the source carries [[nativegate::expose]] "
            "annotations. Say so explicitly with `expose: all`, or list the "
            "classes/functions you want under `expose.classes:` / "
            "`expose.functions:`.",
            ExposeWarning,
            stacklevel=3,
        )
    return expose


def _load_language(value, service_dir: Path, config_path: Path) -> str:
    """Require `language:`, or infer it from the sources — never default to cpp."""
    from .discovery import detect_language  # local: discovery has no config deps

    def discovered() -> set[str]:
        # Only native/ — bindings/generated/ holds generated .cpp for every
        # service, Fortran ones included, and would poison the inference.
        native_dir = service_dir / "native"
        if not native_dir.is_dir():
            return set()
        return {
            lang
            for path in native_dir.rglob("*")
            if path.is_file() and (lang := detect_language(path)) is not None
        }

    if value is None:
        found = discovered()
        if len(found) == 1:
            return found.pop()
        if not found:
            raise ConfigError(
                f"{config_path}: `language:` is missing and no native sources were "
                f"found under {service_dir} to infer it from. Set `language:` to one "
                f"of {', '.join(SUPPORTED_LANGUAGES)}."
            )
        raise ConfigError(
            f"{config_path}: `language:` is missing and the sources under "
            f"{service_dir} are mixed ({', '.join(sorted(found))}). Set `language:` "
            "explicitly."
        )

    language = str(value).strip().lower()
    if language not in SUPPORTED_LANGUAGES:
        raise ConfigError(
            f"{config_path}: unsupported `language: {value}`. Supported languages "
            f"are {', '.join(SUPPORTED_LANGUAGES)}."
        )

    found = discovered()
    if found and language not in found:
        raise ConfigError(
            f"{config_path}: `language: {language}` conflicts with the sources "
            f"under {service_dir}, which are {', '.join(sorted(found))}. Fix "
            "`language:` or remove the sources that do not belong."
        )
    return language
