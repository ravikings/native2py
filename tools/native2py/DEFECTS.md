# Defect inventory: what breaks when `native2py` is pointed at real native code

Audit of `tools/native2py` as of `4ae79a3` plus the free-form continuation fix.
Every item below was **reproduced**, not inferred. Probe sources and commands
are given per class.

---

> ## STATUS: every defect below is fixed and verified — merged in `2380fc7`
>
> | | Before | After |
> |---|---|---|
> | Tests | 238 (223 pass, 15 could not run) | **441, all passing** |
> | Probe C++ header | 2 hard compile errors, 0 symbols skipped | compiles clean, 1 symbol correctly skipped |
> | Probe `router.py` | `SyntaxError` | compiles, both overloads reachable |
> | `ModuleIR.skipped` | empty for every defect | populated with reasons |
> | `services/petro_api` | shipped an unparseable `router.py` | imports, and matches a native binary |
>
> The 15 previously-unrunnable tests were not environmental noise — they gate
> the CLI end-to-end path and now execute and pass with the package installed.
>
> Verification was done independently of the agents that made the fixes: every
> probe in this document re-run, a real `clang++ -fsyntax-only` compile of the
> generated bindings, a byte-determinism check, and the generated package
> imported against a freshly built f2py extension.
>
> **A code-review pass over the fix itself found nine further defects**, seven
> confirmed, all resolved — including three in code written during the sweep.
> Two were worth the exercise on their own: `expose: false` was coerced to `{}`
> and bound the entire API (`False or {}` is `{}`), and internal `contains`
> procedures silently retyped an outer routine's arguments. Notably, the finding
> about bare-`end` block termination was **not reproducible as described**;
> probing it anyway surfaced the `contains` bug, which was worse.
>
> Each fix carries a regression test confirmed to fail before it and pass after.
>
> **What remains is in [ROADMAP.md](ROADMAP.md).**
>
> **Re-audited 2026-08-23 against the tree at `3a3c648`.** Since the sentence
> that used to stand here was written, most of Class C has closed too:
>
> - `.F90`/`.F`/`.FOR` are now discovered (`discovery.py:48`) and run through
>   `gfortran -cpp -E -P` into `_expanded/` before any parse
>   (`preprocess.py:283`+), with `fortran: defines:` (`config.py:141`) choosing
>   the branch. C3 is closed, not merely "warned about".
> - `std::vector<T>` binds (`parsers/cpp_ast.py:538`), a raw `T*` paired with a
>   length argument binds as a numpy buffer, and C++ operators bind to Python
>   special methods (`parsers/cpp_ast.py:417`+). C5 is substantially closed;
>   general templates are still refused.
> - Statement functions no longer corrupt intent inference (ROADMAP W1.4).
>
> Still genuinely open from Class C/D: `COMMON`/`EQUIVALENCE` **outputs** are
> still invisible to the parser, `ENTRY` points are still undiscoverable, `.h`
> is still parsed as C++ from any YAML (C4 — see below), and an empty
> `expose:` block still means "expose everything" for C++ (D1).
>
> **Test-suite claim, corrected — and the gating bug behind it, fixed.** The
> 595 quoted in ROADMAP.md was long stale. Worse, on a checkout *without* the
> optional `fparser` extra the suite ran **628 passed, 10 failed, 31 skipped**:
> ten fparser2-only tests (derived-type shims, statement functions, backend
> selection) *failed* rather than skipped, the inverse of the W0 "a skip is not
> a pass" note — a missing optional extra read as a red suite, which buries a
> real regression. The `requires_fparser` marker now lives in `tests/conftest.py`
> and is applied to all of them, so that checkout reports **652 passed, 41
> skipped**.
>
> Do not quote a single collected-test number here. It varies with which
> optional extras are installed (697 with `fparser`, 694 without), so any fixed
> figure is wrong on some machine. Quote the command instead.

---

## Standing blockers — not defects in the generator, limits of the design

