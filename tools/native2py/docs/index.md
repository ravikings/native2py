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
| C++ | Clang AST via libclang (preprocessor, typedefs, overloads), with a regex reader as fallback | pybind11 | ✅ generate + build verified against a real `clang++`/CMake compile |
| Fortran | fparser2 parse tree, with a targeted per-routine regex reader as fallback | f2py | ✅ generate + build verified against a real `gfortran`/f2py compile |
| C | via the C++ path, for `extern "C"` declarations in a header | pybind11 | partial |

Both language paths have been run through a **real compiler**, not just unit
tests: the generated `CMakeLists.txt` was fed to actual `cmake`/`gfortran`/
`clang++`, the resulting `.so` was imported through the generated Python
package, and the generated FastAPI service was exercised with real HTTP
requests. See [Architecture](architecture.md#verified-not-just-tested) for
what was checked.

## What it is, and is not, ready for

Usable today for **internal, single-tenant deployments**, where a team is
putting a Python or HTTP interface on legacy code it already trusts. Not ready
for internet-facing or multi-tenant use.

Two constraints shape that, and neither is a to-do item:

- Every generated **Fortran** endpoint serialises its native call through one
  process-wide lock, because COMMON blocks are process-global storage.
  Concurrency is one native call per process, by design — scale with
  processes, not threads. C++ services are not locked. See
  [Architecture](architecture.md#the-concurrency-model-one-native-call-per-process-fortran).
- For a stateful library the generated API is session-shaped ("configure, then
  read"), which is safe when one tenant owns the process and unsafe otherwise.

[Is this production-ready?](production-readiness.md) carries the detail, and
`DEFECTS.md` in the tool's root tracks known defects.

## Where to start

- New to native2py? Start with [Getting started](getting-started.md) — it
  walks through a small C++ example end to end.
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
