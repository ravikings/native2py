# native2py

A monorepo for getting decades-old engineering code — 1990s C++ over FORTRAN
77 — reachable from Python as deployable services, without rewriting it and
without hand-writing bindings.

It holds three things:

1. **[`tools/native2py/`](tools/native2py/)** — the generator. Point it at a
   header or a Fortran deck; it produces the pybind11/f2py bindings, the CMake
   build, an installable Python package, a FastAPI service, tests, a numerical
   regression baseline, and a Dockerfile.
2. **[`libraries/petro/`](libraries/petro/)** — a period-accurate legacy
   codebase (~4,800 lines, 1988–2004 vintage) that exists to stress the
   generator against the real thing rather than a toy header.
3. **`services/`** — what came out the other side: eight services generated
   from that code and from smaller examples, each an independently buildable
   wheel and container.

## Layout

```
tools/native2py/     the generator + its docs and test suite
libraries/           native code shared by services (petro, common-cpp, ...)
services/<name>/     one generated service per exposed API
gateways/<name>/     optional: several services mounted into one FastAPI app
infrastructure/      docker/ and kubernetes/ (skeleton only, see below)
design.md            the 1,073-line specification all of this implements
```

Every command runs from this directory — `native2py` resolves
`services/<name>/` relative to your working directory.

## Start here

```bash
cd tools/native2py && ./scripts/bootstrap.sh && cd -   # creates .venv, installs the CLI

tools/native2py/.venv/bin/native2py quickstart libraries/geometry/geometry.hpp
tools/native2py/.venv/bin/native2py build geometry
```

Then read [Getting started](tools/native2py/docs/getting-started.md) for the
same path explained, or [the native2py README](tools/native2py/README.md) for
what the tool does and does not handle.

### Using the generator on your own project

You do not have to work inside this repo. native2py installs like any pip
package and resolves `services/` against your current directory:

```bash
cd /path/to/your-project
python3 -m venv .venv
.venv/bin/pip install "native2py[clang,build,test] @ git+https://github.com/ravikings/native2py.git@<sha>#subdirectory=tools/native2py"
.venv/bin/native2py init && .venv/bin/native2py quickstart src/yourlib.hpp
```

Pin the commit — this is a code generator, and an unpinned upgrade can change
the bindings under a service that was working. Details, including which extras
matter and why, are in
[Using it in another project](tools/native2py/README.md#using-it-in-another-project).

## The services

| Service | Language | From | What it exercises |
|---|---|---|---|
| `demo` | C++ | `geometry.hpp` | the simplest end-to-end path; mounted in the gateway |
| `calculator` | C++ | `calculator.hpp` | linking a shared library (`libraries/common-cpp`) |
| `fluid` | C++ | `FluidModel.hpp` | a class over F77 correlations, with a PVT cache |
| `sim` | C++ | `Simulator.hpp` | forward-declared types and raw pointers — mostly *refusals*, each reported with a reason |
| `pvt` | Fortran | `pvtcor.f` | fixed-form F77 with COMMON blocks; carries the recorded `golden.json` |
| `petro` | Fortran | 7 F77 decks + an F90 facade | INCLUDE expansion, inferred intent, module nesting, 12 exposed routines |
| `reservoir` | Fortran | `pressure.f90` | free-form F90 and a C ABI variant |
| `linsolve` | Fortran | `matsol.f` | array arguments (Thomas algorithm) |

Regenerate any of them at any time — the bindings, CMake, Python package and
tests are all generated:

```bash
tools/native2py/.venv/bin/native2py generate petro
```

What is **not** regenerable is the code you wrote: `native/`,
`native2py.yaml`, and the recorded answers in `golden.json`. See
[what is generated, what is yours](tools/native2py/docs/architecture.md#what-is-generated-what-is-yours).

## The legacy library

`libraries/petro/` is the point of the whole exercise: black-oil PVT
correlations, Corey/Stone relative permeability, Beggs & Brill wellbore
hydraulics, Peng-Robinson flash, an IMPES simulator core, Peaceman well
indices — with `IMPLICIT` typing, COMMON blocks, INCLUDE decks, macro-mangled
`extern "C"` bridges and a pre-standard C++ layer on top. It compiles and
returns physically sensible numbers on its own, before native2py touches it.

[`libraries/petro/FINDINGS.md`](libraries/petro/FINDINGS.md) records what
broke when the generator was first pointed at it, and what was fixed —
including a correction to its own first draft, which had blamed fixed-form
parsing for what turned out to be a measurement error in the probe.

## Proving the answers did not change

For a re-host, "it builds and imports" is not the acceptance criterion. Every
service can pin what its bindings return:

```bash
tools/native2py/.venv/bin/native2py golden record pvt    # -> services/pvt/golden.json
tools/native2py/.venv/bin/native2py golden verify pvt    # 4 entry point(s) unchanged
```

`services/pvt/golden.json` is a worked example: the 1988 correlations called
at 35 °API, 0.75 gas gravity, 180 °F and 2500 psia, giving Rs = 610.696
scf/stb and Bo = 1.34069 — values a reservoir engineer can check by hand.
Commit the file, and any rebuild that moves a number fails.
See [Numerical regression](tools/native2py/docs/golden-values.md).

## Serving several services together

```bash
tools/native2py/.venv/bin/native2py gateway platform-api --service demo --service calculator
uvicorn platform_api.app:app
```

Each service keeps its own wheel, version and build; the gateway just mounts
their routers under `/<service>`. The alternative — one image per service
behind an ingress — needs no generated code at all. Both are covered in
[Deployment topologies](tools/native2py/docs/deployment-topologies.md).

## Tests

```bash
cd tools/native2py
.venv/bin/python -m pytest tests -q                        # 220 tests
NATIVE2PY_CPP_PARSER=regex .venv/bin/python -m pytest -q   # the fallback C++ backend
```

Per-service suites (generated smoke tests plus the golden check) run with
`native2py test <name>`.

Beyond unit tests, both language paths are verified by actually building them:
generated CMake fed to real `cmake`/`clang++`/`gfortran`, the extension
imported, the FastAPI service exercised over HTTP. Several real bugs were
found that way and not by inspection — f2py's module nesting, a missing-`.cpp`
link failure that only surfaces at first import, and a golden harness that
replayed stateful F77 routines out of order.

## Status

`infrastructure/docker/` and `infrastructure/kubernetes/` are empty
directories: no CI/CD pipelines or K8s manifests are generated yet. The
generated services have no auth, no rate limiting and no exception handling,
and native code that segfaults takes the worker down with it.

Before running any of this for a workload that matters, read
[Is this production-ready?](tools/native2py/docs/production-readiness.md) —
an honest gap list against `design.md`'s own requirements, not a sales pitch.
