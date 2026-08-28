"""T5 — the oracle comparator: is the Python binding faithful to the native code?

Spec `design-verification-layers.md` sections 2.1, 2.2, 2.6, 2.10, section 4
rule 6. Read those before changing anything here.

This module wires together everything the earlier tasks built:

* `buildinfo` (T1) — extracted compile flags, the safety gate, the pinned
  environment.
* `wire` (T2) — the bitwise wire protocol shared by both sides, and
  `slots_for_entry`, the single source of truth for "which slots does this
  call produce".
* `drivers.fortran.generate_driver` (T3) — the native driver, generated from
  `golden.json`'s recorded entries, never from `plan()` (spec section 2.2).
* `driverbuild` (T4) — compiles the driver TU, links it against the
  extension's own built objects, runs it under the pinned environment.
* `golden` (T1, layer 1) — `invoke()` executes one recorded call against an
  imported package exactly the way `golden record`/`golden verify` do, which
  is what makes the driver and the Python side "the same calls by
  construction" (spec section 2.2).

`oracle_check` runs both paths in the same build, in file order, and
compares every slot **bitwise** — hex text equality, never `math.isclose`,
because spec section 2.1 is emphatic that there is no rounding difference to
allow for. `oracle_show` prints coverage and slot layout without compiling or
running anything.

**Sequence discipline (spec section 2.2 / section 4 rule 6).** Both drivers
execute the file's entries in file order. If the Python side cannot execute
an entry the driver ran, or the driver skipped an entry the Python side can
run, that is a hard failure naming the entry — never a skip — and nothing at
or after the divergence point is compared, because legacy Fortran carries
state across calls in COMMON blocks and a downstream comparison after a
divergence is measuring nothing.

**Classification (spec section 2.10).** `_classify` turns a set of mismatched
slots for one call into one of the table's six readings. The table's
left-hand "signature" column is a description of a *symptom*, not a formal
predicate, so the boundaries between "wildly different" / "~7 significant
digits" / "everything differs slightly, uniformly" are judgement calls here
— documented at the point of the threshold — rather than in the spec text
itself. A mismatch that fits none of the five specific signatures still gets
a diagnostic message, just not one of the spec's quoted readings.
"""

from __future__ import annotations

import importlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import buildinfo, driverbuild, golden
from . import wire
from .drivers.fortran import generate_driver as generate_fortran_driver
from .drivers.cpp import generate_driver as generate_cpp_driver
from .ir import FunctionDef, ModuleIR, module_from_dict
from .wire import Slot, format_value, slots_for_entry, unpack_float

SERVICES_DIR = "services"
IR_RELATIVE_PATH = Path(".nativegate") / "ir.json"
ORACLE_FILENAME = "oracle.json"
ORACLE_FORMAT_VERSION = 1

# --- spec section 2.10's table, quoted verbatim (the "reading" column) -----

READING_ARGUMENT_ORDER = "argument order or intent is wrong"
READING_TRANSPOSITION = "row/column-major transposition"
READING_NARROWING = "`float32` narrowing somewhere in the binding"
READING_INPLACE_MISSING = "in-place output is not being surfaced"
READING_SEQUENCE_DIVERGED = (
    "the two paths diverged before comparison — fix the entry, nothing "
    "downstream is meaningful"
)
READING_FLAGS_OR_LINK = (
    "the driver was not linked against the extension's objects, or flags "
    "differ — check the extracted flags first"
)


class OracleError(RuntimeError):
    """Base for every hard oracle failure. Never a skip (spec section 2.2)."""


class SequenceDivergenceError(OracleError):
    """The driver and the Python side ran a different set of entries."""


# --- results -----------------------------------------------------------


@dataclass
class OracleReport:
    """The result of one `oracle_check` run."""

    covered: int
    skipped: dict = field(default_factory=dict)  # key -> reason
    failures: list = field(default_factory=list)  # classified, human-readable
    driver_sha256: str = ""
    link_target_sha256: str | None = None
    # T6 (spec section 2.6/4 rules 7-9): populated only when an oracle.json
    # exists for this service. `historical_diff` holds bitwise mismatches
    # against the recorded file's slots — populated ONLY when
    # `historical_diff_refused` is None, because a stale-provenance file is
    # never compared numerically (no tolerance mode exists to fall back to).
    historical_diff: list = field(default_factory=list)
    historical_diff_refused: str | None = None

    @property
    def passed(self) -> bool:
        # A check covering zero entries is a failure, not a pass (spec
        # section 2.9's "N covered, M skipped" coverage-reporting rule).
        # The historical diff against a recorded oracle.json is provenance,
        # not the gate (spec section 2.6): it never affects `passed`.
        return not self.failures and self.covered > 0


