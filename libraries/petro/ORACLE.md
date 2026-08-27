# The oracle — checking the re-hosted numbers yourself

`FINDINGS.md` says the Python bindings return the same numbers as the native
binary. That claim was originally established by hand, once, by a person
reading two terminals. This document is about the part that replaced that:
an automated oracle you can run without taking anyone's word for it.

```
tools/nativegate/tests/test_native_oracle.py
```

## What it actually does

It builds the same calculation three ways from the same unmodified sources
and asserts all three agree.

| Path | Built by | Touches nativegate? |
|---|---|---|
| Fortran, natively | `gfortran` on `services/petro_api/native/petro_api.f90` + the seven F77 decks | no |
| C++, natively | `clang++` on `libraries/petro/cpp` (`FluidModel` → `FortranBridge.hpp` → the same F77 routines) | no |
| Python | `numpy.f2py`, after `preprocess.expand_includes` and `preprocess.resolve_kind_parameters` | yes — this is the thing under test |

Then it checks:

1. Python == the gfortran binary, for `solution_gor`, `oil_fvf`,
   `oil_viscosity`, `gas_z_factor`, `bubble_point`, `vogel_rate`,
   `tubing_bhp`, and all nine properties `pvt_state` writes into the caller's
   array.
2. Python == the clang++ binary, for the five correlations and the nine
   `PvtState` fields.
3. The two native binaries agree with each other. If they do not, the
   disagreement is in the legacy library and every other comparison here is
   measuring the wrong thing.
4. **The values committed in `services/petro_api/golden.json` are what the
   freshly compiled native binary computes.** This is the load-bearing one:
   it is what makes the golden file evidence rather than an assertion.
5. `cpp/examples/smoke.cpp` still builds and still prints the numbers
   `FINDINGS.md` quotes.
6. The comparison would notice a one-part-per-million error. An oracle that
   cannot fail is decoration.

## Running it

```bash
cd tools/nativegate
python -m pytest tests/test_native_oracle.py -v
```

It needs `gfortran`, `clang++` (or `g++`) and `numpy`. Roughly five seconds:
it compiles the F77 decks once per session and shares the objects between the
two native drivers.

On macOS:

```bash
brew install gcc          # gfortran
xcode-select --install    # clang++
```

On Debian/Ubuntu:

```bash
sudo apt install gfortran g++
```

Without a toolchain every test **skips with a reason naming what is missing**
— it does not fail. That is deliberate: a test that goes red on a laptop
without gfortran gets deleted from CI within a week, and then nothing checks
the numbers at all. It is also the reason a *build* failure is a hard failure
rather than a skip: the toolchain is present, so something about the sources
or the generated wrapper is wrong, and noticing that is the whole point.

## The case it runs

One fluid, stated once at the top of the test file and shared by all three
paths:

| | |
|---|---|
| API gravity | 35.0 |
| Gas gravity | 0.65 |
| Reservoir temperature | 180 °F |
| Correlation | 2 — Vazquez-Beggs |
| Pressure | 2000 psia |
| Target GOR for the bubble point | 1000 scf/stb |

These are the same inputs `golden.json` records, so the two artifacts
corroborate each other rather than testing different things. They are inside
every correlation's stated range on purpose: pinning behaviour at inputs no
correlation is defined for (the harness's generated default is an API gravity
of 1.0) records an extrapolation, and an extrapolation is where two libm
implementations are most likely to disagree.

`bubble_point` is comparable across the C++ and Fortran paths only because
`PVTINI` caches `PB = PVTBUB(1000.0D0)` (`fortran/pvtcor.f:75`) and
`FluidModel::bubble_point()` returns that cached value. That is why the target
GOR is 1000 and not some rounder number.

## What it produced when it was written

All three paths agreed **bit for bit** — not within tolerance, identically —
on an arm64 macOS machine with Homebrew GCC 16.2.0 and Apple clang 17.0.0:

```
                gfortran            clang++             f2py / Python
solution_gor    355.0764805578969   355.0764805578969   355.0764805578969
oil_fvf         1.2376603049350903  1.2376603049350903  1.2376603049350903
oil_viscosity   0.7693058330673301  0.7693058330673301  0.7693058330673301
gas_z_factor    0.8821195397407416  0.8821195397407416  0.8821195397407416
bubble_point    4784.822092740284   4784.822092740284   4784.822092740284
rho_oil         45.33883597248819   45.33883597248819   45.33883597248819
tubing_bhp      2964.7917938859628  —                   2964.7917938859628
vogel_rate      3375.0              —                   3375.0
```

