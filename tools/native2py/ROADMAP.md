# Roadmap: API-ready without rewriting native code

The goal is not a better compiler. It is this acceptance test:

> Point native2py at an unmodified legacy source tree. Get back a running HTTP
> API that returns the same numbers as the native binary and is safe to operate
> under concurrent load.

Every item is judged against one of two questions. **Can we ingest more code
without editing it?** **Is the resulting API safe to operate?**

Status as of `2380fc7`. Test suite: **238 → 441, all passing.**

---

## Where things stand

| | Status |
|---|---|
| **W1** Widen unmodified ingestion | Done except the parser replacement (1.4) |
| **W2** Make the generated API safe to operate | Concurrency serialised; crash containment, bounds, auth/observability open |
| **W3** Codegen and IR hygiene | Partially done; `ast` migration still deferred |
| **W0** Adoption prerequisites (added later) | Done except a real security contact |

The single most consequential change is not on this list as a line item: the
generated services now carry **evidence**. `services/petro_api/golden.json`
exists with all 10 entry points recorded, toolchain provenance, and a native
oracle that compiles the Fortran and C++ directly and compares. That is what
turns "trust the author" into "run it yourself."

---

## The principle

native2py solved "don't modify the source" once, correctly. `FINDINGS.md` #3:
modern `real(dp)` kind parameters broke the f2py build, and the fix was to
resolve kinds on a **generated copy** under `native/_expanded/`, leaving the
original untouched. The same mechanism now carries INCLUDE expansion (both
dialects), `Cf2py` intent directives, and kind resolution.

Every remaining "native2py can't read this, so reformat your Fortran" case
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

### Open — 1.4, replace the regex front end with fparser2

Still the right call, unchanged in substance. **Recommendation: `fparser`
(fparser2), pinned `>=0.2,<0.3`.** BSD-3-Clause, ~116k downloads/month, STFC
maintained, pure Python so it adds no toolchain to the install.

```python
from fparser.common.readfortran import FortranFileReader
from fparser.two.parser import ParserFactory
from fparser.two.symbol_table import SYMBOL_TABLES
```

One reader handles both dialects and follows `INCLUDE` natively.
`SYMBOL_TABLES` gives a real per-scoping-unit symbol table, which the current
code has no equivalent of.

What is still broken without it:

- **Statement functions** corrupt intent inference. `infer_intents` decides what
  a statement *is* by matching its first word against a frozenset, so an F77
  statement function `FUNC(X) = X*2` is indistinguishable from an assignment to
  a dummy argument.
- **`COMMON` / `EQUIVALENCE` outputs are invisible.** A routine that writes its
  result through a COMMON block is scored as having no outputs.
- **Preprocessed Fortran** (`.F90`, `.F`, `.FOR`) is detected and **warned about**
  but not handled — conditional code is still read as if every `#ifdef` branch
  were live.
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

**2.1b — the lock is per-call, not per-session.** A deliberately passing test
records this so it cannot be mistaken for a complete fix: `POST /pvt_set_fluid`
followed by `POST /solution_gor` still races across the two requests. Closing it
means either process affinity per session, or detecting the write→read COMMON
dependency at parse time and generating a **session-shaped** API (`POST /session`
returning a handle). The latter is the only option that honestly describes what
the native code does, and it depends on 1.4's symbol table.

**2.2 — crash containment.** A Fortran `STOP`, a segfault, or an out-of-bounds
write takes down the whole worker process, not just the request. Legacy
numerical code does all three outside its validated envelope. Needs a supervised
worker pool with request-level isolation — a deployment-topology change, not a
codegen change, and it needs a decision before anyone writes code.

**2.3 — input bounds.** Every array endpoint still accepts an unbounded body:
`def pvt_state_endpoint(pressure: float, n: int, props: list[float])`. That is a
memory-exhaustion vector. Needs array extents in the IR (from 1.4) for a real
`Field(max_length=...)`; a configurable global cap works until then. `n` is also
not validated against `len(props)` — a mismatch goes straight to Fortran, which
indexes past the end.