@dataclass
class ShowEntry:
    key: str
    arguments: list
    slots: list  # list[str] — str(Slot), or None if the IR has no match


@dataclass
class ShowReport:
    entries: list  # list[ShowEntry]
    skipped: dict  # key -> reason


# --- the pure comparator (no I/O — this is what the unit tests exercise) ---


def _numeric(value_text: str) -> float | None:
    """Best-effort float for a wire `<value>` text.

    16 lowercase hex digits is `pack_float`'s float channel; anything else
    that parses as a plain float (an int slot's decimal text, say) is
    accepted too, so classification does not need to know the entry's type
    to reason about magnitude. Returns None for a string/bool value the
    numeric heuristics below cannot use.
    """
    if len(value_text) == 16 and re.fullmatch(r"[0-9a-f]{16}", value_text):
        try:
            return unpack_float(value_text)
        except ValueError:
            return None
    try:
        return float(value_text)
    except ValueError:
        return None


def _classify(key: str, driver: dict, python: dict) -> str | None:
    """One mismatch message for one call, or None if every slot agreed.

    `driver`/`python` are `{Slot: value_text}` for this one entry key.
    Comparison is the wire's `<value>` TEXT, i.e. bitwise for floats
    (spec section 2.1: "no rounding allowance, because there is no rounding
    difference to allow for").
    """
    driver_keys = set(driver)
    python_keys = set(python)
    common = driver_keys & python_keys
    mismatched = {s for s in common if driver[s] != python[s]}
    missing_in_python = driver_keys - python_keys
    missing_in_driver = python_keys - driver_keys

    if not mismatched and not missing_in_python and not missing_in_driver:
        return None

    # "return is right, arg: slots missing" -> in-place output not surfaced.
    return_slots = {s for s in driver_keys if s.role == "return"}
    arg_slots_missing_in_python = {s for s in missing_in_python if s.role == "arg"}
    if (
        arg_slots_missing_in_python
        and arg_slots_missing_in_python == missing_in_python
        and not missing_in_driver
        and not (mismatched & return_slots)
    ):
        return (
            f"{key}: {READING_INPLACE_MISSING} — the return slot(s) match, "
            f"but arg slot(s) {sorted(str(s) for s in arg_slots_missing_in_python)} "
            "the driver printed never appeared on the Python side"
        )

    if missing_in_python or missing_in_driver:
        # A structural mismatch that does not fit the "arg-only missing"
        # signature above — not one of spec section 2.10's five specific
        # rows, so this is diagnostic text rather than a quoted reading.
        return (
            f"{key}: driver and Python printed a different set of slots "
            f"(driver-only: {sorted(str(s) for s in missing_in_python)}, "
            f"python-only: {sorted(str(s) for s in missing_in_driver)})"
        )

    # Every slot present on both sides; classify the numeric disagreement.
    all_slots = sorted(common, key=str)
    rel_errors: dict[Slot, float] = {}
    for slot in mismatched:
        dv, pv = _numeric(driver[slot]), _numeric(python[slot])
        if dv is None or pv is None or not math.isfinite(dv) or not math.isfinite(pv):
            # Non-finite values (inf/nan on either side, e.g. from a real
            # overflow one side hit and the other didn't) can never land in
            # any of the small-relative-error buckets below — dividing by an
            # infinite `dv` or subtracting inf-from-inf produces `nan`, which
            # fails every `>=`/`<=` threshold check and would otherwise fall
            # through to the generic "no signature" message even when the
            # mismatch is obviously severe. Treat it as maximally wrong.
            rel_errors[slot] = float("inf")
            continue
        rel_errors[slot] = abs(pv - dv) / abs(dv) if dv != 0 else (0.0 if pv == 0 else float("inf"))

    all_slots_mismatched = mismatched == set(all_slots)

    if all_slots_mismatched and len(all_slots) > 1:
        driver_values = [_numeric(driver[s]) for s in all_slots]
        python_values = [_numeric(python[s]) for s in all_slots]
        if None not in driver_values and None not in python_values and sorted(
            driver_values
        ) == sorted(python_values):
            # "array slots are right but permuted": the same multiset of
            # values reappears at different slot positions.
            return (
                f"{key}: {READING_TRANSPOSITION} — every slot mismatched, "
                "but the same values reappear at different positions"
            )

    if all_slots_mismatched:
        # "every slot of one call differs, wildly" — every mismatched slot's
        # relative error is large (>= 1e-3), i.e. not a last-few-bits or
        # last-few-digits disagreement. Applies even to a single-slot call
        # (a call with one observable output is still "every slot" of it).
        if all(e >= 1e-3 for e in rel_errors.values()):
            return (
                f"{key}: {READING_ARGUMENT_ORDER} — every slot of this call "
                "differs, and not by a small amount"
            )

    # "values agree to ~7 significant digits then diverge" — a relative
    # error near float32's rounding error (up to half a float32 ULP,
    # ~6e-8, but as small as ~1e-9 depending where the true value falls
    # between two float32 representable values), the size a real(4)/float
    # truncation leaves behind. The window (1e-9, 1e-5) is this module's
    # judgement call for "~7 significant digits", not spec text.
    if rel_errors and all(1e-9 <= e <= 1e-5 for e in rel_errors.values()):
        return (
            f"{key}: {READING_NARROWING} — values agree to roughly 7 "
            f"significant digits, then diverge ({sorted(str(s) for s in mismatched)})"
        )

    # "everything differs slightly, uniformly" — every mismatched slot's
    # relative error is tiny (below the narrowing window) — the size a
    # different -O level or FMA contraction leaves behind, not a marshalling
    # bug. Uniformity is judged per call; a caller comparing many entries can
    # additionally look for the same signature recurring across all of them.
    if rel_errors and all(e < 1e-9 for e in rel_errors.values()):
        return (
            f"{key}: {READING_FLAGS_OR_LINK} — every mismatched slot differs "
            f"by a tiny, uniform amount ({sorted(str(s) for s in mismatched)})"
        )

    return (
        f"{key}: {len(mismatched)} slot(s) mismatched with no single "
        f"signature from spec section 2.10 ({sorted(str(s) for s in mismatched)})"
    )


