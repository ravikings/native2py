# Getting started

This walks a C++ example end to end using the real CLI. Everything here is
self-contained — you create the source, so nothing depends on an example
service being present in the repo.

Run all commands from the **repo root**. Every command resolves
`services/<name>/` relative to your working directory.

## 1. Install nativegate

One-shot setup — creates `.venv` and installs the CLI plus the build, test,
and docs dependencies:

```bash
cd tools/nativegate
./scripts/bootstrap.sh
cd -
```

Then either activate the venv, or prefix commands with its path:

```bash
source tools/nativegate/.venv/bin/activate     # then: nativegate ...
# or
tools/nativegate/.venv/bin/nativegate ...
```

`requirements.txt` only covers Python packages. `ngate build` also needs
a system toolchain — `cmake`, `ninja`, a C++ compiler, and `gfortran` for
Fortran services. `bootstrap.sh` warns if any are missing; see
[Troubleshooting](troubleshooting.md) to install them.

!!! warning "Fortran on Python 3.12+ needs `meson`"

    `ngate build` runs `python -m numpy.f2py -c`. Python 3.12 removed
    `distutils`, so f2py switches to its **meson** backend and shells out to a
    `meson` executable. Without it the build fails with
    `FileNotFoundError: [Errno 2] No such file or directory: 'meson'` after
    printing what looks like a successful wrapper generation — the wrappers
    really are written, then the compile step dies.

    `meson` and `ninja` are in the `[build]` extra, so
    `pip install -e ".[build]"` (or `requirements.txt`) covers it. The
    generated Dockerfile installs them in its builder stage for the same
    reason. Install with pip rather than apt: Debian stable's `meson` is older
    than f2py's backend expects.

## 2. Write some C++

Keep your source outside `services/` — nativegate copies it in, and
`services/` holds generated output. `libraries/` is a good home:

```bash
mkdir -p libraries/geometry
```

In this repo `libraries/geometry/` already exists with exactly the two files
below, so you can skip ahead to step 3 and use it as-is. Create it yourself
only if you are following along in your own project.

```cpp
// libraries/geometry/geometry.hpp
#pragma once

class Geometry {
public:
    double circle_area(double radius);
    double hypotenuse(double a, double b);
};
```

```cpp
// libraries/geometry/geometry.cpp
#include "geometry.hpp"
#include <cmath>

double Geometry::circle_area(double radius) {
    return M_PI * radius * radius;
}

double Geometry::hypotenuse(double a, double b) {
    return std::sqrt(a * a + b * b);
}
```

