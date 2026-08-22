---
name: native2py
description: Expose existing C++ or Fortran code to Python without hand-writing bindings. Use whenever a task involves calling native code from Python — pybind11 or f2py wrappers, ctypes/cffi shims, a CMake/scikit-build extension module, wheel packaging for a native library, or standing up an HTTP service over one. Triggers on "bind this header", "call this C++ from Python", "wrap this Fortran routine", "make a Python package for this .hpp/.f90", "pybind11", "f2py", "extension module", and on any request to check that a re-hosted numerical library still returns the same answers.
---

# native2py

`native2py` generates the whole native→Python path from one source file:
pybind11/f2py bindings, the CMake build, an installable Python package, a
FastAPI service, a pytest suite, a numerical regression baseline, and a
Dockerfile.

**Do not hand-write the binding, the CMakeLists, the pyproject, or the
setup.py.** Emitting those by hand costs thousands of output tokens per file
and then costs more on every compile-error repair round. Run the tool and read
its checklist instead.

## Before anything else

```bash
native2py --version
```

If that fails, install it and stop guessing at an alternative:

```bash
pip install -e "<repo>/tools/native2py[clang,build,test]"
```

The `clang` extra is not optional. Without libclang the C++ front end falls
back to a regex reader that **silently** drops free functions and anything
behind a macro, so you get a package that is quietly missing symbols. If
`inspect`/`suggest` prints `regex reader` rather than `clang AST`, install the
extra and re-run before you trust any of the output.

`build` also needs a system toolchain pip cannot supply: `cmake`, `ninja`, a
C++ compiler, and `gfortran` for Fortran.

## The path to take

Run every command from the project root — `services/` and `libraries/` resolve
against the working directory.

**1. If the user named a file**, go straight to it:

```bash
native2py quickstart path/to/FluidModel.hpp --name fluidmodel --build
```

That is scaffold + expose + generate + build in one call. It also finds and
copies the matching `.cpp` (side-by-side or `include/`+`src/` layouts). If it
warns that no implementation was found, say so — the build can still succeed
and then fail at `import` with an undefined-symbol error.

**2. If the user pointed at a codebase**, do not pick a file by reading
headers yourself — that is exactly the token spend this tool exists to avoid:

```bash
native2py suggest libraries/petro/cpp
```

It parses every header and ranks them by how cleanly they bind, and prints the
exact `quickstart` line for the best candidate. `✔` = binds everything and
pulls in no other header; `~` = binds more than it skips; `✘` = unusable.
Dependencies outrank skip count, because one `#include` of another subsystem
costs you that subsystem's entire build.

**3. Pin the numbers.** For re-hosted engineering code "it imports" is not the
acceptance criterion:

```bash
native2py golden record fluidmodel   # writes services/fluidmodel/golden.json
native2py golden verify fluidmodel   # 4 entry point(s) unchanged (0 not covered).
```

Commit `golden.json`. Re-run `verify` after any regeneration, compiler change,
or flag change and report the result verbatim — it is a real signal about
whether the answers moved, which no test you write from inspection can give
you. The generated `tests/test_golden.py` replays the same calls in CI.

## The rest of the commands

```bash
native2py inspect src/pvt.hpp     # what the parser sees, before generating
native2py generate pvt            # re-run codegen after editing native2py.yaml
native2py build pvt               # wheel via scikit-build-core -> CMake
native2py test pvt                # the generated pytest suite
native2py docker pvt --build      # multi-stage image, non-root, healthcheck
native2py gateway platform-api --service pvt --service sim
```

Fortran differs in one way that matters: parsing is targeted per routine. For
a large legacy deck, use `create-service` + list only the routines you need
under `expose.functions:` in `native2py.yaml` + `generate`, rather than
`quickstart`, which scans the whole file.

## Reading the output

Everything the parser recognises but cannot bind is reported at `generate`
time **with a reason** — templates, `std::vector<T>`, raw numeric pointers,
class-typed pointer returns, forward-declared types. Relay those lines to the
user rather than summarizing them as "some methods were skipped"; which four
of thirty are missing is the whole content of the message.

A header that does not compile is refused outright. Do not try to route around
that by falling back to the regex parser: clang recovers from an unknown type
by pretending it was `int`, and binding that produces signatures that disagree
with the C++ underneath.

## What you still own

The generated `services/<name>/` is self-contained — it does not depend on
native2py at runtime and builds on a machine that never installed it. But the
generated endpoints have **no auth, no rate limiting, and no exception
handling**; a segfault in native code takes the worker down; there is no
sandboxing or per-call timeout; there are no CI/CD or Kubernetes templates. If
the user is heading for production, say this unprompted and point at
`docs/production-readiness.md`.

Regenerated files are rewritten from source every run. Do not hand-edit
anything under `bindings/generated/`, the generated `CMakeLists.txt`,
`pyproject.toml`, or the package `__init__` — change `native2py.yaml` and
re-run `generate`.
