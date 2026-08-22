# Getting started

This walks a C++ example end to end using the real CLI. Everything here is
self-contained — you create the source, so nothing depends on an example
service being present in the repo.

Run all commands from the **repo root**. Every command resolves
`services/<name>/` relative to your working directory.

## 1. Install native2py

One-shot setup — creates `.venv` and installs the CLI plus the build, test,
and docs dependencies:

```bash
cd tools/native2py
./scripts/bootstrap.sh
cd -
```

Then either activate the venv, or prefix commands with its path:

```bash
source tools/native2py/.venv/bin/activate     # then: native2py ...
# or
tools/native2py/.venv/bin/native2py ...
```

`requirements.txt` only covers Python packages. `native2py build` also needs
a system toolchain — `cmake`, `ninja`, a C++ compiler, and `gfortran` for
Fortran services. `bootstrap.sh` warns if any are missing; see
[Troubleshooting](troubleshooting.md) to install them.

## 2. Write some C++

Keep your source outside `services/` — native2py copies it in, and
`services/` holds generated output. `libraries/` is a good home:

```bash
mkdir -p libraries/geometry
```

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
native2py quickstart libraries/geometry/geometry.hpp --name demo
```

```
Copied libraries/geometry/geometry.hpp -> services/demo/native/geometry.hpp
Copied libraries/geometry/geometry.cpp -> services/demo/native/geometry.cpp
Generated Python package at services/demo/python/demo
```

It found the sibling `.cpp` automatically, scaffolded `services/demo/`,
exposed everything in the header, and generated the bindings, CMake, Python
package, FastAPI layer, and a smoke test.

`libraries/geometry/` is untouched — only read and copied.

??? note "Prefer the step-by-step route?"

    `quickstart` is `create-service` + `expose` + `generate` in one. To do it
    manually — which you'll want for partial exposure or multi-file services:

    ```bash
    native2py create-service demo --language cpp
    cp libraries/geometry/geometry.* services/demo/native/
    native2py inspect services/demo/native/geometry.hpp   # see what it parsed
    native2py generate demo
    ```

## 4. Check what it parsed

```bash
native2py inspect services/demo/native/geometry.hpp
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
them; if libclang is missing, native2py says so and falls back to a weaker
regex reader — see [Exposing C++](cpp-guide.md#the-fallback-reader).

Types are already mapped C++ → Python (`double` → `float`). Anything
native2py can't bind is listed here with a reason rather than dropped
silently — see [Exposing C++](cpp-guide.md#nothing-is-dropped-silently).

## 5. Build it

```bash
native2py build demo
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
uvicorn demo.service:app
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
native2py golden record demo
```

That writes `services/demo/golden.json` — every bound entry point, the inputs
it was called with, and the answer. Commit it. From then on, any rebuild that
moves a number fails:

```bash
native2py golden verify demo
# 2 entry point(s) unchanged (0 not covered).
```

For real engineering code, edit the recorded `arguments` to values your
engineers recognise (a real pressure, a real API gravity) and re-record — your
inputs are kept across future re-records. See
[Numerical regression](golden-values.md).

## 8. Test and containerize

```bash
native2py test demo                  # pytest tests/ (includes the golden check)
native2py docker demo                # writes Dockerfile
native2py docker demo --build        # also runs docker build
```

The generated image is multi-stage (no compilers in the runtime layer),
runs as a non-root user, and carries a `HEALTHCHECK`.

## Where to go next

- Real C++ headers — overloads, templates, forward declarations, external
  includes: [Exposing C++](cpp-guide.md)
- Legacy Fortran, especially fixed-form F77:
  [Exposing Fortran](fortran-guide.md)
- Every `native2py.yaml` field:
  [configuration reference](configuration.md)
- Several services behind one URL:
  [Deployment topologies](deployment-topologies.md)
- Proving a re-host did not change the answers:
  [Numerical regression](golden-values.md)
- What is generated versus what is yours (and what survives a deletion):
  [Architecture](architecture.md#what-is-generated-what-is-yours)
- Before running any of this for real:
  [Is this production-ready?](production-readiness.md)