These are not bugs to fix in a sweep. They are properties of putting HTTP in
front of a process-global native library, and they decide where a generated
service may be deployed. Both were verified in the tree at `3a3c648`.

### S1. No per-request isolation

`services/petro_api/python/petro_api/router.py:44` serialises **every** native
call through one process-wide `threading.RLock`. This is a correctness
mechanism, not a performance choice. Fortran `COMMON` blocks are process-global
storage: `PVTSET`/`PVTINI` writes `COMMON /FLUID/` and `PVTRS`/`PVTBO` read it
back. FastAPI runs synchronous `def` endpoints in a threadpool, so without the
lock two concurrent requests configuring different fluids interleave their
writes and reads in the same block and **each caller silently receives the
other caller's numbers** — nothing raises, nothing logs.

Three limits follow, and all three matter more than the lock:

- The lock only holds **within one worker process**. It does not make the
  service multi-tenant safe across workers, pods or replicas — that is why
  Fortran services pin `WEB_CONCURRENCY=1` (ROADMAP W2 2.2).
- It caps native throughput at **one concurrent call per process**.
- **The lock is emitted for Fortran only.** `generators/python_pkg_gen.py:398`
  sets `serialise = module.language == "fortran"`; a generated C++ service has
  no `_NATIVE_LOCK` and no critical section at all. That is right for the
  common case — C++ endpoints get a fresh instance per request — but a C++
  library with file-scope or function-local `static` state has exactly the S1
  problem with none of the S1 mitigation, and the parser cannot detect that it
  needs one. For such a header, the protection has to come from the deployment
  (one tenant per process), not from the generator.

### S2. The API is session-shaped, and HTTP is not

The native contract is "configure, then read" — stateful by construction. HTTP
is not. Two endpoints where the second depends on process-global state left by
the first is not a safe public contract: nothing in the protocol ties the pair
to one caller, and nothing in the generated service can tell an interleaved
sequence from an intended one. See ROADMAP "Closing the shared-state gap" for
the three routes out and the recommendation.

### Two things the generated error handling cannot cover

- **Crashes outrun Python.** A segfault, a Fortran `STOP` or a hang kills the
  worker before any Python exception handler runs — stated in the generated
  code itself at `services/petro_api/python/petro_api/service.py:52`. The
  unhandled-exception handler (`generators/error_gen.py`) gives failures a JSON
  body and an `error_id`; it cannot contain a process death. Only supervision
  (gunicorn, W2 2.2 tier 1) or process-per-request isolation (tier 2) can.
- **`MAX_ARRAY_ITEMS` is an explicit placeholder.**
  `services/petro_api/python/petro_api/router.py:22` (comment from line 11)
  caps every array argument of every endpoint at one configurable number
  (`NATIVE2PY_MAX_ARRAY_ITEMS`, default 65536), because the IR records that a
  parameter *is* an array, not how long the routine expects it to be. Real
  per-parameter extents need the fparser2 front end (ROADMAP 1.4).

### Deployment verdict

**Deployable today for internal, single-tenant-per-service use** — a team
calling its own trusted legacy code, one service per deck, one tenant per
deployment, callers who are not adversaries. The evidence for that is real:
`golden.json` with 10 recorded entry points and toolchain provenance, a native
oracle, reproducible builds, API-key auth and rate limiting.

**S1 and S2 are blockers for anything internet-facing or multi-tenant.** Until
per-request isolation exists, one process is one tenant, and the "configure
then read" contract cannot be safely exposed to callers who do not trust each
other.

---

Ordered by severity, and severity here means *silence*, not loudness. A
generated service that fails to compile costs an afternoon. A generated service
that builds, imports, passes its smoke test and returns a wrong number is the
one that reaches a decision.

**The through-line: `ModuleIR.skipped` is the tool's own safety net — the
mechanism for "recognised but not bindable, here's why". Not one defect below
populates it.** On every probe in this document the tool reported complete
success.

---

## Reproduction

Two probe files, both ordinary code no one would call adversarial.

