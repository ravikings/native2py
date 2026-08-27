# Roadmap: API-ready without rewriting native code

The goal is not a better compiler. It is this acceptance test:

> Point nativegate at an unmodified legacy source tree. Get back a running HTTP
> API that returns the same numbers as the native binary and is safe to operate
> under concurrent load.

Every item is judged against one of two questions. **Can we ingest more code
without editing it?** **Is the resulting API safe to operate?**

Status as of `3a3c648` (re-audited 2026-08-23). Test suite: green on both
paths — `python -m pytest -q` with the `[fparser]` extra installed, and
without it (the fparser2-only tests skip). The collected count varies with
which optional extras are present, so it is deliberately not quoted here.
The "595 passing, 0 skipped" figure that stood here was from `fbbd285` and is
superseded. Ten fparser2-only tests used to *fail* rather than skip on a
checkout without the optional `[fparser]` extra; the `requires_fparser` marker
now lives in `tests/conftest.py` and covers them, so that checkout reports
**652 passed, 41 skipped** and the extra-installed run **695 passed, 1 skipped,
1 xfailed**. Both were measured. Coverage under both Fortran backends and on
Python 3.10–3.12, and the current CI state, were **not** re-verified here.

---

## Where things stand

| | Status |
|---|---|
| **W1** Widen unmodified ingestion | 1.4 done — fparser2 is now the default when installed, on measured evidence |
| **W2** Make the generated API safe to operate | Concurrency serialised, bounds capped, crash containment tier 1, auth/rate limiting/request ids shipped; **per-request isolation and the session-shaped API still open — the two deployment blockers** |
| **W3** Codegen and IR hygiene | IR versioned, differential harness built; `ast` migration still deferred |
| **W0** Adoption prerequisites (added later) | Done; CI has run (see W0 Open), state at this commit not re-verified |

The single most consequential change is not on this list as a line item: the
generated services now carry **evidence**. `services/petro_api/golden.json`
exists with all 10 entry points recorded, toolchain provenance, and a native
oracle that compiles the Fortran and C++ directly and compares. That is what
turns "trust the author" into "run it yourself."

---

## The principle

nativegate solved "don't modify the source" once, correctly. `FINDINGS.md` #3:
modern `real(dp)` kind parameters broke the f2py build, and the fix was to
resolve kinds on a **generated copy** under `native/_expanded/`, leaving the
original untouched. The same mechanism now carries INCLUDE expansion (both
dialects), `Cf2py` intent directives, and kind resolution.

Every remaining "nativegate can't read this, so reformat your Fortran" case
should become either a parser capability or a transform on the `_expanded`
copy. The original tree stays read-only. Anything that cannot be handled that
way must be **reported through `ModuleIR.skipped` with a reason** — never
silently mis-bound. That channel was empty for every defect in the original
audit; it is now populated.

---

## W1 — Widen unmodified ingestion

### Done

| | Defect | Resolution |
|---|---|---|
| a | Free-form `&` continuation unhandled | `normalize_free_form`, quote-aware, comment-tolerant |
| b | Generated Python never syntax-checked | `compile()` gate in `cli.py` before write |
| c | Python keywords as native names | `ir.python_identifier`, true keywords only |
| d | Free-form had no IMPLICIT handling | `parse_implicit_map` applied on both dialects |
| e | Bare `end` / `end function` not matched | Accepted, without terminating on nested constructs |
| f | Arg-less free-form routines undiscoverable | Discovery and extraction fixed together |
| — | Derived-type returns defaulted to `float` | Skipped with a reason |
| — | `optional` arguments became required | `Parameter.is_optional` |
| — | Internal (`contains`) procedures retyped outer args | Body truncated at `contains` |
| — | Non-const `char*` bound as `str` | Refused — it is an output buffer with no length |
| — | Overloads, namespaces, const fields, enums, struct ctors | See `DEFECTS.md` Class B |

### Done — 1.4, fparser2 is now the default front end

**The seam has landed.** `parsers/fortran.py` is now a front door that picks a
backend: `fortran_fparser.py` (a real fparser2 parse tree) or
`fortran_regex.py` (the original line/regex reader, kept dependency-free and as
the parity yardstick). Selection is by `backend=` argument,
`$NATIVEGATE_FORTRAN_PARSER` (`auto` | `fparser2` | `regex`), then config.
**`auto` now resolves to `fparser2` when fparser is importable**, and to the
regex reader otherwise — the same arrangement the C++ side has with Clang.
Asking for `fparser2` on a machine without it is an error, not a silent
fallback.

