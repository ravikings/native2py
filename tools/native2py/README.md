# native2py — automated modernization of legacy scientific computing

Expose existing C++ and Fortran code to Python as deployable microservices —
without hand-writing bindings, and without touching the numerics.

Point it at a header or a Fortran source and it generates the pybind11/f2py
bindings, the CMake build, an installable Python package, a FastAPI service,
smoke tests, a numerical regression baseline, and a Dockerfile.

```bash
native2py quickstart libraries/geometry/geometry.hpp --name demo --build
pip install services/demo/dist/*.whl
```

```python
from demo import Geometry

Geometry().hypotenuse(3.0, 4.0)   # 5.0
```

...plus a FastAPI service over the same binding:

```bash
native2py serve demo
curl -X POST "http://localhost:8000/hypotenuse?a=3&b=4"
```

Built for re-hosting the kind of code that runs an engineering business:
1990s C++ over F77, fixed-form decks with COMMON blocks and INCLUDE files,
PVT correlations nobody wants to rewrite and nobody can afford to get wrong.
The worked example of that in this repo is `services/petro_api`, generated
from the Fortran decks in `libraries/petro/`.

## Install

```bash
cd tools/native2py
./scripts/bootstrap.sh            # creates .venv, installs everything
```

Or piecemeal, into an environment of your own:

```bash
pip install -e ".[clang,build,test,docs]"
```

| Extra | What it adds |
|---|---|
| `clang` | libclang — the C++ AST parser. Without it, C++ falls back to a much weaker regex reader |
| `build` | scikit-build-core, pybind11, numpy, fastapi, uvicorn — needed to build generated services |
| `test` | pytest, httpx |
| `docs` | mkdocs + material |

Python 3.10+. `native2py build` also needs a system toolchain that pip cannot
install: `cmake`, `ninja`, a C++ compiler, and `gfortran` for Fortran services.

### Global install (`native2py` on your PATH everywhere)

