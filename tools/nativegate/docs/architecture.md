# Architecture

## Pipeline

```
native source (.hpp / .f90)
        │
        ▼
   language parser          nativegate/parsers/{cpp,fortran}.py
                            C++:     cpp.py picks cpp_ast.py (Clang AST)
                                     or cpp_regex.py (fallback reader)
                            Fortran: fortran.py picks fortran_fparser.py
                                     (fparser2) or fortran_regex.py (fallback)
        │
        ▼
 intermediate representation  nativegate/ir.py  (ModuleIR, ClassDef, FunctionDef, Parameter)
        │
        ▼
   generators                nativegate/generators/*.py
        │
   ┌────┼────┬────────┬─────────┬──────────┐
   ▼    ▼    ▼        ▼         ▼          ▼
bindings CMake  Python pkg  tests  Dockerfile  K8s
(pybind_gen / f2py_gen)  (python_pkg_gen +   (docker_gen) (k8s_gen)
                          middleware_gen)
```

For a `.F90`/`.F` source, or a fixed-form deck with `INCLUDE` statements, a
preprocessing step runs before the parser: `nativegate/preprocess.py` expands
INCLUDEs and shells out to `gfortran -cpp -E -P`, writing the result into
`services/<name>/native/_expanded/`. That directory is what f2py actually
compiles, which is why it appears in the generated file list.

Generators never know which language produced the `ModuleIR` they're given
— they only read `ModuleIR.language` where behavior genuinely differs (e.g.
the compiled extension symbol name: `<name>_cpp` for C++, `<name>` for
Fortran). This is the seam the project's `design.md` calls out in section 24
("Future RPC Mode") as the reason to introduce the IR early: swapping
pybind11 for gRPC only touches one layer. Replacing the interim regex C++
reader with a real Clang AST parser was exactly that exercise, and it changed
nothing below `parsers/`: both backends emit the same `ModuleIR`.

## Intermediate representation (`ir.py`)

| Type | Represents |
|------|------------|
| `ModuleIR` | everything exposed from one source file: classes, functions, language, and (Fortran only) the enclosing `fortran_module` name |
| `ClassDef` | a C++ class: name, namespace, methods, constructors, fields, and public base classes bound by the same service |
| `StructDef` | a plain-data struct: name, fields (bound read/write) |
| `SkippedSymbol` | a declaration recognised but not bindable, with the reason — surfaced by the CLI so nothing is dropped silently |
| `FunctionDef` | a function or Fortran subroutine: name, parameters, return type, `is_subroutine` |
| `Parameter` | name, Python-mapped type, `is_array`, `intent` (Fortran calling convention) |

Type mapping is deterministic and fails loudly: `map_type` (C++) and
`map_fortran_type` (Fortran) raise `NativeTypeError` for anything not in
`TYPE_MAP` / `FORTRAN_TYPE_MAP`, rather than silently emitting an unsafe or
broken binding. This is applied **at parse time**, not generation time — by
the time a `ModuleIR` exists, every parameter and return type is already a
valid Python type name.

## Parsers

**C++** is parsed by Clang itself (`parsers/cpp_ast.py`, via libclang): the
preprocessor runs, and templates, typedefs, overloads, access and type
completeness are read off the AST. `parsers/cpp.py` is the front door that
picks a backend — Clang when libclang is importable, otherwise the original
token/brace reader in `parsers/cpp_regex.py`, which is kept for machines
without libclang and selectable with `parser: regex`. Both produce the same
`ModuleIR`, and the generators cannot tell which one ran; the CLI always
prints which one did.

**Fortran** has the same two-backend shape. `parsers/fortran.py` is the front
door; the default is `parsers/fortran_fparser.py`, which builds an fparser2
parse tree, and `parsers/fortran_regex.py` is the fallback for machines
without the package (`parser: regex`, or `$NATIVEGATE_FORTRAN_PARSER`).
`parsers/fixed_form.py` carries the F77-specific helpers both backends use —
IMPLICIT typing maps and fixed CHARACTER lengths.

