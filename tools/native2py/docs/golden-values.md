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
native2py generate pvt
native2py build pvt
pip install services/pvt/dist/*.whl

native2py golden record pvt      # -> services/pvt/golden.json  (commit this)
native2py golden verify pvt      # -> "4 entry point(s) unchanged (0 not covered)."
```

`golden.json` is the artifact. Commit it, review its diffs like any other
change to behaviour, and let CI run `native2py test pvt` — the generated
`tests/test_golden.py` replays the same calls inside the service's own suite.

When a value legitimately changes, re-record deliberately:

```bash
native2py golden record pvt --force
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

```json
"PVTINI": {
  "kind": "function",
  "name": "PVTINI",
  "arguments": [35.0, 0.75, 180.0, 1],
  "result": null
},
"PVTRS": { "arguments": [2500.0], "result": 610.6958746835878 }
```

That is 35 °API oil, 0.75 gas gravity, 180 °F, at 2500 psia — numbers a
reservoir engineer can sanity-check, from the real F77 correlations in
`libraries/petro`.

## Order is part of the fixture

Entries are recorded and replayed **in the order the service exposes them**,
never sorted. Legacy F77 keeps state in COMMON blocks: `PVTINI` initialises
what `PVTRS` and `PVTBO` then read. Replaying alphabetically calls `PVTBO`
first, against uninitialised state, and returns different numbers from an
unchanged build. (This was a real bug in an early version of this harness,
caught by the `pvt` service.)

The same applies to any stateful C++ class — which is why an instance is also
constructed fresh for each recorded method call.

## What it covers, and what it cannot

`golden show` prints both:

```
services/pvtcheck/golden.json: 5 recorded, 2 not covered (rtol=1e-12, atol=0.0)
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
$ native2py golden verify pvtcheck
1 numerical difference(s):
  - PvtModel.psi_to_bar: expected 0.06894757280343135, got 0.06894744825494009
Error: The bindings no longer return the recorded values. If the change is
intended, re-record with `native2py golden record pvtcheck --force`.
```

That one came from changing a conversion constant from `14.5037738` to
`14.5038` — a change no compiler, test suite or type checker would flag.

## Tolerance

Comparison is float-aware: `rtol=1e-12`, `atol=0.0` by default, both stored in
the file. Exact equality would fail on any platform with a different libm; the
tolerance is what makes "unchanged" a decidable claim. Raise it if you compare
across architectures, and say so in the commit that does.