`reservoir.hpp` — a namespaced C++ API with an overload, a `const` field, an
`enum class`, a `uint64_t`, a C-style output buffer, and a parameter named
`lambda`:

```cpp
namespace petro {
enum class Correlation { Standing, VazquezBeggs, Glaso };

struct Sample {
    const double reference_pressure;
    double value;
};

class PvtModel {
public:
    PvtModel(std::uint64_t well_id, float api, Correlation corr);
    double viscosity(double pressure) const;
    double viscosity(double pressure, double temperature) const;
    double attenuate(double lambda, double from) const;
    void   well_name(char* out, int len) const;
};

double bubble_point(double gor, double api);
}  // namespace petro
```

`modern.f90` — a free-form module with a derived type, an assumed-shape array,
an `optional` argument and an argument-less subroutine.

---

## Class A — silently wrong bindings

The tool succeeds, the service builds, the numbers are wrong.

### A9. An INTENT-less derived-type dummy loses its output components — **open**

`parsers/fortran_fparser.py:402` defaults a declaration with no INTENT clause
to `"in"`, and line 471 stores that verbatim in `derived_intents`. Fortran does
not: a dummy argument with no INTENT is usable for both read and write, so
`type(fluid_state) :: state` with no INTENT is legal, common in older code, and
writable.

Two consequences. The defensive fallback at line 620,
`decls.derived_intents.get(key, "inout") or "inout"`, is **dead code** — the key
is always present, holding `"in"`, so the `"inout"` default can never fire. And
with intent resolved to `"in"`, the generated shim emits `shim_pre` but not
`shim_post` (lines 364-367), so components the native routine updated are never
copied back. The caller receives its own input values, unchanged. Nothing
raises. A smoke test that does not assert on output-field mutation passes.

Fix is to distinguish *absent* from `"in"` in `_attributes`, and let absent fall
through to `"inout"` for derived dummies.

### A10. Flattening shims are spliced into the wrong module — **open**

`cli.py:1301` walks every `end module` in the file and keeps the **last** one
(`pass  # keep the last one`), contradicting the docstring five lines above it:
"Before `end module`, because the shim constructs the derived type and that type
is only visible from inside the module that defines it."

For the single-module sources in the corpus, first and last are the same line
and the bug is invisible. In a file with two or more modules where the exposed
routine lives in an earlier one, the shim lands in a later, unrelated module,
where the derived type is out of scope — the `_expanded` copy fails to compile.
Fix is to anchor on the `end module` of the module that declares the type, not
the file's last.

### A11. Fixed-form `CHARACTER(32) MSG` falls through to implicit typing — **open**

`parsers/fixed_form.py:152` accepts three spellings —
`character*32`, `character(len=32)`, `character(32)` — and is described as "one
pattern, used by BOTH Fortran backends, so the rule cannot drift between them".
But the declaration regex that feeds it, `_OLD_DECL_RE` via `_LENGTH_SUFFIX`
(line 154), only ever captures a leading `*`. So on the fixed-form path a
`CHARACTER(32) MSG` declaration matches nothing at all: `MSG` never reaches
`declared_types` or `char_lengths` and is typed by the implicit rules instead —
i.e. as a real. Nonstandard for fixed form, but widely accepted by compilers and
present in real decks. The two patterns *have* drifted, in the direction the
comment says they cannot.

### A1. Fortran derived-type return silently becomes `float`

```fortran
function state_at(p) result(st)
    type(pvt_state) :: st
```

```
state_at: params=[('p','float',False,'in')] returns=float skipped=0
```

`_extract_function` looks up the result variable's declaration with `_DECL_RE`,
which matches only intrinsic types. `type(pvt_state)` does not match, so the
lookup misses and line 147 falls through to `returns = "float"`. f2py cannot
return a derived type at all; the generated router emits
`{"result": state_at(p)}` on a value that is not a float.

**Root cause:** unresolvable types default instead of raising. Same pattern at
`fortran.py:141` for parameters.

### A2. `optional` arguments silently become required

