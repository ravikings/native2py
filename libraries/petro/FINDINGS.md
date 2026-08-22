# native2py vs. a real legacy codebase — findings and fixes

Ran `native2py` against `libraries/petro` (~4,800 lines: 7 fixed-form F77
decks with COMMON blocks and INCLUDE decks, a 2003 F90 facade, and a
pre-standard C++ layer), found what broke, and fixed it.

**Status: both languages now go end-to-end from unmodified legacy source to a
working Python wheel, returning numbers identical to the native binary.**

```
                          before        after
Fortran routines           25 / 49      49 / 49    (discovery)
Fortran scalar outputs     dropped      returned   (intent inference)
C++ headers parsed          0 / 6        5 / 6     (6th is macro-only)
C++ methods bound           0           107
End-to-end import           fails       works
tests                      54           115
```

> **Correction to the first draft of this file.** It claimed fixed-form F77
> was entirely unparseable. That was a measurement error: the probe called
> `fortran.parse_source` directly and bypassed the CLI's INCLUDE-expansion
> step, so it never reached `parsers/fixed_form.py`. Through the real entry
> point, all 25 subroutines already parsed with IMPLICIT typing applied. The
> genuine Fortran defects were narrower and are listed below.

---

## Fixed

### 1. Discovery missed every typed function — `parsers/fortran.py`

`list_routine_names` (what `quickstart` uses to auto-populate `expose:`) ran
the free-form regex over raw text. Two consequences on fixed-form source:

* the prefix pattern allowed **one** token before `FUNCTION`, so every
  `DOUBLE PRECISION FUNCTION` was skipped — `PVTRS`, `PVTBO`, `PVTVIS`,
  `PVTZ`, `KROIL`, `IPRVOG`, `PEACEM`, `FIPOIL` and 15 more;
* continuation lines were never joined and column-1 comments never stripped.

It found **25 of 49** routines and said nothing about the other 24, so
`quickstart` generated a service exposing half the API with no warning.

Fixed by routing fixed-form files through `fixed_form.normalize_fixed_form`
first, allowing multi-token return-type prefixes in both the free-form and
fixed-form patterns, and accepting argument-less routines
(`SUBROUTINE TRNCAL`) which extraction already handled but discovery did not.

### 2. `CHARACTER*(*)` silently bound as an integer — `parsers/fixed_form.py`

The declaration pattern accepted `REAL*8` and `CHARACTER*8` but not the
assumed-length form `CHARACTER*(*) MSG`. So `MSG` fell through to IMPLICIT
typing, `M` is in the `I-N` integer range, and it bound as an **int** — a
binding that compiles, runs, and hands a pointer to a routine expecting a
string descriptor. This was the one defect that could return a wrong answer
rather than fail.

### 3. `real(dp)` broke the f2py build — `preprocess.py`

f2py's generated wrapper does not carry the module's `PARAMETER`
declarations, so the standard modern idiom

```fortran
integer, parameter :: dp = kind(1.0d0)
real(dp), intent(in) :: x
```

compiles as `real(dp)` in a scope with no `dp`. gfortran degrades the
argument to `REAL(4)` and the build dies ~200 lines later in a type mismatch
naming neither `dp` nor the line at fault.

Now resolved to a literal (`real(8)`) on a generated copy under
`native/_expanded/`, the same mechanism already used for INCLUDE expansion.
The original file is never modified. Handles `kind(1.0d0)`, `kind(1.0)`,
`selected_real_kind(...)`, and the `real(kind=dp)` spelling; anything it
cannot resolve is left alone so it still fails loudly rather than being
silently given the wrong width.

### 4. A facade over legacy decks was rejected outright — `cli.py`

Mixing module-scoped F90 routines with bare F77 routines in one service
raised *"can't mix routines from different Fortran modules"*. That is exactly
the shape of a migration in progress: a modern facade over fixed-form decks.

`FunctionDef` now carries its own `fortran_module`, and `generate_init_py`
re-exports each routine from wherever f2py actually put it — nested under
`_native.petro_api.*` for module routines, top level for F77 ones.

### 5. F77 scalar outputs were computed and thrown away — `parsers/fixed_form.py`

F77 has no `intent` attribute; every dummy argument is passed by reference and
any of them may be an output. Given nothing else, f2py binds scalars as
`intent(in)`, so

```fortran
SUBROUTINE STEP (DTIN, DTOUT, ICONV)
```

became `step(dtin, dtout, iconv) -> None`. Nothing crashed — the caller simply
never saw the answer. The same applied to `NODAL` (rate, flowing BHP,
convergence flag), `TRAVER` (bottomhole pressure), `GASVIS` (viscosity), and
`LSOR` (iteration count, residual).

The intent is recoverable from the body, and is now inferred:

* assigned to → output
* read before it is assigned → also an input, so `inout`
* passed to a `CALL` → resolved against the callee's own inferred intents
  when the callee is in the same file, and widened to `inout` when it is not

Care was needed in four places, each of which produced a wrong answer first:

* **Declarations are not reads.** Counting `DOUBLE PRECISION X` or
  `DIMENSION A(N)` as a use made every declared output look like an input.
* **A logical IF is two contexts.** `IF (NSEG .LT. 1) NSEG = 20` reads NSEG in
  the condition and writes it in the guarded statement; looking at either half
  alone gets it wrong.
