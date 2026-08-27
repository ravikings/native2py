# Task breakdown: verification layers 2 and 3

Source of truth: [`design-verification-layers.md`](design-verification-layers.md).
Each task below is written to be assigned to an agent on its own: it names the
spec sections it implements, what it must deliver, and how to know it is done.
An agent should read the named spec sections and the named source files before
writing code, and must not weaken a determinism rule (spec §4) to make a test
pass.

Conventions for every task:

- Code lives in `tools/nativegate/nativegate/` beside `golden.py`; follow its
  style (argued docstrings, stdlib-first, no new runtime dependencies without
  a stated reason).
- Tests live in the existing test layout under `tools/nativegate/`; every task
  ships tests that run without a Fortran toolchain unless the task says
  otherwise, and marks compiler-requiring tests so CI can select them.
- `services/petro_api` is the reference service; its `golden.json` is the
  fixture of record. Do not edit it.

---

## Dependency map

```
T1 (flags/env)        ─┐
T2 (wire protocol)    ─┼─→ T4 (driver build+link) ─→ T5 (oracle check CLI) ─→ T6 (oracle record/show)
T3 (fortran driver)   ─┘                                      │
T7 (c++ driver)  [after T3, T5]                               │
                                                              ▼
T8 (yaml declarations) ─→ T9 (lattice) ─→ T10 (structural invariants)
                                       └→ T11 (declared invariants + bisection)
T10, T11 ─→ T12 (invariants.json + aggregate verify)
```

Parallel-safe starting set: **T1, T2, T3, T8** (no dependencies between them).
T9 can start once T8's schema is agreed. T5 is the first integration point.

---

## T1 — Build-flag extraction and environment preconditions

**Spec:** §2.3 (flag extraction), §2.8 (hard preconditions), §4 rules 2–3.

Build a module (suggested name `buildinfo.py`) that:

1. Extracts the extension's *actual* compile flags for each native source from
   the build system's record — `compile_commands.json` where CMake produces
   it; investigate what the current `ngate build` pipeline
   (`cli.py`, `generators/`, `services/petro_api/CMakeLists.txt`) actually
   emits and use that, never a restated config value.
2. Exposes `refuse_unsafe(flags)` — raises a hard error if `-ffast-math`,
   `-Ofast` or `-funsafe-math-optimizations` appear.
3. Exposes `codegen_flags(flags)` — the subset of flags that affect code
   generation (optimization level, FP contraction, target/arch, `-std=`),
   used later by T4 to compile the driver TU identically and to detect
   driver-vs-extension divergence.
4. Exposes `pinned_environment()` — the env dict a harness must run native
   code under: `OMP_NUM_THREADS=1` plus BLAS equivalents
   (`OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`).
   The harness **sets** these; it never refuses because they are unset.
5. Hashes: SHA-256 of the extracted flag list (canonical ordering) and of a
   linked archive/object set, for provenance (spec §4 rule 8).

**Acceptance:** unit tests with fixture `compile_commands.json` files —
extraction, unsafe-flag refusal (all three flags, and clean pass-through),
codegen-flag subsetting, environment dict. No compiler needed.

---

## T2 — Bit-emission wire protocol (Python side)

**Spec:** §2.4 (bits, widening, strings), §2.5 (wire format, slot grammar).

Build a module (suggested name `wire.py`) that owns both directions of the
protocol, so the driver generators (T3, T7) and the comparator (T5) share one
implementation:

1. `pack_float(value) -> str` — `struct.pack(">d", value).hex()`, 16 lowercase
   hex digits. Includes the startup self-check: assert the known constant
   `355.0764805578969 == "4076313943ad6f27"` once per process, hard error on
   mismatch (spec §2.4 "the harness must assert this").
2. Value channel for non-floats: decimal integers, `0`/`1` booleans,
   percent-escaped strings (`%09`, `%0A`, `%25` for tab, newline, percent;
   escape nothing else) with the matching unescape.
3. Line format `<key>\t<slot>\t<value>` — emit and parse. A malformed line,
   duplicate `(key, slot)`, or key out of expected order is an error carrying
   the offending line text.
