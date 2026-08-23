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
| Pin compiler/toolchain versions | ✅ fixed — the generated Dockerfile digest-pins its base image for both stages, and the tool's own install is hash-pinned via `constraints.txt`. Still open: the generated service's `apt-get`/`pip`/system-gfortran steps are not reproducible — see [Deployment topologies](deployment-topologies.md) |
| Generate SBOMs | ✅ fixed — a deterministic CycloneDX SBOM is generated, recording native sources by SHA-256 |
| Scan dependencies | ✅ fixed — `pip-audit` runs in CI against the hash-pinned constraints |
| Sign container images | ❌ not implemented |
| Restrict filesystem access | ✅ fixed — generated Kubernetes manifests set `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, `capabilities: drop [ALL]`, `runAsNonRoot` and `seccompProfile: RuntimeDefault`, with an emptyDir for `/tmp` so a read-only root can still start |
| Never compile untrusted source in prod | ⚠️ your responsibility — native2py will happily compile and expose anything you point it at; there's no sandboxing |

### Observability (design.md §22)

| Requirement | Status |
|---|---|
| Structured logging | ✅ fixed — one access-log line per request on `native2py.<service>.access` with structured `request_id` / `method` / `path` / `status` / `duration_ms`, plus a correlatable `error_id` on failures |
| OpenTelemetry traces/metrics/logs | ❌ not implemented |
| Separate native execution time from HTTP overhead | ⚠️ partial — total request duration is measured and returned as `X-Response-Time-Ms`, but it is not split between the native call and HTTP/marshalling overhead |
| Health endpoint | ✅ `/healthz` liveness and a separate `/readyz` that reports 503 while draining on SIGTERM (see [Readiness and draining](#readiness-and-draining)). Still true: nothing verifies the extension *computes correctly* — e.g. after COMMON-block corruption in stateful Fortran |

### Error handling — the sharpest edge

**Correction (2026-08-22).** This page previously said an uncaught exception
returned "a raw traceback in the response — an information leak". **That was
wrong.** It was checked by building the generated app and making a real
request: with `debug=False`, which is FastAPI's default and what the generated
app uses, Starlette answers exactly `Internal Server Error` as `text/plain`.
No traceback, no exception message, no paths. There was no leak.

✅ **What did change:** generated apps now register an unhandled-exception
handler, emitted from one place (`generators/error_gen.py`) into both the
standalone `service.py` and the composed gateway app — a handler attaches to
an *app*, and a router mounted into a gateway runs under the gateway's app, so
installing it in only one place would miss the topology more likely to face
the internet. It gives a failure a JSON body and an `error_id` that also
appears in the log line carrying the traceback, under a `native2py.<service>`
logger. `HTTPException` is unaffected, so the generated argument-validation
422s keep their own status and detail.

That is a traceability and consistency fix, not a security fix. Two limits,
both measured and pinned by tests in `tests/test_gateway_gen.py`:

- **`debug=True` does leak the full traceback, and the handler cannot stop
  it** — Starlette consults `debug` *before* the handler, so the debug
  response wins. Do not enable debug on a deployed service.
- **Python-level failures only.**

Which leaves the real hazard: **native code that segfaults, aborts, or hangs**
raises no Python exception at all. FastAPI can't catch a SIGSEGV, and no
`except` clause will ever see one. That is addressed at the process level
instead — see the next section.

### Crash containment — the topology decision

**Decided (2026-08-23): a supervised worker pool. Not process-per-call.**

Generated images no longer run a bare `uvicorn`. They run **gunicorn**
supervising `uvicorn-worker` processes:

```dockerfile
CMD ["gunicorn", "svc.service:app", \
     "--worker-class", "uvicorn_worker.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "60", "--graceful-timeout", "30", \
     "--max-requests", "1000", "--max-requests-jitter", "100"]
```

**Verified against a real gunicorn, not asserted from the text.** A genuine
`SIGSEGV` (`ctypes.string_at(0)`) killed a worker; gunicorn logged
`Worker (pid:20372) was sent SIGSEGV!`, booted a replacement, and the service
answered the next request without interruption. A request that slept for 600s
under `--timeout 5` was reaped and the service stayed up.

What each flag is actually for:

| Flag | Failure mode it covers |
|---|---|
| supervision (gunicorn master) | segfault, `abort()`, Fortran `STOP` — worker is reaped and respawned; the master holds the listening socket so the service never stops accepting |
| `--timeout 60` | **hangs** — the only mechanism here that can address them. A worker stuck inside native code never returns to Python, so nothing in-process can time it out |
| `--max-requests` | native code that leaks rather than crashes — workers are recycled before it accumulates |

**What this does not do: per-request isolation.** Requests sharing the crashed
worker die with it. Containing a crash to *only* the request that caused it
needs process-per-call, which puts marshalling cost on every request. That is
a deliberate second tier and it is **not implemented**. If your native code
takes untrusted input, or you can't argue it's memory-safe for arbitrary
input, you need that tier and this isn't enough.

**Why one worker is the Fortran default.** COMMON blocks are per-*process*
storage. With one worker, a client's `/pvt_set_fluid` and its follow-up
`/solution_gor` at least reach the same COMMON. With several, they can land on
different processes and the compute reads a COMMON that was never configured —
turning the interleaving race described below into a plainly wrong answer.
`ENV WEB_CONCURRENCY=1` for Fortran, `2` for C++ (per-request instances, no
process-global state). gunicorn reads `WEB_CONCURRENCY` itself, so raising it
is an env change, not an image rebuild — but raise it only for routines that
keep no state between calls.

The honest cost of that default: with a single worker there is a sub-second
gap during respawn where the service has no worker. With two or more, the
survivor serves immediately and the crash is invisible to other callers. For
stateful Fortran, correctness wins that trade; for C++ it doesn't have to.

### API surface

- ✅ fixed — **authentication, rate limiting, body limits, request ids and
  access logging** are generated into every service as `middleware.py`, and
  installed by both the standalone app and the gateway. See
  [Securing the generated API](#securing-the-generated-api) below.
- Still missing: CORS configuration, and any notion of per-caller
  authorization (a valid key can call every endpoint).
- ✅ fixed — array parameters carry a `MAX_ARRAY_ITEMS` cap
  (`NATIVE2PY_MAX_ARRAY_ITEMS`, default 65536) instead of accepting a body of
  any length, and where a size argument can be paired unambiguously with the
  array it sizes, a generated guard rejects `n != len(props)` with a 422
  before it reaches Fortran and indexes past the end of the buffer. The
  pairing is inferred conservatively: an ambiguous case yields no guard,
  because a false pairing would reject valid calls.
- Beyond those, no request validation past FastAPI's default type coercion —
  a malformed body/query param gets FastAPI's generic 422, not anything
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

### Securing the generated API

Generated services carry a `middleware.py`, installed by both `service.py` and
the gateway app. Every layer below was verified against a real app making real
requests, not asserted on the generated text — see
`tests/test_middleware_gen.py`.

#### Authentication

Set it in `native2py.yaml`:

```yaml
api:
  auth: api_key        # or: none
```

Keys are read at startup from `NATIVE2PY_API_KEYS` (comma-separated), and
accepted as either `X-API-Key: <key>` or `Authorization: Bearer <key>`.

Three decisions worth knowing about, because each was the alternative to a
worse default:

- **Keys never live in `native2py.yaml`.** That file is committed. A
  credential in it is a credential in every clone and every image layer.
- **It fails closed.** `auth: api_key` with no keys in the environment makes
  the service **refuse to start**. A misconfiguration that silently disables
  authentication is found by an attacker; one that refuses to boot is found by
  whoever deployed it.
- **The mode is baked in at generate time**, not read from the environment. A
  service generated to require authentication cannot be downgraded by the
  environment it happens to start in. An unrecognised mode (`apikey`, a
  plausible typo) is a config error rather than a silent fall back to `none`.

`auth: none` remains valid for a genuinely internal service, but it logs a
warning at every startup naming what it means.

`/healthz` and `/readyz` never require a key — an orchestrator has no
credentials, and a liveness probe that 401s is a service that looks dead and
gets restarted forever.

#### Limits

| Env var | Default | What it does |
|---|---|---|
| `NATIVE2PY_RATE_LIMIT_PER_MINUTE` | `0` (off) | Per-client sliding window, keyed by API key when there is one and client address otherwise |
| `NATIVE2PY_MAX_REQUEST_BYTES` | `1048576` | Rejects an oversized body with 413 *before* it is read into memory |

**The rate limiter is per process, and that matters.** Generated images run
several gunicorn workers, so a limit of N is really N x `WEB_CONCURRENCY`
across the service, and it resets when a worker is recycled. It is a crude
backstop against one client melting one worker — not a quota system. Real
global rate limiting belongs at the ingress, where there is one place to
count. An in-process counter presented as a global limit is the kind of
control that is trusted right up until the day it matters.

#### Request identity and access logs

Every response carries `X-Request-ID` and `X-Response-Time-Ms`. An inbound
`X-Request-ID` is honoured so a trace survives the hop from a gateway or
service mesh — but it is bounded to 64 printable ASCII characters first,
because the value is attacker-controlled and ends up in log lines, where an
unbounded one is log injection.

Access logs go to `native2py.<service>.access` with structured `extra` fields
(`request_id`, `method`, `path`, `status`, `duration_ms`), so a JSON formatter
picks them up while the default formatter still prints something readable.

#### Readiness and draining

`/readyz` is separate from `/healthz`, and not for the obvious reason. "Has the
app started?" is worth nothing here: `service.py` imports `router.py`, which
imports the native extension at module scope, so any response at all is proof
the extension loaded. A started/not-started probe would just be a slower
liveness check.

What readiness actually buys is **draining**. On SIGTERM — a rolling deploy or
a scale-down — `/readyz` starts reporting 503 while the service keeps serving
in-flight work. That takes the worker out of the load balancer *before* it
stops accepting, which is the difference between a clean deploy and a burst of
502s. The SIGTERM handler is chained rather than replaced, because gunicorn and
uvicorn install their own to run graceful shutdown and clobbering one would
turn a graceful drain into an abrupt exit.

Liveness deliberately does not do this: a draining process is not ready but is
perfectly alive, and a liveness probe failing here would get it killed
mid-request instead of drained.

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
  The repo's own CI does run tests across {ubuntu, macOS} × {3.10, 3.11, 3.12}
  plus a `pip-audit` job, but nothing generates a pipeline for *your* service.
- ✅ fixed — **`native2py k8s <name>`** generates a Deployment and Service with
  the security context above, resource requests, and probes wired the only way
  round that works: readiness → `/readyz` (which drains on SIGTERM) and
  liveness → `/healthz` (which does not). Swapping them either kills a draining
  pod mid-request or keeps routing to one that is shutting down. Validated
  against the official Kubernetes API models, not just parsed as YAML.
- No API versioning strategy beyond the `pyproject.toml` version string —
  a breaking native signature change doesn't fail a build or a compat
  check, it just quietly changes behavior on next `generate` + `build`.
- No service registry, no independent-deployability tooling beyond "each
  service is its own directory."

### Parser/type coverage (also see [Exposing C++](cpp-guide.md), [Exposing Fortran](fortran-guide.md))

- C++: overloads, public inheritance, typedefs, enums, macros, `#include`d
  types and **`std::vector<T>`** are handled. Still unsupported: templates, and
  raw numeric pointers (`double*`) carry no length so they're refused — a
  numerical C++ API built on bare pointers still needs a wrapper that carries
  its own size. A non-const `std::vector<T>&` is refused on purpose: pybind11
  silently discards writes through it (see [Exposing C++](cpp-guide.md)). See
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
- The Fortran front end now has two backends behind one seam: the original
  regex reader (the default) and a real fparser2 parse tree, selectable with
  `NATIVE2PY_FORTRAN_PARSER=fparser2`. `auto` stays on the regex reader until
  the IR-parity harness (`tests/corpus/harness.py`) is green across the whole
  corpus. Under the regex reader, declarations it recognises but can't bind
  are reported with a reason rather than dropped silently, but syntax it
  doesn't match at all can slip past unreported.

## Enterprise readiness — the prioritized gap list

An assessment (2026-08-22) of everything above, ordered by what would actually
stop an enterprise deployment. Numbered cross-references (2.2, 1.4, …) are `ROADMAP.md` work items in the
repository root — not part of this docs site.

**Blockers — would stop a deployment today**

1. **Crash containment (2.2).** A segfault, Fortran `STOP`, or out-of-bounds
   write kills the whole worker process, and no exception handler can ever
   see it. Needs a supervised worker pool with request-level isolation; a
   topology decision, not just codegen. This is the one that should decide
   whether you deploy.
2. **No auth, rate limiting, or request-size limits (2.4).** Array *element*
   counts are now capped and size arguments guarded (2.3, done), but nothing
   authenticates a caller or bounds a request rate.
3. **The COMMON-block session race (2.1b).** Configure-then-compute pairs
   still race across requests — silent wrong numbers under concurrent
   multi-tenant load.
4. **`SECURITY.md` contact is a placeholder.** Enabling GitHub private
   vulnerability reporting closes this in minutes; it is table stakes for
   vendor security review.

**Major — needed to operate at scale**

5. **Observability: barely started.** Failures now log a correlatable
   `error_id`; there is still no request logging on the successful path, no
   request id, no OpenTelemetry, no native-vs-HTTP timing split, and no
   readiness probe distinct from `/healthz` liveness.
6. **No CI/CD or Kubernetes generation**, no image signing, no
   dependency-vulnerability scan step.
7. **Parser coverage ceilings.** Fortran: statement functions, COMMON
   outputs, preprocessed `.F90`, `ENTRY` — all await the fparser2 backend
   becoming the default (1.4); the backend and its parity suite exist and
   pass, but `auto` still resolves to the regex reader. C++: no
   templates/`std::vector`, raw pointers refused. No C support at all.
8. **No versioning/compat story for the generated API.** `ir.json` now
   carries `schema_version`, but a breaking native signature change still
   silently changes API behaviour on the next generate + build rather than
   failing a compatibility check. No releases, no published wheels, no
   changelog.

Repo hygiene checks out: no build artifacts, caches, virtualenvs, or binary
wheels are tracked in git — local clutter of that kind is `.gitignore`d.

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
