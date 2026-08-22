# CLI reference

Generated from `native2py --help` / `native2py <command> --help` output —
if this drifts from what the CLI actually prints, trust the CLI.

In a hurry? Skip straight to [`native2py quickstart`](#native2py-quickstart-source)
— one command, no `native2py.yaml` editing.

## `native2py init`

Scaffold the monorepo layout: `services/`, `libraries/`, `tools/`,
`infrastructure/docker/`, `infrastructure/kubernetes/`.

```bash
native2py init
```

It creates **empty directories only**, and only those that are missing, so it
is safe to re-run. It does not create `gateways/` (the `gateway` command does)
and it restores no service content: a deleted `services/` takes your native
sources and `native2py.yaml` with it. See
[Architecture](architecture.md#what-is-generated-what-is-yours) for what each
command does bring back.

## `native2py create-service <name>`

Scaffold a new `services/<name>/` directory: `native/`,
`bindings/generated/`, `python/<name>/`, `tests/`, and a starter
`native2py.yaml`.

```bash
native2py create-service demo --language cpp
native2py create-service reservoir --language fortran
```

| Option | Values | Default |
|--------|--------|---------|
| `--language` | `cpp`, `fortran` | `cpp` |
| `--force` | flag | off — errors if `services/<name>` already exists |

## `native2py quickstart <source>`

One command: scaffold a service from a single C++/Fortran file, expose
everything found in it, and generate the Python package — no manual
`native2py.yaml` editing. This is `create-service` + `expose` + `generate`
collapsed into one step, for the common case of "I have one file, make it a
Python package."

```bash
native2py quickstart path/to/widget.hpp
native2py quickstart path/to/mixer.f90 --build
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
  routine. For that case, use `create-service` + hand-edit `native2py.yaml`
  + `generate` instead, so only the routine(s) you actually list get parsed
  — see [Exposing Fortran](fortran-guide.md#designed-for-large-legacy-files).

| Option | Default |
|--------|---------|
| `--name` | the source file's stem (e.g. `widget.hpp` → service `widget`) |
| `--force` | off |
| `--build` / `--no-build` | `--no-build` |

## `native2py suggest <directory>`

Rank the native sources under a directory by how cleanly they would bind, to
answer "which file do I point native2py at first?" for a codebase you did not
write. Parses every header it finds — the same parse `inspect` does — and
sorts the results.

```bash
native2py suggest libraries/petro/cpp
native2py suggest src/ --parser clang --include vendor/eigen
```

| Mark | Meaning |
|------|---------|
| `✔` | binds everything it declares, and needs no other local header |
| `~` | binds more than it skips |
| `✘` | binds nothing, or skips at least as much as it binds |

Ranking puts dependency count ahead of skip count: one skipped array method
costs you that method, while one `#include` of another subsystem's header
costs you that subsystem's entire build. The `notes` column names the
implementation file found (see [`quickstart`](#native2py-quickstart-source)
for how it is located) and any local headers pulled in — by the header *or*
by its `.cpp`, since a header can look self-contained while its
implementation bridges to another subsystem.

Only headers (`.hpp`/`.hh`/`.h`) are offered as candidates when any exist —
you point native2py at an interface, not an implementation file. Fortran
sources are ranked by routine count.

!!! warning "Install the AST parser first"
    Without libclang the fallback regex reader runs, and it silently misses
    free functions and anything behind a macro — so the ranking is scored on
    an incomplete picture. If the header line reads `regex reader` rather
    than `clang AST`, run `pip install "native2py[clang]"` and try again.

| Option | Default |
|--------|---------|
| `--parser` | `auto` (`clang` if available, else `regex`) |
| `--include` | none (repeatable) |
| `--std` | `c++17` |
| `--all` | off — show every file rather than the top 15 |

## `native2py detect <path>`

Detect the native language of a file, or list every native source found
under a directory, grouped by language.

```bash
native2py detect services/demo/native/geometry.hpp
native2py detect services/reservoir/native
```

`init` only creates directories that are missing, so it is safe to re-run.
It restores the empty skeleton, never service content — see
[Architecture](architecture.md#what-is-generated-what-is-yours) for what does
come back after a deletion.

## `native2py inspect <path>`

Parse a single native source file and print the resulting intermediate
representation — useful for checking what native2py sees before running
`generate`.

```bash
native2py inspect services/demo/native/geometry.hpp
```

The first line of the output names the parser that ran:

```
$ native2py inspect services/demo/native/geometry.hpp
parser: clang AST (libclang 18.1.1)
```

C++ options: `--parser clang|regex|auto` chooses the backend, `--std` sets the
language standard (default `c++17`), and `--include DIR` (repeatable) adds a
header search directory — the same settings `native2py.yaml`'s `clang:` block
supplies during `generate`.

```bash
native2py inspect services/sim/native/Simulator.hpp \
  --include libraries/common-cpp/include --std c++20
```

Fortran requires at least one `--function` (repeatable), since the parser
targets specific routines rather than scanning the whole file:

```bash
native2py inspect services/reservoir/native/pressure.f90 \
  --function calculate_pressure --function normalize
```

## `native2py expose <path>`

Convenience wrapper: given a header/source path (or the `native/` directory
containing it), finds the enclosing service's `native2py.yaml` and runs the
same codegen as `generate`.

```bash
native2py expose services/demo/native/geometry.hpp
native2py expose services/demo/native
```

## `native2py generate <name>`

Re-run binding/CMake/package/test generation for `services/<name>/`, from
its `native/` sources and `native2py.yaml`. Safe to re-run any time — every
file it writes is regenerated from source, never hand-edited in place.

```bash
native2py generate demo
native2py generate reservoir
```

## `native2py build <name>`

Build the service's Python wheel: runs `pip wheel . -w dist` inside
`services/<name>/`, which drives `scikit-build-core` → CMake → pybind11 (C++)
or f2py (Fortran).

```bash
native2py build demo
```

Requires a working C++ toolchain + `cmake` + `pybind11` for C++ services, or
`gfortran` + `numpy` for Fortran services. See
[Troubleshooting](troubleshooting.md).

## `native2py test <name>`

Run `pytest tests/` inside `services/<name>/`.

```bash
native2py test demo
```

## `native2py docker <name>`

Write (or overwrite) `services/<name>/Dockerfile` from the multi-stage
template for the service's language.

```bash
native2py docker demo
native2py docker demo --build   # also runs `docker build -t demo:latest .`
```

| Option | Default |
|--------|---------|
| `--build` / `--no-build` | `--no-build` |

## `native2py gateway <name> --service <a> --service <b>`

Generate a composed FastAPI app that serves several services on one URL,
each mounted under `/<service-name>`.

```bash
native2py gateway platform-api --service demo --service pvt
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

## `native2py clean <name>`

Remove build artifacts (`dist/`, `build/`, `*.egg-info`, `__pycache__/`)
under `services/<name>/`.

```bash
native2py clean demo
```

## `native2py golden record|verify|show <name>`

Numerical regression: prove a rebuild still returns the same answers.

```bash
native2py build pvt && pip install services/pvt/dist/*.whl
native2py golden record pvt     # writes services/pvt/golden.json
native2py golden verify pvt     # replays it against the installed package
native2py golden show pvt       # what it covers, and what it does not
```

`record` needs the *built* package importable — the point is to pin what the
compiled extension computes, not what the source says. It refuses to
overwrite a golden file whose values have changed; pass `--force` once you
have decided the change is intended. `--rtol` / `--atol` set the comparison
tolerance stored in the file (default `rtol=1e-12`).

The same check runs inside the service's own suite via the generated
`tests/test_golden.py`, so `native2py test <name>` and CI catch drift with no
extra wiring. See [Numerical regression](golden-values.md).