4. Slot grammar: `return`, `return[<n>]`, `arg:<i>`, `arg:<i>[<n>]` — parse
   into a typed structure. `<i>` is the Python-visible argument index (the
   `golden.json` `arguments` index space); document this in the docstring.
5. `slots_for_entry(entry, ir_function) -> list[Slot]` — given a golden.json
   entry and its IR signature, the expected slot list: `return` (or
   `return[<n>]` when f2py returns a tuple for `intent(out)` params),
   `arg:<i>[...]` for each recorded `argument_effect`. This is the single
   source of truth T3/T7 generate print statements from and T5 compares
   against.

**Read first:** `golden.py` (`invoke`, `argument_effects`), `ir.py`
(`intent`, `is_subroutine`, `length_param`), spec §2.5's index-space warning.

**Acceptance:** round-trip tests for every value type including the escape
corners (string containing tab/newline/percent/empty), the big-endian
self-check (including a test that the `<d` byte-reversal is detected), slot
parsing, and `slots_for_entry` against the real `petro_api/golden.json`
(`pvt_state` must yield `arg:1[0..8]`; `solution_gor` must yield `return`).

---

## T3 — Fortran driver generator

**Spec:** §2.2 (plan from golden.json, call order, skip discipline),
§2.4 (Fortran emission, widening), §2.5 (wire format).

Build the generator that turns `(golden.json document, ModuleIR)` into one
Fortran driver source file:

1. Iterate `document["entries"]` **in file order**; emit one call per entry
   using the entry's recorded `arguments` (materialized the way
   `golden._materialise` does — arrays from lists, etc.), mapping
   Python-visible arguments to native parameter positions (re-inserting
   `intent(out)` parameters and array-size locals the Python side never
   sees; sizes come from `length_param`, and an entry whose `intent(out)`
   array has no `length_param` is recorded as skipped with a reason).
2. Emit output statements for exactly the slots `wire.slots_for_entry`
   (T2) returns, using `transfer` into `integer(8)` and `Z16.16`,
   tab-separated. A `real(4)` value is assigned to a `real(8)` local first
   and the double is transferred — never `transfer` a `real(4)` into
   `integer(8)` (spec §2.4, undefined padding).
3. Strings: trim trailing blanks (`trim`) before emission, percent-escape per
   T2's rules (generate a small escape subroutine into the driver).
4. Determinism: byte-identical output for identical inputs — no timestamps,
   no paths, no environment in the generated source. SHA-256 of the generated
   source is the `driver_sha256`.
5. Entries golden skipped are skipped with the same reason strings; the
   driver never invents calls golden did not record.

**Read first:** `golden.py` in full, `ir.py`, an actual generated f2py
binding under `services/petro_api/bindings/` to see the real calling
conventions, and the native sources under `libraries/petro/`.

**Acceptance:** golden-file tests asserting the generated driver source for a
small synthetic IR + document (function returning `real(8)`; subroutine with
`intent(inout)` array; subroutine with `intent(out)` array + `length_param`;
`CHARACTER` output; `real(4)` return exercising the widening) is byte-stable.
Compiler-marked test: generate the `petro_api` driver, compile with gfortran,
run it, parse with T2, and assert the full slot set appears in file order.

---

## T4 — Driver build: compile one TU, link the extension's objects

**Spec:** §2.3 (one set of object code), §2.8 (flag preconditions),
§4 rules 2–3, 5, 8.

Build the step that turns a generated driver source into a runnable binary:

1. Locate the built library objects/archive the extension linked (from the
   service's build tree; investigate what `ngate build` leaves behind and
   prefer the archive/target CMake already produces). **Never recompile the
   library sources.** If only `-fPIC` objects exist, link those into the
   executable.
2. Compile only the driver TU, with T1's `codegen_flags` of the extension's
   own extracted flags; refuse to proceed if T1's `refuse_unsafe` fires or if
   the driver flags would differ from the extension's in a codegen-affecting
   way.
3. Run the driver under T1's `pinned_environment()`, capture stdout/stderr;
   non-zero exit is a hard failure whose message includes stderr.