The `.cpp` matters: a header of declarations with no definitions links
"successfully" and then fails on first import. See
[Exposing C++](cpp-guide.md#declarations-need-definitions).

## 3. Generate everything with one command

```bash
ngate quickstart libraries/geometry/geometry.hpp --name demo
```

```
  services/demo
  libraries/geometry/geometry.hpp -> services/demo/native/geometry.hpp
WARNING: services/demo/nativegate.yaml: `expose:` is empty, so every symbol
nativegate finds will be bound unless the source carries [[nativegate::expose]]
annotations. Say so explicitly with `expose: all`, or list the
classes/functions you want under `expose.classes:` / `expose.functions:`.
  libraries/geometry/geometry.cpp -> services/demo/native/geometry.cpp
Parsing C++ with clang AST (libclang 18.1.1).
  services/demo/python/demo
✔ Scaffold service
✔ Copy native source
✔ Generate bindings & package

Done — services/demo is ready.
```

It found the sibling `.cpp` automatically, scaffolded `services/demo/`,
exposed everything in the header, and generated the bindings, CMake, Python
package, FastAPI layer, middleware, and a smoke test.

The warning is expected here and worth understanding: `quickstart` leaves
`expose:` empty, which still binds everything but does not *say* that was the
intent. For a service you keep, set `expose: all` in `nativegate.yaml`, or list
the specific classes and functions you want.

`libraries/geometry/` is untouched — only read and copied.

??? note "Prefer the step-by-step route?"

    `quickstart` is `create-service` + `expose` + `generate` in one. To do it
    manually — which you'll want for partial exposure or multi-file services:

    ```bash
    ngate create-service demo --language cpp
    cp libraries/geometry/geometry.* services/demo/native/
    ngate inspect services/demo/native/geometry.hpp   # see what it parsed
    ngate generate demo
    ```

## 4. Check what it parsed

```bash
ngate inspect services/demo/native/geometry.hpp
```

```
parser: clang AST (libclang 18.1.1)
module: geometry (cpp)
  class Geometry
    circle_area(radius: float) -> float
    hypotenuse(a: float, b: float) -> float
```

The first line names the parser that ran. C++ is parsed by Clang itself, so
`#include`s, macros and `#ifdef`s are resolved the way your build resolves
them; if libclang is missing, nativegate says so and falls back to a weaker
regex reader — see [Exposing C++](cpp-guide.md#the-fallback-reader).

Types are already mapped C++ → Python (`double` → `float`). Anything
nativegate can't bind is listed here with a reason rather than dropped
silently — see [Exposing C++](cpp-guide.md#nothing-is-dropped-silently).

## 5. Build it

```bash
ngate build demo
```

This runs `pip wheel .` in `services/demo/`, driving scikit-build-core →
CMake → pybind11 to compile the extension into a wheel.

## 6. Use it from Python

```bash
pip install services/demo/dist/demo-1.0.0*.whl
```

```python
from demo import Geometry

g = Geometry()
g.circle_area(2.0)      # 12.566370614359172
g.hypotenuse(3.0, 4.0)  # 5.0
```

The generated FastAPI service:

```bash
ngate serve demo
```

```bash
curl -X POST "http://localhost:8000/circle_area?radius=2"
# {"result":12.566370614359172}

curl http://localhost:8000/healthz
# {"status":"ok","service":"demo"}
```

## 7. Pin the numbers

Before anything else changes, record what the bindings return:

```bash
ngate golden record demo
```

That writes `services/demo/golden.json` — every bound entry point, the inputs
it was called with, and the answer. Commit it. From then on, any rebuild that
moves a number fails:

```bash
ngate golden verify demo
# 2 entry point(s) unchanged (0 not covered).
```

For real engineering code, edit the recorded `arguments` to values your
engineers recognise (a real pressure, a real API gravity) and re-record — your
inputs are kept across future re-records. See
[Numerical regression](golden-values.md).

## 8. Test and containerize

```bash
ngate test demo            # pytest tests/ (includes the golden check)
ngate lock demo            # pin deps by version + SHA-256
ngate docker demo          # writes Dockerfile
ngate docker demo --build  # also runs docker build
```

Run `lock` first. Without a `requirements.lock`, `docker` prints a note that
pip will resolve fastapi/uvicorn/numpy from PyPI at build time, so the same
Dockerfile built a week later can ship different code with nothing recording
the change.

The generated image is multi-stage (no compilers in the runtime layer), runs
as a non-root `appuser`, and carries a `HEALTHCHECK`.

Alongside your two endpoints, the service also serves `/healthz` (liveness),
`/readyz` (readiness, and it reports 503 while draining on SIGTERM), and
`/_unexposed` — the symbols the parser recognised and refused to bind, with
the reason. For `demo` that last one is empty, because nothing was refused.

To generate Kubernetes manifests wired to those probes:

```bash
ngate k8s demo                   # -> infrastructure/kubernetes/demo.yaml
```

## Where to go next

- Real C++ headers — overloads, templates, forward declarations, external
  includes: [Exposing C++](cpp-guide.md)
- Legacy Fortran, especially fixed-form F77:
  [Exposing Fortran](fortran-guide.md)
- Every `nativegate.yaml` field:
  [configuration reference](configuration.md)
- Several services behind one URL:
  [Deployment topologies](deployment-topologies.md)
- Proving a re-host did not change the answers:
  [Numerical regression](golden-values.md)
- What is generated versus what is yours (and what survives a deletion):
  [Architecture](architecture.md#what-is-generated-what-is-yours)

## Before you deploy this

What you just built is suitable for **internal, single-tenant** use — a team
calling legacy code it already trusts. It is not ready to face the internet or
to serve multiple tenants from one process.

Two things to know before sizing anything:

- A **Fortran** service serialises every native call through one process-wide
  lock, because COMMON blocks are process-global state. Concurrency comes from
  more processes, not more threads. (C++ services like `demo` are not locked —
  they build a fresh instance per request.)
- A **segfault in native code takes the worker down.** There is no sandbox and
  no per-call timeout.

[Is this production-ready?](production-readiness.md), and `DEFECTS.md` in the
tool's root, are the honest gap lists.
