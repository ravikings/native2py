# CLI reference

Generated from `ngate --help` / `nativegate <command> --help` output —
if this drifts from what the CLI actually prints, trust the CLI.

Service names in the examples below (`demo`, `pvt`, `reservoir`, …) are
illustrative. The only service committed in this repo is `petro_api`;
substitute your own, or scaffold one first with `quickstart`.

In a hurry? Skip straight to [`ngate quickstart`](#nativegate-quickstart-source)
— one command, no `nativegate.yaml` editing.

## `ngate init`

Scaffold the monorepo layout: `services/`, `libraries/`, `tools/`,
`infrastructure/docker/`, `infrastructure/kubernetes/`.

```bash
ngate init
```

It creates **empty directories only**, and only those that are missing, so it
is safe to re-run. It does not create `gateways/` (the `gateway` command does)
and it restores no service content: a deleted `services/` takes your native
sources and `nativegate.yaml` with it. See
[Architecture](architecture.md#what-is-generated-what-is-yours) for what each
command does bring back.

## `ngate create-service <name>`

Scaffold a new `services/<name>/` directory: `native/`,
`bindings/generated/`, `python/<name>/`, `tests/`, and a starter
`nativegate.yaml`.

```bash
ngate create-service demo --language cpp
ngate create-service reservoir --language fortran
```

| Option | Values | Default |
|--------|--------|---------|
| `--language` | `cpp`, `fortran` | `cpp` |
| `--force` | flag | off — errors if `services/<name>` already exists |

## `ngate quickstart <source>`

One command: scaffold a service from a single C++/Fortran file, expose
everything found in it, and generate the Python package — no manual
`nativegate.yaml` editing. This is `create-service` + `expose` + `generate`
collapsed into one step, for the common case of "I have one file, make it a
Python package."

```bash
ngate quickstart path/to/widget.hpp
ngate quickstart path/to/mixer.f90 --build
```

If you point it at a C++ header, it also looks for the `.cpp`/`.cc`/`.cxx`
implementation files that define it and copies those in too — a header with
declared-but-not-defined methods can't actually compile. Two layouts are
searched:

- **side by side** — `foo.hpp` next to `foo.cpp`;
- **split** — `include/foo.hpp` with `src/foo.cpp`, the conventional layout
  for any C++ project of size. The nearest `include/`, `inc/` or `headers/`
  ancestor is located and its sibling `src/`, `source/`, `sources/` or
  `lib/` is searched recursively, so `src/` subdivided to mirror `include/`
  still resolves.

If none is found, `quickstart`
prints a warning rather than failing silently: without an implementation
file, `--build` can still *succeed* (undefined symbols are only caught at
import time on macOS, not at link time) but the built extension fails on
first `import` with a confusing `dlopen`/undefined-symbol error. See
[Troubleshooting](troubleshooting.md#symbol-not-found-in-flat-namespace-after-a-successful-build).

- C++: every class/function found in the file is exposed (empty `expose:`
  list = expose everything — see [Exposing C++](cpp-guide.md)).
- Fortran: every `function`/`subroutine` declaration found is auto-listed
  under `expose.functions:`. This does a full-file scan, unlike `generate`'s
  targeted per-routine extraction — fine for a single small/medium file, but
  **not** what you want for a large legacy template where you only need one
  routine. For that case, use `create-service` + hand-edit `nativegate.yaml`
  + `generate` instead, so only the routine(s) you actually list get parsed
  — see [Exposing Fortran](fortran-guide.md#designed-for-large-legacy-files).

| Option | Default |
|--------|---------|
| `--name` | the source file's stem (e.g. `widget.hpp` → service `widget`) |
| `--force` | off |
| `--build` / `--no-build` | `--no-build` |

## `ngate suggest <directory>`

Rank the native sources under a directory by how cleanly they would bind, to
answer "which file do I point nativegate at first?" for a codebase you did not
write. Parses every header it finds — the same parse `inspect` does — and
sorts the results.

```bash
ngate suggest libraries/petro/cpp
ngate suggest src/ --parser clang --include vendor/eigen
```

| Mark | Meaning |
|------|---------|
| `✔` | binds everything it declares, and needs no other local header |
| `~` | binds more than it skips |
| `✘` | binds nothing, or skips at least as much as it binds |

Ranking puts dependency count ahead of skip count: one skipped array method
costs you that method, while one `#include` of another subsystem's header
costs you that subsystem's entire build. The `notes` column names the
implementation file found (see [`quickstart`](#nativegate-quickstart-source)
for how it is located) and any local headers pulled in — by the header *or*
by its `.cpp`, since a header can look self-contained while its
implementation bridges to another subsystem.

Only headers (`.hpp`/`.hh`/`.h`) are offered as candidates when any exist —
you point nativegate at an interface, not an implementation file. Fortran
sources are ranked by routine count.

!!! warning "Install the AST parser first"
    Without libclang the fallback regex reader runs, and it silently misses
    free functions and anything behind a macro — so the ranking is scored on
    an incomplete picture. If the header line reads `regex reader` rather
    than `clang AST`, run `pip install "nativegate[clang]"` and try again.

| Option | Default |
|--------|---------|
| `--parser` | `auto` (`clang` if available, else `regex`) |
| `--include` | none (repeatable) |
| `--std` | `c++17` |
| `--all` | off — show every file rather than the top 15 |

## `ngate detect <path>`

Detect the native language of a file, or list every native source found
under a directory, grouped by language.

```bash
ngate detect libraries/geometry/geometry.hpp
ngate detect services/petro_api/native
```

## `ngate inspect <path>`

Parse a single native source file and print the resulting intermediate
representation — useful for checking what nativegate sees before running
`generate`.

```bash
ngate inspect services/demo/native/geometry.hpp
```

The first line of the output names the parser that ran:

```
$ ngate inspect services/demo/native/geometry.hpp
parser: clang AST (libclang 18.1.1)
```

C++ options: `--parser clang|regex|auto` chooses the backend, `--std` sets the
language standard (default `c++17`), and `--include DIR` (repeatable) adds a
header search directory — the same settings `nativegate.yaml`'s `clang:` block
supplies during `generate`.

```bash
ngate inspect services/sim/native/Simulator.hpp \
  --include libraries/common-cpp/include --std c++20
```

Fortran requires at least one `--function` (repeatable), since the parser
targets specific routines rather than scanning the whole file:

```bash
ngate inspect services/reservoir/native/pressure.f90 \
  --function calculate_pressure --function normalize
```

## `ngate expose <path>`

Convenience wrapper: given a header/source path (or the `native/` directory
containing it), finds the enclosing service's `nativegate.yaml` and runs the
same codegen as `generate`.

```bash
ngate expose services/demo/native/geometry.hpp
ngate expose services/demo/native
```

## `ngate generate <name>`

Re-run binding/CMake/package/test generation for `services/<name>/`, from
its `native/` sources and `nativegate.yaml`. Safe to re-run any time — every
file it writes is regenerated from source, never hand-edited in place.

```bash
ngate generate demo
ngate generate reservoir
```

## `ngate build <name>`

Build the service's Python wheel: runs `pip wheel . -w dist` inside
`services/<name>/`, which drives `scikit-build-core` → CMake → pybind11 (C++)
or f2py (Fortran).

```bash
ngate build demo
```

Requires a working C++ toolchain + `cmake` + `pybind11` for C++ services, or
`gfortran` + `numpy` for Fortran services. See
[Troubleshooting](troubleshooting.md).

## `ngate serve <name>`

Run the service's FastAPI app (`<name>.service:app`) under a single uvicorn
process, against the **installed** package — build and install the wheel
first, or it fails with those instructions rather than an import traceback.

```bash
ngate serve demo
ngate serve demo --host 0.0.0.0 --port 9000
ngate serve demo --reload        # development only
```

| Option | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | Interface to bind. Loopback by default: a generated service is not hardened for an untrusted network — see [Is this production-ready?](production-readiness.md) |
| `--port` | `8000` | Port to bind |
| `--reload` | off | Restart on source changes |

This is a development convenience, not a deployment. The generated Dockerfile
runs gunicorn with uvicorn workers; that is what should serve real traffic.
There is deliberately no `--workers` here: a Fortran service keeps its state
in COMMON blocks, which are per-process, so a "configure, then read" sequence
split across two workers reads state the other one never wrote.

## `ngate verify <name>`

Run every verification layer, in order, reporting each one by name so one
layer's failure never masks another's:
[`oracle check`](#nativegate-oracle-checkshow-name) (skipped, visibly, if no
compiler is on PATH), then
[`golden verify`](#nativegate-golden-recordverifyshow-name), then
[`invariants verify`](#nativegate-invariants-verify-name) (skipped, visibly,
if no `state:`/`invariants:`/`ranges:` are declared).

```bash
ngate verify demo
# oracle: passed (9 covered, 0 skipped)
# golden: passed
# invariants: passed
```

See [Verification](../README.md#verification) for what each layer asks,
compares against, and catches.

## `ngate oracle check|show|record <name>`

Layer 2: is the binding faithful to the legacy binary, not just unchanged?
`check` generates a driver **in the original language** from `golden.json`'s
recorded entries, compiles only the driver translation unit, links it
against the extension's own built objects, and compares every observable
value **bitwise** against the Python binding, in the same build.

```bash
ngate oracle check pvt   # generate, build, run, compare bitwise
ngate oracle show pvt    # coverage + wire slots, no build
ngate oracle record pvt  # run a full check, then write oracle.json provenance
```

`check` needs a compiler; it needs no committed file, and regenerates the
driver on every run so a stale one can never pass. `record` only writes
`oracle.json` on a passing check — the file is provenance for later runs
(driver hash, extracted compile flags, linked-object hash), never the gate
itself. When `oracle.json` exists, `check` also verifies the driver hasn't
gone stale and, only if the recorded build provenance still matches the
current one, diffs today's bits against the recorded ones — bitwise, with no
tolerance mode. See [Verification](../README.md#verification).

## `ngate invariants verify <name>`

Layer 3: are the declared properties true over a swept lattice? Runs T10's
structural properties (`finite`, `total`, `no_error_flag`, `idempotent`,
`order_independent`) and T11's declared properties (`bounds`, `monotone`,
`sum_to_one`, declared under `invariants:` in `nativegate.yaml`) over
`golden.json`'s recorded entries, swept per `ranges:`, and writes
`services/<name>/invariants.json`.

```bash
ngate invariants verify pvt
# 6 function(s) checked, 1 uncovered (services/pvt/invariants.json)
```

A parameter with no declared `ranges:` entry is not skipped silently — it is
recorded under `invariants.json`'s `uncovered` block. A run whose `checked`
block comes out empty is a hard failure, never a quiet pass. See
[Verification](../README.md#verification).

## `ngate test <name>`

Run `pytest tests/` inside `services/<name>/`.

```bash
ngate test demo
```

## `ngate docker <name>`

Write (or overwrite) `services/<name>/Dockerfile` from the multi-stage
template for the service's language.

```bash
ngate docker demo
ngate docker demo --build   # also runs `docker build -t demo:latest .`
```

| Option | Default |
|--------|---------|
| `--build` / `--no-build` | `--no-build` |

For a **C++** service that declares `libraries:`, the build context becomes the
repo root and the command becomes
`docker build -f services/<name>/Dockerfile -t <name>:latest .`, because `COPY`
cannot reach outside a service directory. A Fortran service keeps the
service-directory context: its library sources are already vendored into
`native/_expanded/` by `generate`.

If no `requirements.lock` exists, this prints a note that pip will resolve
dependencies at build time, so two builds a week apart can ship different
code. Run `ngate lock <name>` first.

## `ngate lock <name>`

Pin the service's Python dependencies by version **and SHA-256**, writing
`services/<name>/requirements.lock`. The generated Dockerfile installs it with
`--require-hashes`.

```bash
ngate lock demo
```

Without it, `docker build` resolves fastapi/uvicorn/numpy from PyPI on every
build, so an image rebuilt later ships different code with nothing recording
the change. Resolution targets the **image** — Python 3.12 on Linux, both
architectures — not the machine running the command. Re-run
`ngate docker <name>` afterwards.

## `ngate k8s <name>`

Generate Kubernetes manifests for the service.

```bash
ngate k8s demo
ngate k8s demo --image registry.example.com/demo:1.4.0 --replicas 3
```

| Option | Default |
|--------|---------|
| `--image` | `<name>:latest` |
| `--replicas` | `2` |
| `--output` | `infrastructure/kubernetes/<name>.yaml` |

Replicas, resources and the image are starting points you are expected to
tune. The security context and the probe wiring (`/healthz` for liveness,
`/readyz` for readiness) are not — they are the reason to generate this rather
than hand-write it.

For a Fortran service, note that native calls are serialised per process (see
[Architecture](architecture.md#the-concurrency-model-one-native-call-per-process-fortran)),
so replica count, not thread count, is what buys you concurrency there.

## `ngate gateway <name> --service <a> --service <b>`

Generate a composed FastAPI app that serves several services on one URL,
each mounted under `/<service-name>`.

```bash
ngate gateway platform-api --service demo --service pvt
uvicorn platform_api.app:app
```

Writes `gateways/<name>/` containing the app plus a `pyproject.toml` that
depends on each service's **wheel** — so services stay independently built
and versioned. Use this when you want one image and one URL; keep separate
images behind an ingress instead when you need independent scaling. Full
trade-off comparison in
[Deployment topologies](deployment-topologies.md).

| Option | Notes |
|--------|-------|
| `--service` | Repeatable, required. Each named service must already exist under `services/`. |

## `ngate clean <name>`

Remove build artifacts under `services/<name>/`: `dist/`, `build/`,
`*.egg-info`, `__pycache__/`, `.pytest_cache/`, and `native/_expanded/`.

`native/_expanded/` is the vendored+preprocessed Fortran tree — regenerable,
but note that it holds the library sources a Fortran `libraries:` service
compiles against, so `clean` must be followed by `generate` before `build`.

```bash
ngate clean demo
```

## `ngate golden record|verify|show <name>`

Numerical regression: prove a rebuild still returns the same answers.

```bash
ngate build pvt && pip install services/pvt/dist/*.whl
ngate golden record pvt  # writes services/pvt/golden.json
ngate golden verify pvt  # replays it against the installed package
ngate golden show pvt    # what it covers, and what it does not
```

`record` needs the *built* package importable — the point is to pin what the
compiled extension computes, not what the source says. It refuses to
overwrite a golden file whose values have changed; pass `--force` once you
have decided the change is intended. `--force`, `--rtol` and `--atol` are
options of `record` only; `verify` and `show` take none. `--rtol` / `--atol`
set the comparison tolerance stored in the file (defaults **`rtol=1e-9`,
`atol=1e-12`**).

The same check runs inside the service's own suite via the generated
`tests/test_golden.py`, so `ngate test <name>` and CI catch drift with no
extra wiring. See [Numerical regression](golden-values.md).