4. Report provenance: driver source hash, linked-object hash, extracted
   flags.

**Acceptance:** compiler-marked integration test on `petro_api`: build the
extension, generate the driver (T3), build via this task, run, and verify
T2 parses every expected line. A negative test injecting `-ffast-math` into a
fixture flag record must refuse. A test must demonstrate the "no recompile"
property (e.g. assert the compile command list contains only the driver TU).

---

## T5 — `oracle check`: the gate

**Spec:** §2.1, §2.2 (identical-sequence rule), §2.6, §2.10, §4 rule 6.

Build the comparator and the CLI command:

1. `ngate oracle check <name>`: load `golden.json` + IR, generate driver
   (T3), build and run it (T4), then execute the same entries through the
   imported Python extension under the same pinned environment, collecting
   per-slot values via T2's `slots_for_entry` + `pack_float`.
2. **Sequence discipline:** if the Python side cannot execute an entry the
   driver ran (or vice versa), fail hard naming the entry and compare nothing
   at or after the divergence point — state carries downstream (spec §2.2).
3. Compare bitwise per slot, in order. Report failures using §2.10's
   classification table: detect and name the signatures (all-slots-wild,
   permuted array, ~7-significant-digit agreement suggesting `float32`
   narrowing, missing `arg:` slots, uniform slight drift → point at
   flags/link first).
4. Report coverage golden-style: `N covered, M skipped (reasons)`. Exit
   non-zero on any failure; a check that covered zero entries is a failure,
   not a pass.
5. `ngate oracle show <name>`: print, without running anything, the
   entries, their slot lists, and the skips — "coverage + what each slot is".

**Read first:** `cli.py` for command style and the step-runner, spec §2.10's
table verbatim (the message wording is part of the deliverable).

**Acceptance:** unit tests for the comparator with synthetic slot streams
covering each §2.10 signature and the sequence-divergence hard failure.
Compiler-marked end-to-end test: `oracle check petro_api` passes against the
real service, and a deliberately sabotaged binding (e.g. swap two arguments
in a copy of the generated pyf/binding) fails with the argument-order
classification.

---

## T6 — `oracle record` and the optional `oracle.json`

**Spec:** §2.6 (file is provenance, not the gate), §2.7 (schema), §4 rules
7–9. **Depends on T5.** Deliberately last in the oracle line (spec §7 step 5).

1. `ngate oracle record <name>`: run a full `check`; on pass, write
   `oracle.json` per §2.7 — golden's provenance plus `compile_flags`
   (extracted), `driver_sha256`, `link_target_sha256`; entries in golden
   order; `indent=2`, `sort_keys=False`, LF, trailing newline. Nothing
   environmental (no timestamps/paths/hostnames).
2. When an `oracle.json` exists, `check` additionally: (a) verifies
   `driver_sha256` against the freshly generated driver and fails loudly on
   mismatch with a message saying the generator changed and re-recording is
   part of upgrading the tool; (b) diffs the recorded bits — but **only**
   when the recorded provenance matches the current build (platform,
   compiler, flags, source digests). On any provenance mismatch the CLI
   refuses the historical comparison with a message explaining the file is
   pinned to its build image; the live check still runs. There is **no**
   tolerance mode.

**Acceptance:** schema golden-file test (byte-stable serialization), the
provenance-mismatch refusal, the stale-driver-hash failure, and a
compiler-marked record→check round trip on `petro_api`.

---

## T7 — C++ driver generator

**Spec:** §2.4 (C++ emission), §2.9 (scope: free functions, static methods,
instance methods with recorded ctor args; structs by value skipped).
**Depends on T3 (mirror its structure) and T5 (plugs into the same check).**

Same contract as T3 in C++: `memcpy` into `uint64_t` (never a union or
pointer cast — spec calls these out as strict-aliasing violations), `printf`
`%016llx` tab-separated, `float` widened to `double` before the memcpy,
instance methods constructed from the entry's `constructor_arguments`,
structs-by-value entries recorded as skipped. Uses T2's slot source of truth
and T4's build step (linking the C++ archive, compiling one driver TU with
extracted flags).