To get the `native2py` command in every shell/project without activating a
venv, install this checkout editable with [pipx](https://pipx.pypa.io):

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath        # adds ~/.local/bin to PATH, restart your shell after
pipx install -e tools/native2py   # run from the repo root
```

`-e` keeps it editable, so changes under `native2py/` take effect immediately
— no reinstall needed. Verify with `native2py --version` from any directory.

If `pyproject.toml`'s `dependencies` list changes (e.g. a new package is
added), `pipx install -e` won't pick it up on its own — the venv pipx made
already exists. Re-run the install to rebuild it:

```bash
pipx reinstall native2py
```

## Using it in another project

native2py is an ordinary pip package with a console entry point. Nothing in it
refers to this repo: `services/` and `libraries/` resolve against your
**current working directory**, so "use it elsewhere" means install it into
that project's environment and run it from that project's root.

```bash
cd /path/to/other-project
python3 -m venv .venv
.venv/bin/pip install "native2py[clang,build,test] @ git+https://github.com/ravikings/native2py.git@<sha>#subdirectory=tools/native2py"

.venv/bin/native2py quickstart src/mylib.hpp --build
```

`quickstart` scaffolds `services/mylib` itself — no separate `init` needed
unless you also want the empty `libraries/`/`infrastructure/` directories.
`--build` compiles the wheel in the same run. Progress prints as a live
checklist (scaffold → copy source → generate bindings → build):

```
✔ Scaffold service
✔ Copy native source
✔ Generate bindings & package
✔ Build wheel

Done — services/mylib is ready.
```

Pin `@<sha>` (or a tag). This is a code generator: an unpinned upgrade can
change the bindings under a service that was working, and you want that to be
a deliberate commit rather than a surprise on someone's laptop.

Installing from a local checkout works the same way, and is what you want
while changing the tool itself:

```bash
pip install -e "/path/to/haliburtion/tools/native2py[clang,build,test]"
```

Three things worth knowing before rolling it out to a team:

- **Extras are not optional in practice.** Without `clang` the C++ front end
  silently falls back to the regex reader and your macro-defined and
  `#include`d symbols quietly vanish — set `parser: clang` in
  `native2py.yaml` so a mis-provisioned machine fails loudly instead. Without
  `build`, `native2py build` cannot run; without `test`, `native2py test`
  cannot.
- **Run from the project root.** `libraries:` and `include_paths:` in
  `native2py.yaml` resolve from there too.
- **Generated services do not depend on native2py.** `services/<name>/` is a
  self-contained wheel plus a FastAPI app — hand it to a team that has never
  installed the generator and it still builds.

## Use it as an agent skill

The tool ships a Claude Code skill at `tools/native2py/skill/SKILL.md`, so a
coding agent reaches for `native2py` instead of hand-writing a binding. This
repo already exposes it — `.claude/skills/native2py` is a symlink to that
directory, so editing `SKILL.md` updates the installed skill with no copy step.

To install it in another project:

```bash
mkdir -p .claude/skills
cp -r /path/to/native2py/tools/native2py/skill .claude/skills/native2py
# or, to track the tool: ln -s /path/to/native2py/tools/native2py/skill .claude/skills/native2py
```

The skill covers what the agent has no way to infer from the CLI's `--help`:
that `suggest` exists so it should not read headers itself to pick a starting
file, that a `regex reader` line means the parse is missing symbols and must
not be trusted, that skipped-binding reasons should be relayed rather than
summarized, and that generated files are rewritten every run so hand-edits are
lost. It also carries the production-readiness gap list, so the agent raises
the process-wide call lock and the single-tenant boundary unprompted rather
than handing over a service as if it were ready for anything.

### Why an agent should use it: the token argument

For a human the saving is hours. For an agent it is context, and context is
the binding constraint on whether the task finishes at all.

Hand-writing the binding for one 20-method C++ class means *emitting* the
pybind11 translation unit, `CMakeLists.txt`, `pyproject.toml`, the package
`__init__`, a FastAPI app and a test module — on the order of 500 lines, call
it 7–10k output tokens if every line lands correctly on the first attempt.
They do not. Each compile or link error costs another round of reading the
error, re-reading the file, and re-emitting a corrected version; three or four
rounds is normal for template-adjacent code, and `include/`-vs-`src/` layout
and undefined-symbol-at-import failures are exactly the kind of thing that
burns several. A realistic figure is **20–40k tokens per file**, most of it
output, and all of it resident in context afterwards — crowding out the actual
engineering question the agent was asked.

The same work through native2py is one command and a four-line checklist:
roughly **300–500 tokens**, and the generated files never enter context
because the agent has no reason to read them. Those are estimates from the
size of the generated artifacts, not a measured benchmark — but the ratio is
not a close call, and it is structural rather than a matter of prompting the
agent better.

Three effects beyond the raw count:

- **The repair loop disappears, not just shrinks.** The generator emits code
  that already compiles for the constructs it accepts, and refuses the ones it
  cannot bind *with a reason*. The agent never enters the read-error →
  re-emit → rebuild cycle that dominates hand-written binding cost, and never
  spends a round discovering that a header does not compile.
- **`golden verify` is a verification signal the agent cannot fake.** Left to
  itself, an agent asked whether a re-hosted library still works writes tests
  from its own reading of the source — which validates its interpretation, not
  the numbers. `golden record` / `golden verify` gives a real pass/fail on the
  answers for a couple of hundred tokens.
- **Correctness stops depending on how much context is left.** A hand-written
  binding degrades as the window fills — later methods get less attention than
  earlier ones. Generated output is identical at the start of a session and at
  80% context.

## Which file do I point it at?

For a codebase you did not write, `suggest` parses every header it can find
and ranks them by how cleanly they would bind — preferring self-contained
files, because a good first service is one that drags nothing else in:

```
$ native2py suggest libraries/petro/cpp

      file                        binds                     skipped  notes
  ~   include/Units.hpp           11 fn, 1 class, 9 method        1  Units.cpp
  ~   include/WellModel.hpp       1 class, 21 method                 WellModel.cpp; needs FortranBridge.hpp
  ~   include/FluidModel.hpp      1 class, 18 method, 1 struct       FluidModel.cpp; needs FortranBridge.hpp
  ~   include/Simulator.hpp       1 class, 24 method             10  Simulator.cpp; needs DeckReader.hpp, FluidModel.hpp +2
  ✘   include/FortranBridge.hpp   2 fn, 3 struct                 26  no .cpp found

Start with libraries/petro/cpp/include/Units.hpp:

  native2py quickstart libraries/petro/cpp/include/Units.hpp --name units --build
```

`✔` binds everything it declares and needs no other header; `~` binds more
than it skips; `✘` binds nothing usable. Dependencies outrank skip count in
the ranking — one skipped array method costs you that method, while one
`#include` of another subsystem costs you that subsystem's whole build.

If the header line says *regex reader* rather than *clang AST*, install the
parser first (`pip install "native2py[clang]"`) and re-run: the fallback
reader silently misses free functions and anything behind a macro, so the
ranking will be scored on an incomplete picture.

## The loop

Run everything from the repo root — commands resolve `services/<name>/`
relative to your working directory.

```bash
native2py init                       # optional: empty libraries/, infrastructure/ dirs
native2py suggest src/                    # which file should I start with?
native2py quickstart src/pvt.hpp --build  # scaffold + expose + generate + build, one shot
native2py inspect src/pvt.hpp        # what the parser sees, before generating
native2py generate pvt               # re-run codegen after editing native2py.yaml
native2py build pvt                  # pip wheel . -> scikit-build-core -> CMake
native2py test pvt                   # the generated pytest suite
native2py golden record pvt       # pin the numbers (commit golden.json)
native2py golden verify pvt       # prove a rebuild returns the same answers
native2py lock pvt                   # pin deps by version + SHA-256
native2py docker pvt --build         # multi-stage image, non-root, healthcheck
native2py k8s pvt                    # -> infrastructure/kubernetes/pvt.yaml
native2py gateway platform-api --service pvt
```

## What it parses

**C++** — via Clang (libclang), the same front end that compiles the code.
The preprocessor runs, so `#include`d types, `#define`d names, `#ifdef`
branches and macro-mangled `extern "C"` bridge declarations resolve. Handles
classes, structs, overloads, public inheritance, constructors, typedefs and
`using` aliases, enums, namespaces, `std::string`, static methods, abstract
classes, `= default` / `= delete`. Also: `std::vector<T>` of scalars,
`const char*` inputs, operators bound as Python special methods (`__add__`,
`__eq__`, `__getitem__`, …), and a raw `T*` paired with a length argument
bound as a numpy buffer — including on class methods.

Refused *with a reason*, never silently: templates (a typedef does not help),
a non-const `std::vector<T>&` (pybind11 would discard what you write into it),
a non-const `char*` (an output buffer with no length convention), a raw
pointer with no length argument to pair it with, pointer returns of class
types (ownership), and types only forward-declared. A header that does not
compile is refused outright rather than half-bound — clang recovers from an
unknown type by pretending it was `int`, and binding that produces a service
whose signatures disagree with the C++ it calls.

`extern "C"` functions whose bare `T*` arguments are single scalars passed by
reference (the Fortran-linkage convention) can be opted in per function with
`clang.scalar_ref_functions`. It is opt-in and never inferred, because an
array whose extent lives in a COMMON block or PARAMETER looks identical in a C
prototype. It applies to free functions under the clang backend only.

**Fortran** — you name the routines you want, and a large legacy deck costs
only what you expose. Fixed-form F77 and free-form F90+, INCLUDE expansion,
kind-parameter resolution (`real(dp)` → `real(8)`), inferred argument intent,
module-nested routines, array arguments, and `.F90`/`.F` sources run through
`gfortran -cpp` first.

Two newer Fortran features are narrower than they sound, so they are worth
stating precisely. **Derived types** are bound by generating a flattening
shim, but only for a *subroutine*, in *free-form* source, parsed by the
*fparser2* backend, where the routine is inside a module, the type is defined
in the same file, and every component is a scalar `real`/`integer`/`logical`.
**CHARACTER outputs** are bound only when the declaration fixes the length
(`CHARACTER*80`); an assumed-length output is demoted to an input and
reported, because f2py builds it and then silently returns an empty string.
That screening currently runs on the fixed-form path only.

Everything the parser recognises but cannot bind is reported at `generate`
time with the reason — the failure mode that costs the most time is a header
with thirty methods producing a module with twenty-six and nothing saying
which four are missing.

## Verification

For re-hosted engineering code, "it builds and imports" is not the acceptance
criterion. Three gates sit beside each other, each answering a different
question that the others structurally cannot:

| | asks | compares against | catches |
|---|---|---|---|
| `golden.json` | did it change? | its own past | compiler/flag drift, a regenerate picking a different overload, a refactor of the native source |
| `oracle` | is it faithful? | the legacy binary | transposed arrays, wrong intent, narrowing, units, argument-order swaps, a wrong first recording |
| `invariants.json` | is it possible? | mathematics | wrong away from the sample point, NaN at domain edges, hidden process-global state |

Run every layer at once, in the order CI runs them — oracle first (a
faithful binding is a precondition for the other two meaning anything), then
golden, then invariants — with each layer reported by name so one layer's
failure never masks another's:

```bash
native2py verify pvt
# oracle: passed (9 covered, 0 skipped)
# golden: passed
# invariants: passed
```

### Layer 1 — golden.json: did the answer change?

```bash
native2py golden record pvt     # services/pvt/golden.json — commit this
native2py golden verify pvt     # 4 entry point(s) unchanged (0 not covered).
```

`golden.json` records every bound entry point, the inputs it was called with,
and the answer. Edit the inputs to values your engineers recognise (a real
pressure, a real API gravity); they survive future re-records. The generated
`tests/test_golden.py` replays the same calls inside the service's own suite,
so CI catches drift with no extra wiring. Changing a conversion constant from
`14.5037738` to `14.5038` fails the check — a change no compiler, type
checker or unit test would flag.

Its limit is structural: golden only asserts about the one input tuple it
recorded, and only proves the binding is *unchanged*, never that the first
recording was *right*.

### Layer 2 — oracle: is the binding faithful to the legacy code?

```bash
native2py oracle check pvt
# 9 covered, 0 skipped
```

`oracle check` generates a driver **in the original language** from
`golden.json`'s recorded entries (never from a fresh sample plan), compiles
only the driver translation unit, links it against the extension's own built
objects (never a second compilation of the library sources), runs both
paths in the same build, and compares every observable value **bitwise** —
16 hex digits of IEEE-754, no tolerance, because the same machine code
produced both sides. It needs a compiler; it needs no committed file, and
regenerates the driver on every run so a stale one can never pass. A failing
comparison is classified (argument order, transposition, `float32`
narrowing, a missing in-place output, ...) so the fix is obvious from the
message rather than from a difference alone.

### Layer 3 — invariants.json: is it possible?

```bash
native2py invariants verify pvt
# 6 function(s) checked, 1 uncovered (services/pvt/invariants.json):
#   solution_gor: pass (6 propert(y/ies), 33 point(s))
#   [uncovered] tubing_bhp: no range declared for parameter(s) rate
```

Declare what a function's answer must satisfy in `native2py.yaml`:

```yaml
state:
  setup: [pvt_set_fluid]     # replayed before every property evaluation
  mutating: [pvt_set_fluid]  # this routine's job IS to change state
  error_flag: last_error

invariants:
  solution_gor:
    - bounds: {min: 0.0}
    - monotone: {in: pressure, direction: nondecreasing}

ranges:
  pressure: [14.7, 10000.0]  # swept, 33 points, inclusive
```

`invariants verify` checks two kinds of property over a fixed, declared
lattice (a per-parameter sweep, never random, never a cartesian explosion):
**structural** properties (`finite`, `total`, `no_error_flag`, `idempotent`,
`order_independent` — derived from the IR plus the `state:` declaration, no
domain knowledge needed) and **declared** properties (`bounds`, `monotone`,
`sum_to_one` — a closed, non-`eval`'d vocabulary authored by a human). A
failing declared property reports the first failing lattice point and
bisects to the tightest bracket it can prove. A swept parameter with no
declared range is not skipped silently — it is recorded under `uncovered`,
and **a run whose `checked` block comes out empty is a hard failure**, never
a quiet pass: an invariants file that checks nothing must not look like one
that does.

## Layout

```
native2py/
  cli.py            the CLI (click)
  ir.py             language-neutral IR — the seam every layer meets at
  config.py         native2py.yaml
  golden.py         numerical regression harness
  discovery.py      language/dialect detection
  preprocess.py     INCLUDE expansion and gfortran -cpp for .F90/.F
  suggest.py        ranks candidate sources
  locking.py        dependency pinning for requirements.lock
  parsers/
    cpp.py              front door: picks a C++ backend, reports which ran
    cpp_ast.py          Clang AST parser (primary)
    cpp_regex.py        token/brace reader (fallback, no libclang needed)
    fortran.py          front door: picks a Fortran backend
    fortran_fparser.py  fparser2 parse tree (primary)
    fortran_regex.py    targeted per-routine reader (fallback)
    fixed_form.py       F77 fixed-form helpers (IMPLICIT, CHARACTER lengths)
  generators/       pybind11, f2py, CMake, Python package, FastAPI router,
                    middleware, error handling, tests, golden test,
                    Dockerfile, gateway, Kubernetes, pyproject
```

Parsers normalise source into `ir.ModuleIR`; generators consume it without
knowing which language it came from. Both C++ backends emit the identical IR,
which is why swapping the regex reader for a real compiler front end changed
nothing below `parsers/`. The Fortran side has since gone the same way: the
fparser2 backend replaced the regex reader as the default, and the regex
reader is still there for machines without the package.

## Developing

```bash
.venv/bin/python -m pytest tests -q                           # 694 tests
NATIVE2PY_CPP_PARSER=regex .venv/bin/python -m pytest -q      # C++ fallback backend
NATIVE2PY_FORTRAN_PARSER=regex .venv/bin/python -m pytest -q  # Fortran fallback backend
.venv/bin/python -m mkdocs serve                              # docs at :8000
```

The suite runs green under both C++ backends — that is the contract that lets
either one be selected. Beyond unit tests, both language paths are verified by
really building them: generated CMake fed to actual `cmake`/`clang++`/
`gfortran`, the resulting extension imported, the FastAPI service exercised
over HTTP. Two real bugs (f2py module nesting, the missing-`.cpp` link
failure) were found that way and not by inspection.

## Docs

`docs/`, or `mkdocs serve`:

- [Getting started](docs/getting-started.md) — C++ end to end
- [Exposing C++](docs/cpp-guide.md) / [Exposing Fortran](docs/fortran-guide.md)
- [Numerical regression](docs/golden-values.md)
- [Troubleshooting](docs/troubleshooting.md)
- [native2py.yaml reference](docs/configuration.md) · [CLI reference](docs/cli-reference.md)
- [Architecture](docs/architecture.md) — the IR, the parser seam, and what is
  generated versus what is yours
- [Deployment topologies](docs/deployment-topologies.md) — one image per
  service, or one composed gateway

## Before you ship

**Honest positioning: this is usable today for internal, single-tenant
deployments, where a team is putting an HTTP or Python interface on legacy
code it already trusts. It is not ready for internet-facing or multi-tenant
use.**

The compile-and-serve path is real and verified. Generated services do now
carry API-key auth (`api.auth: api_key`), rate limiting, a request size cap,
request IDs and access logging, an exception handler, `/healthz` and `/readyz`
with SIGTERM draining, and Kubernetes manifests via `native2py k8s`.

What remains, and why it is not just a to-do item:

- **Fortran endpoints are serialised process-wide.** Each holds one lock
  across the native call, because COMMON blocks are process-global storage and
  two concurrent requests that configure different states would otherwise read
  back each other's numbers silently. Throughput scales with processes, not
  threads — do not plan capacity as if these endpoints were concurrent. C++
  services are not locked (a fresh instance per request covers the usual
  case), which also means a C++ library holding file-scope static state has no
  protection here and the parser cannot detect that it needs any.
- **Stateful libraries get a session-shaped API** — "configure, then read".
  That is fine when one tenant owns the process and wrong when it does not.
- **`MAX_ARRAY_ITEMS` is a memory guard, not a bounds check.** Emitted for
  services that take array arguments. The IR records that a parameter is an
  array, not the extent the routine actually declares, so one configurable cap
  (`NATIVE2PY_MAX_ARRAY_ITEMS`, default 65536) stands in for all of them. A
  real extent check waits on the fparser2 front end.
- **A segfault still takes the worker down.** No sandbox, no per-call timeout,
  no per-call isolation.
- No CI/CD templates; `infrastructure/docker/` is an empty skeleton.

Read [Is this production-ready?](docs/production-readiness.md) and
[DEFECTS.md](DEFECTS.md) before a paying workload touches this. Those are
honest gap lists, not a sales pitch.

### Refusals are part of the contract

When the parser recognises a symbol but will not bind it, that is a decision,
not a gap it forgot to fill: a non-const `char*` output buffer has no length,
a pointer return has no ownership, a derived-type Fortran result has no
mapping. Each refusal is printed at `generate` time with its reason, and the
generated service publishes them at `GET /_unexposed` so they stay visible to
whoever is calling the API. An empty mapping there means this build refused
nothing; a 404 means the service predates the route and cannot tell you.