def compare_documents(
    order: list,
    driver_skip_reasons: dict,
    driver_slots: dict,
    python_errors: dict,
    python_slots: dict,
) -> tuple[list, dict, list]:
    """The pure comparator: file order in, `(covered, skipped, failures)` out.

    `order` is `document["entries"]`'s key order (spec section 2.2: file
    order, never re-sorted). `driver_skip_reasons`/`python_errors` name
    entries each side could not execute; `driver_slots`/`python_slots` are
    `{key: {Slot: value_text}}` for entries each side DID execute.

    Sequence discipline (spec section 2.2 / section 4 rule 6): the first key
    where one side ran the call and the other could not stops comparison
    entirely — the returned `failures` holds exactly that one message, and
    `covered`/`skipped` reflect only what was compared before it.
    """
    covered: list = []
    skipped: dict = {}
    failures: list = []

    for key in order:
        driver_ran = key not in driver_skip_reasons
        python_ran = key not in python_errors
        if driver_ran != python_ran:
            if driver_ran:
                detail = (
                    "the Python side could not execute an entry the driver ran"
                    f" ({python_errors.get(key)})"
                )
            else:
                detail = (
                    "the driver did not execute an entry the Python side ran"
                    f" ({driver_skip_reasons.get(key)})"
                )
            failures.append(f"{key}: {READING_SEQUENCE_DIVERGED} — {detail}")
            return covered, skipped, failures

        if not driver_ran:
            skipped[key] = driver_skip_reasons.get(key) or python_errors.get(key) or "skipped"
            continue

        message = _classify(key, driver_slots.get(key, {}), python_slots.get(key, {}))
        if message:
            failures.append(message)
            continue
        covered.append(key)

    return covered, skipped, failures


# --- the driver side: turning parsed driver output into {Slot: text} -------