* **Element assignment is not definition.** `X(L) = X(L) + ...` updates an
  array, it does not define it, so X stays `inout`. `THOMAS` — which writes
  every element before reading any — is correctly a pure output.
* **Reads after a write don't matter.** They do not make an output an input.

The inference alone changes nothing about the build, so the results are
written into the generated `_expanded` copy as f2py directives:

```fortran
      SUBROUTINE STEP (DTIN, DTOUT, ICONV)
Cf2py intent(out) DTOUT
Cf2py intent(out) ICONV
```

Scalars use `intent(out)` / `intent(in,out)` — `intent(inout)` on a scalar
would force callers to pass a 0-d numpy array. Arrays always use
`intent(in,out)` so the caller supplies the storage: they are routinely
dimensioned by a PARAMETER from an INCLUDE deck (`AE(MXCELL)`), and a bare
`intent(out)` would have f2py allocate 40,000 elements per call.

Resulting signatures:

```
vg = gasvis(p,z)
dtout,iconv = step(dtin)
nseg,pbh = traver(pwh,qo,qw,qg,dia,eps,tvd,md,nseg)
qsol,pwfsol,iconv = nodal(pravg,qmax,pwh,dia,eps,tvd,md,wcut,gor)
x = thomas(a,b,c,d,x,[n])
```

`NODAL` returns `2432.48 stb/d @ 2984.50 psia (iconv=0)` — identical to the
native binary, where before it returned `None`.

### 6. The C++ parser could not read a normal class — `parsers/cpp.py`

Rewritten to work on brace and token structure instead of line regexes.

| Defect | Effect before |
|---|---|
| `public:` matched as a **return type** | whole header failed: *"Unsupported native type 'public:'"* |
| No access tracking | when it didn't fail, **private methods were bound as public** |
| Constructors never matched | `has_default_constructor` was a guess; arg constructors unreachable |
| `const` not stripped | `const char*`, `const double&` fatal to the header |
| No `extern "C"` handling | the cleanest bindable surface returned an empty module |
| No struct node | every method returning a struct was dropped |
| Comments not stripped | prose and commented-out code parsed as declarations |
| Unsupported type raised | one bad signature cost you the other thirty |

Now: access specifiers tracked (class defaults private, struct defaults
public), constructors captured with their argument types, cv-qualifiers and
references stripped, `char*` → `str`, `extern "C"` and `namespace` blocks
scanned, structs bound with read/write fields, comments and inline bodies
removed lexically, and unsupported symbols **skipped with a printed reason**
instead of aborting the file.

Two refusals are deliberate and remain:

* `double* values` — no length information, so no safe binding exists.
  Guessing gives a segfault instead of an exception.
* returning `FluidModel*` — the default return-value policy would hand
  ownership to Python while C++ still owns the object.

Both are now reported per symbol rather than taking the header down.

---

## Verification

`cpp/examples/smoke.cpp` linked natively:

```
Pb = 4784.82 psia   Rs(2000) = 355.08 scf/stb   Bo(2000) = 1.2377 rb/stb
```

Through generated pybind11 bindings (`FluidModel.hpp` → `services/fluid`):

```python
>>> f = FluidModel_cpp.FluidModel(35.0, 0.65, 180.0, 2)   # arg constructor
>>> f.bubble_point(), f.solution_gor(2000.0), f.oil_fvf(2000.0)
(4784.82, 355.08, 1.2377)
>>> f.properties_at(2000.0).rho_oil                        # struct return
45.339
```

Through generated f2py bindings (7 F77 decks + F90 facade → `services/petro`):

```python
>>> import petro
>>> petro.PVTINI(35.0, 0.65, 180.0, 2)                     # 1988 fixed-form
>>> petro.PVTRS(2000.0), petro.PVTBO(2000.0)
(355.08, 1.2377)
>>> petro.pvt_set_fluid(35.0, 0.65, 180.0, 2)              # 2003 F90 facade
>>> petro.solution_gor(2000.0), petro.bubble_point(1000.0)
(355.08, 4784.82)
```

Inferred F77 outputs, which previously returned `None`:

```python
>>> petro.GASVIS(2000.0, 0.8821)                           # VG is an output
0.01651
>>> petro.NODAL(4000.0, 6000.0, 250.0, 0.2010, 0.0006, 8000.0, 8400.0, 0.15, 600.0)
(2432.48, 2984.50, 0)          # native binary: 2432.48 stb/d @ 2984.50 psia
>>> petro.THOMAS(a, b, c, d, np.zeros(4))                  # array output
array([1., 1., 1., 1.])
```

Identical to the native binary in every case.

Regression coverage: `tests/test_legacy_cpp.py` (26) and
`tests/test_legacy_fortran.py` (35), plus the 54 pre-existing tests — 115 total.

---

## Not fixed

| Item | Why |
|---|---|
| `FortranBridge.hpp` macro-mangled declarations | needs a real preprocessor; now reported as skipped rather than returning an empty module |
| `double*` array parameters | needs a length convention in `native2py.yaml` (e.g. `arrays: {values: n}`) |
| Fortran `libraries:` linking | `libraries:` is wired for C++ but not for f2py; Fortran services must copy sources into `native/` |
| Cross-file CALL resolution | Intent inference resolves callees within one file. A `CALL` into another deck widens the argument to `inout` — correct, just verbose |
| `CHARACTER` output arguments | f2py cannot return a CHARACTER argument; those stay inputs and are reported |