Either way, Fortran binds only the routines named in `expose.functions:`,
which is what keeps a large legacy deck cheap: you pay for what you expose.
The fallback backend goes further and never parses the rest of the file at all
— one targeted search per requested name. The fparser2 backend does parse the
whole file, and buys grammar-accurate routine boundaries, structured
attributes, and derived-type flattening with it.

See [Exposing C++](cpp-guide.md#what-the-parser-supports) and
[Exposing Fortran](fortran-guide.md) for exactly what each one covers.

## Generators

| File | Produces |
|------|----------|
| `pybind_gen.py` | pybind11 `PYBIND11_MODULE(...)` C++ source |
| `f2py_gen.py` | f2py-driven `CMakeLists.txt` custom command |
| `cmake_gen.py` | pybind11 `CMakeLists.txt` |
| `python_pkg_gen.py` | `__init__.py`, the FastAPI `router.py`, and `service.py` |
| `middleware_gen.py` | auth, rate limiting, size caps, request IDs, `/readyz`, draining |
| `error_gen.py` | the service's exception handler |
| `test_gen.py` | pytest import/call smoke test |
| `golden_gen.py` | `tests/test_golden.py`, the numerical regression replay |
| `pyproject_gen.py` | `pyproject.toml` (written only when missing) |
| `docker_gen.py` | multi-stage Dockerfile, per-language build/runtime deps |
| `gateway_gen.py` | a composed app mounting several services' routers |
| `mcp_gen.py` | `mcp_server.py`, the MCP view of the same router ([mcp.md](mcp.md)) |
| `k8s_gen.py` | Kubernetes Deployment/Service manifests |

`python_pkg_gen.generate_init_py` has one piece of language-specific logic
worth knowing about: when a Fortran source's routines are wrapped in a
`module` block, f2py nests them under `<extension>.<module_name>.<routine>`
in the compiled output. The generator detects this
(`ModuleIR.fortran_module`) and re-exports through the nesting so the
Python-facing package still presents a flat `from physics import
calculate_pressure` interface. See
[Exposing Fortran](fortran-guide.md#the-module-wrapper-gotcha) for the full
story — this was found by actually compiling the code, not by unit tests.

## The concurrency model: one native call per process (Fortran)

For a **Fortran** service, the generated `router.py` takes a single
process-wide `threading.RLock` around every native call. This is a deliberate
design constraint, not a placeholder.

Fortran COMMON blocks are process-global storage, not per-call state. A
routine like `PVTSET` writes `COMMON /FLUID/` and a later call to `PVTRS` or
`PVTBO` reads it back — "configure, then use the COMMON" *is* the library's
contract. FastAPI runs synchronous `def` endpoints in a threadpool, so without
the lock two requests configuring different fluids interleave their writes and
reads in the same COMMON block, and each caller gets numbers computed from the
other caller's inputs. Nothing raises and nothing logs; the answers are just
wrong.

Per-request objects cannot fix this the way they fix a stateful C++ class,
where the generator does construct one instance per request. COMMON is
reachable from every instance because it belongs to the process.

**C++ services are not locked.** A fresh instance per request already covers
the usual case, and a C++ free function has no equivalent of COMMON *unless*
it uses file-scope statics — which the parser cannot see. Locking on the
chance that it might would cost every C++ service its concurrency for a guess.
The honest consequence: if your C++ library does keep file-scope mutable
state, nothing here protects it and nothing here will warn you.

Two consequences follow for Fortran, and they should shape how a service is
deployed:

- **Native execution is capped at one concurrent call per process.** Scale
  with more processes or more pods, not more threads. Removing the lock to
  improve a throughput number reintroduces silent numerical corruption.
- **The resulting API is session-shaped** — configure, then read — which is
  safe when a single tenant owns the process and unsafe when it does not.
  [Is this production-ready?](production-readiness.md) covers what that rules
  out, alongside `DEFECTS.md` in the tool's root.

A related placeholder lives in the same file. `MAX_ARRAY_ITEMS`
(`NATIVEGATE_MAX_ARRAY_ITEMS`, default 65536), emitted for any service that
takes array arguments, caps the length of any array argument on any endpoint.
It is a memory guard, not a bounds check: the IR
records that a parameter *is* an array, not the extent the routine declares,
so one cap stands in for all of them. Pydantic rejects an oversized list
during request validation, so the caller gets a 422 and the array is never
materialised. Recovering real declared extents is pending work in the fparser2
front end.

## Verified, not just tested

Unit tests (`tools/nativegate/tests/`) check the parser and
generator logic in isolation. Separately, both language pipelines have been
run through a **real toolchain** end to end, to catch the class of bug unit
tests can't — one showed up doing exactly this (the Fortran module-nesting
issue above).

**C++ (`demo`, from [Getting started](getting-started.md)):**

```bash
brew install cmake ninja && pip install pybind11
ngate quickstart libraries/geometry/geometry.hpp --name demo
ngate build demo
pip install services/demo/dist/demo-1.0.0*.whl
python3 -c "from demo import Geometry; print(Geometry().hypotenuse(3, 4))"  # 5.0
```

**Fortran (`petro_api`, the service committed in this repo):**

```bash
brew install gcc  # provides gfortran
ngate generate petro_api
ngate build petro_api
ngate test petro_api
ngate golden verify petro_api
```

`petro_api` is the harder of the two: an F90 facade over seven fixed-form F77
decks, with INCLUDE expansion, COMMON-block state, inferred intent and array
arguments. Its `golden.json` pins 10 entry points, so `golden verify` is a
real check that the numbers did not move — not just that it compiled.

Both were also exercised through their generated FastAPI `service.py` with
`fastapi.testclient.TestClient`, confirming the HTTP layer works against the
real compiled extension, not a mock.

## What is generated, what is yours

Every file in a service directory is one of three things. This matters the
moment something gets deleted, or a merge conflict lands in a generated file.

| Path | Origin | If you delete it |
|---|---|---|
| `native/` | **yours** — the C++/Fortran being exposed | gone for good; nothing regenerates source |
| `nativegate.yaml` | **yours** — what to expose, parser and clang settings | gone for good; `create-service` writes a fresh default |
| `golden.json` | **yours after the first record** — the inputs are hand-tunable and are the numerical baseline | re-record, but the recorded *answers* are lost, so it no longer proves anything about the past |
| `bindings/generated/` (C++ only), `CMakeLists.txt`, `python/<name>/` (`__init__.py`, `router.py`, `service.py`, `middleware.py`, `mcp_server.py`), `tests/`, `native/_expanded/` (Fortran only), `.nativegate/ir.json` | generated | `ngate generate <name>` restores all of it byte-for-byte |
| `requirements.lock` | generated | `ngate lock <name>` |
| `infrastructure/kubernetes/<name>.yaml` | generated | `ngate k8s <name>` |
| `pyproject.toml` | generated, but hand-editable (pinned deps, version) | `ngate generate` restores it **only if missing** — it never overwrites your edits |
| `Dockerfile` | generated by a separate command | `ngate docker <name>` |
| `dist/` | build output | `ngate build <name>` |
| `gateways/<name>/` | fully generated | `ngate gateway <name> --service a --service b` |

`ngate init` does **not** restore any of this. It creates the empty
top-level skeleton only — `services/`, `libraries/`, `tools/`,
`infrastructure/docker/`, `infrastructure/kubernetes/` — and it is safe to
re-run at any time, because it only ever makes directories that are missing.
Note it does not create `gateways/`; the `gateway` command makes that itself.

So: deleting `services/` deletes your native code and your service
configuration, and no nativegate command brings those back — that is what
version control is for. Deleting a *gateway* directory costs nothing, and
deleting the generated parts of a service costs one `generate`.