```fortran
real(8), intent(in), optional :: salinity
```

```
configure: params=[('api',...), ('salinity','float',False,'in')]
```

`_DECL_RE` captures `attrs` and scans it for `intent(...)` only. `optional` is
never read, so the generated endpoint demands a value the Fortran does not
require — and the caller cannot express "absent".

### A3. C++ constructor argument types are reconstructed from lossy Python names

`PvtModel(std::uint64_t, float, Correlation)` generates:

```cpp
.def(py::init<int, double, int>())
```

The IR stores Python type names (`int`, `float`), so `pybind_gen._CPP_SPELLING`
has to guess the C++ type back. `uint64_t` → `int` truncates a 64-bit well ID.
`float` → `double` silently changes precision and, on an overloaded
constructor, selects a different one. `long double` → `double` likewise.
`char` → `std::string` does not compile at all.

**Root cause:** `ir.Parameter` discards the native type. Constructors are the
only place this matters today, because `.def(&Cls::method)` recovers the real
signature from the function pointer — but that is luck, not design.

### A4. Non-const `char*` output buffer binds as a Python `str` input

```cpp
void well_name(char* out, int len) const;
```

```python
def well_name(well_id: int, api: float, corr: int, out: str, len: int):
```

`_map_type` returns `"str"` for any pointer whose pointee is a char kind, with
**no const check** — the comment says "`const char*` is a C string", the code
never tests it. pybind11 materialises a temporary buffer for the `str`; the C++
writes through it; the write lands in freed memory and the result is discarded.

This is the one item here that is a memory-safety bug, not just a wrong answer.

### A5. Overloaded methods silently lose one overload over HTTP

Two `viscosity` overloads generate two `def viscosity(...)` in the same
`router.py` and two `@router.post("/viscosity")` routes. The second definition
shadows the first in Python; FastAPI dispatches the first registered route. One
overload is unreachable, with no warning. (It also breaks the C++ build — see
B1 — so today you hit that first. Fix B1 without fixing this and the silent
version is what remains.)

### A6. Free-form `INCLUDE` is never expanded

`preprocess.expand_includes` exists because, in its own words, *"f2py silently
mis-wraps routines containing an in-body INCLUDE — the extension builds and
imports but arguments never arrive, so calls return uninitialized memory."*

`cli.py` applies it only to `fixed_form_sources`. Free-form sources get
`resolve_kind_parameters` and nothing else. A `.f90` with `include 'GRID.INC'`
receives none of that protection, and the documented failure mode is
uninitialised memory.

### A7. Free-form Fortran has no IMPLICIT handling

The fixed-form path builds a full implicit map from `IMPLICIT` statements. The
free-form path has no equivalent, so any undeclared argument in a `.f90`
without `implicit none` — legal, and common in transitional 1990s code — binds
as `float` via the same defaulting as A1.

### A8. Non-UTF-8 sources are silently corrupted

`preprocess.py:66` reads with `errors="replace"`. Legacy decks carry latin-1
and control characters from VMS and mainframe lineage; every unmappable byte
becomes U+FFFD in the `_expanded` copy that is then compiled. No warning, no
encoding option.

---

## Class B — generated code does not build

Loud, blocking, and cheap to fix. All confirmed with `clang++ -fsyntax-only`
against exactly what `pybind_gen` emits.

### B1. Overloaded methods emit an ambiguous address-of

```cpp
.def("viscosity", &petro::PvtModel::viscosity)   // ×2
```
```
error: variable 'a' with type 'auto' has incompatible initializer
       of type '<overloaded function type>'
```

Needs `py::overload_cast<double>(&PvtModel::viscosity, py::const_)`. Overloading
is routine in C++; this makes a large fraction of real headers unbuildable.

### B2. Free functions in a namespace are emitted unqualified

```cpp
m.def("bubble_point", &bubble_point);
```
```
error: use of undeclared identifier 'bubble_point';
       did you mean 'petro::bubble_point'?
```

