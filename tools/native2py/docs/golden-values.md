# Numerical regression: golden values

For a re-host of decades-old PVT, well or reservoir code, "it builds and
imports" is not the acceptance criterion. **The answers did not change** is.

Everything native2py touches can move a number without failing a build: a
regenerate that picks a different overload, a compiler or flag change, a
`-ffast-math` in a CMake preset, a parser upgrade that maps a type
differently, a refactor in the native code underneath. The golden harness
records what every bound entry point returns for a fixed set of inputs, and
compares on every build.

## The loop

```bash
native2py generate petro_api
native2py build petro_api
pip install services/petro_api/dist/*.whl

native2py golden record petro_api   # -> services/petro_api/golden.json (commit this)
native2py golden verify petro_api   # -> "10 entry point(s) unchanged (0 not covered)."
```

`golden.json` is the artifact. Commit it, review its diffs like any other
change to behaviour, and let CI run `native2py test petro_api` — the generated
`tests/test_golden.py` replays the same calls inside the service's own suite.

When a value legitimately changes, re-record deliberately:

```bash
native2py golden record petro_api --force
```

Without `--force`, a re-record that would change a recorded value refuses and
prints the differences. Silently re-recording is how a regression gets
committed as the new truth.

## Use inputs your engineers recognise

Generated inputs are positional defaults — `1.0`, `2.5`, `0.75` — chosen only
to be reproducible. For a PVT correlation, an API gravity of `1.0` is outside
the valid range, so a baseline taken there pins the behaviour of an
extrapolation rather than of the correlation.

Edit the `arguments` in `golden.json` to real values and re-record. Hand-edited
inputs are **kept** across re-records:

These two entries are from the real `services/petro_api/golden.json`:

```json
"pvt_set_fluid": {
  "kind": "function",
  "class": null,
  "name": "pvt_set_fluid",
  "constructor_arguments": [],
  "arguments": [35.0, 0.65, 180.0, 2],
  "result": null,
  "tolerance": { "rtol": 1e-09, "atol": 1e-12 }
},
"solution_gor": {
  "kind": "function",
  "class": null,
  "name": "solution_gor",
  "constructor_arguments": [],
  "arguments": [2000.0],
  "result": 355.0764805578969,
  "tolerance": { "rtol": 1e-09, "atol": 1e-12 }
}
```

That is 35 °API oil, 0.65 gas gravity, 180 °F, correlation 2, evaluated at
2000 psia — numbers a reservoir engineer can sanity-check, from the real F77
correlations in `libraries/petro/fortran/`.

## Order is part of the fixture

Entries are recorded and replayed **in the order the service exposes them**,
never sorted. Legacy F77 keeps state in COMMON blocks: `pvt_set_fluid` writes
`COMMON /FLUID/`, and `solution_gor`, `oil_fvf` and `oil_viscosity` read it
back. Replaying alphabetically calls `bubble_point` first, against
uninitialised state, and returns different numbers from an unchanged build.
(This was a real bug in an early version of this harness, caught by the
Fortran PVT service.)

The same applies to any stateful C++ class — which is why an instance is also
constructed fresh for each recorded method call.

## What it covers, and what it cannot

`golden show` prints both:

For the service in this repo, nothing is skipped:

```
$ native2py golden show petro_api
services/petro_api/golden.json: 10 recorded, 0 not covered (rtol=1e-09, atol=1e-12)
```

A service with gaps looks like this instead (illustrative — the reason strings
are the harness's real templates):

```
services/pvtcheck/golden.json: 5 recorded, 2 not covered (rtol=1e-09, atol=1e-12)
  PvtModel.properties_at(1.0) -> {'bo': 1.1, 'rs': 1.0}
  ...
  [skipped] Correlation.apply: no public constructor callable from Python
  [skipped] Registry.adopt: parameter 'model' has type 'PvtModel', which the
            golden harness cannot generate an input for
```

Skips are recorded in the file itself, so a golden file covering three of
forty entry points cannot be mistaken for a clean bill of health. An entry
point that *disappears* between regenerates is reported as a difference, not
quietly dropped — a shrinking API is exactly the drift this catches.

## What a difference looks like

```
$ native2py golden verify pvtcheck        # illustrative
1 numerical difference(s):
  - PvtModel.psi_to_bar: expected 0.06894757280343135, got 0.06894744825494009
Error: The bindings no longer return the recorded values. If the change is
intended, re-record with `native2py golden record pvtcheck --force`.
```

That one came from changing a conversion constant from `14.5037738` to
`14.5038` — a change no compiler, test suite or type checker would flag.

## Tolerance

Comparison is float-aware: **`rtol=1e-9`, `atol=1e-12`** by default, stored
both at the top of the file and per entry. Exact equality would fail on any
platform with a different libm; the tolerance is what makes "unchanged" a
decidable claim. Raise it if you compare across architectures, and say so in
the commit that does. `golden record --rtol/--atol` sets what gets stored.

## What else the file records

Beyond `entries` and `skipped`, a `format: 2` golden file carries a
`provenance` block:

```json
"provenance": {
  "platform": "Darwin 24.6.0",
  "machine": "arm64",
  "python": "3.11.5",
  "numpy": "2.4.6",
  "fortran_compiler": "GNU Fortran (Homebrew GCC 16.2.0) 16.2.0",
  "cxx_compiler": "Apple clang version 17.0.0 (clang-1700.6.4.2)",
  "native2py": "0.1.0",
  "sources": {
    "libraries/petro/fortran/pvtcor.f": "3674ccbc48a4...",
    "services/petro_api/native/petro_api.f90": "dbb552032a7c..."
  }
}
```

This is what makes a failure diagnosable rather than just alarming. `sources`
is a SHA-256 per native source file, so a verify failure whose source digests
are **unchanged** tells you the native code did not move and the difference
came from the toolchain — a compiler upgrade, a different numpy, a new
architecture. The harness says so in its output rather than leaving you to
guess.

### `argument_effects`: covering subroutines

A Fortran subroutine returns nothing and writes its answer into a caller's
array. Recording only `result` would pin `null` and prove nothing. The harness
records the post-call contents of mutated array arguments instead, keyed by
argument position:

```json
"pvt_state": {
  "arguments": [2000.0, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], 9],
  "result": null,
  "argument_effects": {
    "1": [1.2376603049350903, 0.0014210347131269228, 0.9940793505687715,
          355.0764805578969, 0.7693058330673301, 0.01650745389948176,
          45.33883597248819, 34.94636657448389, 0.8821195397407416]
  }
}
```

For a Fortran-heavy codebase this is the mechanism that makes subroutines
coverable at all — without it, most of a legacy deck would record as `null`.
