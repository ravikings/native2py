# Roadmap: API-ready without rewriting native code

The goal is not a better compiler. It is this acceptance test:

> Point native2py at an unmodified legacy source tree. Get back a running HTTP
> API that returns the same numbers as the native binary and is safe to operate
> under concurrent load.

Every item below is judged against one of two questions. **Can we ingest more
code without editing it?** **Is the resulting API safe to operate?** Work that
answers neither is real engineering debt but does not belong on this roadmap's
critical path, and it has been demoted accordingly.

Written against the code as of `4ae79a3`.

---

## The principle to generalise

native2py already solved "don't modify the source" once, correctly.
`FINDINGS.md` #3: modern `real(dp)` kind parameters broke the f2py build,
because f2py's generated wrapper does not inherit the module's `PARAMETER`
declarations. The fix was not "edit the Fortran". It was to resolve kinds on a
**generated copy** under `native/_expanded/`, leaving the original untouched.
The same mechanism carries INCLUDE expansion and `Cf2py` intent directives.

That is the pattern. Every remaining case of *"native2py can't read this, so
reformat your Fortran"* should become either a parser capability or a transform
on the `_expanded` copy. The original tree stays read-only. Anything that
cannot be handled that way must be **reported as skipped with a reason**, never
silently mis-bound.

---

## Fixed while scoping this

A committed service in this repo did not parse as Python.

```
services/petro_api/python/petro_api/router.py:41
    def tubing_bhp_endpoint(..., diameter: float, &
                                                  ^
SyntaxError: invalid syntax
```

`parsers/fortran.py` had no free-form continuation handling —
`normalize_fixed_form` existed for F77, with no free-form counterpart. So
`_find_block`'s `(?P<params>[^)]*)` capture, which matches newlines, swallowed
the `&` and produced a parameter literally named `"&\n roughness"`. It flowed
through the IR into the generated router.

`tubing_bhp` is an ordinary F90 routine whose argument list wraps across two
lines. The only two workarounds available were to reformat the Fortran onto one
line, or hand-edit a file stamped *"Do not edit by hand"* — that is, exactly the
failure this roadmap exists to eliminate.