def driver_slots_by_key(document: dict, module: ModuleIR, driver_skipped: dict, stdout: str) -> dict:
    """`{key: {Slot: value_text}}` from a driver run's stdout, T2-parsed.

    Only entries with at least one slot appear in the ordering check — an
    entry with no observable output (a void function, no intent(out)/
    intent(inout) arguments) never prints a line, matching T3's own test
    convention (see tests/test_driverbuild.py).
    """
    functions_by_name = {fn.name: fn for fn in module.functions}
    expected_order: list[str] = []
    for key, entry in document.get("entries", {}).items():
        if key in driver_skipped:
            continue
        fn = functions_by_name.get(entry.get("name") or key)
        if fn is None:
            continue
        if slots_for_entry(entry, fn):
            expected_order.append(key)

    parsed = wire.parse_lines(stdout.splitlines(), expected_key_order=expected_order)
    return {key: dict(slots) for key, slots in parsed.items()}


# --- the python side: turning golden.invoke()'s live result into slots -----


def python_slot_values(slots: list, result, effects: dict) -> dict:
    """`{Slot: value_text}` for one call's LIVE Python result, T2-formatted.

    `slots` is `wire.slots_for_entry(entry, fn)` — the expected slot list,
    derived from the entry's recorded shape. `result`/`effects` are what
    `golden.invoke` actually returned for this run (not the recorded
    values) — the whole point of the oracle is to compare a fresh
    execution, not the file. Uses `wire.format_value` (which dispatches to
    `wire.pack_float` for floats), so the Python side goes through the exact
    same hex/decimal/escape rules as the driver (spec section 2.4).
    """
    values: dict = {}
    return_slots = [s for s in slots if s.role == "return"]
    if len(return_slots) == 1 and return_slots[0].element is None:
        values[return_slots[0]] = format_value(result)
    elif return_slots:
        for s in return_slots:
            values[s] = format_value(result[s.element])

    for s in slots:
        if s.role != "arg":
            continue
        arg_value = effects.get(str(s.arg_index))
        if arg_value is None:
            continue  # surfaces as "missing" in _classify, not a KeyError here
        if s.element is None:
            values[s] = format_value(arg_value)
        else:
            values[s] = format_value(arg_value[s.element])
    return values


# --- I/O: loading the service, building, running ---------------------------


def _load(service_dir: Path) -> tuple[ModuleIR, dict]:
    ir_path = service_dir / IR_RELATIVE_PATH
    if not ir_path.exists():
        raise OracleError(f"no {ir_path} — run `ngate generate <name>` first")
    module = module_from_dict(json.loads(ir_path.read_text()))

    golden_path = service_dir / golden.GOLDEN_FILENAME
    if not golden_path.exists():
        raise OracleError(
            f"no {golden_path} — run `ngate golden record <name>` first "
            "(the oracle replays golden.json's recorded entries, spec section 2.2)"
        )
    document = golden.read(golden_path)
    return module, document


def _fortran_sources(service_dir: Path) -> tuple[Path, list]:
    """The directory and filenames the extension actually compiles.

    `native/_expanded/` when non-empty (the CLI's rewritten copies — INCLUDE
    expansion, kind-parameter resolution — are what the real extension
    compiles), else `native/` directly.
    """
    expanded = service_dir / "native" / "_expanded"
    directory = expanded if expanded.is_dir() and any(expanded.iterdir()) else service_dir / "native"
    names = sorted(
        p.name
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in (".f", ".f90", ".f95", ".f03")
    )
    if not names:
        raise OracleError(f"no Fortran sources found under {directory}")
    return directory, names


def _facade_source_name(module: ModuleIR, source_names: list) -> str:
    facade = Path(module.source_file).name
    if facade in source_names:
        return facade
    raise OracleError(
        f"the IR's source file {facade!r} is not among the extension's "
        f"compiled sources {source_names}"
    )


# --- C++ (T7 integration): the same jobs `_fortran_sources`/
# `_facade_source_name` do for the Fortran path, above ---------------------


def _cpp_sources(service_dir: Path) -> tuple[Path, list]:
    """The directory and filenames of the C++ implementation sources the
    extension actually compiles (`native/`; C++ has no `_expanded` step)."""
    directory = service_dir / "native"
    names = sorted(
        p.name
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in (".cpp", ".cc", ".cxx")
    )
    if not names:
        raise OracleError(f"no C++ sources found under {directory}")
    return directory, names


