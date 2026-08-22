# Exposing Fortran

Two dialects are supported, and the difference matters:

| | Free-form (`.f90`, `.f95`, `.f03`) | Fixed-form F77 (`.f`, `.for`, `.f77`) |
|---|---|---|
| Declarations | `real(8), intent(in) :: x` | often none — [IMPLICIT typing](#fixed-form-f77) |
| Routine end | `end function foo` | bare `END` |
| INCLUDE files | rare | common, and [f2py mishandles them](#the-include-trap) |

Jump to [Fixed-form F77](#fixed-form-f77) if you're working with legacy
`.f` sources.

## Designed for large legacy files

Fortran sources in this space — reservoir simulators, well-test templates,
other legacy oil & gas code — commonly run tens of thousands of lines with
hundreds of subroutines. native2py's Fortran parser is built around the
common case of needing just one or two of those routines:

- `native2py.yaml`'s `expose.functions:` list is **required** for Fortran
  (unlike C++, there is no "expose everything" fallback)
- each requested name drives one **targeted regex search** for that
  routine's `function ... end function` / `subroutine ... end subroutine`
  block — the rest of the file is never parsed
- a 20,000-line template with one exposed routine costs about the same as a
  200-line file with the same routine

This is verified by `tests/test_fortran_pipeline.py::test_extracts_one_routine_from_a_huge_file`,
which builds a synthetic 500-routine file and confirms only the target
routine is extracted.

```yaml
# native2py.yaml
name: physics
language: fortran

expose:
  functions:
    - calculate_pressure   # only this routine is parsed, even in a huge file
```

A requested routine can live in any `.f90`/`.f95`/`.f03` file under
`native/` — `generate` searches each file in turn until every requested name
is found, so you don't need to say which file it's in.

## What gets parsed per routine

For each exposed routine, native2py extracts:

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

native2py detects the enclosing `module` name during parsing
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
python/<name>/__init__.py      re-exports, bridged through fortran_module if needed
python/<name>/service.py       FastAPI app with one POST endpoint per routine
tests/test_python_api.py       import + call smoke test (NumPy-aware for array params)
```

## Verified against a real build

The `reservoir` example in `services/reservoir/` (Fortran module `physics`,
functions `calculate_pressure` and `normalize`) has been compiled for real
with `gfortran` + `f2py`, imported through the generated package, and
exercised through the generated FastAPI service — see
[Architecture](architecture.md#verified-not-just-tested).

## Fixed-form F77

Legacy `.f` sources are a different dialect, not just older syntax. native2py
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

so the **first letter of the name** decides the type. native2py parses
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
native2py expands INCLUDEs itself and hands f2py flat source, writing the
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
  the contract explicit instead of inferred — `libraries/petro`'s
  `petro_api.f90` is exactly this pattern.

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
- **Initialization order is an undocumented API contract.** native2py can't
  infer it — it comes from the original program's calling sequence.

For a real deployment, wrap the routines in a Python layer that enforces
init-before-use and serializes access (or runs one process per fluid
configuration). See
[Is this production-ready?](production-readiness.md).

### Verified against real code

`libraries/petro` (Peng-Robinson flash, PVT correlations, rel-perm, IMPES
solver — ~2,000 lines of 1988-1997 fixed-form F77) built through native2py:

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

### Known limitation: source that doesn't compile

Two files in `libraries/petro` do not compile with modern `gfortran`, for
reasons that predate native2py:

```fortran
      DIMENSION CP(MXBAND), DP(MXBAND)
      PARAMETER (MXBAND = 512)      ! declared *after* use
```

`gfortran -c matsol.f` fails on this directly. native2py surfaces the
compiler error rather than working around it — fixing the source ordering
is the correct remedy.

### Not yet supported

- Shared Fortran libraries (`libraries:` is C++/CMake only — the f2py path
  doesn't take `add_subdirectory`)
- Derived types / structs
- `COMMON` blocks exposed as readable/writable Python attributes
