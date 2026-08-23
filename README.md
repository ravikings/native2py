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
3. **`services/`** — what came out the other side. The repo currently carries
   one committed service, `petro_api`, generated from the Fortran decks in
   `libraries/petro/`: an independently buildable wheel and container.

## Layout

```
tools/native2py/     the generator + its docs and test suite
libraries/           native code shared by services (petro, common-cpp,
                     geometry, demo)
services/<name>/     one generated service per exposed API
gateways/<name>/     created on demand by `native2py gateway`
infrastructure/      docker/ and kubernetes/ (kubernetes/ holds the generated
                     petro_api manifest)
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

## The service

| Service | Language | From | What it exercises |
|---|---|---|---|
| `petro_api` | Fortran | an F90 facade over 7 fixed-form F77 decks | INCLUDE expansion, COMMON-block state, inferred intent, module nesting, array arguments, 10 exposed routines |

Regenerate it at any time — the bindings, CMake, Python package and tests are
all generated:

```bash
tools/native2py/.venv/bin/native2py generate petro_api
```

The other directories under `libraries/` (`geometry`, `demo`, `common-cpp`)
are inputs used by the docs and the test suite; no service is committed for
them, so the C++ examples in the guides scaffold one as they go.

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
tools/native2py/.venv/bin/native2py golden record petro_api  # -> services/petro_api/golden.json
tools/native2py/.venv/bin/native2py golden verify petro_api
```

`services/petro_api/golden.json` is a worked example: the fluid configured at
35 °API, 0.65 gas gravity and 180 °F, then the 1988 correlations evaluated at
2000 psia, giving Rs = 355.076 scf/stb and Bo = 1.23766 — values a reservoir
engineer can check by hand. It pins 10 entry points, and records the compiler
versions and the SHA-256 of every native source it was recorded from. Commit
the file, and any rebuild that moves a number fails.
See [Numerical regression](tools/native2py/docs/golden-values.md).

## Serving several services together

```bash
tools/native2py/.venv/bin/native2py gateway platform-api --service petro_api
uvicorn platform_api.app:app
```

Each service keeps its own wheel, version and build; the gateway just mounts
their routers under `/<service>`. The alternative — one image per service
behind an ingress — needs no generated code at all. Both are covered in
[Deployment topologies](tools/native2py/docs/deployment-topologies.md).

## Tests

```bash
cd tools/native2py
.venv/bin/python -m pytest tests -q                        # green with or without [fparser]
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

**Where this is usable today: internal, single-tenant-per-service deployments,
where a team is calling legacy code it already trusts. Not internet-facing,
and not multi-tenant.**

What has landed since the first cut: generated services now carry optional API
key auth (`api.auth: api_key`), rate limiting, request size limits, request
IDs and access logging, an exception handler, `/healthz` and `/readyz` with
SIGTERM draining, and generated Kubernetes manifests (`native2py k8s`, output
in `infrastructure/kubernetes/`). `infrastructure/docker/` is still an empty
skeleton and no CI/CD pipelines are generated.

Three limits are structural rather than unfinished work, and are worth knowing
before you size anything:

- **Fortran services run one native call at a time, per process.** Every
  generated Fortran endpoint takes a single process-wide lock before entering
  native code, because COMMON blocks are process-global storage — two requests
  configuring different fluids would otherwise interleave writes into the same
  COMMON block and each caller would get numbers computed from the other's
  inputs, with nothing raised and nothing logged. This is deliberate (see
  `services/petro_api/python/petro_api/router.py`). Concurrency for a Fortran
  service comes from running more processes, not more threads. C++ services
  are not locked: they get a fresh instance per request instead. A C++
  library that keeps file-scope static state is *not* protected, and the
  parser cannot see that it does — that one is on you.
- **The API for a stateful library is session-shaped** — "configure, then
  read" — which is safe when one tenant owns the process and unsafe when it
  does not.
- **A segfault in native code still takes the worker down.** There is no
  sandbox, no per-call timeout and no per-call isolation.

Before running any of this for a workload that matters, read
[Is this production-ready?](tools/native2py/docs/production-readiness.md) and
[the defect list](tools/native2py/DEFECTS.md) — honest gap lists against
`design.md`'s own requirements, not a sales pitch.