The original recommendation stands: **`fparser` (fparser2), pinned
`>=0.2,<0.3`.** BSD-3-Clause, ~116k downloads/month, STFC
maintained, pure Python so it adds no toolchain to the install.

```python
from fparser.common.readfortran import FortranFileReader
from fparser.two.parser import ParserFactory
from fparser.two.symbol_table import SYMBOL_TABLES
```

One reader handles both dialects and follows `INCLUDE` natively.
`SYMBOL_TABLES` gives a real per-scoping-unit symbol table, which the current
code has no equivalent of.

**The parity gate is green.** Three independent measurements:

- `harness.py diff fortran:regex fortran:fparser2` — **no IR differences** across
  the corpus (8 sources, 69 routines), now committed as a snapshot.
- The **full suite passes under both backends** — 555 either way.
- Routine discovery matches file by file.

Getting there required fixing three test fixtures, not the parser. Each put a
declaration or an `implicit` statement somewhere Fortran does not allow it;
gfortran rejects all three (`data declaration statement cannot appear after
executable statements`, `Unclassifiable statement`). The regex reader accepted
them because it has no grammar, so the tests had been encoding its quirks as
requirements. fparser2 was right to refuse.

**The flip is done, and one argument decided it.** The objection was never
parse quality — it was that fparser2 *correctly* refuses invalid Fortran the
regex reader silently accepted, so an existing service could break on upgrade.
The parity harness cannot answer that: it only compares files both backends
accept.

What answers it is that nativegate **must compile the source with gfortran
anyway** — `f2py -c` shells out to it. Any file gfortran rejects could never
have produced a working service, whichever parser read it first. So the
question reduces to: does fparser2 refuse anything **gfortran accepts**?
Measured over 180 files — the nine real 1988–1997 decks in `libraries/petro`
plus numpy's own `f2py/tests/src` corpus, which exists to cover the edge cases
f2py must survive — each parsed with fparser2 and compiled with
`gfortran -fsyntax-only`:

    files where fparser2 is STRICTER than gfortran: 0

All 180 accepted by both. `parser: regex` and `NATIVEGATE_FORTRAN_PARSER=regex`
remain as escape hatches, and a machine without fparser is unaffected.

What the tree parser unblocks, still to be built on it:

- ~~**Statement functions** corrupt intent inference~~ — fixed, though not the
  way 1.4 predicted: fparser2 *cannot* tell `DBL(X)=X*2` from an array write
  without a symbol table (measured — it says Assignment_Stmt). The fix is
  semantic: a subscripted assignment target never declared as an array cannot
  be an element write, so it is a statement function and its definition line
  is skipped by the inferencer.
- **`COMMON` / `EQUIVALENCE` outputs are invisible.** A routine that writes its
  result through a COMMON block is scored as having no outputs. (Derived-type
  arguments and fixed-length CHARACTER outputs, previously in this list's
  company, now bind — via a generated flattening shim and a measured lift of
  the blanket demotion respectively.)
- ~~**Preprocessed Fortran** (`.F90`, `.F`, `.FOR`)~~ — handled: discovered
  (they were not even in the extension table), run through gfortran `-cpp -E`
  into `_expanded/` before any parse, with `fortran: defines:` choosing the
  branch. Verified by compiling the result and calling it.
- **Cross-file `CALL` resolution** widens to `inout`, which is safe but verbose.
- **`ENTRY` points** are not discoverable.

Phases: seam (~1 wk, mirroring `parsers/cpp.py`) → parity (~2 wks, gated on the
differential harness in 3.2) → semantics (~2 wks). Main risk is that fparser2
parses whole files, forfeiting the "20,000-line deck costs the same as 200
lines" property — measure first, then cache on file hash.

Rejected as the default: **Flang** (best semantics, needs a binary, emits a text
dump not an API — revisit as an optional backend the way `clang` is optional);
**tree-sitter-fortran** (CST, no types); **LFortran** (better long-term ASR
target, too young).

---

## W2 — Make the generated API safe to operate

### Done — 2.1a, serialise COMMON access

`libraries/petro/fortran/include/PETRO.INC:34` declares
`COMMON /FLUID/ API, SGG, SGW, TRES, ...`. `PVTINI` writes it; `PVTRS` and
`PVTBO` read it. FastAPI runs synchronous `def` endpoints in a threadpool, so
two callers configuring different fluids interleaved **in one shared COMMON
block** and each received the other's numbers. Nothing crashed, nothing logged.

