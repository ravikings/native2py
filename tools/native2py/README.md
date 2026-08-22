# native2py

Expose existing C++ and Fortran code to Python as deployable microservices —
without hand-writing bindings.

Point it at a header or a Fortran source and it generates the pybind11/f2py
bindings, the CMake build, an installable Python package, a FastAPI service,
smoke tests, a numerical regression baseline, and a Dockerfile.

```bash
native2py quickstart reservoir/native/FluidModel.hpp --build
pip install services/fluidmodel/dist/*.whl
```

```python
from fluidmodel import FluidModel

fluid = FluidModel(35.0, 0.75)   # 35 °API oil, 0.75 gas gravity
fluid.oil_fvf(2500.0)            # formation volume factor at 2500 psia
```

...plus a FastAPI service over the same binding:

```bash
uvicorn fluidmodel.service:app
curl -X POST "http://localhost:8000/oil_fvf?api=35&gas_gravity=0.75&pressure=2500"
```

Built for re-hosting the kind of code that runs an engineering business:
1990s C++ over F77, fixed-form decks with COMMON blocks and INCLUDE files,
PVT correlations nobody wants to rewrite and nobody can afford to get wrong.

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
.venv/bin/pip install "native2py[clang,build,test] @ git+https://github.com/ravikings/ThinkDeck.git@<sha>#subdirectory=haliburtion/tools/native2py"

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
lost. It also carries the production-readiness gap list, so the agent raises it
unprompted rather than handing over a service with no auth as if it were done.

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
native2py docker pvt --build      # multi-stage image, non-root, healthcheck
native2py gateway platform-api --service pvt --service sim
```

## What it parses

**C++** — via Clang (libclang), the same front end that compiles the code.
The preprocessor runs, so `#include`d types, `#define`d names, `#ifdef`
branches and macro-mangled `extern "C"` bridge declarations resolve. Handles
classes, structs, overloads, public inheritance, constructors, typedefs and
`using` aliases, enums, namespaces, `std::string`, static methods, abstract
classes, `= default` / `= delete`.

Refused *with a reason*, never silently: templates, `std::vector<T>`, raw
numeric pointers (no length information), pointer returns of class types
(ownership), and types only forward-declared. A header that does not compile
is refused outright rather than half-bound — clang recovers from an unknown
type by pretending it was `int`, and binding that produces a service whose
signatures disagree with the C++ it calls.

**Fortran** — targeted per-routine extraction: you name the routines you want
and a large legacy deck costs only what you expose, rather than being parsed
whole. Fixed-form F77 and free-form F90+, INCLUDE
expansion, kind-parameter resolution (`real(dp)` → `real(8)`), inferred
argument intent, module-nested routines.

Everything the parser recognises but cannot bind is reported at `generate`
time with the reason — the failure mode that costs the most time is a header
with thirty methods producing a module with twenty-six and nothing saying
which four are missing.

## Numerical regression

For re-hosted engineering code, "it builds and imports" is not the acceptance
criterion — **the answers did not change** is.

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

## Layout

```
native2py/
  cli.py            the CLI (click)
  ir.py             language-neutral IR — the seam every layer meets at
  config.py         native2py.yaml
  golden.py         numerical regression harness
  parsers/
    cpp.py          front door: picks a backend, reports which one ran
    cpp_ast.py      Clang AST parser (primary)
    cpp_regex.py    token/brace reader (fallback, no libclang needed)
    fortran.py      free-form F90+
    fixed_form.py   F77 fixed-form
  generators/       pybind11, f2py, CMake, Python package, FastAPI, tests,
                    golden test, Dockerfile, gateway, pyproject
```

Parsers normalise source into `ir.ModuleIR`; generators consume it without
knowing which language it came from. Both C++ backends emit the identical IR,
which is why swapping the regex reader for a real compiler front end changed
nothing below `parsers/`.

## Developing

```bash
.venv/bin/python -m pytest tests -q                       # 220 tests
NATIVE2PY_CPP_PARSER=regex .venv/bin/python -m pytest -q  # fallback backend
.venv/bin/python -m mkdocs serve                          # docs at :8000
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
- [native2py.yaml reference](docs/configuration.md) · [CLI reference](docs/cli-reference.md)
- [Architecture](docs/architecture.md) — the IR, the parser seam, and what is
  generated versus what is yours
- [Deployment topologies](docs/deployment-topologies.md) — one image per
  service, or one composed gateway

## Before you ship

Read [Is this production-ready?](docs/production-readiness.md) first. Short
answer: not as-is. The compile-and-serve path is real and verified, but the
generated endpoints have no auth, no rate limiting and no exception handling;
native code that segfaults takes the worker down with it; there is no
sandboxing, timeout or per-call isolation; and there are no CI/CD or
Kubernetes templates. That page is an honest gap list, not a sales pitch —
use it to decide what to add before a paying workload touches this.
