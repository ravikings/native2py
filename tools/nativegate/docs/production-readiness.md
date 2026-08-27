# Is this production-ready?

## The verdict, up front

**Deployable today for internal, single-tenant-per-service use** — a team
calling its own trusted legacy code, on a network where everything that can
reach the port is already entitled to call it. That case is covered: the image
builds reproducibly, runs non-root, is supervised so a segfault costs one
worker rather than the service, and the generated Fortran router is correct
under concurrency (see [Concurrency](#concurrency-and-fortran-common-blocks)).

**Not deployable for anything internet-facing or multi-tenant.** Two specific
blockers, not a general unease:

1. **No per-request isolation.** A segfault, `abort()` or Fortran `STOP` takes
   the whole worker process down, killing every request sharing it
   (`service.py:51-54` says this in the code itself). Untrusted input reaching
   native code needs process-per-call, which is not implemented.
2. **The API is session-shaped.** `/pvt_set_fluid` configures process-global
   Fortran COMMON storage and a later `/solution_gor` reads it back. Two
   tenants sharing a deployment interleave in that same storage and each gets
   the other's numbers, silently. The process-wide lock makes a *single call*
   safe; it cannot make a configure-then-read *pair* safe. See
   [What the lock does not fix](#what-the-lock-does-not-fix).

Everything below is the detail behind that verdict.

What's here is a verified MVP — real compiles, real containers, real HTTP
round-trips — but "the pipeline works" and "this is safe to run a paying
workload on" are different bars. This page is an honest gap list against
design.md's own production requirements (sections 19–23), so you can decide
what to add before shipping.

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
| Pin compiler/toolchain versions | ✅ fixed — base image digest-pinned, apt packages version-pinned **including the concrete `gfortran-14`** (the metapackage alone left the compiler free to move), the service's Python dependencies hash-locked with `--require-hashes`, and the wheel made byte-reproducible with `SOURCE_DATE_EPOCH`. Verified by building three times: identical packages, byte-identical `.so` and wheel, and golden values reproduced exactly. See [Deployment topologies](deployment-topologies.md#reproducibility-the-five-pins) |
| Generate SBOMs | ✅ fixed — a deterministic CycloneDX SBOM is generated, recording native sources by SHA-256 |
| Scan dependencies | ✅ fixed — `pip-audit` runs in CI against the hash-pinned constraints |
| Sign container images | ❌ not implemented |
| Restrict filesystem access | ✅ fixed — generated Kubernetes manifests set `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, `capabilities: drop [ALL]`, `runAsNonRoot` and `seccompProfile: RuntimeDefault`, with an emptyDir for `/tmp` so a read-only root can still start |
| Never compile untrusted source in prod | ⚠️ your responsibility — nativegate will happily compile and expose anything you point it at; there's no sandboxing |

### Observability (design.md §22)

| Requirement | Status |
|---|---|
| Structured logging | ✅ fixed — one access-log line per request on `nativegate.<service>.access` with structured `request_id` / `method` / `path` / `status` / `duration_ms`, plus a correlatable `error_id` on failures |
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
appears in the log line carrying the traceback, under a `nativegate.<service>`
logger. `HTTPException` is unaffected, so the generated argument-validation
422s keep their own status and detail.

That is a traceability and consistency fix, not a security fix. Two limits,
both measured and pinned by tests in `tests/test_gateway_gen.py`:

- **`debug=True` does leak the full traceback, and the handler cannot stop
  it** — Starlette consults `debug` *before* the handler, so the debug
  response bypasses the generated handler entirely. Do not enable debug on a
  deployed service.
- **Python-level failures only.**

**`NATIVEGATE_DEBUG_ERRORS` must stay off in production.** It is off by default
and anything other than unset/`0`/`false`/`no` turns it on
(`service.py:38`). When on, the 500 body gains a `detail` field carrying
`type(exc).__name__: exc` (`service.py:63-64`) — and a native routine's
exception text can carry deck values, array contents, or absolute paths from
the build machine. None of that should leave the process because something
failed. Turn it on deliberately, in a non-production environment, when a caller
needs to see why; the traceback is in the log either way, keyed by the same
`error_id` the caller was given.

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

#### What operators should actually do about a worker dying

A segfault, an `abort()`, a Fortran `STOP` or a hang kills the worker process
**before any Python exception handler runs** — the generated handler says so
itself at `services/petro_api/python/petro_api/service.py:51-54`. So the
response to it is entirely at the process and orchestrator level:

- **Keep the supervisor.** gunicorn's master holds the listening socket and
  respawns a killed worker, which is why the service keeps accepting during a
  crash. Running the app under a bare `uvicorn` removes this.
- **Keep `--timeout 60`.** It is the only mechanism here that covers a *hang*:
  a worker stuck inside native code never returns to Python, so nothing
  in-process can time it out. If your routines legitimately run longer than
  60s, raise it deliberately — do not remove it.
- **Expect the crashed request to be lost.** There is no retry and no partial
  result; the caller sees a dropped connection, not a 500.
- **Liveness and readiness are split for this reason, and must not be
  swapped.** `/healthz` (liveness) only asserts the process is up and the
  extension imported (`service.py:22-29`). `/readyz` (readiness) reports 503
  from the moment SIGTERM lands (`middleware.py:300-304`), so a draining pod
  leaves the load balancer *before* it stops accepting. Pointing liveness at
  `/readyz` would get a draining pod killed mid-request; pointing readiness at
  `/healthz` would keep routing to a pod that is shutting down. The generated
  manifest wires them the working way round
  (`infrastructure/kubernetes/petro_api.yaml:61-76`).
- **Startup gets its own probe.** `startupProbe` on `/healthz` with
  `failureThreshold: 30` at `periodSeconds: 3`
  (`petro_api.yaml:79-84`) — a native extension can be slow to load, and
  without it a slow start reads as a failed start and the pod restarts forever.
- **A crash loop is a rollout failure, by design.** `maxUnavailable: 0`
  (`petro_api.yaml:23`) means a new pod must pass readiness before an old one
  goes away, so an extension that fails to load takes the *rollout* down rather
  than the service.
- **Restart policy is Kubernetes' default `Always`** — the manifest does not
  override it — and `terminationGracePeriodSeconds: 45` (`petro_api.yaml:32`)
  is set to outlast gunicorn's `--graceful-timeout 30`.
- **Memory is limited but CPU is not** (`petro_api.yaml:89-94`): a native leak
  should kill the pod rather than the node, while throttling a numerical
  routine mid-call inflates latency badly and CPU is already bounded by
  `WEB_CONCURRENCY`.

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
- ⚠️ partial — array parameters carry a `MAX_ARRAY_ITEMS` cap
  (`NATIVEGATE_MAX_ARRAY_ITEMS`, default 65536 —
  `router.py:22`) instead of accepting a body of any length, and where a size
  argument can be paired unambiguously with the array it sizes, a generated
  guard rejects `n != len(props)` with a 422 before it reaches Fortran and
  indexes past the end of the buffer (`router.py:101-106`). The pairing is
  inferred conservatively: an ambiguous case yields no guard, because a false
  pairing would reject valid calls.

  **The cap is an explicit placeholder, and it is a memory-exhaustion guard —
  not a correctness bound.** The IR records that a parameter *is* an array,
  not the extent the routine actually expects; recovering the declared extent
  needs the fparser2 front end (ROADMAP 1.4). Until then one configurable
  number stands in for every array in the service. A request **within** the cap
  can still hand the native routine an array of entirely the wrong length. All
  the cap buys you is that an unbounded request body cannot exhaust the
  worker's memory: pydantic rejects an oversized list during validation, so the
  caller gets a 422 and the array is never materialised. Raise it for a service
  with genuinely large inputs; do not remove it, and do not read it as
  validation.
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

Set it in `nativegate.yaml`:

```yaml
api:
  auth: api_key        # or: none
```

Keys are read at startup from `NATIVEGATE_API_KEYS` (comma-separated), and
accepted as either `X-API-Key: <key>` or `Authorization: Bearer <key>`.

Three decisions worth knowing about, because each was the alternative to a
worse default:

- **Keys never live in `nativegate.yaml`.** That file is committed. A
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
| `NATIVEGATE_RATE_LIMIT_PER_MINUTE` | `0` (off) | Per-client sliding window, keyed by API key when there is one and client address otherwise |
| `NATIVEGATE_MAX_REQUEST_BYTES` | `1048576` | Rejects an oversized body with 413 *before* it is read into memory |
| `NATIVEGATE_MAX_ARRAY_ITEMS` | `65536` | Cap on elements in any array argument. Memory guard, **not** a correctness bound — see [API surface](#api-surface) |
| `NATIVEGATE_API_KEYS` | *(unset)* | Comma-separated keys, read at startup. Only consulted when the service was generated with `auth: api_key` |
| `NATIVEGATE_DEBUG_ERRORS` | *(unset — off)* | Adds native exception text to the 500 body. **Leave off in production** |
| `WEB_CONCURRENCY` | `1` Fortran, `2` C++ | gunicorn worker count. Read by gunicorn itself, so it is an env change rather than an image rebuild |

Two more knobs are not environment variables on purpose. The **auth mode** is
baked in at generate time from `api.auth` (`middleware.py:36`), so a service
generated to require authentication cannot be downgraded by the environment it
starts in. `MAX_ARRAY_ITEMS` *is* an env var but is read once at import.

**The rate limiter is per process, and that matters.** A limit of N is really
N × `WEB_CONCURRENCY` per pod, times the replica count, and it resets when a
worker is recycled by `--max-requests`. For the Fortran default of one worker
that is N per pod — with `replicas: 2` in the generated manifest, 2N for the
service. It is a crude
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

Access logs go to `nativegate.<service>.access` with structured `extra` fields
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

**Read this before touching anything about threading, workers, or the GIL.**
The one-line version for operators:

> Native execution is capped at **one concurrent call per process**, on
> purpose, for **correctness**. Scale with more processes (replicas/workers),
> never by removing the lock. Removing it to "improve throughput" produces
> silently wrong numbers, not a crash.

#### The hazard

Fortran COMMON blocks are process-global storage. `libraries/petro`'s
`PETRO.INC` declares

```fortran
COMMON /FLUID/ API, SGG, SGW, TRES, PB, PSEP, TSEP, NCOMP
```

and states the contract in a comment: *"CALL PVTSET(P) THEN USE THE COMMON. DO
NOT ADD ARGUMENTS."* `PVTINI` writes it; `PVTRS`, `PVTBO` and `PVTVIS` read it.

nativegate exposes those faithfully, as independent endpoints — `/pvt_set_fluid`
writes the state, `/solution_gor` reads it. FastAPI runs synchronous `def`
endpoints in a threadpool, so two callers configuring different fluids can
interleave. Nothing crashes and nothing logs; each caller just receives numbers
computed from the other caller's fluid.

Per-request objects — the fix that works for a stateful C++ class — cannot
reach this. COMMON belongs to the process, not to any instance.

#### What ships today: a process-wide lock

Generated **Fortran** routers declare, at
`services/petro_api/python/petro_api/router.py:44`:

```python
_NATIVE_LOCK = threading.RLock()
```

and hold it across every native call — every endpoint in that file does
`with _NATIVE_LOCK:` around the call and nothing else. `RLock` rather than
`Lock` so that one generated endpoint calling another cannot deadlock a
request against itself; held around the call only, so argument marshalling and
JSON shaping stay outside the critical section.

**This is a correctness mechanism, not a throughput choice.** It exists
because `PVTSET` writes `COMMON /FLUID/` and a later `PVTRS`/`PVTBO` reads it
back, and COMMON is process-global storage. Without the lock, two concurrent
requests configuring different fluids interleave their writes and reads in the
*same* COMMON block, and each caller silently receives numbers computed from
the other caller's fluid. Nothing raises. Nothing logs.

Consequences an operator has to plan around:

- **Native execution is capped at one concurrent call per process, by design.**
  A second request waits for the first native call to finish.
- **The supported way to scale is more processes** — more replicas, or more
  gunicorn workers *only* for routines that keep no state across calls. Not
  more threads inside one process; threads all contend on this one lock.
- **Do not remove the lock to improve throughput.** The failure mode is wrong
  numbers, not an error, so nothing in your monitoring will tell you.
- **The guarantee is per-process only.** It says nothing about a second process
  or a second pod, which is exactly why the multi-tenant case below is not
  supported.

C++ routers get no lock. They already construct an instance per request, and a
C++ free function has no equivalent of COMMON that the parser can see — locking
every C++ service would trade real concurrency for a hazard that isn't there.
If you bind C++ that uses file-scope statics or a singleton, that is your
hazard to assess; nativegate cannot detect it.

#### What the lock does not fix

The lock is held for **one native call**. A client that POSTs `/pvt_set_fluid`
and then POSTs `/solution_gor` releases it in between, so another client can
reconfigure COMMON in the gap.

**This is why the service is not multi-tenant-safe, and no amount of locking
fixes it.** The API nativegate generates from this header is session-shaped —
"configure, then read" — but HTTP requests carry no session. Two things break
independently:

- Within one process, tenant B's configure lands between tenant A's configure
  and A's read.
- Across processes, A's configure and A's read land on *different* workers or
  pods, so the read sees a COMMON that was never configured for A. The
  generated Kubernetes manifest ships `replicas: 2`
  (`infrastructure/kubernetes/petro_api.yaml:13`), so this is the default
  shape, not a hypothetical — see
  [deployment topologies](deployment-topologies.md#tenancy-what-is-and-is-not-supported).

Run one tenant per service instance until this is closed. Closing it needs one
of:

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

- ✅ **Numerical regression is covered.** `ngate golden record|verify`
  pins what every bound entry point returns for a fixed set of inputs, and the
  generated `tests/test_golden.py` replays it in the service's own suite. See
  [Numerical regression](golden-values.md). This is the check that matters for
  a re-host: "the answers did not change".
- The build toolchain is now recorded in the SBOM as `pkg:deb/debian/...`
  components, scoped to separate what only built the image from what ships in
  it. What compiled a *local* (non-container) build is in `golden.json`'s
  provenance, where it belongs.
- No CI/CD templates — design.md's pipeline (lint → compile → test →
  wheel → Docker → scan → publish) isn't generated by `ngate` anywhere.
  The repo's own CI does run tests across {ubuntu, macOS} × {3.10, 3.11, 3.12}
  plus a `pip-audit` job, but nothing generates a pipeline for *your* service.
- ✅ fixed — **`ngate k8s <name>`** generates a Deployment and Service with
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
  types, **`std::vector<T>`**, and **a raw `T*` paired with a length argument**
  are handled — on free functions AND class methods — so an ordinary C
  numerical API (`int set_porosity(double* values, int n)`) binds, writes in
  place, and returns the written array over HTTP. extern "C" scalars passed
  by reference (the Fortran-linkage convention) bind per-function via
  `clang.scalar_ref_functions` — opt-in, because `double* x` is also how C
  passes an array whose extent lives in a PARAMETER or COMMON block, and that
  cannot be told apart from the prototype. On the real petro corpus: **79%
  of recognised declarations bind with no configuration, 94% with one yaml
  line**. Still unsupported: templates, and a pointer whose length nothing
  names (`dot(a, b, n)` is ambiguous and refused rather than guessed). Two refusals are
  deliberate and worth knowing: a non-const `std::vector<T>&`, and a
  wrong-dtype or read-only array for a writable buffer — pybind11 would
  silently discard the writes in both cases. **Operators** bind as Python
  special methods (`operator+` → `__add__`); the ones with no honest mapping
  (`operator=`, `operator++`, compound assignment) are reported with the
  reason rather than dropped. Destructors are not a gap — pybind11 handles
  destruction itself. See [Exposing C++](cpp-guide.md). See
  [Exposing C++](cpp-guide.md#what-the-parser-supports).
- No C language support at all, despite being in the project name/design.md
  scope.
- ✅ fixed — **`libraries:` now works for Fortran.** It was C++-only, so a
  Fortran service could name a shared library and get nothing: `petro_api`
  built an extension and failed at import with `undefined symbol: iprvog_`.
  The decks are compiled into the extension now (f2py links no target, so they
  are vendored and compiled), and the image builds, runs, and reproduces every
  golden value exactly.
- Fortran: **derived-type arguments now bind** (flattened through a
  generated shim in the `_expanded` copy — scalar components, module
  routines), and **fixed-length CHARACTER outputs bind** (assumed-length ones
  are refused with the measured reason: they build and silently return empty).
  Formerly both were blanket "f2py cannot" refusals. F77 argument direction is *inferred*
  by f2py from the routine body, not declared — usually right, but worth
  checking against the routine's own docs before trusting it. See
  [output parameters](fortran-guide.md#output-parameters-f77-has-no-intent).
- C++ is parsed by Clang itself, so what the compiler sees is what nativegate
  binds; a header that fails to parse is refused outright rather than
  half-bound. Two caveats remain: the parse needs the same include paths and
  `-D` flags as the build (`clang:` in `nativegate.yaml`), and on a machine
  without libclang the front end silently falls back to the regex reader —
  set `parser: clang` to make that an error.
- The Fortran front end now has two backends behind one seam: the original
  regex reader (the default) and a real fparser2 parse tree, selectable with
  `NATIVEGATE_FORTRAN_PARSER=fparser2`. `auto` stays on the regex reader until
  the IR-parity harness (`tests/corpus/harness.py`) is green across the whole
  corpus. Under the regex reader, declarations it recognises but can't bind
  are reported with a reason rather than dropped silently, but syntax it
  doesn't match at all can slip past unreported.

## Enterprise readiness — the prioritized gap list

An assessment (updated 2026-08-23) of everything above, ordered by what would
actually stop an enterprise deployment. Numbered cross-references (2.2, 1.4, …)
are `ROADMAP.md` work items in the repository root — not part of this docs
site.

**Blockers — would stop an internet-facing or multi-tenant deployment today**

1. **No per-request crash isolation (2.2).** A segfault, Fortran `STOP`, or
   out-of-bounds write kills the worker process, and no exception handler can
   ever see it. Partly addressed: gunicorn supervision means one worker dies
   rather than the service, and `--timeout` reaps a hang. Not addressed:
   requests sharing that worker still die with it. Process-per-call is a
   deliberate second tier and is not implemented. This is the one that should
   decide whether you take untrusted input.
2. **The COMMON-block session race (2.1b).** Configure-then-compute pairs
   still race across requests — silent wrong numbers under concurrent
   multi-tenant load, and worse across replicas, where the read can land on a
   worker that was never configured. One tenant per service instance until
   this is closed.
3. **`SECURITY.md` contact is a placeholder.** Enabling GitHub private
   vulnerability reporting closes this in minutes; it is table stakes for
   vendor security review.

*Previously listed here and now done:* auth, rate limiting and request-size
limits (2.4) ship in `middleware.py`; array element counts are capped and size
arguments guarded (2.3) — with the placeholder caveat noted under
[API surface](#api-surface).

**Major — needed to operate at scale**

4. **Observability: partial.** Access logging with `request_id` / `method` /
   `path` / `status` / `duration_ms`, a correlatable `error_id`, and a
   `/readyz` distinct from `/healthz` liveness all ship. Still missing:
   OpenTelemetry traces/metrics/logs, and any split of native execution time
   from HTTP/marshalling overhead.
5. **No CI/CD generation** for *your* service, no image signing. Kubernetes
   manifests and a `pip-audit` step do now exist.
6. **Parser coverage ceilings.** Fortran: statement functions, COMMON
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

Validated for:

- **Prototyping the migration path** — proving a specific native function can
  be reached from Python with correct results, in a container, without
  hand-writing bindings. A load-bearing capability for the
  incremental-migration story in design.md §23.
- **Internal, single-tenant-per-service deployment** — a team calling trusted
  legacy code it owns, behind a network boundary that already decides who may
  call it, with one tenant's traffic per service instance. See the
  [supported topology](deployment-topologies.md#tenancy-what-is-and-is-not-supported).

**Not** validated for: internet-facing traffic, untrusted input, or
multi-tenant workloads sharing a deployment. The two blockers are named at the
top of this page — no per-request crash isolation, and a session-shaped API
over process-global COMMON storage. Closing them means process isolation (or a
memory-safety argument for the native code itself) plus either session affinity
or a session-shaped generated API, on top of the CI/security/observability
tooling design.md §19–22 specifies that nativegate still does not generate for
your service.