`ir.FunctionDef` has **no `namespace` field at all** — classes and structs
carry one, free functions do not. Any free function outside the global
namespace generates uncompilable C++.

### B3. `const` fields are bound read/write

```cpp
.def_readwrite("reference_pressure", &petro::Sample::reference_pressure)
```

`_map_type` strips cv-qualification and the field walk never tests
`type.is_const_qualified()`, so a `const` member gets `def_readwrite` instead of
`def_readonly`. Compile error inside pybind11's templates.

### B4. Python keyword parameter names produce a `SyntaxError`

```python
def attenuate(well_id: int, api: float, corr: int, lambda: float, from: float):
                                                   ^ SyntaxError
```

`lambda` as a physical quantity is near-universal in engineering C++;
`from`, `in`, `is`, `not`, `pass`, `global`, `class` are all legal in both
source languages. No `keyword.iskeyword()` check exists anywhere, and — worth
stating because it is the obvious assumption — **moving codegen to `ast` does
not fix this**: `ast.unparse(ast.Name(id="lambda"))` emits `lambda`.

### B5. Structs always get `py::init<>()`

`_emit_record` emits a default constructor for every `StructDef`
unconditionally. A struct with a `const` or reference member has no default
constructor, and this fails to compile. `Sample` above is exactly that shape.

### B6. One namespace is applied to every record

```python
namespace = module.classes[0].namespace if module.classes else None
```

The first class's namespace qualifies all structs and classes. A service
merging two headers from different namespaces mis-qualifies the second. A
service with structs but no classes gets `None` and emits unqualified names.

### B7. `enum class` parameters bind as `int`

`_map_type` maps any enum to `int`, so the constructor becomes
`py::init<..., int>()`. A scoped enum has no implicit conversion from `int`;
this does not compile. An unscoped enum compiles and silently accepts
out-of-range values, which makes it a Class A defect instead.

---

## Class C — valid code the tool refuses

Every entry here is a routine you can only expose by editing native source.

### C1. Fortran routines with no argument list

```fortran
subroutine reset
end subroutine reset
```
```
discovery -> ['total_rate', 'configure', 'state_at']      # reset missing
reset: ValueError: Could not find function or subroutine 'reset'
```

