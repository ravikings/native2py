# Specification: verification layers 2 and 3

**Status:** implemented. All tasks in `docs/verification-layers-tasks.md` (T1-T12) are complete; see that file for the module-by-module breakdown.
**Revision note:** this version folds in a pressure-test of the first draft.
The load-bearing changes: the oracle driver is generated from `golden.json`'s
recorded entries rather than from `plan()` (§2.2); the driver links against
the extension's own built objects rather than recompiling the sources (§2.3);
the primary oracle mode is `oracle check` — binding against driver in the same
build, with no committed bit file (§2.6); and the structural invariants take a
declared list of state-mutating routines so they do not condemn the API's own
contract (§3.5).
**Companion documents:** [`design.md`](design.md) (the base specification),
[`tools/nativegate/nativegate/golden.py`](tools/nativegate/nativegate/golden.py)
(layer 1, implemented),
[`tools/nativegate/docs/golden-values.md`](tools/nativegate/docs/golden-values.md).

This specifies two gate-keeping artifacts to sit beside `golden.json`:
the **oracle** (fidelity to the legacy binary) and `invariants.json`
(properties over a swept domain). Both are deterministic, both are diffed in
review where a file exists, and neither uses a random number.

---

## 1. What layer 1 proves, and what it does not

`golden.json` answers exactly one question: *did the answer change since I
last recorded it?* That is worth having, and it is not the acceptance
criterion anyone actually cares about. Three limits are structural, not
unfinished work:

**Its guarantee is conditional on the first recording being right.**
`golden record` calls the binding, writes down what came back, and from then
on defends that number. If the binding transposed an array, picked the wrong
overload, narrowed a `real(8)` to `float32`, dropped a unit conversion or got
an `intent` backwards, the first recording captures the wrong answer and every
subsequent `verify` certifies it. Nothing in the repository has ever compared
a binding against the legacy program it was generated from — the binding bugs
in [`libraries/petro/FINDINGS.md`](libraries/petro/FINDINGS.md) were found by
hand, by a person who knew what Rs should be.

**One point is not a function.** Each entry asserts exactly one argument
tuple. `solution_gor` is pinned at 2000 psia. Nothing is asserted about
5000 psia, about the bubble-point discontinuity, about `p = 0`, or about
where the correlation goes non-physical.

**A tolerance is the wrong instrument for a pass-through.** `_close()` uses
`rel_tol=1e-9`, and golden.py argues that number carefully — correctly, for
comparing *across* machines and libm implementations. But for a binding
evaluated against its own native object code on one machine in one build,
there is no legitimate source of difference at all. Any tolerance there is
somewhere for an error to hide.

Layers 2 and 3 are the two questions layer 1 cannot ask.

| | asks | compares against | catches |
|---|---|---|---|
| `golden.json` | did it change? | **its own past** | compiler/flag drift, a regenerate picking a different overload, a refactor of the native source |
| `oracle` | is it faithful? | **the legacy binary** | transposed arrays, wrong intent, narrowing, units, argument-order swaps, **a wrong first recording** |
| `invariants.json` | is it possible? | **mathematics** | wrong away from the sample point, NaN at domain edges, hidden process-global state |

The failure classes are close to disjoint, and the middle layer is the one
whose absence makes the outer layer's guarantee conditional.

---

## 2. Layer 2 — the oracle

### 2.1 Principle

Generate a driver program **in the original language** that calls the same
routines with the same inputs as the binding and prints its results as raw
IEEE-754 bit patterns. Compare against the Python binding's results
**bitwise**, in the same build, on the same machine, in the same run.

Same machine, same object code, two calling paths. A faithful binding must
produce *identical bits*. There is no rounding allowance, because there is no
rounding difference to allow for: the floating-point work is done by the same
machine code either way. Everything that can differ is marshalling — type
width, array order, argument order, intent direction, a missing `&`, a scalar
passed by value where the routine wants a reference — and marshalling errors
do not produce *nearly* the right answer.