**2.4 — security and observability.** No authentication, no rate limiting, no
request-size limit, no structured logging with a request id, no readiness probe
distinct from `/healthz` liveness, no OpenTelemetry. All are generated-code
concerns, so fixing them once fixes every service. Also unbuilt: a
`GET /_unexposed` route returning what the parser skipped and why — that
information currently exists only in generated comments inside a container.

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

**3.2 — `schema_version` and the differential harness.** Neither exists yet.
`ir.json` still carries no version, so a file written by a future native2py
deserialises into a subtly different module with no warning. And there is no
`tests/corpus/` harness that parses `libraries/petro` and `libraries/geometry`
under a named backend and snapshots the IR. **That harness is the acceptance
gate for 1.4b and must exist before the parser swap starts** — it is the only
thing that can tell you whether a new front end changed an answer. ~1 wk.

**3.3 — the `ast` migration.** Zero uses of `ast.unparse` today; the generators
still build text. Worth doing for `generate_router_py`, `generate_init_py` and
`generate_python_api_test` — explicitly **not** CMake, Dockerfile, pybind11 C++
or TOML. `golden_gen.TEMPLATE` should become a shipped `.py` file so linting
covers it. Note the `compile()` gate and identifier validation already graduated
out of this workstream, which is most of the safety benefit; what remains is
maintainability. ~3 wks, whenever there is room.

**Also still open:** `jinja2` is a declared runtime dependency used nowhere in
the codebase. Drop it.

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
- `SECURITY.md`, `CONTRIBUTING.md`, `CODEOWNERS`; both review hooks hardened to
  SHA-256 and fail-closed.

### Open

- **`SECURITY.md` carries a literal `SECURITY-CONTACT-NOT-YET-SET` placeholder.**
  No address was invented. Fill it in before the repo is public.
- **CI has never actually run.** Runner assumptions are untested until first
  push: apt package names, macOS gfortran availability, libclang wheels on
  3.10–3.12. Expect to iterate.
- **The generated service's build is still not reproducible**, even though the
  tool's install and the container base now are. `apt-get` runs unpinned against
  live Debian, the generated Dockerfile's `pip install` has no
  `--no-index`/`--require-hashes`, and `f2py -c` shells out to a system gfortran
  the SBOM never records — two machines with different gfortran produce an
  identical SBOM and different binaries. Documented honestly in
  `docs/deployment-topologies.md` rather than papered over.

---

## What to do next

Ordered by value per hour, not by workstream.

1. **Fill in the security contact** and push once so CI actually runs. Hours.
2. **2.3 input bounds** — a global cap plus `n` vs `len(props)` validation is
   days, and closes a memory-exhaustion vector in every array endpoint.
3. **3.2 `schema_version` + differential harness** — ~1 wk, and it is the
   prerequisite for 1.4.
4. **2.2 crash containment** — needs a topology decision from you first.
5. **1.4 fparser2** — ~5 wks. This is what stops the *next* unmodified library
   hitting the `&` class of defect.
6. **2.1b session-shaped API** — depends on 1.4.
7. **2.4 auth and observability**, **3.3 `ast` migration** — real, deferrable.

## Out of scope

- **C++ semantic analysis beyond Clang.** Binding safety is enforced by refusal —
  no raw pointer returns, no length-free pointers, no non-const `char*`. That is
  a design, not a gap.
- **STL container binding** (`std::vector`, `std::span`) — the highest-value C++
  IR extension, and the largest remaining C++ coverage gap.
- **The `libraries:` linking gap for f2py.** Fortran services must still copy
  sources into `native/`; the petro build needs all seven F77 decks alongside the
  F90 facade.
- **Migrating off f2py.** It is the constraint behind several IR compromises (no
  CHARACTER outputs, arrays forced to `intent(in,out)` to avoid allocating
  `MXCELL` elements per call). Revisit once 1.4 provides real extents.