Fixed: `normalize_free_form` in `parsers/fortran.py`, applied in both
`parse_source` and `list_routine_names`, with quote-aware `!` comment
stripping and support for a `&` opening the resuming line. Five regression
tests, one of which **compiles the generated router** — see [1.2](#12-catch-it-at-generate-time).

This bug is the template for the whole of Workstream 1: an unremarkable input,
a silent corruption, a service that cannot start, and a "fix" that means
touching source we promised not to touch.

---

## Workstream 1 — widen unmodified ingestion

*Every parse gap is a routine you can only expose by editing its source.*

### 1.1 Known blockers

| | Defect | Symptom | Status |
|---|---|---|---|
| a | Free-form `&` continuation unhandled | Generated router is a `SyntaxError` | **Fixed** |
| b | Generated Python never syntax-checked | (a) shipped committed and unnoticed | ~2 days |
| c | Python keywords as native names | `PASS = _native.pass` → SyntaxError | ~3 days |
| d | Free-form has no IMPLICIT handling | Undeclared args silently bind as `float` | ~3 days |
| e | Bare `end` / `end function` not matched | Routine reports "not found"; hard error | ~2 days |
| f | Arg-less free-form routines undiscoverable | Invisible to `quickstart` | ~2 days |
| g | Statement functions corrupt intent | Argument bound with wrong direction | fparser2 |
| h | `COMMON`/`EQUIVALENCE` outputs invisible | Routine scored as having no outputs | fparser2 |

Items (b)–(f) are roughly two weeks total and remove most of the practical
pressure to edit native source. They are worth doing before, and independently
of, the parser replacement.

### 1.2 Catch it at generate time

Only one test in the suite compiles generated code (`tests/test_golden.py:362`).
`router.py`, `__init__.py` and `test_python_api.py` were never checked — which
is why (a) reached a commit.

- Assert `compile()` on every generated `.py` for every corpus service.
- `native2py generate` refuses to write Python it cannot compile.
- Byte-determinism: generate twice, assert identical output.

A generated file that fails to parse must fail the build, not the container
start. This is the single highest value-per-hour item on the roadmap.

### 1.3 Identifier safety

Note this is **not** solved by moving to `ast`: `ast.unparse` does not validate
identifiers, and `ast.Name(id="class")` unparses to `class`. It has to be an
explicit pass over the IR.

Python keywords that are legal identifiers in Fortran and C++ include `pass`,
`lambda`, `global`, `import`, `class`, `assert`, `del`, `is`, `in`, `not`,
`and`, `or`, `from`, `with`, `yield`, `None`, `True`, `False`. Escape
deterministically (`pass_`, per PEP 8), keep the original for the native symbol
lookup and the HTTP route — the Python name changes, the wire contract does not.
Also detect duplicate names across structs, classes and functions;
`generate_router_py` emits `from . import ...` straight from IR order today.

### 1.4 Replace the regex front end with fparser2

The general form of 1.1. **Recommendation: `fparser` (fparser2), pinned
`>=0.2,<0.3`.** BSD-3-Clause, ~116k downloads/month, maintained by STFC for
PSyclone/LFRic, last release June 2026. Pure Python, so it adds no toolchain to
the install — which matters, because native2py's story is already `pip install`
plus system compilers.

```python
from fparser.common.readfortran import FortranFileReader
from fparser.two.parser import ParserFactory
from fparser.two.symbol_table import SYMBOL_TABLES

reader = FortranFileReader(path, ignore_comments=False, include_dirs=[...])
tree = ParserFactory().create(std="f2008")(reader)
```

One reader handles both dialects and follows `INCLUDE` natively, collapsing
today's two-parser split. `SYMBOL_TABLES` provides a real per-scoping-unit
symbol table — there is no equivalent in the current code.

Rejected as the default: **Flang** (best semantics, but needs a binary and
emits a text dump, not an API — revisit as an optional backend the way `clang`
is optional today); **tree-sitter-fortran** (CST, no types, weak fixed-form);
**LFortran** (better long-term ASR target, too young as a dependency).

fparser2's own limits, scoped honestly: no cross-scope type resolution, it
raises `FortranSyntaxError` rather than degrading to a partial tree, and
`SYMBOL_TABLES` is a **module-level global singleton** that must be cleared
between parses and is not thread-safe.

**Phases.**

- **1.4a — backend seam (~1 wk).** Mirror `parsers/cpp.py` exactly:
  `resolve_backend()`, `NATIVE2PY_FORTRAN_PARSER` (`auto` | `fparser2` |
  `regex`), a `parser:` key in `native2py.yaml`, and the same rule the C++ side
  already enforces — an explicit request for a missing backend is an **error,
  not a silent downgrade**. Existing logic moves to `fortran_regex.py`;
  `infer_intents` and `inject_intent_directives` move to `fortran_intent.py`,
  since directive injection is text manipulation on the `_expanded` copy and is
  backend-independent. Pure refactor, no behaviour change.
- **1.4b — parity (~2 wks).** Identical `ModuleIR` for everything the regex
  readers handle. Keep `parse_implicit_map` (rename to `implicit.py`, it is
  dialect-independent) and apply it against the symbol table — that closes (d)
  for both dialects from one code path. Gate: **zero unexplained IR diffs**
  against the differential harness ([3.2](#32-the-ir-as-a-contract)).
- **1.4c — semantics (~2 wks).** Intent inference walks `Assignment_Stmt` /
  `Call_Stmt` / `If_Stmt` / `Where_Construct` nodes instead of matching
  statement prefixes against a frozenset. The rules are already correct; only
  the recognition changes. This deletes `_NON_ASSIGNMENT_KEYWORDS`,
  `_LOGICAL_IF_RE`, `_split_logical_if`, `_ASSIGNMENT_RE`, and (g) with them.
  Adds `COMMON`/`EQUIVALENCE` aliasing (h), `ENTRY` points, cross-file `CALL`
  resolution, and `PARAMETER`-dimensioned array extents.

**Main risk:** fparser2 parses whole files, which forfeits the documented
property that a 20,000-line deck with one exposed routine costs the same as a
200-line one. Measure on `libraries/petro` first — if whole-file parse is under
~2s the property was a premature optimisation. If not, cache the tree on
`(file sha256, std, include set)` under `.native2py/parse-cache/`.

### 1.5 Acceptance

- `libraries/petro` parses and binds with no edit to any file under
  `libraries/`.
- Free-form and fixed-form reach feature parity; no dialect-asymmetric bugs.
- Every generated `.py` compiles, in a test, for every corpus service.
- A service exposing a routine named `PASS` generates, builds and serves.
- Numbers unchanged: `NODAL` → `2432.48 stb/d @ 2984.50 psia`,
  `PVTRS(2000)` → `355.08`.

---

## Workstream 2 — make the generated API safe to operate

Binding the code is not the same as having an API. This workstream is what
stands between the two, and most of it is not in the parser at all.

### 2.1 Statefulness and concurrency — the real blocker

**`COMMON` blocks make the generated API thread-unsafe, and no amount of parser
work fixes it.**

`libraries/petro/fortran/include/PETRO.INC:34` declares
`COMMON /FLUID/ API, SGG, SGW, TRES, PB, ...`. `PVTINI` writes it; `PVTRS`,
`PVTBO` and `PVTVIS` read it. The INC file states the contract explicitly:

```
C     "CALL PVTSET(P) THEN USE THE COMMON".  DO NOT ADD ARGUMENTS.
```

The generated router faithfully exposes this as independent endpoints:

```python
@router.post("/pvt_set_fluid")   # writes process-global state
@router.post("/solution_gor")    # reads it
```

FastAPI runs synchronous `def` endpoints in a threadpool. Two callers
configuring different fluids concurrently interleave `PVTINI` and `PVTRS`
**in one shared COMMON block**, and each receives numbers computed from the
other's fluid. Nothing crashes. Nothing logs. The answers are simply wrong.

The router already handles this correctly for C++ — it constructs an instance
per request, with a comment explaining exactly why. There is no Fortran
equivalent, because `COMMON` is process-global: per-request objects cannot
reach it.

Options, in ascending order of cost and value:

1. **Serialise.** A process-wide lock around every routine that touches a
   `COMMON` block. Correct, trivially implementable, and caps throughput at one
   concurrent request per process. Ship this first — it is correct-by-default
   and buys time for 3.
2. **Process affinity.** One worker process per session, routed by a session
   key. Preserves the flat API shape at real operational cost.
3. **Generate a stateful API.** Detect the write→read `COMMON` dependency at
   parse time (1.4c makes this possible) and generate a *session-shaped* API —
   `POST /session` returning a handle, subsequent calls scoped to it — instead
   of pretending the endpoints are independent. This is the only option that is
   an honest description of what the native code actually does.

Sequencing note: 1 is days, 3 depends on 1.4c. Do 1 immediately regardless —
today's service is silently wrong under load.

### 2.2 Crash containment

`docs/production-readiness.md` already calls this "the sharpest edge": a Fortran
`STOP`, a segfault, or an out-of-bounds write takes down **the whole worker
process**, not just the request. Legacy numerical code does all three on inputs
outside its validated envelope.

Needs a supervised worker pool with request-level isolation, so a crash returns
502 for one request instead of dropping every in-flight request on that worker.
This is a deployment-topology change (`docs/deployment-topologies.md`), not a
codegen change.

### 2.3 Input bounds

Every array endpoint accepts an unbounded body:

```python
def pvt_state_endpoint(pressure: float, n: int, props: list[float]):
```

That is a memory-exhaustion vector in every generated array endpoint. With
1.4c's array extents in the IR, emit a real `Field(max_length=...)`; without
them, a configurable global cap. Also validate `n` against `len(props)` — today
a mismatch is passed straight to Fortran, which indexes past the end.

### 2.4 Security and observability

Both are already inventoried in `docs/production-readiness.md` against
design.md §21 and §22, and both are generated-code concerns, which means
fixing them once fixes them for every service: authentication, rate limiting,
request-size limits, structured logging with a request id, a readiness probe
distinct from `/healthz` liveness.

Add `GET /_unexposed` returning what the parser skipped and why. That
information exists today only as a comment in a file inside a container.

### 2.5 Acceptance

- A concurrency test: two clients, two fluid configurations, interleaved
  requests, each receives its own numbers.
- A crash test: one request triggering a Fortran `STOP` returns 502 and the
  service continues serving.
- An oversized array body is rejected with 413, not an OOM kill.
- `/_unexposed` lists every skipped symbol with its reason.

---

## Workstream 3 — codegen and IR hygiene (deferred)

Real debt, genuinely worth doing, and **not on the critical path for an API**.
Two pieces graduate out of it because they answer Workstream 1's question; the
rest waits.

### 3.1 What graduates

The `compile()` gate ([1.2](#12-catch-it-at-generate-time)) and identifier
validation ([1.3](#13-identifier-safety)) were originally scoped as part of an
`ast` migration. They are independent of it, cheaper than it, and worth more.

### 3.2 The IR as a contract

`ir.py:module_from_dict` defaults every missing key
(`returns=fn.get("returns", "void")`), there is no `schema_version`, and
`services/petro_api/.native2py/ir.json` is committed — so an older file
deserialises into a subtly different module with no warning.

Needs: `schema_version` checked on load; strict deserialisation with unknown
keys rejected; a round-trip property test; and a **differential harness**
(`tests/corpus/`) that parses `libraries/petro` and `libraries/geometry` under a
named backend and snapshots the IR JSON. The harness is the acceptance gate for
1.4b and must exist before that phase starts — it is the only thing that can
tell you whether a new front end changed an answer. ~1 wk.

### 3.3 The `ast` migration

Replace string concatenation with `ast` trees plus `ast.unparse` in the three
generators whose output is IR-shaped: `generate_router_py`, `generate_init_py`,
`generate_python_api_test`. Explicitly **not** CMake, Dockerfile, pybind11 C++
or TOML — those are not Python and `ast` does not apply. `golden_gen.TEMPLATE`
should become a real shipped `.py` file so native2py's own linting covers it.

Preserve every public signature so no call site in `cli.py` changes.

`ast` has no comment nodes, and comments are load-bearing today
(`# FluidModel.get: no endpoint — returns a bound class`). Rather than
re-injecting them, promote the content to a module-level
`UNEXPOSED: dict[str, str]` — testable, survives `unparse`, and serves
[2.4](#24-security-and-observability)'s `/_unexposed`.

Also drop `jinja2`: it is a declared runtime dependency used nowhere in the
codebase.

~3 wks, whenever there is room.

---

## Sequencing

```
W1  (b)(c)(d)(e)(f) ──► 3.2 harness ──► 1.4a seam ──► 1.4b parity ──► 1.4c semantics
                                                                            │
W2  2.1 lock ──────────────────────────────────► 2.2 crash ──► 2.3 bounds ──┴──► 2.1 sessions
                                                       2.4 security / observability

W3  3.3 ast migration ····································· whenever there is room
```

**Minimum path to a defensible API — about 6 weeks:**
W1 (b)–(f), the 2.1 lock, 2.2 crash containment, 2.3 bounds, 2.4 auth and
logging. That gets you correct answers under concurrent load, a service that
survives bad input, and something you can put behind a real front door — without
touching a line of native source.

fparser2 (1.4) is what stops the *next* unmodified library from hitting the same
class of defect as the `&` bug. Worth the four weeks, but it is a durability
investment, not a launch blocker.

| Phase | Effort |
|---|---|
| W1 (b)–(f) generate-time gates and parser gaps | 2 wks |
| 3.2 IR contract + differential harness | 1 wk |
| 1.4a–c fparser2 seam, parity, semantics | 5 wks |
| 2.1 COMMON serialisation lock | 3 days |
| 2.1 session-shaped API | 2 wks |
| 2.2 crash containment | 1 wk |
| 2.3 input bounds | 3 days |
| 2.4 security and observability | 1 wk |
| 3.3 `ast` migration | 3 wks |

## Out of scope

Named so they are chosen rather than forgotten:

- **C++ semantic analysis beyond Clang.** Binding safety is enforced by refusal
  — no raw pointer returns, no length-free pointers. That is a defensible
  design, not a gap.
- **STL container binding** (`std::vector`, `std::span`) — the highest-value C++
  IR extension, unrelated to either workstream here.
- **The `libraries:` linking gap for f2py**, still open from `FINDINGS.md`;
  Fortran services must copy sources into `native/`.
- **Migrating off f2py.** It is the constraint behind several IR compromises
  (no CHARACTER outputs, arrays forced to `intent(in,out)` to avoid allocating
  `MXCELL` elements per call). Revisit once 1.4c provides real extents.
