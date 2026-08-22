# native2py

**native2py** exposes native C++ and Fortran code to Python and packages it as a
deployable microservice — a compiled Python extension, a FastAPI HTTP layer,
and a Dockerfile, generated from your existing source.

```
Existing C++ / Fortran  →  native2py  →  Python package + FastAPI + Docker
```

You write the native function. native2py writes the binding code, the CMake
build, the Python package `__init__.py`, a FastAPI service layer, and a
pytest smoke test — so you never hand-write pybind11 or f2py glue.

## What's implemented today

This is an early, verified slice of the full design spec (`design.md` in the
repo root):

| Language | Parser | Binding tool | Status |
|----------|--------|--------------|--------|
| C++ | Clang AST via libclang (preprocessor, templates, typedefs, overloads), with a regex reader as fallback | pybind11 | ✅ generate + build verified against a real `clang++`/CMake compile |
| Fortran | regex-based, targeted per-routine extraction | f2py | ✅ generate + build verified against a real `gfortran`/f2py compile |
| C | — | — | not started |

Both language paths have been run through a **real compiler**, not just unit
tests: the generated `CMakeLists.txt` was fed to actual `cmake`/`gfortran`/
`clang++`, the resulting `.so` was imported through the generated Python
package, and the generated FastAPI service was exercised with real HTTP
requests. See [Architecture](architecture.md#verified-not-just-tested) for
what was checked.

## Where to start

- New to native2py? Start with [Getting started](getting-started.md) — it
  walks through the C++ `calculator` example end to end.
- Exposing your own C++ code? See [Exposing C++](cpp-guide.md).
- Exposing Fortran, especially a large legacy source file? See
  [Exposing Fortran](fortran-guide.md) — this is the case native2py's
  Fortran parser is specifically designed around.
- Looking up a command? See the [CLI reference](cli-reference.md).
- Something not working? See [Troubleshooting](troubleshooting.md).
- Thinking about running this for real? Read
  [Is this production-ready?](production-readiness.md) first — it's an
  honest gap list (security, observability, error handling, CI/CD), not a
  sales pitch.