Bit-for-bit is what you should expect *on one machine*, where the same libm
serves every path. Across machines it will not be, which is what the tolerance
below is for.

## Tolerance, and why it is not 1e-12

The comparisons use `golden.DEFAULT_RTOL` = **1e-9** relative with a **1e-12**
absolute floor. The reasoning is in the module docstring of
`tools/nativegate/nativegate/golden.py`; the short version:

* `pow`, `exp`, `log10` and `asin` are not correctly rounded in any
  mainstream libm, and Apple's, glibc's and musl's disagree by a few ULP.
  These correlations are nothing but chained transcendentals.
* The chain amplifies: the relative error of `10**x` is about `|x·ln 10|`
  times the relative error of `x`, and there are several exponentiations per
  correlation.
* FMA contraction is an optimisation-flag decision invisible in the source.

1e-9 is about 4.5 million ULP of headroom, which covers all of that — and it
is still six to seven orders of magnitude tighter than any change that could
be called a different answer. These correlations agree with laboratory data
to roughly ±10%; a changed coefficient, a dropped unit conversion, a swapped
argument pair or `-ffast-math` all move a result by 1e-3 relative or more.

Where a routine cannot be held to that, the *entry* in `golden.json` says so
and why, rather than the whole file being loosened to accommodate its worst
member. There is one such entry today:

* **`tubing_bhp`, rtol 1e-6.** `TRAVER` marches 20 tubing segments, iterating
  each segment's midpoint pressure to a fixed-point test of 1e-4 relative
  (`fortran/hydrau.f:279`). A ULP of libm difference can put that test on the
  other side of its threshold, so the answer is reproducible to the solver's
  own tolerance, not to machine precision.

Two routines look like they need the same treatment and do not.
`PVTBUB` (`pvtcor.f:82`) stops when its Newton step falls below `1e-6·P`, but
Newton converges quadratically, so one iteration more or fewer changes the
answer by the square of that — around 1e-12 relative, inside the default.
`PVTZ` (`pvtcor.f:239`) is Newton on the Hall-Yarborough reduced density with
a step tolerance of 1e-10, and the same argument applies with far more room.

## When it fails

**Build failure.** The sources or the generated wrapper stopped compiling.
The full compiler output is in the assertion message.

**A number moved.** Work out which comparison failed:

* *Python disagrees with both native binaries.* The re-hosting changed
  something — a type, an intent, an argument order, a flag. This is
  nativegate's bug.
* *The two native binaries disagree with each other.* Something in
  `libraries/petro` changed, or the two compilers are doing genuinely
  different arithmetic. Not a nativegate bug.
* *`golden.json` disagrees with a native binary that Python agrees with.* The
  committed baseline is stale — the native code legitimately changed and
  nobody re-recorded. Check `provenance.sources` in `golden.json` against the
  current source digests to confirm, then re-record.

Do not widen a tolerance to make this pass. Every tolerance here is derived
from something — libm behaviour, or a solver's own convergence test — and
widening one past what its derivation supports converts a check into a
formality. If a routine genuinely needs a looser number, give it a per-entry
tolerance with the reason, the way `tubing_bhp` has one.

## The other half: `golden.json`

The oracle answers "is this correct". `services/petro_api/golden.json`
answers "has it changed since", which is a cheaper question that can be asked
on every build without a compiler in the loop. Record it with:

```bash
ngate golden record petro_api      # needs the built extension installed
ngate golden verify petro_api
ngate golden show petro_api
```

It records, per entry, the arguments, the result, any argument the call wrote
in place, and the tolerance that entry is held to — plus one `provenance`
block for the file: platform, machine, Python, numpy, the `gfortran` and
`clang++` identification strings, nativegate's version, and the SHA-256 of
every native source the values came from. No timestamps, no paths, no
hostnames: the file is diffed in review, and the only thing that should ever
show up in that diff is a changed answer.

The provenance exists because a golden failure has two possible causes that
need opposite responses — the code changed, or the toolchain did — and the
documented remedy for a failure (re-record) destroys the evidence needed to
tell them apart. So the failure message reports the recorded environment
against the current one, and says explicitly when they match:

```
numerical regression:
  solution_gor: expected 355.0764805578969, got 355.0764805579012 (rtol=1e-09, atol=1e-12)

The toolchain ALSO changed since these values were recorded. A platform
difference looks exactly like a regression — rule it out first:
  fortran_compiler: recorded 'GNU Fortran (Homebrew GCC 16.2.0) 16.2.0', now 'GNU Fortran (Ubuntu 13.2.0) 13.2.0'
```

A changed toolchain on its own is never a failure. A new compiler that
returns the same answers has not broken anything.
