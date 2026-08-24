---
name: native2py
description: Expose existing C++ or Fortran code to Python without hand-writing bindings. Use whenever a task involves calling native code from Python — pybind11 or f2py wrappers, ctypes/cffi shims, a CMake/scikit-build extension module, wheel packaging for a native library, or standing up an HTTP service over one. Triggers on "bind this header", "call this C++ from Python", "wrap this Fortran routine", "make a Python package for this .hpp/.f90", "pybind11", "f2py", "extension module", and on any request to check that a re-hosted numerical library still returns the same answers.
---

# native2py — automated modernization of legacy scientific computing

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
native2py lock pvt                # pin deps by version + SHA-256
native2py docker pvt --build      # multi-stage image, non-root, healthcheck
native2py k8s pvt                 # -> infrastructure/kubernetes/pvt.yaml
native2py gateway platform-api --service pvt
```

Fortran differs in one way that matters: it binds only the routines named in
`expose.functions:`, and there is no "expose everything" fallback. For a large
legacy deck, use `create-service` + list only the routines you need in
`native2py.yaml` + `generate`, rather than `quickstart`, which scans the whole
file. For C++, state "bind everything" explicitly as `expose: all` — an empty
`expose:` block still does it but warns that the intent was never stated.

## Reading the output

Everything the parser recognises but cannot bind is reported at `generate`
time **with a reason** — templates, a non-const `std::vector<T>&`, a non-const
`char*`, a raw pointer with no length argument, class-typed pointer returns,
forward-declared types. Relay those lines to the user rather than summarizing
them as "some methods were skipped"; which four of thirty are missing is the
whole content of the message.

Refusals are part of the contract, not a bug to work around: a non-const
`char*` output buffer has no length, a pointer return has no ownership, a
derived-type Fortran result has no mapping. The running service republishes
them at `GET /_unexposed`, so you can point the user there. An empty mapping
means the build refused nothing; a 404 means the service predates the route.

Do not report a construct as unsupported without checking — several things
that used to be refused now bind: `std::vector<T>` of scalars, operators as
Python special methods, a raw `T*` paired with a length argument (including on
methods), Fortran derived types, and fixed-length Fortran CHARACTER outputs.
The last two are narrow: derived types need free-form source, the fparser2
backend, a subroutine inside a module, the type defined in the same file, and
all-scalar components; a CHARACTER output must have its length fixed in the
declaration or it is demoted to an input.

A header that does not compile is refused outright. Do not try to route around
that by falling back to the regex parser: clang recovers from an unknown type
by pretending it was `int`, and binding that produces signatures that disagree
with the C++ underneath.

## What you still own

The generated `services/<name>/` is self-contained — it does not depend on
native2py at runtime and builds on a machine that never installed it.

Generated services **do** carry API-key auth (`api: {auth: api_key}` in
`native2py.yaml`, keys from `NATIVE2PY_API_KEYS`), rate limiting, a request
size cap, request IDs and access logging, an exception handler, `/healthz` and
`/readyz` with SIGTERM draining, and Kubernetes manifests via `native2py k8s`.
Do not tell the user these are missing.

What you **should** raise unprompted if the user is heading for production:

- **This is for internal, single-tenant use.** Not internet-facing, not
  multi-tenant.
- **Fortran services run one native call at a time, per process.** Each
  endpoint holds a process-wide lock across the native call, because COMMON
  blocks are process-global storage and interleaved calls would silently
  return each other's numbers. Never suggest removing it to improve throughput
  — capacity comes from more processes. C++ services are not locked (fresh
  instance per request); if a C++ library keeps file-scope static state, say
  so, because nothing generated protects it.
- **Stateful libraries get a session-shaped API** (configure, then read),
  which is unsafe to share between tenants.
- **`NATIVE2PY_MAX_ARRAY_ITEMS` is a memory guard, not a bounds check** — the
  IR knows a parameter is an array, not its declared extent.
- **A segfault in native code takes the worker down.** No sandbox, no per-call
  timeout, no per-call isolation.

Point at `docs/production-readiness.md` and `DEFECTS.md` for the detail.

Regenerated files are rewritten from source every run. Do not hand-edit
anything under `bindings/generated/`, the generated `CMakeLists.txt`, or
anything in `python/<name>/` (`__init__.py`, `router.py`, `service.py`,
`middleware.py`) — change `native2py.yaml` and re-run `generate`.
`pyproject.toml` is the exception: it is generated once and then yours, and
`generate` restores it only if it is missing.
