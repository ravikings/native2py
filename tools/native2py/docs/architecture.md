# Architecture

## Pipeline

```
native source (.hpp / .f90)
        │
        ▼
   language parser          native2py/parsers/{cpp,fortran}.py
                            C++: cpp.py picks cpp_ast.py (Clang AST)
                            or cpp_regex.py (fallback reader)
        │
        ▼
 intermediate representation  native2py/ir.py  (ModuleIR, ClassDef, FunctionDef, Parameter)
        │
        ▼
   generators                native2py/generators/*.py
        │
   ┌────┼────┬────────┬─────────┐
   ▼    ▼    ▼        ▼         ▼
bindings CMake  Python pkg  tests  Dockerfile
(pybind_gen / f2py_gen)  (python_pkg_gen)  (test_gen)  (docker_gen)
```

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

**Fortran** is still regex-based — see
[Exposing C++](cpp-guide.md#what-the-parser-supports) and
[Exposing Fortran](fortran-guide.md) for exactly what each one covers. The
notable design choice there: the Fortran parser does **targeted extraction** —
one regex search per requested routine name — rather than tokenizing the
whole file, specifically to stay cheap on the large legacy Fortran files
common in this domain.

## Generators

| File | Produces |
|------|----------|
| `pybind_gen.py` | pybind11 `PYBIND11_MODULE(...)` C++ source |
| `f2py_gen.py` | f2py-driven `CMakeLists.txt` custom command |
| `cmake_gen.py` | pybind11 `CMakeLists.txt` |
| `python_pkg_gen.py` | `__init__.py` + FastAPI `service.py` |
| `test_gen.py` | pytest import/call smoke test |
| `docker_gen.py` | multi-stage Dockerfile, per-language build/runtime deps |

`python_pkg_gen.generate_init_py` has one piece of language-specific logic
worth knowing about: when a Fortran source's routines are wrapped in a
`module` block, f2py nests them under `<extension>.<module_name>.<routine>`
in the compiled output. The generator detects this
(`ModuleIR.fortran_module`) and re-exports through the nesting so the
Python-facing package still presents a flat `from physics import
calculate_pressure` interface. See
[Exposing Fortran](fortran-guide.md#the-module-wrapper-gotcha) for the full
story — this was found by actually compiling the code, not by unit tests.

## Verified, not just tested

Unit tests (`tools/native2py/tests/`) check the parser and
generator logic in isolation. Separately, both language pipelines have been
run through a **real toolchain** end to end, to catch the class of bug unit
tests can't — one showed up doing exactly this (the Fortran module-nesting
issue above).

**C++ (`demo`, from [Getting started](getting-started.md)):**

```bash
brew install cmake ninja && pip install pybind11
native2py quickstart libraries/geometry/geometry.hpp --name demo
native2py build demo
pip install services/demo/dist/demo-1.0.0*.whl
python3 -c "from demo import Geometry; print(Geometry().hypotenuse(3, 4))"  # 5.0
```

**Fortran (`reservoir`):**

```bash
brew install gcc  # provides gfortran
native2py generate reservoir
cd services/reservoir/native
python3 -m numpy.f2py -m physics -c pressure.f90 --fcompiler=gfortran
python3 -c "from physics import calculate_pressure; print(calculate_pressure(10.0, 300.0))"  # 3000.0
```

Both were also exercised through their generated FastAPI `service.py` with
`fastapi.testclient.TestClient`, confirming the HTTP layer works against the
real compiled extension, not a mock.

## What is generated, what is yours

Every file in a service directory is one of three things. This matters the
moment something gets deleted, or a merge conflict lands in a generated file.

| Path | Origin | If you delete it |
|---|---|---|
| `native/` | **yours** — the C++/Fortran being exposed | gone for good; nothing regenerates source |
| `native2py.yaml` | **yours** — what to expose, parser and clang settings | gone for good; `create-service` writes a fresh default |
| `golden.json` | **yours after the first record** — the inputs are hand-tunable and are the numerical baseline | re-record, but the recorded *answers* are lost, so it no longer proves anything about the past |
| `bindings/generated/`, `CMakeLists.txt`, `python/<name>/`, `tests/`, `.native2py/ir.json` | generated | `native2py generate <name>` restores all of it byte-for-byte |
| `pyproject.toml` | generated, but hand-editable (pinned deps, version) | `native2py generate` restores it **only if missing** — it never overwrites your edits |
| `Dockerfile` | generated by a separate command | `native2py docker <name>` |
| `dist/` | build output | `native2py build <name>` |
| `gateways/<name>/` | fully generated | `native2py gateway <name> --service a --service b` |

`native2py init` does **not** restore any of this. It creates the empty
top-level skeleton only — `services/`, `libraries/`, `tools/`,
`infrastructure/docker/`, `infrastructure/kubernetes/` — and it is safe to
re-run at any time, because it only ever makes directories that are missing.
Note it does not create `gateways/`; the `gateway` command makes that itself.

So: deleting `services/` deletes your native code and your service
configuration, and no native2py command brings those back — that is what
version control is for. Deleting a *gateway* directory costs nothing, and
deleting the generated parts of a service costs one `generate`.
