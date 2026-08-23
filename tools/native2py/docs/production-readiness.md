# Is this production-ready?

**Short answer: no, not as-is.** What's here is a verified MVP — real
compiles, real containers, real HTTP round-trips — but "the pipeline works"
and "this is safe to run a paying workload on" are different bars. This page
is an honest gap list against design.md's own production requirements
(sections 19–23), so you can decide what to add before shipping.

Everything below was checked against the actual generated code, not the
aspirational spec — see [Architecture](architecture.md#verified-not-just-tested)
for what "verified" means elsewhere in these docs.

## What's actually solid

- **The compile-and-serve path is real.** Both C++ (real
  `clang++`/CMake/pybind11 build) and Fortran (real `gfortran`/f2py build)
  produce a working extension, a working Python package, a working FastAPI
  service, and a Docker image that runs as a non-root user and reports
  `healthy` on a real container healthcheck.
- **It handles genuinely old code.** `libraries/petro` — ~2,000 lines of
  1988–1997 fixed-form F77 with INCLUDE files, IMPLICIT typing and COMMON
  blocks — binds and returns numerically correct PVT results. See
  [Exposing Fortran](fortran-guide.md#verified-against-real-code).
- **Type mapping fails loudly, not silently.** An unsupported native type
  raises `NativeTypeError` at generate time rather than producing an unsafe
  binding — see [Architecture](architecture.md).
- **The Fortran `module`-nesting bug** and **the missing-`.cpp`-implementation
  bug** were both found by actually compiling and running the generated
  code, not by inspection — see [Troubleshooting](troubleshooting.md). That's
  some evidence the "verify by really building it" discipline catches real
  bugs; it's not evidence there are no more.

## Concrete gaps

### Security (design.md §21)

| Requirement | Status |
|---|---|
| Run containers as non-root | ✅ fixed — `USER appuser` in the generated Dockerfile |
| Minimal runtime image | ✅ multi-stage build, compiler toolchain stays in the builder stage |
| Pin compiler/toolchain versions | ❌ `python:3.12-slim` is a moving tag, not a digest pin |
| Scan dependencies / generate SBOMs | ❌ not implemented — no `pip-audit`/`syft` step anywhere |
| Sign container images | ❌ not implemented |
| Restrict filesystem access | ❌ Dockerfile doesn't set `--read-only` / drop capabilities |
| Never compile untrusted source in prod | ⚠️ your responsibility — native2py will happily compile and expose anything you point it at; there's no sandboxing |

### Observability (design.md §22)

| Requirement | Status |
|---|---|
| Structured logging | ❌ none — generated `service.py` has no logging at all |
| OpenTelemetry traces/metrics/logs | ❌ not implemented |
| Separate native execution time from HTTP overhead | ❌ not implemented |
| Health endpoint | ⚠️ generated services expose `/healthz`, and importing the package proves the native extension loaded — but it is liveness only. There is no readiness check, and nothing verifies the extension still *computes correctly* (e.g. after a COMMON-block corruption in stateful Fortran) |

### Error handling — the sharpest edge

The generated endpoint has **no exception handling**:

```python
@app.post("/add")
def add(a: float, b: float):
    return {"result": calculator.add(a, b)}
```

A Python exception from the native call becomes an unhandled 500 with a
raw traceback in the response — an information leak, not just a UX rough
edge. Worse: **native code that segfaults, aborts, or hangs takes down the
whole worker process** (or the whole process, if you run one worker), not
just the one request. FastAPI/Python can't catch a SIGSEGV. If your native
function isn't provably memory-safe for arbitrary input, this is the single
biggest risk in taking this to production as generated. No sandboxing,
timeout, or process-per-call isolation exists here.

### API surface

- No auth, no rate limiting, no CORS configuration.
- No request validation beyond FastAPI's default type coercion — a
  malformed body/query param gets FastAPI's generic 422, not anything
  tailored to the native function's actual preconditions.
- ✅ fixed — an instance is now constructed **per request**, and a class
  whose constructor takes arguments gets them from the request rather than
  crashing the service at import. Structs cross the boundary through
  generated Pydantic models; a signature that cannot be serialised (a bound
  class as a parameter or return) gets no endpoint and a comment saying why,
  instead of failing route registration for the whole service.
- ✅ fixed — generated Fortran routers serialise every native call through a
  process-wide lock, so concurrent requests can no longer interleave inside a
  routine that reads and writes a COMMON block. See
  [Concurrency and Fortran COMMON blocks](#concurrency-and-fortran-common-blocks)
  for what that does *not* cover.
- Still open: constructing a fresh native object per request is correct but
  not free. A class that is expensive to build (a simulator reading a deck)
  needs an explicit session/handle API rather than one object per HTTP call.

### Concurrency and Fortran COMMON blocks

**Read this before touching anything about threading or the GIL.** Getting the
order of the two changes below wrong is worse than doing neither.

#### The hazard

Fortran COMMON blocks are process-global storage. `libraries/petro`'s
`PETRO.INC` declares

```fortran
COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
```

and states the contract in a comment: *"CALL PVTSET(P) THEN USE THE COMMON. DO
NOT ADD ARGUMENTS."* `PVTINI` writes it; `PVTRS`, `PVTBO` and `PVTVIS` read it.

native2py exposes those faithfully, as independent endpoints — `/pvt_set_fluid`
writes the state, `/solution_gor` reads it. FastAPI runs synchronous `def`
endpoints in a threadpool, so two callers configuring different fluids can
interleave. Nothing crashes and nothing logs; each caller just receives numbers
computed from the other caller's fluid.

Per-request objects — the fix that works for a stateful C++ class — cannot
reach this. COMMON belongs to the process, not to any instance.

#### What ships today: a process-wide lock

Generated **Fortran** routers declare

```python
_NATIVE_LOCK = threading.RLock()
```

and hold it across every native call. It is `RLock` so that one generated
endpoint calling another cannot deadlock a request against itself, and it is
held around the call only — argument marshalling and JSON shaping stay outside
the critical section.

This is the conservative option on purpose: correct by construction, and it
caps native execution at **one concurrent call per process**. Scale with more
processes/replicas, not by deleting the lock.

C++ routers get no lock. They already construct an instance per request, and a
C++ free function has no equivalent of COMMON that the parser can see — locking
every C++ service would trade real concurrency for a hazard that isn't there.
If you bind C++ that uses file-scope statics or a singleton, that is your
hazard to assess; native2py cannot detect it.

#### What the lock does not fix

The lock is held for **one native call**. A client that POSTs `/pvt_set_fluid`
and then POSTs `/solution_gor` releases it in between, so another client can
reconfigure COMMON in the gap. Closing that needs one of:

- **process affinity per session** — pin a session to one worker process, so
  its configure and its read reach the same COMMON;
- **detecting the write→read COMMON dependency at parse time** and generating a
  session-shaped (or combined configure+compute) API instead of independent
  endpoints.

Both are out of scope for the lock and neither is implemented.
`tests/test_service_gen.py::test_the_lock_does_not_make_a_configure_then_compute_pair_atomic`
pins this gap so nobody reads the lock as a complete fix.

#### The sequencing constraint — COMMON safety before GIL release

The pybind11 bindings never release the GIL (no
`py::call_guard<py::gil_scoped_release>`), and the f2py path emits no
`threadsafe` directive. Every native call therefore holds the GIL for its full
duration.

That is a throughput ceiling. It is **also** the reason the COMMON race is
silent rather than a crash: the GIL prevents two threads from being inside
Fortran simultaneously, so you never get memory corruption. The race is *across*
calls, and the GIL is released between them.

So: if someone profiles this, finds the GIL bottleneck, and adds
`gil_scoped_release` (or a `threadsafe` f2py directive) to fix throughput
**without first making COMMON access safe, they convert a silent logical race
into genuine memory corruption** — two threads writing the same COMMON storage
at the same time.

**COMMON safety must land before GIL release.** In order:

1. the per-call lock (done);
2. session affinity or a session-shaped API for write→read COMMON pairs (not
   done);
3. only then, releasing the GIL for throughput — and at that point the lock
   above becomes load-bearing against memory corruption, not just wrong
   numbers, so it must not be removed in the same change.

### CI/CD, versioning, deployment (design.md §19, §20, §23)

- ✅ **Numerical regression is covered.** `native2py golden record|verify`
  pins what every bound entry point returns for a fixed set of inputs, and the
  generated `tests/test_golden.py` replays it in the service's own suite. See
  [Numerical regression](golden-values.md). This is the check that matters for
  a re-host: "the answers did not change".
- No CI/CD templates — design.md's pipeline (lint → compile → test →
  wheel → Docker → scan → publish) isn't generated by `native2py` anywhere.
- No Kubernetes manifests (design.md §4 mentions
  `infrastructure/kubernetes/`; `native2py init` creates the empty
  directory and nothing else).
- No API versioning strategy beyond the `pyproject.toml` version string —
  a breaking native signature change doesn't fail a build or a compat
  check, it just quietly changes behavior on next `generate` + `build`.
- No service registry, no independent-deployability tooling beyond "each
  service is its own directory."

### Parser/type coverage (also see [Exposing C++](cpp-guide.md), [Exposing Fortran](fortran-guide.md))

- C++: overloads, public inheritance, typedefs, enums, macros and `#include`d
  types are handled since the move to the Clang AST parser. Still unsupported:
  templates and `std::vector`, and raw numeric pointers (`double*`) carry no
  length so they're refused — which means real numerical C++ APIs built on raw
  arrays aren't expressible yet. See
  [Exposing C++](cpp-guide.md#what-the-parser-supports).
- No C language support at all, despite being in the project name/design.md
  scope.
- Fortran: derived types aren't parsed. F77 argument direction is *inferred*
  by f2py from the routine body, not declared — usually right, but worth
  checking against the routine's own docs before trusting it. See
  [output parameters](fortran-guide.md#output-parameters-f77-has-no-intent).
- C++ is parsed by Clang itself, so what the compiler sees is what native2py
  binds; a header that fails to parse is refused outright rather than
  half-bound. Two caveats remain: the parse needs the same include paths and
  `-D` flags as the build (`clang:` in `native2py.yaml`), and on a machine
  without libclang the front end silently falls back to the regex reader —
  set `parser: clang` to make that an error.
- The Fortran parser is still regex-based, not a compiler frontend.
  Declarations it recognises but can't bind are reported with a reason rather
  than dropped silently, but syntax it doesn't match at all can slip past
  unreported.

## What "valid" means today

Treat this as validated for: **prototyping the migration path** — proving a
specific native function can be reached from Python with correct results,
in a container, without hand-writing bindings. That's a real, load-bearing
capability for the incremental-migration story in design.md §23.

Treat this as **not yet validated** for: internet-facing production traffic,
untrusted input, multi-tenant workloads, or anything where a crashed worker
or a leaked stack trace has real consequences. Closing that gap means, at
minimum: exception handling + input validation in generated endpoints,
process isolation or a memory-safety argument for the native code itself,
and the CI/security/observability tooling design.md §19–22 already
specifies but nothing here generates yet.
