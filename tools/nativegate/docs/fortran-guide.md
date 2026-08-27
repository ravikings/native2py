# Exposing Fortran

Two dialects are supported, and the difference matters:

| | Free-form (`.f90`, `.f95`, `.f03`, `.f08`) | Fixed-form F77 (`.f`, `.for`, `.f77`) |
|---|---|---|
| Declarations | `real(8), intent(in) :: x` | often none — [IMPLICIT typing](#fixed-form-f77) |
| Routine end | `end function foo` | bare `END` |
| INCLUDE files | rare | common, and [f2py mishandles them](#the-include-trap) |

Uppercase suffixes (`.F90`, `.F`) mean the same dialect but run through the C
preprocessor first — see [Preprocessed Fortran](#preprocessed-fortran-f90-f-and-ifdef).

Jump to [Fixed-form F77](#fixed-form-f77) if you're working with legacy
`.f` sources.

## Designed for large legacy files

Fortran sources in this space — reservoir simulators, well-test templates,
other legacy oil & gas code — commonly run tens of thousands of lines with
hundreds of subroutines. nativegate's Fortran parser is built around the
common case of needing just one or two of those routines:

- `nativegate.yaml`'s `expose.functions:` list is **required** for Fortran
  (unlike C++, there is no "expose everything" fallback)
- only the routines you name are bound, however large the file is

How much of the file gets *read* depends on the backend. Under the regex
fallback, each requested name drives one **targeted search** for that
routine's `function ... end function` / `subroutine ... end subroutine` block
and the rest of the file is never parsed — so a 20,000-line template with one
exposed routine costs about the same as a 200-line file. The default fparser2
backend builds a parse tree of the whole file, which costs more on a huge
deck but buys grammar-accurate routine boundaries, structured attributes, and
derived-type support.

The targeted-extraction path is verified by
`tools/nativegate/tests/test_fortran_pipeline.py::test_extracts_one_routine_from_a_huge_file`,
which builds a synthetic 500-routine file and confirms only the target
routine is extracted.

```yaml
# nativegate.yaml
name: physics
language: fortran

expose:
  functions:
    - calculate_pressure   # only this routine is parsed, even in a huge file
```

A requested routine can live in any Fortran source under `native/` —
free-form `.f90`/`.f95`/`.f03`/`.f08`, fixed-form `.f`/`.for`/`.f77`, or the
preprocessed uppercase variants `.F90`/`.F` (searched via their expansions in
`native/_expanded/`). `generate` searches each file in turn until every
requested name is found, so you don't need to say which file it's in.

## What gets parsed per routine

For each exposed routine, nativegate extracts:

- parameter names, in declaration order
- parameter types, from `intent(in/out/inout) :: name` declarations, mapped
  via `ir.FORTRAN_TYPE_MAP` (`real(8)` → `float`, `integer` → `int`, ...)
- whether a parameter is an array (`dimension(...)` or `name(n)` syntax) —
  array parameters get NumPy handling in the generated service layer
- for `function`, the return type (from the `result(...)` variable's own
  declaration, or the function name's declaration if no explicit `result`)
- for `subroutine`, `is_subroutine=True` — no return value; results come
  back through `intent(out)`/`intent(inout)` array or scalar parameters

## The `module` wrapper gotcha

If your routines are wrapped in a Fortran `module` block:

```fortran
module physics
contains
    function calculate_pressure(density, temperature) result(p)
        ...
    end function calculate_pressure
end module physics
```

**f2py compiles this into a nested object**: the raw compiled extension
exposes `physics.physics.calculate_pressure`, not `physics.calculate_pressure`
at the top level. This is easy to miss and was caught during a real
`gfortran`/f2py compile, not by unit tests — the initial generator imported
at the wrong nesting level and would have failed at import time for every
module-wrapped Fortran service.

nativegate detects the enclosing `module` name during parsing
(`ModuleIR.fortran_module`) and bridges it in the generated `__init__.py` so
callers still get the flat interface design.md promises:

```python
# python/physics/__init__.py (generated)
from ._native import physics as _native

calculate_pressure = _native.physics.calculate_pressure
normalize = _native.physics.normalize

__all__ = ["calculate_pressure", "normalize"]
```

```python
from physics import calculate_pressure   # works — the nesting is hidden
```

If your Fortran source has **no** enclosing `module` (bare functions/
subroutines at file scope), f2py exposes them directly at the top level and
the generated `__init__.py` uses a plain `from ._native.<file> import ...`
instead — no bridging needed.

A service can't mix routines from two different Fortran modules; `generate`
raises a clear error if you try.

## What `generate` produces

```
CMakeLists.txt                 f2py-driven custom command + python_add_library(...)
native/_expanded/              INCLUDE- and cpp-expanded copies of the sources
                               actually handed to f2py, plus any `libraries:` decks
python/<name>/__init__.py      re-exports, bridged through fortran_module if needed
python/<name>/router.py        APIRouter with one POST per routine, GET /_unexposed,
                               and the process-wide native call lock
python/<name>/service.py       standalone FastAPI app, /healthz, exception handler
python/<name>/middleware.py    auth, rate limiting, body-size cap, /readyz, draining
tests/test_python_api.py       import + call smoke test (NumPy-aware for array params)
tests/test_golden.py           replays golden.json (skips until recorded)
pyproject.toml                 generated once, then yours
.nativegate/ir.json             machine-readable record of what bound
```

The endpoints live in `router.py`, not `service.py` — `service.py` is the thin
standalone app that mounts the router and adds `/healthz`. That split is what
lets the same router be mounted into a gateway.

**Every Fortran endpoint holds one process-wide lock across its native call**,
because COMMON blocks are process-global storage. This caps native execution
at one concurrent call per process, deliberately: without it, two requests
configuring different fluids interleave their writes into the same COMMON
block and each caller silently gets the other's numbers. Scale with processes,
not threads. See
[Architecture](architecture.md#the-concurrency-model-one-native-call-per-process-fortran).

## Verified against a real build

`services/petro_api/` — an F90 facade over the seven fixed-form F77 decks in
`libraries/petro/fortran/` — is compiled for real with `gfortran` + `f2py`,
imported through the generated package, and exercised through the generated
FastAPI service. Its `golden.json` pins 10 entry points, so a rebuild has to
reproduce the numbers, not merely compile. See
[Architecture](architecture.md#verified-not-just-tested).

## Fixed-form F77

Legacy `.f` sources are a different dialect, not just older syntax. nativegate
normalizes them before parsing: column-1 comments (`C`, `*`) stripped,
continuation lines (non-blank column 6) joined onto the statement they
continue, and columns 73+ discarded as punch-card sequence numbers.

### IMPLICIT typing

F77 routines usually declare nothing about their arguments:

```fortran
      DOUBLE PRECISION FUNCTION PVTBUB (RSTGT)
      INCLUDE 'PETRO.INC'
```

`RSTGT` has no declaration anywhere. Its type comes from an IMPLICIT
statement — typically inside the INCLUDE file:

```fortran
      IMPLICIT DOUBLE PRECISION (A-H,O-Z)
      IMPLICIT INTEGER (I-N)
```

so the **first letter of the name** decides the type. nativegate parses
IMPLICIT statements (including the FORTRAN default of I-N integer,
everything else real) and applies them to any undeclared parameter. Under
`IMPLICIT NONE`, an undeclared parameter raises an error instead of being
guessed.

This is not a nicety. Guessing `float` for `ICORR` would produce a binding
that compiles, imports, runs, and returns **wrong numbers**.

### The INCLUDE trap

f2py accepts an `--include-paths` flag and looks like it handles INCLUDE
files. It does not — for a routine with an INCLUDE **in its body**, the
wrapper it generates is silently broken: arguments never reach the Fortran
side. Measured against `libraries/petro`:

```
INCLUDE 'PETRO.INC' present      probe1(35.0) -> 1.09e-314   (uninitialized memory)
same IMPLICIT rules, no INCLUDE  probe1(35.0) -> 70.0        (correct)
INCLUDE expanded textually       probe1(35.0) -> 70.0        (correct)
```

No error, no warning — just garbage where your answer should be. So
nativegate expands INCLUDEs itself and hands f2py flat source, writing the
expanded copies to `services/<name>/native/_expanded/` (regenerated every
time; don't edit them).

Point it at your include directories:

```yaml
name: pvt
language: fortran
expose:
  functions:
    - PVTINI
    - PVTRS
include_paths:
  - libraries/petro/fortran/include
```

An INCLUDE that can't be resolved is an error naming the search path, not a
silent skip.

### Whole-file wrapping fails; per-routine works

f2py is also invoked with an `only: <routines> :` clause. For legacy F77
this is a **correctness** requirement, not an optimization — wrapping a
whole file pulls in routines whose PARAMETER-dimensioned arrays f2py emits
but never defines:

```
petromodule.c:932:16: error: use of undeclared identifier 'mxcell'
```

`matsol.f` fails to build whole-file and builds fine per-routine. This is
the same "name exactly what you expose" discipline the parser already
requires, now enforced through to the compiler.

### Output parameters (F77 has no `intent`)

Fixed-form F77 declares nothing about direction — every argument is
pass-by-reference and might be read, written, or both:

```fortran
      SUBROUTINE GASVIS (P, Z, VG)
```

`VG` is an output. Nothing in the signature says so.

f2py infers direction by analysing the routine body: a variable that is
assigned before being read becomes `intent(out)` and is **returned** rather
than passed in. Against `libraries/petro` this matched the decks' own
documentation exactly:

| Fortran | Generated Python |
|---|---|
| `SUBROUTINE GASVIS (P, Z, VG)` | `vg = GASVIS(p, z)` |
| `SUBROUTINE STEP (DTIN, DTOUT, ICONV)` | `dtout, iconv = STEP(dtin)` |
| `SUBROUTINE NODAL (..., QSOL, PWFSOL, ICONV)` | `qsol, pwfsol, iconv = NODAL(...)` |
| `SUBROUTINE THOMAS (A, B, C, D, X, N)` | `x = THOMAS(a, b, c, d, x, [n])` |

The generated FastAPI layer follows the same shape, returning the outputs as
JSON fields.

Two caveats worth knowing:

- **It is inference, not a declaration.** A routine that conditionally
  writes an argument, or writes it through a COMMON alias, can be classified
  wrongly. Check the generated signature (`help(ROUTINE)`) against the
  routine's own documentation before trusting it in production.
- **A modern F90 facade removes the guesswork.** If you can add one, a thin
  wrapper module with explicit `intent(in)` / `intent(out)` attributes makes
  the contract explicit instead of inferred —
  `libraries/petro/fortran/modern/petro_api.f90` is exactly this pattern.
  (`services/petro_api/native/petro_api.f90` is the service's own copy, and
  that is the one actually bound.)

### COMMON blocks mean the API is stateful

Legacy F77 passes state through COMMON blocks, not arguments. That survives
into Python as **module-level global state**:

```python
from pvt import PVTINI, PVTRS

PVTINI(35.0, 0.65, 180.0, 1)   # MUST be called first — sets COMMON
PVTRS(2000.0)                   # reads the COMMON set above
```

Call `PVTRS` without `PVTINI` and you get whatever was in COMMON before —
usually zeros, and `Rs = 0.0` looks like a plausible answer rather than an
error.

Two consequences worth taking seriously before putting this behind an HTTP
service:

- **It is not thread-safe or concurrency-safe.** Two requests configuring
  different fluids will corrupt each other, because they share one COMMON
  block in one process. The generated FastAPI layer does nothing to prevent
  this.
- **Initialization order is an undocumented API contract.** nativegate can't
  infer it — it comes from the original program's calling sequence.

For a real deployment, wrap the routines in a Python layer that enforces
init-before-use and serializes access (or runs one process per fluid
configuration). See
[Is this production-ready?](production-readiness.md).

### Verified against real code

`libraries/petro` (Peng-Robinson flash, PVT correlations, rel-perm, IMPES
solver — ~2,000 lines of 1988-1997 fixed-form F77) built through nativegate:

```
P=    500 psia   Rs=   79.85 scf/stb   Bo=1.0862 rb/stb
P=   1000 psia   Rs=  178.69 scf/stb   Bo=1.1261 rb/stb
P=   2000 psia   Rs=  405.73 scf/stb   Bo=1.2243 rb/stb
P=   3000 psia   Rs=  657.95 scf/stb   Bo=1.3416 rb/stb

PVTBUB(400) = 1976.225 psia -> PVTRS(pb) = 400.0000   (exact inverse)
```

and the `THOMAS` tridiagonal solver checked against NumPy:

```
max |THOMAS - numpy.linalg.solve| = 2.2e-16
```

### Preprocessed Fortran: `.F90`, `.F`, and `#ifdef`

An uppercase suffix means "run the C preprocessor first" — the file is not
Fortran yet, it is *input* to cpp. nativegate used to warn about these and then
read every `#ifdef` branch as simultaneously live, which silently binds
whichever branch happens to lex. Worse, `.F90` was not in the discovery table
at all, so a directory of preprocessed Fortran reported "No Fortran sources
found".

They are handled now: `generate` runs **gfortran's own preprocessor**
(`-cpp -E -P`) into `native/_expanded/` before anything parses, so discovery,
intent inference and f2py all see the code gfortran would have compiled.
gfortran's preprocessor rather than a bare `cpp`, because Fortran is not C — a
traditional cpp mangles `//` concatenation and fixed-form columns.

Which branch is live is the build's decision, so it comes from the config:

```yaml
fortran:
  defines:
    - DOUBLE_IT
```

Verified end to end: a `.F90` with an `#ifdef` generated, compiled through
f2py, and returned the configured branch's value. If gfortran is missing,
generation **stops with an error** rather than falling back to the old
misreading.

Two limits on where this expansion reaches:

- It runs in the `generate` path only. `inspect` and `suggest` do **not**
  preprocess, so an `#ifdef`-heavy `.F90` inspects differently from how it
  generates. Trust `generate`.
- It does **not** cover sources pulled in through `libraries:`. Those decks
  are copied and INCLUDE-expanded, but not cpp-expanded, so a library `.F`/
  `.F90` with live preprocessor branches is still read with every branch
  apparently active. Keep preprocessed sources in the service's own `native/`
  if the branch choice matters.

### Statement functions no longer widen intents

`DBL(X) = X*2.0*B` is a declaration wearing an assignment's clothes. The
intent inferencer used to count its right-hand side as a read, marking an
output like `B` as `intent(inout)` — never a wrong answer (widening keeps the
argument in the signature), but a signature demanding a value the routine
never consumes.

fparser2 is no help here — without a symbol table it classifies the line as a
plain assignment, same as a real array write (measured). The disambiguation is
semantic and exact: a *subscripted* assignment target that is never declared
as an array cannot be an array-element write, so it is a statement function.
Character substrings (`STR(1:3) = ...`) and dummy arguments are excluded, so
a real write is never skipped — skipping one would lose reads, and a lost
read can flip a genuinely-read argument to `intent(out)`, which f2py then
drops from the call.

The regex fallback backend keeps the old widening (safe, just noisier); the
fix rides the fparser2 default.

### Known limitation: source that doesn't compile

Two files in `libraries/petro` do not compile with modern `gfortran`, for
reasons that predate nativegate:

```fortran
      DIMENSION CP(MXBAND), DP(MXBAND)
      PARAMETER (MXBAND = 512)      ! declared *after* use
```

`gfortran -c matsol.f` fails on this directly. nativegate surfaces the
compiler error rather than working around it — fixing the source ordering
is the correct remedy.

### Sharing decks between services: `libraries:`

A facade usually calls into decks that are not its own. Name the library and
they are compiled into the extension:

```yaml
name: petro_api
language: fortran
libraries:
  - petro          # libraries/petro/**/*.f, plus any directory under it
                   # holding *.INC (here: libraries/petro/fortran/include/)
expose:
  functions:
    - solution_gor
```

This works differently from the C++ side, and the difference is not cosmetic.
C++ **links** a CMake target through `add_subdirectory`; `f2py -c` has no
target to link — it takes a list of sources and compiles them itself. So
nativegate copies the library's sources into `native/_expanded/` alongside the
service's own, expanding their INCLUDEs on the way, and hands the lot to f2py.

Three consequences worth knowing:

- **INCLUDE directories are discovered, not declared.** Any directory under a
  named library containing `*.INC` joins the search path. Explicit
  `include_paths:` still apply — these are added to them.
- **The original tree stays read-only**, as everywhere else in nativegate. What
  gets rewritten is the copy under `native/_expanded/`.
- **The Docker build context stays the service directory.** Everything the
  build needs has been copied under it, so unlike a C++ service with
  `libraries:`, you still build with `docker build -t petro_api:latest .` from
  `services/petro_api/`.

If the service has its own file with the same name as one in the library — the
usual case, where the F90 facade was copied in — the service's copy wins and
`generate` says so. Compiling both would be a duplicate-symbol link error.

#### What this fixed

`services/petro_api` bound its ten routines, produced an extension, and then
failed at import with `undefined symbol: iprvog_`: the seven F77 decks the
facade calls into were never compiled, because `libraries:` did nothing for
Fortran. The image now builds and runs, and every entry point returns its
recorded golden value exactly — including `tubing_bhp`, which is the one that
reaches `IPRVOG` in `wellib.f`.

### Derived types: flattened through a generated shim

f2py cannot pass a derived type — that has not changed. What changed: for a
module routine taking one, nativegate now generates a **Fortran shim** in the
`_expanded` copy that takes the components as ordinary scalars, builds the
type, calls the real routine, and copies output components back:

```fortran
type :: fluid_state
    real(dp) :: pressure
    real(dp) :: temperature
    integer  :: ncomp
end type

subroutine density(state, rho)          ! your routine, untouched
    type(fluid_state), intent(in) :: state
```

binds as `density(state_pressure, state_temperature, state_ncomp)` — the
published name is yours; the shim (`density_n2p`) is what f2py actually
wraps, and `intent(out)`/`inout` components come back in the response.
Verified by compiling the generated service and calling it.

The preconditions are narrow. Each is refused with its reason when unmet:

- **free-form source only** — the shim generator is not reachable from the
  fixed-form path, where a derived-type parameter is a hard error;
- **the fparser2 backend only** — under `NATIVEGATE_FORTRAN_PARSER=regex`,
  derived types are refused outright;
- the routine must be **inside the module that defines the type** (the shim
  constructs the type, and that is the only place it is visible from);
- the **type must be defined in the same file** as the routine, or its
  components cannot be read to flatten them;
- components must be **scalar numeric/logical** — arrays, CHARACTER and
  nested types don't flatten yet;
- subroutines only for now (wrap a function in one);
- non-derived companion arguments must be scalars.

Miss any of these and you get a refusal with the reason, not a wrong binding —
but do not plan on derived types as a general feature. Most legacy F77 will
not meet the first two conditions at all.

### CHARACTER outputs: fixed length binds, assumed length refuses loudly

Measured with numpy 2.5's f2py: `CHARACTER*32, intent(out)` builds and
returns the value — the old blanket "f2py cannot return a CHARACTER" threw
that away. It now binds, and the endpoint decodes the blank-padded bytes
f2py returns into a clean JSON string.

`CHARACTER*(*)` (assumed length) as an output is the dangerous one: it
**builds, imports, and silently returns an empty string** — measured, and
worse than a build failure. It stays demoted to an input, and the skip
message says to give it a length.

**This screening currently runs on the fixed-form F77 path only.** A free-form
`character(len=*), intent(out)` is not caught by it — it is bound with no
demotion and no warning, and will exhibit the empty-string behaviour. Declare
a fixed length in free-form source too; the tool will not remind you.

### Not yet supported

- `COMMON` blocks exposed as readable/writable Python attributes
- array / CHARACTER / nested components in derived types (scalars flatten)