Generated Fortran routers now hold a module-level `RLock` across every native
call. C++ routers are unchanged — they already construct per request.

**Ordering constraint, and it matters.** The pybind11 bindings never release the
GIL and the f2py path emits no `threadsafe` directive, so every native call
holds the GIL. That is a throughput ceiling — but it is *also* why the COMMON
race was silent rather than memory corruption: the GIL prevents two threads
being inside Fortran simultaneously, and the race is *across* calls. Anyone who
profiles this, finds the GIL bottleneck, and adds `gil_scoped_release` **before**
COMMON access is safe converts a wrong-answer bug into genuine corruption.
COMMON safety lands first. This is documented in `docs/production-readiness.md`.

### Open

**2.1b — the lock is per-call, not per-session.** The lock is at
`services/petro_api/python/petro_api/router.py:44`, and it holds only *within*
one worker process: it does not make the service safe across workers, pods or
replicas, and it caps native throughput at one concurrent call per process.
Design routes for closing this are below, under "Closing the shared-state gap".
A deliberately passing test
records this so it cannot be mistaken for a complete fix: `POST /pvt_set_fluid`
followed by `POST /solution_gor` still races across the two requests. Closing it
means either process affinity per session, or detecting the write→read COMMON
dependency at parse time and generating a **session-shaped** API (`POST /session`
returning a handle). The latter is the only option that honestly describes what
the native code does, and it depends on 1.4's symbol table.

**2.2 — crash containment. Tier 1 decided and shipped; tier 2 open.**

Generated images ran a bare `uvicorn` — one process, so a segfault, a Fortran
`STOP` or a hang took the whole service down with nothing left to restart it.
They now run **gunicorn supervising uvicorn workers**, with `--timeout` (the
only thing here that can address a hang, since a worker stuck in native code
never returns to Python) and `--max-requests` recycling.

Verified against a real gunicorn: a genuine SIGSEGV killed a worker, gunicorn
logged it and booted a replacement, and the service answered the next request.

Fortran defaults to `WEB_CONCURRENCY=1` because COMMON is per-process — with
more workers a configure call and the compute that reads it can land on
different processes, which is worse than the interleaving race in 2.1b, not
better. C++ defaults to 2.

**Still open — tier 2, per-request isolation.** Requests sharing the crashed
worker still die with it. Containing a crash to only the request that caused
it needs process-per-call, which puts marshalling cost on every request.
Required for untrusted input or any native code whose memory safety cannot be
argued. Deliberately not implemented: the cost is real and the trade should be
explicit.

**2.3 — input bounds. Done, with one part deferred.** Array parameters are
annotated `Field(max_length=MAX_ARRAY_ITEMS)` (`NATIVEGATE_MAX_ARRAY_ITEMS`,
default 65536), closing the memory-exhaustion vector, and a generated guard
rejects `n != len(props)` with a 422 before the value reaches Fortran and
indexes past the end of the buffer.

The size/array pairing is *inferred* — by name, then structurally — and only
where unambiguous, because a false pairing rejects valid calls while a missed
one merely leaves that endpoint as unsafe as it was. Per-parameter extents from
the IR (1.4) would replace the inference and the single global cap with a real
per-array bound; that part is still open.

