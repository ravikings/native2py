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
> **What remains is in [ROADMAP.md](ROADMAP.md).** Class C is the only part of
> this document not fully closed: `.F90` preprocessed Fortran is now *detected
> and warned about* but still not handled, and STL/template coverage is
> unchanged. Both are the fparser2 argument (W1.4). Defects (g) statement
> functions and (h) `COMMON`/`EQUIVALENCE` outputs also await that work.

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

### C3. Preprocessed Fortran (`.F90`, `.F`, `.FOR`)

By convention the uppercase suffix means the file needs the C preprocessor —
`#ifdef`, `#include`, `#define`. `discovery.py` lowercases the suffix and routes
these to the ordinary parser, which has no preprocessor. Conditional code is
parsed as if every branch were live.

### C4. `.h` is always parsed as C++17

`_EXTENSIONS` maps `.h` → `"cpp"`, and `ClangOptions.command_line` hardcodes
`-x c++`. A C header using `restrict`, K&R declarations, or a C-only keyword
fails to parse, and the failure is reported as a C++ diagnostic.

### C5. STL containers and templates

Refused by design, and defensible — but `std::vector<double>` is how modern C++
carries an array with its length, which is precisely the information
`_map_type` refuses raw pointers for. The refusal is currently the largest
single gap in C++ coverage.

---

## Class D — tool-level and configuration

### D1. An empty `expose:` block means "expose everything" (C++ only)

`ExposeConfig.is_exposed` returns `True` for every name when both lists are
empty. Pointed at a large header, native2py binds its entire public surface
with no opt-in. Fortran requires explicit `expose.functions:`; C++ should too,
or should at least require an explicit `expose: all`.

### D2. `language` silently defaults to `cpp`

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

## Suggested order

1. **D3** — the `compile()` gate. Cheapest, and it catches B4 and anything like
   it permanently.
2. **A1, A2, A7** — make unresolvable Fortran types raise or skip instead of
   defaulting to `float`. One change, three silent-wrong-answer defects.
3. **A4, B3, B7** — carry const-ness and enum identity through `_map_type`.
4. **B2** — add `namespace` to `ir.FunctionDef`. Small, unblocks every
   namespaced C++ API.
5. **B1, A5** — overload support via `py::overload_cast`, plus distinct route
   names.
6. **A3** — carry the native type spelling in `ir.Parameter` so `py::init<>`
   stops guessing.
7. **A6, A8** — extend include expansion to free-form; make encoding explicit.
8. **C1, C2** — free-form parser parity with the fixed-form fixes.
9. **B4** — identifier escaping in `ir.validate`.

Items 1–6 are roughly three weeks and remove every silent-wrong-answer defect
in Class A except A6/A8. Class C is the fparser2 argument, already scoped in
[ROADMAP.md](ROADMAP.md).