**Acceptance:** byte-stable generation tests for each §2.9 scope row
including the skip; compiler-marked end-to-end `oracle check` against
whichever existing C++ demo service the repo has (investigate `services/`;
if none builds today, add a minimal C++ fixture service under the test tree
rather than the services tree).

---

## T8 — `nativegate.yaml` declarations: `state:`, `invariants:`, ranges

**Spec:** §3.2 (declared vocabulary, `no_error_flag`), §3.3 (closed
vocabulary — no eval, ever), §3.4 (ranges, no default range), §3.5
(`setup`/`mutating`/`error_flag`).

Extend `config.py` to parse and validate:

```yaml
state:
  setup: [pvt_set_fluid]
  mutating: [pvt_set_fluid]
  error_flag: last_error
invariants:
  solution_gor:
    - bounds: {min: 0.0}
    - monotone: {in: pressure, direction: nondecreasing}
ranges:
  pressure: [14.7, 10000.0]
```

1. The property vocabulary is a **closed set**: `bounds`, `monotone`,
   `sum_to_one`, `symmetric_in`, `scales_linearly_in` (declared) — anything
   else is a validation error naming the vocabulary. No expression strings,
   no eval (spec §3.3 is emphatic; treat any pressure to add one as a spec
   change, not a task decision).
2. Validate references: `setup`/`mutating`/`error_flag` entries and invariant
   keys must name exposed functions; `monotone.in` must name a parameter of
   that function; `direction` ∈ {nondecreasing, nonincreasing}; `bounds`
   needs at least one of min/max; `sum_to_one` fields must exist with an
   optional `tolerance`.
3. Ranges: `[lo, hi]` with `lo < hi`, finite. **No default range** — a swept
   parameter with no range is not an error at parse time; it becomes an
   `uncovered` entry at run time (T12), so the parser must distinguish
   "absent" cleanly.
4. Typed result objects the runners (T10/T11) consume, so property semantics
   live in one place.