def compiled_sources(service_dir: Path, language: str) -> tuple[Path, list]:
    """The sources the extension actually compiles, for either language.

    Public because `golden record` must hash exactly the same files this
    module hashes when it verifies. Recording a digest set the check then
    computes differently would report every source as "now (absent)" and
    refuse the historical diff — a provenance field is only worth having if
    both sides derive it identically, so they share one resolver.
    """
    return _cpp_sources(service_dir) if language == "cpp" else _fortran_sources(service_dir)


def _cpp_facade_source_name(module: ModuleIR, source_names: list) -> str:
    """The implementation `.cpp` the IR's header (`module.source_file`)
    pairs with — same-stem, the pairing `quickstart` itself uses
    (`calculator.hpp` <-> `calculator.cpp`). Falls back to the sole source
    when there is exactly one, so a single-file service never needs the
    stems to match by convention."""
    stem = Path(module.source_file).stem
    candidates = [n for n in source_names if Path(n).stem == stem]
    if candidates:
        return candidates[0]
    if len(source_names) == 1:
        return source_names[0]
    raise OracleError(
        f"cannot tell which of the extension's compiled sources {source_names} "
        f"implements the IR's header {module.source_file!r} — expected a "
        "same-stem .cpp, and there is more than one candidate source"
    )


def _sanitize_module_name(name: str) -> str:
    return "n2p_oracle_" + re.sub(r"\W", "_", name)


def _f2py_extension_cwd(build_dir: Path) -> Path:
    """Where `driverbuild.build_extension_with_compile_commands` actually
    leaves the built `.so` — see that function's docstring: f2py's meson
    backend "moves the finished extension to cwd" of the subprocess it ran,
    which is a sibling of `build_dir`, not `build_dir` itself."""
    return build_dir.parent / f"{build_dir.name}-f2py-cwd"


def _import_extension(module_name: str, search_dir: Path):
    sys.path.insert(0, str(search_dir))
    try:
        importlib.invalidate_caches()
        return importlib.import_module(module_name)
    finally:
        sys.path.pop(0)


def _package_namespace(module: ModuleIR, extension):
    """The object golden.invoke() calls attributes on.

    f2py nests everything under the enclosing Fortran `module X` block, if
    there is one — the same rule generate_init_py's re-export and
    test_native_oracle.py's `petro_oracle.petro_api` both rely on.
    """
    if module.fortran_module:
        return getattr(extension, module.fortran_module)
    return extension


# --- the public entry points ------------------------------------------------


def _oracle_path(service_dir: Path) -> Path:
    return service_dir / ORACLE_FILENAME


def _driver_hash_mismatch_message(recorded_sha: str, fresh_sha: str) -> str:
    return (
        "the recorded oracle.json's driver_sha256 "
        f"({recorded_sha}) does not match the freshly generated driver "
        f"({fresh_sha}): the driver generator changed since this file was "
        "recorded (design-verification-layers.md section 2.6 — 'every "
        "nativegate generator change invalidates every recorded oracle "
        "file'). Re-recording the file is part of upgrading the tool: run "
        "`ngate oracle record` again once you have reviewed the new "
        "driver output."
    )


_PROVENANCE_MATCH_FIELDS = (
    "platform",
    "fortran_compiler",
    "cxx_compiler",
    "compile_flags",
    "sources",
)


def _provenance_matches_build(recorded: dict, current: dict) -> bool:
    """spec section 2.6/4 rule 9: only platform, compiler, flags and source
    digests gate the historical (bitwise) comparison — a file pinned to a
    different build image is refused, never tolerance-compared."""
    return all(recorded.get(f) == current.get(f) for f in _PROVENANCE_MATCH_FIELDS)


def _provenance_mismatch_message(name: str, recorded: dict, current: dict) -> str:
    lines = []
    for field_name in _PROVENANCE_MATCH_FIELDS:
        was, now = recorded.get(field_name), current.get(field_name)
        if was != now:
            lines.append(f"  {field_name}: recorded {was!r}, now {now!r}")
    detail = "\n".join(lines)
    return (
        f"refusing the historical comparison against {name}'s recorded "
        "oracle.json: it is pinned to the build image that recorded it "
        "(platform, compiler, extracted flags and source digests), and "
        "this build differs:\n"
        f"{detail}\n"
        "There is no tolerance mode to fall back to (design-verification-"
        "layers.md section 2.6) — this is a usage error, not a numeric "
        "disagreement. The live oracle check above still ran and is "
        "unaffected; re-record oracle.json from this build image if you "
        "want a historical comparison here."
    )


