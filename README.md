# nativegate — automated modernization of legacy scientific computing

Decades-old engineering code — 1990s C++ over FORTRAN 77 — reachable from
Python as deployable services, without rewriting it and without hand-writing
bindings. Modernization here means the interface, the packaging and the
deployment; the numerics stay exactly where they are, and stay provably
unchanged.

This monorepo holds three things:

1. **[`tools/nativegate/`](tools/nativegate/)** — the generator. Point it at a
   header or a Fortran deck; it produces the pybind11/f2py bindings, the CMake
   build, an installable Python package, a FastAPI service, tests, a numerical
   regression baseline, and a Dockerfile.
2. **[`libraries/petro/`](libraries/petro/)** — a period-accurate legacy
   codebase (~4,800 lines, 1988–2004 vintage) that exists to stress the
   generator against the real thing rather than a toy header. It is the
   modernization target the whole repo is measured against.
3. **`services/`** — what came out the other side. The repo currently carries
   one committed service, `petro_api`, generated from the Fortran decks in
   `libraries/petro/`: an independently buildable wheel and container.

## Layout

```
tools/nativegate/     the generator + its docs and test suite
libraries/           native code shared by services (petro, common-cpp,
                     geometry, demo)
services/<name>/     one generated service per exposed API
gateways/<name>/     created on demand by `ngate gateway`
infrastructure/      docker/ and kubernetes/ (kubernetes/ holds the generated
                     petro_api manifest)
design.md            the 1,073-line specification all of this implements
```

Every command runs from this directory — `ngate` resolves
`services/<name>/` relative to your working directory.

## Start here

```bash
cd tools/nativegate && ./scripts/bootstrap.sh && cd -   # creates .venv, installs the CLI

tools/nativegate/.venv/bin/ngate quickstart libraries/geometry/geometry.hpp
tools/nativegate/.venv/bin/ngate build geometry
```

Then read [Getting started](tools/nativegate/docs/getting-started.md) for the
same path explained, or [the nativegate README](tools/nativegate/README.md) for
what the tool does and does not handle.

### Using the generator on your own project

You do not have to work inside this repo. nativegate installs like any pip
package and resolves `services/` against your current directory:

```bash
cd /path/to/your-project
python3 -m venv .venv
.venv/bin/pip install "nativegate[clang,build,test] @ git+https://github.com/ravikings/nativegate.git@<sha>#subdirectory=tools/nativegate"
.venv/bin/ngate init && .venv/bin/ngate quickstart src/yourlib.hpp
```