This is the layer that turns *"the answers did not change"* into
*"the answers are the ones the 1988 Fortran computes."*

### 2.2 The call plan comes from `golden.json`, not from `plan()`

The driver is generated from the **recorded entries in `golden.json`** — the
same keys, the same `arguments`, the same `constructor_arguments`, in the same
order.

The first draft of this document said the driver reuses `golden.plan()`
verbatim. That is wrong, and wrong in a way that would have severed the two
layers: `golden.run()` deliberately reuses hand-edited arguments from the
existing file (golden.py: *"edit them in golden.json to values your engineers
recognise, re-record, and those inputs survive every future re-record"*), and
the committed `petro_api/golden.json` already exercises this —
`solution_gor` is recorded at 2000.0 psia and `pvt_set_fluid` at a real fluid
description, not at `plan()`'s positional samples. A driver generated from
`plan()` would validate `solution_gor(1.0)` while golden asserts about
2000 psia, and the two layers would silently be talking about different
functions of different inputs. Rs = 355.076 — the number this layer exists to
retroactively validate — only exists at the hand-edited inputs.

Reading the file rather than regenerating the plan preserves everything the
plan-sharing argument wanted:

- The two layers describe **the same calls by construction**, so an oracle
  failure and a golden failure on the same key are directly comparable.
- There is no second input generator, so there is no way for the two to drift
  into calling the routine differently.
- Call **order** is the file's key order, which golden already defines as
  meaningful (legacy Fortran keeps state in COMMON blocks; an initialisation
  routine sets up what the next call reads). The driver emits its lines in
  entry order and the comparison is order-sensitive.

**The executed call sequences must be identical before any bit is compared.**
`golden.run()` converts a raising call into a skip and continues; the oracle
must not. Because state carries across calls, an entry executed by the driver
but skipped by the Python side (or vice versa) silently changes every
downstream answer in ways that look like marshalling bugs. So: the driver
executes exactly the file's entries; if the Python side cannot execute one of
them, that is a **hard oracle failure naming the entry**, never a skip, and
nothing after it is compared.

### 2.3 One set of object code, not two compilations of one source

"Compiled from the same sources with the same flags" is not the same claim as
"the same object code," and bitwise equality rests on the latter. A driver
program compiled from the same `.f90` files can be inlined, IPA'd and
constant-folded differently than the shared library — the compiler is
entitled to contract or reassociate differently when the call site is visible
in the same translation unit — and the resulting last-bit differences would
be *correct* failures of the gate, which is the kind that gets a gate
switched off.

So the driver does not recompile the library sources. It compiles **only the
generated driver translation unit** and **links against the same built
objects/archive the extension links against** (the CMake target the build
already produces). The routines execute as the identical machine code on both
paths, which is what makes the no-tolerance claim true rather than merely
plausible.

Consequences:

- The driver needs the library's symbols to be linkable from a plain program.
  For the Fortran services this is already how the extension is built; where
  a build only ever produced position-dependent objects for the extension, the
  oracle links the same `-fPIC` objects into the driver executable — linking
  PIC objects into an executable is legal everywhere this runs.
- The extension's *actual* compile flags are extracted from the build system's
  own record (`compile_commands.json`, or the generator's equivalent), never
  restated in a config file. §2.8's flag preconditions are checked against
  that record, because a precondition checked against configuration measures
  the configuration.

### 2.4 Emitting bits, not text

Decimal formatting is a lossy channel and must not appear anywhere in the
comparison path. Each side emits the 64-bit pattern:

**Fortran** — reinterpret, then print fixed-width hex. `transfer` rather than
a `Z` edit descriptor applied directly to the real, so the width and byte
order are unambiguous. Tab-separated, matching the wire format below:

```fortran
integer(8) :: bits
bits = transfer(value, 0_8)
write(*,'(A,A1,A,A1,Z16.16)') key, char(9), slot, char(9), bits
```

**C++** — `memcpy` into a `uint64_t` (not a union, not a pointer cast, both of
which are strict-aliasing violations that an optimiser is entitled to
miscompile):

```cpp
std::uint64_t bits;
std::memcpy(&bits, &value, sizeof bits);
std::printf("%s\t%s\t%016llx\n", key, slot, (unsigned long long)bits);
```

**Python** — `struct.pack(">d", value).hex()`, never `repr` and never
`float.hex()` string-compared against a differently-formatted counterpart.

**Big-endian, and this is not a preference.** Both native sides print the
*integer value* of the reinterpreted bits, which renders most-significant
nibble first regardless of the machine's byte order. Python's `>d` matches it;
`<d` produces the byte-reversed string on any little-endian host and every
comparison fails. Verified on arm64 — `355.0764805578969` is
`4076313943ad6f27` from gfortran's `Z16.16`, from clang's `%016llx`, and from
`struct.pack(">d", …).hex()`, and `276fad4339317640` from `<d`. The harness
must assert this on a known constant at startup rather than trusting it.

**Narrow floats widen; they do not get their own channel.** `transfer` of a
`real(4)` into an `integer(8)` has undefined padding bits, so a 32-bit value
must never touch the 64-bit path directly. Both sides widen `real(4)`/`float`
to double **before** packing — widening is exact, so the bitwise claim
survives — and the driver generator emits the widening from the IR's declared
kind. (The alternative, a parallel `Z8.8`/`%08x` 32-bit channel, buys nothing
and doubles the format.)

**Integers and booleans** are compared by value. **Strings** are compared by
value after one declared normalization — Fortran's trailing blanks are
stripped on the native side exactly as the binding strips them — and are
emitted percent-escaped (`%09`, `%0A`, `%25` for tab, newline, percent) so a
`CHARACTER` output containing a tab or newline cannot break the line
protocol. The binding layer already surfaces `CHARACTER` outputs, so this is
not hypothetical.

### 2.5 Wire format

The driver writes one line per observable value to stdout:

```
<key>\t<slot>\t<value>
```

where `<value>` is 16 lowercase hex digits for floats, a decimal integer for
integers, `0`/`1` for booleans, and a percent-escaped string otherwise.
`slot` names *which* value this is, because one call can produce many:

| slot | meaning |
|---|---|
| `return` | the function's return value |
| `return[<n>]` | element `n` of a tuple return — f2py surfaces `intent(out)` scalars and arrays as return values, not as argument effects |
| `arg:<i>` | scalar argument `i` after the call (`intent(inout)`) |
| `arg:<i>[<n>]` | element `n` of array argument `i` after the call |

**`<i>` counts over the Python-visible argument list**, i.e. the file's
`arguments` array — the same index space as golden's `argument_effects`.
This must be said, because it is *not* the Fortran call site's index space:
`plan()` drops `intent(out)` parameters from the Python argument list, and the
native call site additionally carries hidden `CHARACTER` length arguments.
The driver generator owns the mapping from Python-visible index to native
parameter position; the wire format never exposes native positions.

This mirrors `golden.invoke()`'s `argument_effects`, which exists for exactly
this reason: a Fortran routine whose whole answer is written into an
`intent(inout)` array returns `None`, and recording only the return value
would record `None` and assert nothing.

stdout is parsed; stderr is captured and reported on failure. A driver that
exits non-zero, prints a malformed line, omits a key, or emits keys out of
file order is a hard failure — never a skip.

### 2.6 `oracle check` is the gate; the file is optional provenance

The first draft made `oracle record`/`oracle verify` mirror golden: record a
committed `oracle.json` on one machine, verify against it elsewhere. That
model contradicts the layer's own scope claim. §2.9 (correctly) says bitwise
equality is only meaningful within a single build — and this repository is
developed on arm64 macOS while CI is Linux, so every cross-machine verify
would have to drop to tolerance mode, surrendering exactly the property layer
2 exists for, in the only place it runs.

The resolution is that the oracle's real invariant is not *"the bits match
what they were"* — that is golden's question — but *"the binding and the
native code agree, here, now."* That invariant needs no committed file:

```
ngate oracle check <name>      # generate driver, compile, link, run both
                                   # paths, compare bitwise, in this build
ngate oracle show <name>    # coverage + what each slot is
ngate oracle record <name>  # optional: write oracle.json from a check
```

`oracle check` is the gate. It runs wherever the build runs — every CI build,
every local `ngate build` if desired — and passes or fails with no file
involved. Because both sides execute in the same build, there is nothing to
go stale, nothing to re-record, and no mode where a tolerance is needed.

`oracle record` exists for one purpose: a committed `oracle.json` from the
**canonical build image** documents the bits that image produces, so a future
`check` in the same image can additionally diff against history, and so the
numbers are reviewable. It is provenance, not the gate; it is only ever
compared bitwise inside the image that recorded it, and comparing it anywhere
else is a usage error the CLI refuses. There is no tolerance mode — a
cross-build numeric comparison is golden's job, and golden already does it
with an argued tolerance.

The driver is regenerated and recompiled on **every** check rather than
cached. A stale driver that passes is worse than no driver. Where an
`oracle.json` exists, its `driver_sha256` is checked against the freshly
generated driver so a change in the generator is visible rather than silently
absorbed — which means, stated plainly: **every nativegate generator change
invalidates every recorded oracle file**, and re-recording them is part of
upgrading the tool. That friction is accepted; a generator change that moved
the driver is exactly the event the hash exists to surface.

`ngate verify <name>` (the aggregate) runs `oracle check` if a toolchain
is present, then golden, then invariants if `invariants.json` exists — and
reports each separately. Which layer failed *is* the diagnostic.

### 2.7 File schema (for the optional recorded file)

```jsonc
{
  "format": 1,
  "service": "petro_api",
  "language": "fortran",
  "provenance": {                 // golden.provenance(), plus:
    "compile_flags": ["-O2", "-fPIC", "-std=legacy"],  // extracted from the build, not config
    "driver_sha256": "…",         // the generated driver itself
    "link_target_sha256": "…",    // the archive/objects the driver linked
    "sources": { "…": "sha256" }  // identical to golden's
  },
  "entries": {                    // golden.json entry order, sort_keys=False
    "solution_gor": {
      "arguments": [2000.0],
      "slots": {
        "return": "4076313943ad6f27"
      }
    },
    "pvt_state": {
      "arguments": [2000.0, [0.0, 0.0, 0.0], 9],
      "slots": {
        "arg:1[0]": "3ff3cfb2…",
        "arg:1[1]": "4076313…"
      }
    }
  },
  "skipped": { "Grid.solve": "no public constructor callable from Python" }
}
```

Serialization follows `golden.write()`: `indent=2`, `sort_keys=False`,
trailing newline. Key order is call order and carries meaning.

### 2.8 Hard preconditions

`oracle check|record` refuses to run, rather than warning, when:

- `-ffast-math`, `-Ofast` or `-funsafe-math-optimizations` appear in the
  extension's **extracted** flags (§2.3). They discard IEEE semantics; a
  bitwise claim under them means nothing, and the point of the gate is that a
  claim is being made.
- The driver translation unit's flags differ from the extension's extracted
  flags in any way that affects code generation. If they differ, the
  comparison measures the flags, not the binding.

And the harness **sets**, rather than checks:

- `OMP_NUM_THREADS=1` and, where the link line pulls in a BLAS, its threading
  equivalents, in both processes. Reduction order changes bits. (An earlier
  draft phrased this as a refuse-when-unset precondition; setting it is
  strictly better — there is no configuration in which the harness wants more
  than one thread.)

### 2.9 Scope of v1

- **Fortran free functions and subroutines:** full support. This is where the
  value is, and where the whole `petro_api` surface lives.
- **C++ free functions and static methods:** full support.
- **C++ instance methods:** the driver must construct the object with the
  file's recorded `constructor_arguments`. Supported where those exist;
  skipped with the same reason string as golden otherwise.
- **Structs by value:** skipped in v1, recorded as skipped.

Coverage is reported the way golden reports it — `N covered, M skipped` — so a
check covering three of forty symbols cannot be mistaken for a clean bill of
health.

Bitwise mode is a **build-time gate, not a portability gate**. It proves the
binding is faithful in the build where it runs. Cross-platform numeric drift
is real (libm, FMA contraction, `-O2`-vs-`-O3`) and is golden's territory,
with golden's argued tolerance; the oracle never enters it.

### 2.10 What an oracle failure tells you

The failure message classifies before it accuses, because the responses are
entirely different:

| signature | reading |
|---|---|
| every slot of one call differs, wildly | argument order or intent is wrong |
| array slots are right but permuted | row/column-major transposition |
| values agree to ~7 significant digits then diverge | `float32` narrowing somewhere in the binding |
| return is right, `arg:` slots missing | in-place output is not being surfaced |
| the Python side could not execute an entry the driver ran | the two paths diverged before comparison — fix the entry, nothing downstream is meaningful |
| everything differs slightly, uniformly | the driver was not linked against the extension's objects, or flags differ — check the extracted flags first |

---

## 3. Layer 3 — `invariants.json`

### 3.1 Principle

Universally-quantified claims, checked over a **fixed lattice declared in the
file**. One property statement covers a surface where a golden entry covers a
point, and coverage grows without anyone hand-recording more numbers.

No RNG. No `hypothesis`. No wall clock. The lattice is written down, so the
same file checks the same points on every machine forever, and a reviewer can
see exactly what was and was not covered.

### 3.2 Two kinds of property

**Structural** — derived automatically from the IR plus one declaration
(§3.5), no domain knowledge:

| property | statement | catches |
|---|---|---|
| `finite` | no NaN, no Inf, at any lattice point | domain-edge blowups |
| `total` | no exception at any lattice point | unguarded `sqrt`/`log`, division by zero |
| `no_error_flag` | the library's declared error accessor reads clean after every lattice point | in-band failure the routine reports instead of raising |
| `idempotent` | `f(x)` twice in a row returns identical **bits** | hidden state, uninitialised memory |
| `order_independent` | `f(x); g(y); f(x)` ≡ `f(x); f(x)` for non-mutating `g` | **undeclared COMMON-block leakage across entry points** |

`no_error_flag` exists because `total` and `finite` are weak against this
library's actual error convention: petro routines report through `last_error`
and return in-band values rather than raising, so an unguarded domain edge
passes `total` trivially and can pass `finite` with garbage. Declaring the
accessor (`error_flag: last_error` in `nativegate.yaml`) makes the sweep check
what the library actually signals.

**Declared** — authored by a human in `nativegate.yaml`, because no parser can
know them:

```yaml
invariants:
  solution_gor:
    - bounds: {min: 0.0}
    - monotone: {in: pressure, direction: nondecreasing}
  oil_fvf:
    - bounds: {min: 1.0}          # Bo < 1 is not a physical oil
  relperm_corey:
    - bounds: {min: 0.0, max: 1.0}
  saturations:
    - sum_to_one: [sw, so, sg]
      tolerance: 1e-12
```

`monotone` compares **raw float64 values with `>=`/`<=`, no tolerance**. At
lattice spacing (33 points across a physical range) a genuinely monotone
correlation clears last-bit noise by many orders of magnitude, and a declared
tolerance is exactly the "to be safe" loosening that ends with the property
meaning nothing. If a correlation is legitimately non-monotone at fine scale,
the honest fix is to not declare `monotone` for it.

The first draft also listed `type_bounds` ("result fits the declared return
type without narrowing") as a structural property. It is cut: from Python the
result has already arrived as `float64` and narrowing is unobservable after
the fact. The narrowing check belongs to layer 2 (a narrowed value diverges
from the oracle at ~7 significant digits, §2.10) and to the IR-level checks
the generator already performs.

### 3.3 Why the property language is closed

Properties are a **fixed vocabulary of declarative forms** — `bounds`,
`monotone`, `sum_to_one`, `finite`, `total`, `no_error_flag`, `idempotent`,
`order_independent`, `symmetric_in`, `scales_linearly_in` — and emphatically
**not** an `eval`'d Python expression.

An `eval`'d predicate would make the gate exactly as trustworthy as arbitrary
code in a config file, would be unreviewable in a diff, and could not be
reasoned about mechanically (you cannot bisect a counterexample for a
predicate you cannot analyse). A closed vocabulary can be checked, shrunk,
explained in a failure message, and read by a reservoir engineer who does not
write Python. If a property does not fit the vocabulary, the vocabulary grows
in a reviewed commit.

### 3.4 The lattice, pinned to the bit

Full cartesian sweeps explode; random sampling is non-deterministic. The
design is a **fixed, small, declared** set:

1. **Per-parameter 1-D sweeps.** Hold all arguments at the values recorded in
   `golden.json` for that entry, sweep one parameter across `N` points
   (default 33) inclusive of both endpoints. Cost is linear in arity, and
   monotonicity is a 1-D claim anyway.
2. **Declared corners.** Explicit tuples a human considers dangerous — bubble
   point, `Sw = Swc`, zero rate — listed in the file.
3. **A recorded scatter set**, optional: splitmix64 with **the seed written
   into the file**, mapped to each range, for a fixed count. Reproducible,
   and honest about being a plain pseudo-random sample. (An earlier draft
   said "Sobol/splitmix"; they are different generators with different
   properties. This is splitmix64, and it is not low-discrepancy — if
   low-discrepancy coverage is ever wanted, that is a new, separately
   specified lattice kind.)

**The point-generation formula is part of the specification**, because "the
same points forever" is otherwise a fiction: two linspace formulations differ
in their last bits. Sweep point `i` of `n` over `[lo, hi]` is

```
x_i = lo + (i * (hi - lo)) / (n - 1)      # each operation in float64, this
                                          # exact association; x_0 = lo and
                                          # x_{n-1} = hi assigned exactly
```

and any implementation, in any language, must reproduce these bits.

Ranges come from the declaration (`pressure: [14.7, 10000]`), and there is no
default range — a swept parameter with no declared range is an error, not a
guess, recorded under `uncovered`. A gate that invents its own operating
envelope is a gate that reports failures nobody believes.

### 3.5 State: declared mutators, not wishful purity

The first draft applied `idempotent` and `order_independent` structurally to
every entry point. On the flagship service that fails **by design** on day
one: `petro_api`'s contract is that `pvt_set_fluid` mutates COMMON state that
`solution_gor` then reads, and `last_error` is a stateful accessor.
`f(x); pvt_set_fluid(y); f(x)` is *supposed* to differ — that is the API, not
leakage — and a gate that condemns the API's own contract is the crying-wolf
gate §4 warns about.

So the service declares its mutators:

```yaml
state:
  setup: [pvt_set_fluid]          # replayed (with golden.json's arguments)
                                  # before every property evaluation
  mutating: [pvt_set_fluid]       # routines whose job is to change state
  error_flag: last_error
```

- `idempotent` is checked for every entry point **not** in `mutating`.
- `order_independent` uses only non-`mutating` routines as the interposed
  `g`, and checks every non-`mutating` `f`. What it then catches is exactly
  the right thing: state coupling **nobody declared**.
- Every property evaluation runs after the `setup` sequence, replayed with
  the arguments recorded in `golden.json`; where isolation matters
  (`idempotent`, `order_independent`) the sequence runs in a fresh process.

An undeclared mutator does not escape: it fails `order_independent` when used
as an `f`, and the fix — adding it to `mutating` — is a reviewed, visible
admission that the routine holds state. This is what makes the hazard the
README currently concedes — *"a C++ library that keeps file-scope static
state is not protected, and the parser cannot see that it does — that one is
on you"* — mechanically detectable rather than a matter of trust.

### 3.6 Counterexample reporting

A failing property reports the **first lattice point** in sweep order, then
**deterministically bisects** toward the boundary between passing and failing
points to report the tightest bracket it can prove:

```
invariant `oil_fvf: bounds{min: 1.0}` failed
  first failure : pressure = 8437.50   -> 0.9994
  last passing  : pressure = 8125.00   -> 1.0002
  bracket       : the property breaks between 8125.00 and 8437.50 psia
  lattice       : pressure ∈ [14.7, 10000], 33 points, index 27
```

The bisection rule is pinned the way the lattice is: the bracket is the last
passing lattice point and the first failing one; the midpoint is `(a + b) / 2`
in float64; the side keeping the bracket valid is retained; iteration stops
when `(a + b) / 2` equals either endpoint (float64 exhaustion) or after 40
steps, whichever is first. Bisection evaluates points **off the lattice** —
that is its job, and it is still deterministic: fixed bracket, fixed midpoint
rule, so the message is byte-identical across runs and can itself be asserted
in tests.

### 3.7 File schema

`invariants.json` is a **result** artifact, not the source of truth — the
properties live in `nativegate.yaml` where a human edits them. The JSON records
what was checked, so the diff shows coverage changing:

```jsonc
{
  "format": 1,
  "service": "petro_api",
  "provenance": { /* as golden */ },
  "lattice": {
    "points_per_sweep": 33,
    "ranges": {"pressure": [14.7, 10000.0]},
    "corners": [[2500.0], [14.7]],
    "scatter": {"count": 0, "seed": null}
  },
  "state": {
    "setup": ["pvt_set_fluid"],
    "mutating": ["pvt_set_fluid"],
    "error_flag": "last_error"
  },
  "checked": {
    "solution_gor": {
      "properties": ["finite", "total", "no_error_flag", "idempotent",
                     "bounds{min:0.0}", "monotone{pressure,nondecreasing}"],
      "points": 33,
      "status": "pass"
    }
  },
  "uncovered": {
    "tubing_bhp": "no range declared for parameter 'rate'"
  }
}
```

`uncovered` is as important as `checked`, for the same reason golden records
skips: an invariants file that silently checks nothing must not look like a
pass.

---

## 4. Determinism rules (both layers)

Non-negotiable. Break one and the gate becomes flaky, which is worse than no
gate — a gate that fails for correct reasons gets disabled, and then nothing
is checked at all.

1. **Bits, not decimals.** Every float comparison goes through the 64-bit
   pattern. No `repr`, no `%f`, no `str()` round-trip. Narrow floats widen
   to double (exactly) before packing; they never get their own bit path.
2. **No `-ffast-math`, no `-Ofast`.** Hard error, not a warning, checked
   against the build's extracted flags rather than its configuration.
3. **Single-threaded native.** `OMP_NUM_THREADS=1` and the BLAS equivalents
   are **set** by the harness in both processes, not checked or assumed.
   Reduction order changes bits.
4. **No RNG.** Lattices are declared, and their point-generation formula is
   pinned to the bit (§3.4); any scatter carries its seed in the file and
   uses splitmix64, never `random` or `numpy.random`'s global state.
5. **One set of object code.** The oracle driver links the extension's own
   built objects; it never recompiles the library sources into a second body
   of machine code whose last bits it would then measure.
6. **Identical call sequences.** The oracle compares nothing after the two
   paths' executed sequences diverge; a one-sided skip is a hard failure.
7. **Canonical serialization.** `indent=2`, `sort_keys=False` (order carries
   meaning), LF, trailing newline — matching `golden.write()`. The artifact's
   value is that a human reads the diff.
8. **Everything hashed.** Sources, extracted flags, the generated driver, and
   the linked objects. A stale artifact must not be able to pass.
9. **Nothing environmental in the file.** No timestamps, paths, hostnames or
   user names — the constraint golden already holds itself to.

---

## 5. Where each layer runs

| | needs a compiler | needs the wheel | runs in |
|---|---|---|---|
| `golden verify` | no | yes | anywhere, including a user's laptop against an installed wheel |
| `oracle check` | **yes** | yes | wherever the build runs — every CI build, optionally every local build |
| `invariants verify` | no | yes | anywhere |

This is why `oracle` cannot simply be folded into `golden`: golden's ability
to run against an installed wheel with no toolchain is a feature, and layer 2
structurally cannot have it. Conversely, `oracle` needs no committed file to
do its job (§2.6), which is why it is the one layer without a mandatory
artifact.

CI ordering: `oracle check` first (a faithful binding is a precondition for
the other two meaning anything), then `golden`, then `invariants`.

---

## 6. What this does and does not prove

Worth stating plainly, since the repository's tone elsewhere is an honest gap
list rather than a sales pitch, and this document should not be the exception.

**Genuinely established:**

- Bitwise equality between the binding and the legacy object code is a real
  equality claim, not a sampled approximation, for the points checked. It
  admits no tolerance argument, because the same machine code produced both
  sides.
- The structural invariants — determinism, idempotence of non-mutating
  routines, order independence outside the declared mutators — are properties
  of the *implementation*, and a violation is a definite defect no matter
  what the physics says.

**Checked, not proved:**

- Every declared invariant. A finite lattice can miss a spike between two
  points; monotonicity on 33 samples is evidence, not a theorem. The file
  should say "checked at 33 points", and the CLI should never print the word
  "proved".

**Genuinely exhaustive, in one narrow case:** for a single-argument `float32`
routine the entire domain is 2³² values, enumerable in minutes. That is a real
"for all inputs" result, and it is worth an `--exhaustive` mode for 1-D
functions. For `float64`, or two arguments, it is hopeless and the honest word
stays "swept".

**Out of scope entirely:** memory safety, thread safety beyond the declared
process lock, and anything about the *correctness of the legacy code itself*.
Layer 2 proves the Python binding agrees with the Fortran. If the 1988 Fortran
is wrong, all three layers agree with it, in unison, forever.

---

## 7. Build order

1. `oracle check` for Fortran free functions, driven by `golden.json`'s
   recorded entries. Smallest surface, largest payoff — it retroactively
   validates every `golden.json` already committed, including `petro_api`'s
   Rs = 355.076 at the hand-edited 2000 psia inputs where that number lives.
2. `oracle check` for C++ free functions and static methods.
3. Structural invariants (`finite`, `total`, `no_error_flag`, `idempotent`,
   `order_independent`) with the `state:` declaration. `order_independent`
   closes a gap the README currently concedes.
4. Declared invariants, the lattice, and counterexample bisection.
5. `oracle record` and the optional committed file, once a canonical build
   image exists to pin it to.
6. `--exhaustive` for 1-D `float32`.

## 8. Open questions

- **Where does the generated driver live?** Emitted to the build directory
  and hashed (the hash appears in output and, when recorded, in the file), or
  additionally committed for reviewability? The hash makes staleness
  detectable either way; committing adds a generated artifact to the tree,
  which cuts against "what is generated, what is yours".
- **`intent(out)` arrays need a size at the driver's call site.** The IR
  carries `length_param`; where it is absent the entry is skipped, but it may
  be worth a declaration in `nativegate.yaml` instead.
- **Does `invariants` need its own JSON at all**, or should results live only
  in the test report? The argument for the file is that coverage changes show
  up in review; the argument against is a generated file nobody edits.

Resolved since the first draft: the driver's inputs come from `golden.json`,
not `plan()` (§2.2); the oracle links the extension's objects rather than
recompiling sources (§2.3); the primary mode is a same-build `oracle check`
with no committed file, which also settles "should oracle subsume golden's
numbers?" — golden remains the committed cross-time artifact, the oracle is
the per-build faithfulness gate, and neither derives from the other (§2.6).