**Acceptance:** validation unit tests — every accept/reject rule above, plus
round-tripping the example block. Update `services/petro_api/nativegate.yaml`
with the real `state:` block and at least `solution_gor`/`oil_fvf` invariants
and a `pressure` range (values from the spec's examples).

---

## T9 — The lattice engine

**Spec:** §3.4 in full — the point formula is pinned to the bit.

A pure module (no native calls):

1. `sweep(lo, hi, n)` — exactly `x_i = lo + (i * (hi - lo)) / (n - 1)`, each
   operation in float64 in that association, with `x_0 = lo` and
   `x_{n-1} = hi` assigned exactly (not computed). Default `n = 33`.
2. Corner tuples passed through verbatim.
3. `scatter(seed, count, ranges)` — splitmix64 (implement it; ~10 lines,
   test against the reference vectors from the published algorithm), mapped
   to each range deterministically; never `random` or numpy's global state.
4. Per-entry sweep construction: hold all arguments at the `golden.json`
   recorded values, sweep one declared-range parameter at a time (spec §3.4
   item 1 — note the base point is the *golden file's* arguments, not
   `plan()` samples).

**Acceptance:** bit-exact tests — assert `struct.pack(">d", x).hex()` of
several sweep points against hard-coded hex (compute once, freeze), endpoint
exactness, splitmix64 reference vectors, and that two independent
implementations of the formula's wrong association (e.g. `lo*(1-t)+hi*t`)
would differ (i.e. the test is actually sensitive to the formula).

---

## T10 — Structural invariants runner

**Spec:** §3.2 (structural table), §3.5 (mutator scoping, fresh process,
setup replay). **Depends on T8, T9.**

1. `finite` / `total` at every lattice point, after replaying the declared
   `setup` sequence with `golden.json`'s recorded arguments.
2. `no_error_flag`: call the declared `error_flag` accessor after every
   lattice point; nonzero is a failure at that point.
3. `idempotent`: `f(x)` twice, compare **bits** (via T2's `pack_float`), for
   every entry point not in `mutating`; run in a fresh process (subprocess
   with the pinned environment) so prior calls cannot mask hidden state.
4. `order_independent`: `f(x); g(y); f(x)` ≡ `f(x); f(x)` with `g` drawn
   only from non-`mutating` routines, checked for every non-`mutating` `f`;
   fresh process per sequence. This is the property that catches
   **undeclared** mutators — a legitimate failure's fix is adding the routine
   to `mutating` in review, and the failure message must say so.
5. All native execution under T1's `pinned_environment()`.

**Acceptance:** tests against `petro_api` (wheel needed, no compiler):
`pvt_set_fluid` is exempted as declared; a synthetic fixture module with a
deliberate hidden global must fail `order_independent` with the
add-to-mutating message; `no_error_flag` flags a point where a petro routine
sets `last_error` (drive one out of range deliberately in the fixture setup).

---

## T11 — Declared invariants and bisection counterexample reporting

**Spec:** §3.2 (declared vocabulary, `monotone` = raw `>=`, no tolerance),
§3.6 (bisection rule, byte-identical message). **Depends on T8, T9.**

1. Evaluate `bounds`, `monotone`, `sum_to_one` (and stubs erroring cleanly
   for `symmetric_in`/`scales_linearly_in` if not implemented this pass —
   an unimplemented vocabulary word must be a hard error, never a silent
   pass) over T9's lattice, after T10's setup replay.
2. `monotone` compares raw float64 with `>=`/`<=` — no tolerance parameter
   exists in the code, so none can be added in config.
3. On failure: report first failing lattice point in sweep order, then bisect
   per the pinned rule — bracket = last passing/first failing lattice point,
   midpoint `(a + b) / 2` in float64, retain the side keeping the bracket
   valid, stop when the midpoint equals an endpoint or after 40 steps.
   Message format is spec §3.6's block, byte-identical across runs.
4. The word "proved" must not appear anywhere in output; the vocabulary is
   "checked at N points" (spec §6).

**Acceptance:** a fixture function with a known bounds violation must produce
the §3.6 message byte-for-byte (assert the exact string, including the
bracket values); monotone failure and pass cases; bisection termination on a
step function (bracket collapses to adjacent floats).

---

## T12 — `invariants.json`, `invariants verify`, and the aggregate gate

**Spec:** §3.7 (schema, `uncovered`), §2.6 (aggregate ordering), §5.
**Depends on T10, T11 (and T5 for the aggregate).**

1. `ngate invariants verify <name>`: run structural + declared properties,
   write/compare `invariants.json` per §3.7 — `lattice` block (including
   `scatter.seed`), `state` block, `checked` with property lists and point
   counts, `uncovered` with reasons (no declared range, unimplemented form).
   Serialization: `indent=2`, `sort_keys=False`, LF, trailing newline,
   nothing environmental.
2. An invariants run where `checked` is empty is a failure — "an invariants
   file that silently checks nothing must not look like a pass."
3. Extend `ngate verify <name>`: run `oracle check` if a toolchain is
   present (report "oracle: skipped, no toolchain" otherwise — visibly, not
   silently), then golden, then invariants if declarations exist; report each
   layer separately and name the failing layer. CI ordering documented:
   oracle → golden → invariants.
4. Update the README's verification section and
   `tools/nativegate/docs/golden-values.md` cross-references to describe the
   three layers, reusing the spec's table from §1.

**Acceptance:** schema golden-file test; empty-checked failure; aggregate
test showing per-layer reporting with a forced failure in each layer
(monkeypatched); `uncovered` populated for a function with an undeclared
range.

---

## Suggested assignment waves

| wave | tasks | note |
|---|---|---|
| 1 | T1, T2, T3, T8 | fully parallel |
| 2 | T4, T9 | T4 needs T1; T9 needs T8's schema only |
| 3 | T5, T10, T11 | T5 is the first oracle integration; T10/T11 parallel |
| 4 | T6, T7, T12 | T6 last per spec §7; T7 mirrors T3; T12 closes the loop |

Every task's PR should quote the spec section it implements in the
description, and any place an agent finds the spec ambiguous or wrong is a
finding to report back, not a decision to make silently.