Pin the commit — this is a code generator, and an unpinned upgrade can change
the bindings under a service that was working. Details, including which extras
matter and why, are in
[Using it in another project](tools/nativegate/README.md#using-it-in-another-project).

## The CLI

One binary covers the whole path from legacy source to running container.
`ngate --help` is authoritative; this is the map.

| Stage | Command | What it does |
|---|---|---|
| Survey | `detect <path>` | Identify the language and dialect of a source tree |
| | `suggest <dir>` | Rank which files are worth exposing first |
| | `inspect <path>` | List the symbols a file offers, and what would be skipped |
| Scaffold | `init` | Create `services/` in the current directory |
| | `create-service <name>` | Empty service skeleton (`--language cpp\|fortran`) |
| | `expose <path>` | Print the `nativegate.yaml` stanza for a source |
| | `quickstart <source>` | detect → scaffold → generate in one step (`--build`) |
| Generate | `generate <name>` | Regenerate bindings, CMake, package, service, tests |
| | `build <name>` | Compile the extension |
| | `lock <name>` | Pin the service's Python dependencies with hashes |
| Verify | `golden record <name>` | Record the numbers the bindings currently return |
| | `verify <name>` | Fail if any of them moved (= `golden verify`) |
| | `golden show <name>` | Print the recorded baseline |
| | `test <name>` | Generated smoke tests plus the golden check |
| Ship | `serve <name>` | Run the service locally with uvicorn |
| | `docker <name>` | Generate (and optionally `--build`) the Dockerfile |
| | `k8s <name>` | Generate a Kubernetes manifest |
| | `gateway <name> --service ...` | Mount several services behind one app |
| | `clean <name>` | Remove generated build artifacts |

### Running the app

```bash
tools/nativegate/.venv/bin/ngate build petro_api
tools/nativegate/.venv/bin/pip install services/petro_api/dist/*.whl
tools/nativegate/.venv/bin/ngate serve petro_api   # 127.0.0.1:8000
curl localhost:8000/healthz
```

`serve` is a development convenience: one uvicorn process, bound to loopback,
against the installed wheel. It is not the deployment path. A generated
service is an ordinary FastAPI app (`<name>.service:app`) in an ordinary
wheel, so anything that already runs your Python services can run it — and
the generated Dockerfile does exactly that, with gunicorn and uvicorn
workers:

```bash
tools/nativegate/.venv/bin/ngate docker petro_api --build
docker run -p 8000:8000 petro_api:latest
```

## The service

| Service | Language | From | What it exercises |
|---|---|---|---|
| `petro_api` | Fortran | an F90 facade over 7 fixed-form F77 decks | INCLUDE expansion, COMMON-block state, inferred intent, module nesting, array arguments, 10 exposed routines |

Regenerate it at any time — the bindings, CMake, Python package and tests are
all generated:

```bash
tools/nativegate/.venv/bin/ngate generate petro_api
```

The other directories under `libraries/` (`geometry`, `demo`, `common-cpp`)
are inputs used by the docs and the test suite; no service is committed for
them, so the C++ examples in the guides scaffold one as they go.

What is **not** regenerable is the code you wrote: `native/`,
`nativegate.yaml`, and the recorded answers in `golden.json`. See
[what is generated, what is yours](tools/nativegate/docs/architecture.md#what-is-generated-what-is-yours).

## The legacy library

`libraries/petro/` is the point of the whole exercise: black-oil PVT
correlations, Corey/Stone relative permeability, Beggs & Brill wellbore
hydraulics, Peng-Robinson flash, an IMPES simulator core, Peaceman well
indices — with `IMPLICIT` typing, COMMON blocks, INCLUDE decks, macro-mangled
`extern "C"` bridges and a pre-standard C++ layer on top. It compiles and
returns physically sensible numbers on its own, before nativegate touches it.

[`libraries/petro/FINDINGS.md`](libraries/petro/FINDINGS.md) records what
broke when the generator was first pointed at it, and what was fixed —
including a correction to its own first draft, which had blamed fixed-form
parsing for what turned out to be a measurement error in the probe.

## Proving the answers did not change

This is the part that makes automated modernization defensible rather than
merely fast. For a re-host, "it builds and imports" is not the acceptance
criterion. Every
service can pin what its bindings return:

```bash
tools/nativegate/.venv/bin/ngate golden record petro_api  # -> services/petro_api/golden.json
tools/nativegate/.venv/bin/ngate golden verify petro_api
```

`services/petro_api/golden.json` is a worked example: the fluid configured at
35 °API, 0.65 gas gravity and 180 °F, then the 1988 correlations evaluated at
2000 psia, giving Rs = 355.076 scf/stb and Bo = 1.23766 — values a reservoir
engineer can check by hand. It pins 10 entry points, and records the compiler
versions and the SHA-256 of every native source it was recorded from. Commit
the file, and any rebuild that moves a number fails.
See [Numerical regression](tools/nativegate/docs/golden-values.md).

## Serving several services together

```bash
tools/nativegate/.venv/bin/ngate gateway platform-api --service petro_api
uvicorn platform_api.app:app
```

Each service keeps its own wheel, version and build; the gateway just mounts
their routers under `/<service>`. The alternative — one image per service
behind an ingress — needs no generated code at all. Both are covered in
[Deployment topologies](tools/nativegate/docs/deployment-topologies.md).

## Tests

```bash
cd tools/nativegate
.venv/bin/python -m pytest tests -q                        # green with or without [fparser]
NATIVEGATE_CPP_PARSER=regex .venv/bin/python -m pytest -q   # the fallback C++ backend
```

Per-service suites (generated smoke tests plus the golden check) run with
`ngate test <name>`.

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
SIGTERM draining, and generated Kubernetes manifests (`ngate k8s`, output
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
[Is this production-ready?](tools/nativegate/docs/production-readiness.md) and
[the defect list](tools/nativegate/DEFECTS.md) — honest gap lists against
`design.md`'s own requirements, not a sales pitch.