def _historical_bit_diff(recorded_entries: dict, python_slots: dict) -> list[str]:
    """Bitwise diff of `python_slots` (this run) against `oracle.json`'s
    recorded `entries[*].slots` (spec section 2.6: "diff the recorded bits
    against the fresh run"). No tolerance — hex text equality only."""
    diffs: list[str] = []
    for key, recorded_entry in recorded_entries.items():
        recorded_slots = recorded_entry.get("slots") or {}
        fresh = python_slots.get(key)
        if fresh is None:
            continue  # not covered this run (e.g. now skipped) — nothing to diff
        fresh_by_name = {str(slot): value for slot, value in fresh.items()}
        for slot_name, recorded_value in recorded_slots.items():
            fresh_value = fresh_by_name.get(slot_name)
            if fresh_value is None:
                diffs.append(f"{key}.{slot_name}: recorded but not produced this run")
            elif fresh_value != recorded_value:
                diffs.append(
                    f"{key}.{slot_name}: recorded {recorded_value}, now {fresh_value}"
                )
        for slot_name in fresh_by_name:
            if slot_name not in recorded_slots:
                diffs.append(f"{key}.{slot_name}: produced this run but not recorded")
    return diffs


def oracle_check(
    name: str,
    *,
    service_dir: Path | None = None,
    keep_build: bool = False,
    _artifacts: dict | None = None,
) -> OracleReport:
    """Generate the driver, build+run it, run the same entries through a
    freshly-built Python extension (same build, spec section 2.6), compare
    bitwise. Never returns with `covered == 0` reported as a pass.

    When `services/<name>/oracle.json` exists (T6, spec section 2.6/2.7),
    additionally: verifies the file's `driver_sha256` against the freshly
    generated driver (hard failure, loudly, on mismatch — never silent); and,
    only when the file's recorded provenance matches this build, diffs its
    recorded bits against this run's — bitwise, no tolerance mode. A
    provenance mismatch refuses that historical comparison but never blocks
    the live check above.

    `_artifacts`, if given, is filled in with `python_slots`/`document`/
    `current_provenance` from this run — an internal seam `oracle_record`
    uses to write `oracle.json` from an already-passing check without
    re-running it, never a public API.
    """
    service_dir = Path(service_dir) if service_dir is not None else Path(SERVICES_DIR) / name
    module, document = _load(service_dir)

    oracle_path = _oracle_path(service_dir)
    recorded_document = json.loads(oracle_path.read_text()) if oracle_path.exists() else None

    is_cpp = module.language == "cpp"
    if is_cpp:
        # T7's driver — see drivers/cpp.py — needs the header(s) it #includes
        # to see the declarations it calls; the IR's own source_file is that
        # header (spec section 4: passed explicitly rather than embedding
        # whatever absolute path a parser run captured).
        driver = generate_cpp_driver(document, module, Path(module.source_file).name)
    else:
        driver = generate_fortran_driver(document, module)

    if recorded_document is not None:
        recorded_driver_sha = (recorded_document.get("provenance") or {}).get("driver_sha256")
        if recorded_driver_sha and recorded_driver_sha != driver.driver_sha256:
            raise OracleError(_driver_hash_mismatch_message(recorded_driver_sha, driver.driver_sha256))

    work_root = Path(tempfile.mkdtemp(prefix=f"n2p_oracle_{re.sub(r'[^A-Za-z0-9_]', '_', name)}_"))
    try:
        if is_cpp:
            sources_dir, source_names = compiled_sources(service_dir, module.language)
            extension_source_name = _cpp_facade_source_name(module, source_names)
            module_name = _sanitize_module_name(name)

            build_dir = work_root / "ext_build"
            compile_commands = driverbuild.build_cxx_extension_with_compile_commands(
                module,
                Path(module.source_file).name,
                [sources_dir / n for n in source_names],
                module_name,
                build_dir,
            )

            driver_result = driverbuild.build_and_run_driver(
                driver.source,
                compile_commands,
                extension_source_name,
                source_names,
                work_root / "driver",
                language="cpp",
            )

            driver_slots = driver_slots_by_key(document, module, driver.skipped, driver_result.stdout)

            # The pybind11 module symbol is `<module.name>_cpp` (see
            # generators/pybind_gen.py / cmake_gen.py) — `module_name` above
            # is `_sanitize_module_name(name)`, used only to name the CMake
            # project, not the compiled Python extension symbol.
            extension = _import_extension(
                f"{module.name}_cpp", driverbuild._cxx_extension_search_dir(build_dir)
            )
            package = _package_namespace(module, extension)
        else:
            sources_dir, source_names = compiled_sources(service_dir, module.language)
            extension_source_name = _facade_source_name(module, source_names)
            module_name = _sanitize_module_name(name)

            build_dir = work_root / "ext_build"
            compile_commands = driverbuild.build_extension_with_compile_commands(
                [sources_dir / n for n in source_names], module_name, build_dir
            )

            driver_result = driverbuild.build_and_run_driver(
                driver.source,
                compile_commands,
                extension_source_name,
                source_names,
                work_root / "driver",
            )

            driver_slots = driver_slots_by_key(document, module, driver.skipped, driver_result.stdout)

            extension = _import_extension(module_name, _f2py_extension_cwd(build_dir))
            package = _package_namespace(module, extension)

        functions_by_name = {fn.name: fn for fn in module.functions}
        python_slots: dict = {}
        python_errors: dict = {}

        # Spec section 2.8 / section 4 rule 3: the harness SETS thread
        # pinning in both processes it runs. The driver gets it via
        # driverbuild.run_driver; the in-process Python call gets it here.
        old_env = {k: os.environ.get(k) for k in buildinfo.pinned_environment()}
        os.environ.update(buildinfo.pinned_environment())
        try:
            for key, entry in document.get("entries", {}).items():
                if key in driver.skipped:
                    # The driver never attempted this entry; mark it as not
                    # run on the Python side too (rather than leaving it out
                    # of both `python_slots` and `python_errors`), so
                    # `compare_documents`'s `python_ran = key not in
                    # python_errors` check correctly reads this as a mutual
                    # skip instead of a one-sided sequence divergence.
                    python_errors[key] = driver.skipped[key]
                    continue
                fn = functions_by_name.get(entry.get("name") or key)
                if fn is None:
                    python_errors[key] = "no matching function in the IR"
                    continue
                try:
                    result, effects = golden.invoke(entry, package)
                except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                    python_errors[key] = f"{type(exc).__name__}: {exc}"
                    continue
                slots = slots_for_entry(entry, fn)
                python_slots[key] = python_slot_values(slots, result, effects)
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        order = list(document.get("entries", {}).keys())
        covered, skipped, failures = compare_documents(
            order, driver.skipped, driver_slots, python_errors, python_slots
        )

        skipped_total = dict(skipped)
        for key, reason in document.get("skipped", {}).items():
            skipped_total.setdefault(key, reason)

        if not failures and not covered:
            failures = [
                "the check covered zero entries — a check with no coverage "
                "is a failure, not a pass (design-verification-layers.md "
                "section 2.9)"
            ]

        # Same file set `golden record` hashes (see compiled_sources): the
        # digests are what make "the code did not change" checkable rather
        # than asserted, and they only compare if both sides list the same
        # sources.
        current_provenance = golden.provenance(
            sources=[sources_dir / n for n in source_names]
        )
        current_provenance["compile_flags"] = list(driver_result.extracted_flags)

        historical_diff: list = []
        historical_diff_refused: str | None = None
        if recorded_document is not None:
            recorded_provenance = recorded_document.get("provenance") or {}
            if _provenance_matches_build(recorded_provenance, current_provenance):
                historical_diff = _historical_bit_diff(
                    recorded_document.get("entries") or {}, python_slots
                )
            else:
                historical_diff_refused = _provenance_mismatch_message(
                    name, recorded_provenance, current_provenance
                )

        report = OracleReport(
            covered=len(covered),
            skipped=skipped_total,
            failures=failures,
            driver_sha256=driver.driver_sha256,
            link_target_sha256=driver_result.link_target_sha256,
            historical_diff=historical_diff,
            historical_diff_refused=historical_diff_refused,
        )
        if _artifacts is not None:
            _artifacts["python_slots"] = python_slots
            _artifacts["document"] = document
            _artifacts["current_provenance"] = current_provenance
        return report
    finally:
        if not keep_build:
            shutil.rmtree(work_root, ignore_errors=True)