**2.4 — security and observability.** Partly closed. Generated apps now
register an unhandled-exception handler (`generators/error_gen.py`, emitted
into both `service.py` and the gateway app, because a handler binds to an app
and a mounted router runs under the gateway's). A failure gets a JSON body and
an `error_id` that also appears in the log line carrying the traceback.

Worth recording why, because the original entry here was based on a false
premise: this page and `docs/production-readiness.md` both claimed an uncaught
exception returned a raw traceback to the caller. Building the generated app
and issuing a real request disproved it — with `debug=False` Starlette answers
a bare `Internal Server Error` in `text/plain`. The handler is therefore a
traceability and consistency fix, **not** a leak fix, and it explicitly does
not protect a service running with `debug=True` (Starlette checks `debug`
before the handler). Both limits are pinned by tests.

**Mostly closed since — this paragraph was stale and is corrected.** Shipped in
`fbbd285`/`52dae51` and verified in the tree: API-key auth that fails closed
(`generators/middleware_gen.py:40`, keys from `NATIVEGATE_API_KEYS`, never
baked in), a rate limiter (`middleware_gen.py:120`), per-request ids and
request logging (`middleware_gen.py:152`+), a `/readyz` readiness probe
distinct from `/healthz` liveness — reporting 503 while draining, and wired
into the generated Kubernetes manifests (`generators/k8s_gen.py:157`) — and
the `GET /_unexposed` route, which is generated (`python_pkg_gen.py:505`) and
present in `services/petro_api/python/petro_api/router.py:58`.

Still open, verified by grep over `nativegate/`: **no OpenTelemetry** (zero
references), and **no authorization** — a valid API key can call every
endpoint (see "What to do next" #4).

---

## Closing the shared-state gap — design, not implementation

Nothing in this section is built. It exists so the choice is made deliberately
rather than by default. The problem is stated in DEFECTS.md S1/S2: native
process-global state (Fortran `COMMON`, C++ file-scope statics) makes the
native contract "configure, then read", HTTP has no session, and the current
answer is a process-wide lock that serialises everything and still only holds
inside one worker — and that lock is emitted for Fortran only
(`generators/python_pkg_gen.py:398`), so a C++ header carrying file-scope
`static` state gets no protection at all.

Three routes. They are not exclusive — (a) and (b) compose.

### (a) Reshape the API via codegen — fuse configure + compute

Extend the IR to record, per routine, **which process-global state it writes
and which it reads**. Then generate one endpoint per write→read chain, taking
the union of both routines' arguments, so a single HTTP request performs the
configure and the compute inside one lock acquisition and leaves nothing
behind that a later request depends on.

What it changes in the generator:

- **IR.** `FunctionDef` needs a globals-effect record — something like
  `writes_globals: list[str]` / `reads_globals: list[str]`, naming the COMMON
  block (or module variable, or C++ static) touched and in which direction. It
  must serialise, version under `schema_version`, and default empty so an older
  `ir.json` still loads (the existing pattern in `ir.py`).
- **Fortran front end.** This is the work `COMMON`/`EQUIVALENCE` outputs need
  in W1.4 and cannot be done by the regex reader: fparser2's per-scoping-unit
  `SYMBOL_TABLES` is what tells you that a name in an assignment target is a
  COMMON member rather than a local. Cross-file `CALL` resolution has to
  propagate the effect transitively, or a routine that configures via a
  subroutine call looks pure.
- **Pairing rule.** Deciding which writer pairs with which reader must be
  explicit and conservative, and where it is ambiguous the honest output is a
  `ModuleIR.skipped` entry plus the unfused endpoints, not a guess. A YAML
  escape hatch (`api: fuse:`) is the likely fallback.
- **Router codegen.** Emit fused endpoints, merge and de-duplicate argument
  names, and decide what happens to the unfused ones — probably still emitted,
  documented as unsafe under concurrency, or omitted behind config.

Trade-offs. Stateless: one request is one self-contained native call, so it is
safe across workers, pods and replicas, and the `WEB_CONCURRENCY=1` pin and the
one-worker-per-pod Kubernetes pin can both be lifted. It is a **codegen
change**, which is exactly nativegate's mandate — no new runtime, no new
operational surface. It does **not** contain crashes: a segfault still kills
the worker. It cannot express a genuinely long-lived session (configure once,
then a thousand reads) without re-sending the configuration each time, which
costs native work per request. And it depends on IR work that does not exist.

### (b) Isolate per request at runtime — a subprocess pool

Run the native extension in child processes, not in the API worker. One native
process per in-flight request, or per session token if a session API is kept.
`COMMON` is then per-tenant by construction, because it is per-process.

What it changes in the generator:

- A generated pool module: spawn, health-check, recycle after N calls, cap
  size, and a supervisor that replaces a dead child.
- A marshalling layer for every bound signature — arrays especially. Shared
  memory for large `numpy` buffers if the IPC cost is not to dominate.
- Endpoints call through the pool instead of importing the extension directly;
  the process-wide `RLock` disappears, replaced by pool checkout.
- Timeouts become enforceable per request rather than per worker
  (`gunicorn --timeout` today), and a session token, if kept, must pin to a
  child and expire.

Trade-offs. This is the only route that **also contains segfaults, Fortran
`STOP` and hangs** — the crash kills a child, the request gets a 5xx, the
service stays up. It is the honest answer for untrusted input or native code
whose memory safety cannot be argued. It costs IPC latency on every call and
adds a large runtime surface for nativegate to own and debug — pool lifecycle,
marshalling, backpressure, zombie reaping — which is a different kind of
project than a code generator.

### (c) Make the native source re-entrant

Move state out of `COMMON` into a derived type (or a C++ object) threaded
through the call chain. Then there is no shared state, the lock is unnecessary,
and both (a) and (b) become unnecessary too.

What it changes in the generator: very little — a re-entrant deck binds with
what exists today, and the derived-type shim machinery already landed
(`3a3c648`) is most of what a state handle would need.

Trade-offs. Cleanest by a wide margin and the only route that fixes the problem
rather than working around it. It is also the most expensive, requires editing
the native source — which contradicts nativegate's founding constraint — and
requires the numerical revalidation that makes anyone touching a 1988 deck
nervous. Usually off the table for legacy code. Worth naming anyway, because
for a *new* library it is simply the right answer, and nativegate should not
teach people otherwise.

### Recommendation

**(a) is the default.** It fits the mandate — a codegen change, driven by IR
the front end should be recovering anyway, with W1.4 already on the critical
path for it — and it produces a stateless API that scales horizontally.

**(b) is what multi-tenant or crash-prone libraries actually need**, and no
amount of (a) substitutes for it: fusing endpoints does not stop a segfault
from taking down every co-resident request. If nativegate is ever pointed at
untrusted input, (b) is not optional.

**(c) when you own the source and the revalidation budget.**

---

## W3 — Codegen and IR hygiene

### Done

`ir.Parameter` gained `native_type` / `is_const` / `is_optional`; `Method`
gained `is_const` / `is_overloaded`; `FunctionDef` gained `namespace` /
`is_overloaded`; `StructDef` gained `has_default_constructor`. Deserialisation
handles each explicitly rather than `Parameter(**item)`, so an older `ir.json`
loads with defined defaults. `ir.validate()` catches unspellable names and
collisions in the *emitted* namespace.

### Open

**3.2 — `schema_version` and the differential harness.** Both have landed:
`ir.py` now writes and checks `schema_version` (a document without one is
treated as the pre-versioning format), and `tests/corpus/harness.py` parses the
real libraries under a named backend and compares IR. **The harness is the
acceptance gate for flipping 1.4's `auto` to `fparser2`** — it is the only
thing that can tell you whether the new front end changed an answer.

**3.3 — the `ast` migration.** Zero uses of `ast.unparse` today; the generators
still build text. Worth doing for `generate_router_py`, `generate_init_py` and
`generate_python_api_test` — explicitly **not** CMake, Dockerfile, pybind11 C++
or TOML. Note the `compile()` gate and identifier validation already graduated
out of this workstream, which is most of the safety benefit; what remains is
maintainability. ~3 wks, whenever there is room.

**3.4 — carry doc comments into the IR. DONE.** `FunctionDef.doc` carries the
documentation attached to a routine in its own source, and the generated
endpoint's docstring uses it — which means it becomes the route's OpenAPI
`description` and then the MCP tool description a model reads before calling
native code (docs/mcp.md). Schema went 1.5 -> 1.6.

* **C++** (`parsers/cpp_ast.py`): `raw_comment`, not `brief_comment` — the
  latter returns only the `\brief` paragraph, and the legacy headers this tool
  targets usually have an unlabelled first paragraph followed by `@param`
  lines, so it yields nothing where real documentation exists. Doxygen
  structure is kept as prose. The regex fallback still yields None: it has no
  reliable comment-to-declaration attachment, and guessing is what that reader
  is already criticised for.
* **Fortran** (`parsers/fixed_form.py`, shared by both backends): the comment
  run inside the routine (F77 style) wins over the run above it (free-form
  style). Cleaning drops separator banners — "contains no letter or digit", so
  `C=====` goes and `C---- 05-FEB-1996: FIXED DIVIDE BY ZERO ----` stays — and
  truncates past 30 lines, where a header stops being documentation and becomes
  a change log. Comment runs terminate at the INCLUDE-expansion markers;
  without that, PETRO.INC's own banner was appended to the description of every
  routine in every deck that includes it. Implemented in BOTH backends because
  `test_both_backends_produce_the_same_ir` compares whole ModuleIRs — parity
  was a constraint, not an afterthought.
* **Escaping lives in the generator, not the parsers.** The text lands inside a
  `"""` literal, where a `"""` closes it early and a trailing backslash escapes
  the closing quotes. Both parsers originally neutralised those in place; that
  was reverted, because it is lossy and rewrites a routine's documentation to
  suit one consumer's quoting rules while every other consumer of the IR
  inherits the damage. `python_pkg_gen._docstring_literal` emits `repr()` for
  anything carrying a quote, backslash or control character and a readable
  triple-quoted block otherwise. tests/test_endpoint_docstrings.py executes the
  hostile cases rather than only compiling them.

Not done, and deliberately: nothing decides what to do about headers whose
comments are stale, and nothing should. The text is passed through verbatim so
the staleness stays attributable to the source.

Done from this workstream: `golden_gen.TEMPLATE` is now a real shipped module
(`nativegate/templates/golden_test_template.py`, asserted present in a built
wheel by `tests/test_golden.py`), and the unused `jinja2` runtime dependency
has been dropped from `pyproject.toml` and `requirements.txt`.

---

## W0 — Adoption prerequisites

### Done

- **Apache-2.0** with `NOTICE`. The repo previously had no license, so it was
  all-rights-reserved by default and legally unadoptable.
- **CI** across {ubuntu, macos} × {3.10, 3.11, 3.12}, installing the package
  editable so the CLI tests genuinely run, plus a step that fails the build if
  the Clang AST backend silently falls back to the regex reader.
- **Numerical evidence.** `golden.json` recorded for all 10 entry points with
  toolchain provenance (compiler identity and version, platform, numpy, SHA-256
  of all 10 native sources, no timestamps). Tolerance moved from a hidden global
  `rtol=1e-12` — tighter than cross-platform libm reproducibility for
  correlations dense in `exp`/`log`/`pow` — to per-entry values defaulting to
  `1e-9` / `atol 1e-12`, with `tubing_bhp` at `1e-6` derived from `TRAVER`'s own
  `1e-4` fixed-point stop.
- **A native oracle** (`tests/test_native_oracle.py`) that compiles the Fortran
  and C++ directly and compares them against the generated Python path. Skips
  cleanly with no toolchain.
- **Supply chain.** `constraints.txt` with 52 hash-pinned packages; the generated
  Dockerfile digest-pins its base for both stages; a deterministic CycloneDX SBOM
  recording native sources by SHA-256.
- **A working private security channel.** GitHub private vulnerability
  reporting is enabled on the repository and `SECURITY.md` points at it. No
  email address was invented: a mailbox on a one-maintainer project is the
  thing that silently stops being read.
- `SECURITY.md`, `CONTRIBUTING.md`, `CODEOWNERS`; both review hooks hardened to
  SHA-256 and fail-closed.

### Open

- ~~**CI has never actually run.**~~ **It has now, and it earned its keep on
  the first run** (`32616648280`). ubuntu/macOS × 3.10/3.11 passed; **both
  3.12 legs failed**, on two real bugs neither laptop could have shown:

  1. **Every generated Fortran image was unbuildable on Python 3.12.** The
     generated CMake runs `python -m numpy.f2py -c`; distutils is gone in
     3.12, so f2py drives its meson backend and shells out to `meson`. The
     generated Dockerfile installed `build-essential gfortran cmake` and the
     base image is `python:3.12-slim`, so the build died with
     `FileNotFoundError: 'meson'`. Reproduced outside CI on a 3.12
     interpreter — `f2py -c` exits 1 and emits no `.so`; with `meson` and
     `ninja` installed it exits 0 and the extension imports and computes.
     Fixed in `docker_gen` (pip, not apt — Debian stable's meson is older
     than f2py's backend wants) and in the `[build]` extra.
  2. **`setuptools` is absent from a 3.12 venv**, so the wheel-contents guard
     in `tests/test_golden.py` errored. Declared in `[test]`.

  Both were invisible to `pytest` on 3.10/3.11 and to every local run. The
  suite is now **559 passed, 0 skipped on 3.12**, verified on a real 3.12
  interpreter before pushing.

- **Runner assumptions beyond that are still only proven for one commit.**
- **A skip is not a pass.** `fparser` was imported by the new Fortran backend
  but declared in no dependency file, so all ~30 of its tests skipped silently
  on every machine — the suite went green with the backend untested. It is now
  an `[fparser]` extra, installed in CI, with a guard step that fails the build
  if it is missing, mirroring the existing libclang guard. Worth watching for
  the same shape elsewhere.
- **The generated service's build is still not reproducible**, even though the
  tool's install and the container base now are. `apt-get` runs unpinned against
  live Debian, the generated Dockerfile's `pip install` has no
  `--no-index`/`--require-hashes`, and `f2py -c` shells out to a system gfortran
  the SBOM never records — two machines with different gfortran produce an
  identical SBOM and different binaries. Documented honestly in
  `docs/deployment-topologies.md` rather than papered over.

---

## Deployment verdict

**Deployable today for internal, single-tenant-per-service use**: a team
calling trusted legacy code it owns, one deck per service, one tenant per
deployment. The supporting evidence is real and checkable — `golden.json` with
10 recorded entry points and toolchain provenance, a native oracle that
compiles the Fortran and C++ directly, reproducible container builds, API-key
auth, rate limiting, request ids, `/healthz` + `/readyz`.

**Not deployable internet-facing or multi-tenant.** Two blockers, both in
DEFECTS.md ("Standing blockers") and both open here:

1. **No per-request isolation.** `router.py:44` serialises every native call
   through one process-wide `RLock` for correctness, not speed; it is emitted
   for Fortran services only, it holds only
   within a worker, so nothing protects tenants across replicas, and a
   segfault, `STOP` or hang still takes the worker's co-resident requests with
   it (`service.py:52`).
2. **The API is session-shaped.** "Configure, then read" over stateless HTTP is
   not a safe public contract. See "Closing the shared-state gap".

Neither is a bug to be patched. Closing them is the design work above.

## What to do next

Ordered by value per hour, not by workstream.

1. **2.2 tier 2, per-request isolation** — process-per-call. Required before
   untrusted input, and the last thing standing between "supervised" and
   "contained". Deliberately deferred so far because it costs marshalling on
   every request; that trade should be made deliberately.
   Route (b) in "Closing the shared-state gap".
2. **2.1b — close the shared-state gap**, preferably by route (a): record
   global-state effects in the IR and generate fused configure+compute
   endpoints. Unblocked by 1.4's symbol table, and more
   urgent than it looks: the crash-containment default pins Fortran to one
   worker, and the Kubernetes manifests pin it to one worker per pod, both
   *because this is unsolved*. Fixing it lifts a throughput ceiling in two
   places at once.
3. ~~**Reproducible generated builds**~~ **— done.** apt packages are
   version-pinned (including the concrete `gfortran-14`, because pinning the
   metapackage left the compiler free to move), the service's Python
   dependencies are hash-locked via `ngate lock` + `--require-hashes`, the
   wheel is byte-reproducible via `SOURCE_DATE_EPOCH`, and the toolchain is
   recorded in the SBOM. Verified by three `--no-cache` builds: identical
   packages, byte-identical `.so` and wheel, golden values reproduced exactly
   from a different compiler than recorded them, and a tampered hash failing
   the build. Still unpinned: `apt-get update` fetches live indexes, so a
   superseded pin fails loudly rather than drifting.
4. **Authorization, not just authentication** — a valid API key today can call
   every endpoint. Per-key scopes are the obvious next step.
5. **OpenTelemetry, and splitting native time from HTTP overhead** — request
   duration is measured; the split is not.
6. **Image signing** (cosign) and a **release story** (published wheels, a
   changelog, an API-compat check).
7. **3.3 `ast` migration**, **CORS**, **C language support** — real,
   deferrable.

## Out of scope

- **C++ semantic analysis beyond Clang.** Binding safety is enforced by refusal —
  no raw pointer returns, no *length-free* pointers, no non-const `char*`
  (`parsers/cpp_ast.py:608`). That is a design, not a gap.
- ~~**STL container binding**~~ **— partly done, no longer out of scope.**
  `std::vector<T>` binds through typedefs (`parsers/cpp_ast.py:538`), with a
  non-const `std::vector<T>&` argument deliberately refused because pybind11
  discards writes through it (`parsers/cpp_ast.py:492`); a raw `T*` paired with
  a length argument binds as a numpy buffer. Still out of scope: `std::span`,
  `std::map`, and general template instantiation.
- **Migrating off f2py.** It is the constraint behind several IR compromises
  (arrays forced to `intent(in,out)` to avoid allocating `MXCELL` elements per
  call). Note this no longer includes CHARACTER outputs: fixed-length ones now
  bind via a lifted demotion (see 1.4 above and DEFECTS.md), though only on the
  fixed-form path. Revisit once 1.4 provides real extents.