`_ROUTINE_DECL_RE` and `_find_block` both require an opening `(`. Argument-less
routines that work purely through `COMMON` are a standard F77/F90 idiom —
`fixed_form.py` was fixed for this (`FINDINGS.md` #1), the free-form path was
not. Note discovery and extraction fail *together* here, so fixing only
discovery would turn a silent omission into a hard error.

### C2. Bare `end` / `end function`

`_find_block` requires `end function <name>`; `_MODULE_BLOCK_RE` requires
`end module <name>`. Both spellings are optional in the standard. A bare `end`
makes the routine unfindable, and a bare `end module` mis-attributes
`fortran_module`, which sends `generate_init_py` looking for the symbol in the
wrong place.

### C3. Preprocessed Fortran (`.F90`, `.F`, `.FOR`) — **closed**

Handled since `bf9ba9c`: `discovery.py:48` lists the preprocessed suffixes,
`preprocess.py:283`+ runs `gfortran -cpp -E -P` (with `-D` flags from
`config.py:141`) into the `_expanded/` copy before parsing. Original text kept
for the record:

By convention the uppercase suffix means the file needs the C preprocessor —
`#ifdef`, `#include`, `#define`. `discovery.py` lowercases the suffix and routes
these to the ordinary parser, which has no preprocessor. Conditional code is
parsed as if every branch were live.

### C4. `.h` is always parsed as C++17 — **open, half-addressed**

`ClangOptions` now has a `language` field and `command_line` passes
`-x <language>`, also suppressing a mismatched `-std`
(`parsers/cpp_ast.py:247-270`). But `discovery.py:22` still maps `.h` → `cpp`,
`config.ClangConfig` (`config.py:49`) has **no `language` field**, and no call
site passes one — so from a `native2py.yaml` there is still no way to say "this
header is C". The plumbing exists; the knob is not wired.

### C5. STL containers and templates — **largely closed**

`std::vector<T>` binds through typedefs (`parsers/cpp_ast.py:538`), with one
shape deliberately refused: a non-const `std::vector<T>&` argument, because
pybind11 silently discards writes through it (`parsers/cpp_ast.py:492`). A raw
`T*` paired with a length argument binds as a numpy buffer. What remains
refused is general template instantiation — `std::span`, `std::map`, and
user templates.

---

## Class D — tool-level and configuration

### D1. An empty `expose:` block means "expose everything" (C++ only) — **open**

Still true at `config.py:35-41`: with neither `classes:` nor `functions:` named,
`is_exposed` returns `True` for every name unless the user wrote `all: false`.
An explicit `all:` flag was added; the permissive default was not changed.

`ExposeConfig.is_exposed` returns `True` for every name when both lists are
empty. Pointed at a large header, native2py binds its entire public surface
with no opt-in. Fortran requires explicit `expose.functions:`; C++ should too,
or should at least require an explicit `expose: all`.

### D2. `language` silently defaults to `cpp` — **closed**

`ServiceConfig.load` now calls `_load_language` (`config.py:128`, defined at
`config.py:259`), which requires `language:` or infers it from the files under
`native/` and never falls back to `cpp`. Original text:

`ServiceConfig.load` uses `data.get("language", "cpp")`. A typo'd or missing key
routes Fortran sources into the C++ parser.

### D3. Generated Python is never syntax-checked

Only `tests/test_golden.py:362` compiles generated output. This is why B4
ships, and why the `&` continuation bug reached a commit. A `compile()` gate in
`generate` converts every Class B Python defect from a container-start failure
into a build failure.

### D4. `skipped` is empty for all of the above

The reporting channel exists, is well designed, and is used correctly for the
refusals the authors anticipated (pointer returns, templates, incomplete
types). None of the defects in this document reach it. Any fix that cannot
bind a construct must land there rather than defaulting.

---

## The order this was fixed in — all complete

Kept as a record of how the sweep was sequenced. **Every item below shipped**;
this is not a to-do list. For open work see [ROADMAP.md](ROADMAP.md).

| | Items | What it bought |
|---|---|---|
| 1 | **D3** | The `compile()` gate. Cheapest, and it catches B4 and anything like it permanently. |
| 2 | **A1, A2, A7** | Unresolvable Fortran types skip with a reason instead of defaulting to `float` — one change, three silent-wrong-answer defects. |
| 3 | **A4, B3, B7** | const-ness and enum identity carried through `_map_type`. |
| 4 | **B2** | `namespace` on `ir.FunctionDef`. Small, unblocked every namespaced C++ API. |
| 5 | **B1, A5** | `py::overload_cast`, plus distinct route names so no overload is silently unreachable. |
| 6 | **A3** | Native type spelling in `ir.Parameter`, so `py::init<>` stopped guessing. |
| 7 | **A6, A8** | Include expansion extended to free-form; encoding made explicit. |
| 8 | **C1, C2** | Free-form parser parity with the fixed-form fixes. |
| 9 | **B4** | Identifier escaping, wired into `ir.validate`. |

Steps 1–6 removed every silent-wrong-answer defect in Class A. Verified by
re-running each probe in this document against the merged tree, not by
inspection.

**Class C was the one part not closed by that sweep, and most of it has closed
since** (re-verified 2026-08-23 at `3a3c648`): `.F90` is preprocessed properly,
`std::vector<T>` and length-paired `T*` bind, statement functions no longer
corrupt intent inference. What is still open, and still needs the symbol table
a real parse tree provides: `COMMON`/`EQUIVALENCE` **outputs**, `ENTRY` points,
per-parameter array extents, C-vs-C++ selection for `.h` (C4), and the
permissive empty `expose:` default (D1). Scoped as W1.4 and W2 in
[ROADMAP.md](ROADMAP.md).