def oracle_show(name: str, *, service_dir: Path | None = None) -> ShowReport:
    """Coverage and slot layout, without building, compiling, or running
    anything — reads `golden.json` and the IR only."""
    service_dir = Path(service_dir) if service_dir is not None else Path(SERVICES_DIR) / name
    module, document = _load(service_dir)
    functions_by_name = {fn.name: fn for fn in module.functions}

    entries: list = []
    for key, entry in document.get("entries", {}).items():
        fn = functions_by_name.get(entry.get("name") or key)
        slots = [str(s) for s in slots_for_entry(entry, fn)] if fn is not None else None
        entries.append(ShowEntry(key=key, arguments=entry.get("arguments") or [], slots=slots))

    skipped = dict(document.get("skipped") or {})
    return ShowReport(entries=entries, skipped=skipped)


# --- T6: `oracle record` and the optional oracle.json -----------------------


def build_oracle_document(
    name: str,
    module: ModuleIR,
    golden_document: dict,
    python_slots: dict,
    current_provenance: dict,
    driver_sha256: str,
    link_target_sha256: str | None,
) -> dict:
    """The `oracle.json` schema (spec section 2.7), from an already-passing
    `oracle_check` run's artifacts.

    `provenance` is golden's own provenance block (platform, compilers,
    `sources` — "identical to golden's", per spec section 2.7's comment)
    plus this run's `compile_flags` (extracted, per T1), `driver_sha256`
    and `link_target_sha256`. `entries` follow golden.json's own key order
    (call order carries meaning, spec section 2.2/4 rule 7) and hold only
    the arguments and the slots this run actually produced — an entry
    `oracle_check` could not cover (skipped, or a Python-side error) is
    recorded under `skipped` instead, exactly like golden does.
    """
    provenance = dict(current_provenance)
    provenance["driver_sha256"] = driver_sha256
    provenance["link_target_sha256"] = link_target_sha256

    entries: dict = {}
    skipped: dict = dict(golden_document.get("skipped") or {})
    for key, entry in golden_document.get("entries", {}).items():
        slots = python_slots.get(key)
        if slots is None:
            skipped.setdefault(key, "not covered by the oracle check that recorded this file")
            continue
        entries[key] = {
            "arguments": entry.get("arguments") or [],
            "slots": {str(slot): value for slot, value in slots.items()},
        }

    return {
        "format": ORACLE_FORMAT_VERSION,
        "service": name,
        "language": module.language,
        "provenance": provenance,
        "entries": entries,
        "skipped": dict(sorted(skipped.items())),
    }


