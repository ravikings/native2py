# Nativegate Console — MVP Design & Build Plan

**Status:** Buildable spec. Scoped to one week of work.
**Version:** 3.0 (supersedes the v2.0 platform vision, retained as [Appendix A](#appendix-a--platform-north-star-v20)).
**Goal:** A developer drops in C++/Fortran source — by upload or public GitHub URL — and
walks away with a live REST endpoint, a live MCP endpoint, and a container they can
run themselves. No account required to self-host; GitHub login on the shared instance.

## 0. What changed from v2.0, and why

v2.0 described the end-state platform: control plane, deployment manager, k8s
orchestrator drivers, gVisor sandboxes, multi-tenant RBAC, billing. That is
months of work and none of it is demoable on its own.

This version inverts the order. The `ngate` CLI in `tools/nativegate/` already
does the hard part — parse, generate bindings, build a wheel, emit a FastAPI
service and a Dockerfile. The console is a **thin web skin over commands that
already work**, plus the one genuinely missing generator (MCP). Everything in
v2.0 that isn't on the critical path to "paste a repo URL, get two URLs back"
is deferred, not deleted.

| v2.0 concept | MVP treatment |
|---|---|
| Control plane / deployment manager | One FastAPI app. A `projects` table in SQLite. |
| Kubernetes orchestrator drivers | `docker run` on one VM. `ngate k8s` still emits manifests for users who want them. |
| Out-of-process worker shim, IPC daemon | The container **is** the isolation boundary. One container per service, restart-on-crash. |
| gVisor, network egress policy | Non-root container, `--network none` at build, read-only rootfs, dropped caps. |
| Multi-tenant RBAC, billing | GitHub OAuth + owner-scoped rows. Hard quotas, no metering. |
| Semantic tool aggregator | Ship raw tools, but let the user untick functions before deploy. |
| Async job queue (KEDA, polling tokens) | Builds are async (they take minutes). Requests are synchronous with a timeout. |

## 1. The one-screen product

Four screens. That is the whole console.

```
  /                    Landing → "Paste a repo. Get an API."
  /new                 Source picker → Discovery review → Deploy
  /p/<slug>            Project page: status, endpoints, live logs, try-it
  /p/<slug>/build/<n>  Build log (streamed, permalinked)
```

### The flow

```
  ┌─────────────┐
  │  1. SOURCE  │  upload .zip/.tar.gz/single file, or paste a public GitHub URL
  └──────┬──────┘
         │  ngate detect  →  language, candidate source files
         ▼
  ┌─────────────┐
  │ 2. DISCOVER │  ngate inspect → IR json → table of discovered functions
  └──────┬──────┘  user unticks what shouldn't be public, confirms types
         │  writes nativegate.yaml
         ▼
  ┌─────────────┐
  │  3. BUILD   │  ngate generate → build → test, in a throwaway container
  └──────┬──────┘  log streams to the browser over SSE, line by line
         │
         ▼
  ┌─────────────┐
  │  4. LIVE    │  https://<slug>.ngate.dev/           REST + OpenAPI
  └─────────────┘  https://<slug>.ngate.dev/mcp        MCP (streamable HTTP)
                   docker run ghcr.io/…/<slug>         same image, yours to keep
```

**Discovery review is the screen that matters.** Everything else is plumbing.
It is where the product earns trust with a computational expert: it shows them
exactly what the parser saw — parameter intents, array shapes, `COMMON` block
state it detected, which functions it could not resolve and why — and lets them
correct it before anything gets compiled. A tool that silently guesses wrong
about an `intent(inout)` array is worse than no tool. Surface the guess.

## 2. Architecture (MVP)

One VM. One Python process. Docker socket. SQLite.

```
                        ┌───────────────────────────────┐
   browser ── HTTPS ──▶ │  console  (FastAPI + Jinja)   │
                        │  ├─ routes/   HTML + SSE      │
                        │  ├─ jobs.py   build runner    │
                        │  └─ ngate.py  CLI subprocess  │
                        └───────┬───────────────┬───────┘
                                │               │
                     SQLite ◀───┘               │ docker API (unix socket)
                  projects,builds               │
                                                ▼
                        ┌──────────────────────────────────────┐
                        │  build container (ephemeral)         │
                        │  ngate generate → build → verify     │
                        │  --network none, non-root, 10 min cap │
                        └───────────────┬──────────────────────┘
                                        │ pushes image
                                        ▼
                        ┌──────────────────────────────────────┐
                        │  service containers (one per project)│
                        │  uvicorn: /  REST   +  /mcp  MCP     │
                        │  non-root, ro-rootfs, mem+cpu capped │
                        └───────────────┬──────────────────────┘
                                        ▲
                          Caddy ─────────┘  wildcard TLS, <slug>.ngate.dev → port
```

Why this shape:

- **No queue, no k8s, no separate worker service.** One process holds the
  in-memory job table and shells out to `docker`. If the box reboots, running
  builds are marked failed on startup. That is an acceptable MVP failure mode.
- **The build runs in a container, not in the console process.** Customer
  Fortran gets compiled by a compiler we do not control. That must never share
  a process, a filesystem, or a network namespace with the console.
- **The deployed artifact is a plain OCI image.** Whatever we host, the user can
  `docker run` identically. This is the open-source promise: the console is a
  convenience, never a lock-in. Every project page shows the exact CLI commands
  the console just ran.

### Deployment topology

`docker-compose.yml` at the repo root brings up the whole thing: `console`,
`caddy`, and a named volume for SQLite + build workspaces. `NGATE_AUTH=none`
runs it with no login at all for local/self-host use. The shared instance sets
`NGATE_AUTH=github`. Same image, same code path, one env var.

## 3. UI direction

Audience is reservoir engineers, computational physicists, numerics people. They
are unimpressed by rounded gradients and instantly suspicious of anything that
hides what it did. Design accordingly.

**Instrument panel, not SaaS dashboard.**

- Monospace-forward (JetBrains Mono / Berkeley Mono for data, one clean sans for
  prose). Dense tables, real numbers, no truncated cells.
- Dark by default, light theme available; both are first-class, both readable in
  a lit office.
- Ruled lines and a visible grid over cards and shadows. Draw the pipeline as an
  actual pipeline — the four-stage flow above should be a live progress element
  on the build page, with each stage lighting up as it passes.
- Colour carries meaning only: pass, fail, drift-within-tolerance, drift-exceeded.
  Nothing decorative gets to use green or red.
- **Build logs stream, uncut, at full width.** No spinner where output belongs.
  Compiler warnings from a 1994 Fortran deck are the most interesting thing on
  the page to this user — do not collapse them behind "Details".
- Show the numbers where numbers exist: wheel size, build seconds, cold-start ms,
  p50 call latency, and the golden-test max relative deviation vs. tolerance.

**The one flourish:** the discovery screen renders the parsed signature next to
the original source line, side by side, so you can see the wrapper it inferred
against the F77 it inferred it from. That single view is the demo.

Stack: FastAPI + Jinja2 + HTMX + a little Alpine.js for the function-picker.
SSE for log streaming. No node build step, no second deploy target, no CORS.
Hand-written CSS with custom properties for theming — under 400 lines, no framework.

## 4. Auth

Three modes, one flag. Ship the first two this week.

| `NGATE_AUTH` | Behaviour | Week |
|---|---|---|
| `none` | No login. Everything is owned by a local user. For self-hosting and dev. | 1 |
| `github` | GitHub OAuth. Projects scoped to the GitHub user id. | 1 |
| `email` | Magic-link accounts. | Later |

GitHub is the right first provider beyond the identity argument: the OAuth token
also lets us **clone the user's public repos directly**, which is the second
input path — paste `github.com/org/repo`, we shallow-clone it, and `ngate suggest`
scans it for buildable native sources. For MVP, public repos only, cloned
unauthenticated; the OAuth scope stays `read:user` so we are not holding tokens
that can read private code. Private-repo import is a later, deliberate decision.

## 5. Limits (MVP, enforced, shown in the UI)

| Limit | Value | Enforced where |
|---|---|---|
| Upload size | 50 MB | Console, before it touches disk |
| Build wall clock | 10 min | Build container `--timeout`, killed hard |
| Build network | none | `docker run --network none` |
| Projects per user | 5 | DB check on create |
| Service memory / CPU | 512 MB / 1 core | `docker run -m --cpus` |
| Request timeout | 30 s | uvicorn + gateway |
| Idle service shutdown | 24 h | Sweeper job, restartable from the UI |

Every one of these is a visible number on the project page, not a surprise 500.

## 6. What has to be built

Assessed against `tools/nativegate/` as it exists today.

### Already there — the console only shells out to it

`ngate detect` · `ngate suggest` · `ngate inspect` (emits IR JSON) ·
`ngate expose` · `ngate generate` · `ngate build` · `ngate test` ·
`ngate verify` · `ngate golden record|verify` · `ngate docker` · `ngate serve` ·
`ngate k8s`. The generated service is already a FastAPI app with a router,
middleware, a golden-test baseline — and, as of this week, an `/mcp` endpoint
(see below).

### Done since v3.0 drafted: MCP

**`generators/mcp_gen.py` has landed** (`docs/mcp.md`, `tests/test_mcp_gen.py`),
along with native doc-comment capture in the IR. This was the week's largest
backend task and the console no longer has to fund it. What shipped is stronger
than what §6 originally asked for:

- **Parity is structural, not tested.** Rather than emitting a second
  `@mcp.tool` per function, the generator builds a bare inner `FastAPI`
  carrying the *same prefix-less `APIRouter`* that `service.py` mounts, and
  hands it to `FastMCP.from_fastapi()`. The tool surface and the HTTP surface
  are literally the same routes, so they cannot drift and no parity test is
  needed. A tool call re-enters the same endpoint function in the same process,
  so `_NATIVE_LOCK` and the shared-state contract still hold.
- **Tool descriptions are the native source's own comments** — Doxygen/`///`
  blocks, Fortran `C`/`*`/`!` headers — carried verbatim into the IR and out to
  the model. Verbatim is the right call: a paraphrase of a numerical routine's
  contract is worse than none. Comment text is injection-safe (`repr()` fallback
  for anything with a quote or backslash), pinned by a test that *executes* the
  generated module.
- **Gateways serve one `/mcp` covering every mounted service**, tools qualified
  by service name. The "one URL" promise extends to the LLM interface.
- **Auth is inherited.** The MCP app mounts under the service app, so
  `api.auth: api_key` guards `/mcp` exactly as it guards a REST route.

Consequences for this plan: **Day 4 is free** (§7 is now a six-day plan), the
"bit-identical" line in §8 collapses into a much cheaper smoke check, and the
project page's MCP config snippet has a real endpoint to point at.

Still deliberately out: raw tool-per-route only. The v2.0 Semantic Tool
Aggregator remains deferred — the function-picker on the discovery screen is the
MVP's answer to context bloat. Unit metadata from `nativegate.yaml` does not yet
reach the tool schema — a nice-to-have for LLM callers ("pressure is in psi"),
not a blocker for the console.

### New: the console app itself

```
console/
├── app.py             FastAPI entry, config, NGATE_AUTH switch
├── db.py              SQLite schema + migrations (projects, builds, users)
├── auth.py            none | github modes
├── ngate.py           subprocess wrapper around the CLI, structured results
├── jobs.py            build runner: docker run, log capture, SSE fan-out
├── deploy.py          service container lifecycle, port alloc, Caddy config
├── sources.py         upload unpack + public GitHub shallow clone, path safety
├── routes/
│   ├── pages.py       /, /new, /p/<slug>
│   └── stream.py      SSE endpoints for build logs + status
├── templates/         Jinja: base, landing, new, discover, project, build
└── static/            one CSS file, one JS file
```

## 7. Week plan

Six days, not seven — the MCP generator (originally Day 4) is done. Each day
ends with something runnable. Nothing is integrated at the end.

The spare day goes to Day 6, which is the one most likely to expand: dogfooding
always surfaces more than a day's worth of papercuts.

**Day 1 — Skeleton and source intake.**
`console/` app boots, SQLite schema, `NGATE_AUTH=none` path, base template and
the CSS system (theme tokens, type scale, table and log-view styles). Upload
accepts a zip/tarball/single file, unpacks it safely (path traversal, symlink,
and size checks — this handles untrusted archives, treat it as such), and the
GitHub URL path shallow-clones a public repo. Ends with: a project row exists and
`ngate detect` output renders in the browser.

**Day 2 — Discovery screen.**
`ngate inspect` → IR JSON → the function table, with the source-line-beside-
inferred-signature view. Checkboxes to include/exclude. Writes `nativegate.yaml`.
Show unresolved symbols and *why* they failed to parse. Ends with: a reviewed,
committed contract on disk.

**Day 3 — Build pipeline.**
Build container image (toolchain: cmake, ninja, gcc/g++, gfortran, python).
`jobs.py` runs `generate → build → test → verify` inside it with the limits from
§5, captures stdout line by line into the DB, fans out over SSE. The build page
renders the four-stage pipeline live. Ends with: a wheel and an image built from
an upload, watched in real time.

**Day 4 — Deploy and route.**
`deploy.py` starts the service container, allocates a port, writes Caddy config,
wildcard TLS. Project page shows both live URLs, the OpenAPI link, an MCP client
config snippet ready to paste into Claude Desktop / an agent config, the
`docker run` line, and the exact `ngate` commands we ran. Try-it panel posts a
request and shows the response and its latency. Ends with: end-to-end, a public
URL from an upload.

**Day 5 — Auth, quotas, landing.**
GitHub OAuth. Owner scoping on every query — audit it, do not assume it. Quota
enforcement with the numbers surfaced. Idle sweeper. Landing page that states
the pitch and links a live demo project. `docker-compose.yml` and a self-host
README, both tested from scratch on a clean box.

**Day 6 — Harden and dogfood.**
Run `libraries/petro/` through the console as a first-time user with no shortcuts
and fix what hurts. Failure paths: build fails, source unparseable, container
OOMs, golden test drifts. Every one of those needs a page that explains itself.
Security pass: upload handling, container flags, secrets, ownership checks.

**Cut list, in order, if the week runs short:** try-it panel → GitHub URL import
(upload still works) → light theme → idle sweeper. Do not cut the discovery
review screen or the container isolation flags.

## 8. Definition of done

- [ ] From a clean browser: upload a Fortran tarball → live REST + MCP URLs, no CLI touched.
- [ ] `github.com/org/repo` paste does the same for a public repo.
- [ ] A deployed service answers the same call over REST and over `/mcp` with the
      same number (structurally guaranteed by `mcp_gen`; smoke-check it once end to end).
- [ ] The project page's MCP snippet pastes straight into a client and connects.
- [ ] Golden tolerance check runs on every build and blocks deploy on drift.
- [ ] Build logs stream live and are permalinked after the fact.
- [ ] `docker compose up` with `NGATE_AUTH=none` gives a working private console on a clean box.
- [ ] Customer code never executes outside a non-root, network-isolated, resource-capped container.
- [ ] Every console action names the `ngate` command behind it.
- [ ] A failed build explains why in the UI, without reading raw logs.

## 9. Deliberately out of scope

Kubernetes deploys (the CLI still emits manifests), autoscaling and scale-to-zero,
async job API for long simulations, GPU/MPI/FlexLM (v2.0 Tier 3), private-repo
import, teams and RBAC, billing and metering, the semantic tool aggregator,
custom domains, API-key management for deployed services.

Each of these is a real requirement for the platform in Appendix A. None of them
is required to prove the loop works.

---

# Appendix A — Platform north star (v2.0)

Retained verbatim as the architecture the MVP grows into. Where it conflicts with
the sections above, the sections above win for the MVP.

1. Executive Summary
NativeBridge wraps legacy computational code into production-ready microservices exposed via REST APIs and Model Context Protocol (MCP) toolkits.
Legacy Code (C++/Fortran/Python)
        │
        ▼
Upload & Analyze (native2py Inspection)
        │
        ▼
Interface Contract (nativebridge.yaml)
        │
        ▼
Isolated Build & Binding Generation
        │
        ▼
Numerical & Tolerance Validation
        │
        ▼
Sandboxed Containerization (Worker Shim)
        │
        ▼
Deployment (k3s / EKS / Customer K8s)
        │
        ▼
Dual Gateway (REST API + MCP Server)
        │
        ▼
AI Agent / Enterprise Client Application

Core Value Proposition
 * Zero Source Rewrites: Converts 30-year-old native code into cloud-native microservices.
 * Hardened Reliability: Eliminates process contagion—native segfaults (SIGSEGV) or floating-point errors (SIGFPE) cannot crash the web gateway or control plane.
 * AI Readiness: Unifies REST schema generation and MCP tool definitions from a single, deterministic function registry.
2. Updated Core Architecture
The architecture separates control, build, and isolated data execution planes, using native2py as the underlying binding extraction engine.
                         ┌──────────────────────┐
                         │      Web Console     │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │     API Gateway      │
                         └──────────┬───────────┘
                                    │
                 ┌──────────────────┼──────────────────┐
                 ▼                  ▼                  ▼
          Authentication       Control Plane       Usage/Billing
                 │                  │                  │
                 └──────────────────┼──────────────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │ Deployment Manager   │
                         └──────────┬───────────┘
                                    │
                   ┌────────────────┴────────────────┐
                   ▼                                 ▼
            Build Pipeline                    Runtime Manager
          (native2py Engine)                         │
                   │                                 │
                   ▼                                 ▼
          Container Registry                  Kubernetes
                   │                                 │
                   │                     ┌───────────┼───────────┐
                   │                     ▼           ▼           ▼
                   │                  User A      User B      User C
                   │                  Runtime     Runtime     Runtime
                   │               (IPC Worker) (IPC Worker) (IPC Worker)
                   ▼
           Validation Pipeline
                   │
                   ▼
       Dual Gateway (REST + MCP)

3. Code Analysis & Interface Contract Engine
NativeBridge leverages native2py to inspect C++, Fortran, and Python sources, discover symbols, and parse header signatures.
Guided Discovery Strategy
Pure AST discovery fails on complex pointers, assumed-shape Fortran arrays, and unannotated memory ownership. NativeBridge emits a nativebridge.yaml contract during analysis for human or programmatic verification before compilation.
version: "2.0"
target: "reservoir_sim"
engine: "native2py"
functions:
  - name: "calculate_pressure"
    source_symbol: "calc_press_f90_"
    language: "fortran"
    execution_mode: "process_isolated"  # process_isolated | thread_safe
    timeout_seconds: 30
    inputs:
      - name: "depth"
        type: "float64"
        unit: "feet"
      - name: "density"
        type: "float64"
        unit: "g/cm3"
    outputs:
      - name: "pressure"
        type: "float64"
        unit: "psi"
        location: "return_val"

4. Hardened Runtime Isolation Architecture
To protect against native crashes, memory leaks, and global state issues, NativeBridge mandates an Out-of-Process Worker Shim pattern.
[ Dual Gateway (REST / MCP) ]
              │
        (IPC / gRPC / Unix Socket)
              │
              ▼
    [ Worker Runner Daemon ]
              │
        (Fork / Exec Ephemeral Process)
              │
              ▼
   [ native2py C++/Fortran Binary ]
              │
   (SIGSEGV / SIGFPE / OOM occurs here)
              │
              ▼
[ Process Dies -> Gateway Catches Exit Code 139 -> Returns 500 cleanly ]

Execution Mode Classification
Every deployed module strictly runs under one of two execution policies:
| Mode | Criteria | Execution Model |
|---|---|---|
| process_isolated | Fortran COMMON blocks, global C++ state, non-thread-safe libraries | Spawns a dedicated, ephemeral worker process per request. Terminated upon task completion. |
| thread_safe | Pure functional C++ / Python routines without static state | Reuses warm process pools for high-throughput, low-latency execution. |
5. Dual Schema: REST API & MCP Server Integration
API and MCP tools derive from a unified Pydantic schema generated via native2py intermediate definitions.
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

# 1. Unified Intermediate Schema
class ReservoirInput(BaseModel):
    depth: float = Field(..., description="Target depth in feet")
    density: float = Field(..., description="Fluid density in g/cm3")

app = FastAPI(title="NativeBridge Engine")
mcp = FastMCP("NativeBridge Simulator")

# 2. REST Endpoint
@app.post("/v1/calculate-pressure")
def api_calculate_pressure(data: ReservoirInput):
    return {"pressure": run_in_isolated_worker("calculate_pressure", data.model_dump())}

# 3. MCP Tool Endpoint
@mcp.tool(description="Calculate reservoir pressure at specific depth and fluid density.")
def calculate_pressure(depth: float, density: float) -> float:
    return run_in_isolated_worker("calculate_pressure", {"depth": depth, "density": density})

Semantic Tool Aggregator (MCP Optimization)
Exposing hundreds of raw AST functions directly into an AI agent exhausts context windows and triggers hallucinations. NativeBridge includes a Semantic Tool Aggregator layer that:
 * Aggregates related low-level functions into composite domain workflows.
 * Filters out private internal symbols before exporting the MCP manifest.
6. Execution Lifecycle & Predictive Job Scheduling
NativeBridge automatically manages performance profiles for short-running calculations and heavy computational simulations.
Incoming Request
       │
       ▼
Is Cold Start + Execution > 3 Seconds?
       ├── NO  ──> Synchronous HTTP 200 / MCP Direct Tool Result
       │
       └── YES ──> Async HTTP 202 Accepted + Polling Job ID / MCP Status Token

[ POST /v1/jobs ] ────> Returns { "job_id": "job_987", "status": "QUEUED" }
                              │
[ GET /v1/jobs/job_987 ] ─────┴─> Returns { "status": "COMPLETED", "result": {...} }

7. Numerical Validation Framework
NativeBridge validates modern wrapper outputs against historical reference data prior to artifact deployment.
{
  "test_suite": "pvt_regression",
  "tolerance": {
    "absolute": 1e-6,
    "relative": 1e-4,
    "mode": "ULP"
  },
  "cases": [
    {
      "input": { "api": 35.0, "pressure": 2500.0 },
      "expected": { "bubble_point": 610.696 }
    }
  ]
}

If numerical drift exceeds tolerance thresholds during nativebridge validate, the build pipeline aborts with a VALIDATION_FAILED status.
8. Multi-Tiered Capability Matrix
NativeBridge provides a Capability Report during project analysis:
| Capability Tier | Supported Features | Engine Strategy |
|---|---|---|
| Tier 1 (Easy) | Python packages, pure C++ headers, simple Fortran modules | Direct native2py bindings generation to C-ABI. |
| Tier 2 (Legacy) | CMake/Make builds, mixed Fortran/C++, static/shared libraries | native2py build context wrapper with nativebridge.yaml configuration. |
| Tier 3 (Enterprise) | CUDA/GPUs, MPI clusters, proprietary license servers (FlexLM) | Stateful K8s jobs, dedicated GPU plugins, custom VPC sidecars. |
9. Phased Implementation Roadmap
MVP (v0.1) ─────────────────> Stage 2 (v0.5) ────────────────> Enterprise (v1.0)
• native2py core engine      • CMake / Make automated builds   • Managed K8s (EKS/AKS/GKE)
• Python & simple C++/Fortran • Golden numerical tests         • GPU / CUDA workloads
• Process-isolated IPC runner • Scale-to-zero (KEDA)           • FlexLM / RLM sidecars
• FastAPI + FastMCP server   • Multi-tenant RBAC             • Customer-hosted agents
• Single-node k3s deploy     • Asynchronous jobs queue        • SBOM & SLSA provenance

10. Repository Design
nativebridge/
├── control-plane/          # FastAPI Management API, Tenants, Deployments
├── analyzer/               # native2py integration, AST parser, YAML schema generator
├── binder/                 # Dynamic runtime code generators (C-ABI / ctypes)
├── worker/                 # Out-of-process isolation runner daemon (IPC wrapper)
├── gateway/                # Unified REST + FastMCP server routes
├── validator/              # Numerical diff and ULP tolerance engine
├── builder/                # Sandboxed Docker/OCI container builder
├── orchestrator/           # Kubernetes / k3s abstraction drivers
└── cli/                    # `nativebridge` developer CLI tool

11. Security & Isolation Boundaries
                 TRUSTED ZONE
────────────────────────────────────────────────────────
 NativeBridge Control Plane & Database
 Gateway Router & Authentication Layer
────────────────────────────────────────────────────────
                 │
                 │ gRPC / TLS IPC Channel
                 ▼
                UNTRUSTED ZONE
────────────────────────────────────────────────────────
 Sandboxed Worker Container (gVisor / Non-root K8s Pod)
 native2py Dynamically Loaded Native Code (.so / .dll)
 Customer Uploaded Fortran / C++ Executables
────────────────────────────────────────────────────────