def write_oracle_document(path: Path, document: dict) -> None:
    """Canonical serialization (spec section 2.7 / section 4 rule 7):
    `indent=2`, `sort_keys=False` (entry order is call order and carries
    meaning), LF line endings, trailing newline — matching `golden.write()`
    exactly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n")


def oracle_record(name: str, *, service_dir: Path | None = None, keep_build: bool = False) -> dict:
    """Run a full `oracle_check`, and on pass, write `services/<name>/
    oracle.json` (spec section 2.6: "optional: write oracle.json from a
    check"). Refuses to write anything if the check fails — a recorded
    file must document a passing build, never a known-broken one.
    """
    service_dir = Path(service_dir) if service_dir is not None else Path(SERVICES_DIR) / name

    artifacts: dict = {}
    report = oracle_check(name, service_dir=service_dir, keep_build=keep_build, _artifacts=artifacts)
    if not report.passed:
        raise OracleError(
            f"refusing to record oracle.json for {name!r}: the oracle check "
            f"did not pass ({len(report.failures)} failure(s), {report.covered} "
            "covered) — fix the binding first, a recorded file must document "
            "a passing build"
        )

    module, _document = _load(service_dir)
    document = build_oracle_document(
        name,
        module,
        artifacts["document"],
        artifacts["python_slots"],
        artifacts["current_provenance"],
        report.driver_sha256,
        report.link_target_sha256,
    )
    write_oracle_document(_oracle_path(service_dir), document)
    return document
