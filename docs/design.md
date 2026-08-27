# Technical Specification: NativeGate — Automated Native Code to Python Service Generator

## 1. Overview

**NativeGate** is an internal developer platform/CLI that automatically exposes native-language code—initially **C++, C, and Fortran**—to Python and packages it as an independently deployable Docker microservice.

The primary objective is to make native-code modernization nearly frictionless:

```text
Existing C++ / C / Fortran
          │
          ▼
     NativeGate CLI
          │
          ├── Parse native API
          ├── Generate bindings
          ├── Generate Python package
          ├── Generate tests
          ├── Generate Docker image
          └── Generate service metadata
          │
          ▼
      Python API
          │
          ▼
       FastAPI
          │
          ▼
     Docker Container
          │
          ▼
      Kubernetes
```

The developer should be able to expose a native function/class with one command and import it from Python without manually writing binding code.

---

# 2. Goals

### Primary goals

1. Automatically discover public native APIs.
2. Generate Python bindings automatically.
3. Support:

   * C++
   * C
   * Fortran
4. Generate a standard Python package.
5. Generate a Docker image for each service.
6. Support monorepo development.
7. Allow native code to remain the computational core.
8. Provide a stable Python API independent of the underlying language.
9. Support local development and CI/CD.
10. Make migration from monolith → microservices incremental.

### Non-goals

NativeGate will not initially:

* Automatically rewrite native code into Python.
* Expose every native symbol without explicit configuration.
* Replace C++/Fortran compilers.
* Become a general-purpose RPC framework.
* Automatically split an application into domain boundaries.

---

# 3. Developer Experience

The ideal experience is:

```bash
ngate create-service inference
```

Then:

```bash
ngate expose cpp/inference.hpp
```

or:

```bash
ngate expose fortran/solver.f90
```

Then:

```bash
ngate build inference
```

The developer can immediately write:

```python
from inference import InferenceEngine

engine = InferenceEngine()

result = engine.predict(data)
```

The implementation may be C++:

```text
InferenceEngine
      ↓
C++ implementation
```

or Fortran:

```text
calculate_pressure()
      ↓
Fortran implementation
```

The Python developer does not need to know the difference.

---

# 4. Repository Architecture

Recommended monorepo:

```text
repo/
│
├── services/
│   ├── inference/
│   │   ├── native/
│   │   │   ├── inference.hpp
│   │   │   └── inference.cpp
│   │   ├── bindings/
│   │   │   └── generated/
│   │   ├── python/
│   │   │   └── inference/
│   │   │       ├── __init__.py
│   │   │       └── service.py
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   └── nativegate.yaml
│   │
│   └── reservoir/
│       ├── native/
│       │   ├── reservoir.f90
│       │   └── pressure.f90
│       ├── bindings/
│       ├── python/
│       ├── tests/
│       ├── Dockerfile
│       ├── pyproject.toml
│       └── nativegate.yaml
│
├── libraries/
│   ├── common-cpp/
│   └── common-fortran/
│
├── tools/
│   └── nativegate/
│
└── infrastructure/
    ├── docker/
    └── kubernetes/
```

Each `services/<name>` directory is independently buildable and deployable.

---

# 5. Core Architecture

```text
                         ┌────────────────────┐
                         │   nativegate CLI   │
                         └─────────┬──────────┘
                                   │
                                   ▼
                         ┌────────────────────┐
                         │ Language Detector  │
                         └─────────┬──────────┘
                                   │
                  ┌────────────────┼────────────────┐
                  ▼                ▼                ▼
               C/C++            Fortran           C
                  │                │                │
                  ▼                ▼                ▼
              Clang AST          f2py          C ABI parser
                  │                │                │
                  └────────────────┼────────────────┘
                                   ▼
                         ┌────────────────────┐
                         │ Intermediate API   │
                         │ Representation     │
                         └─────────┬──────────┘
                                   │
                                   ▼
                         ┌────────────────────┐
                         │ Binding Generator  │
                         └─────────┬──────────┘
                                   │
                         ┌─────────┴─────────┐
                         ▼                   ▼
                    pybind11             f2py
                         │                   │
                         └─────────┬─────────┘
                                   ▼
                         Python Extension
                                   │
                                   ▼
                         Python Package
                                   │
                                   ▼
                         Docker Generator
                                   │
                                   ▼
                         Kubernetes Service
```

---

# 6. Intermediate Representation

A critical design decision is to **not generate bindings directly from language-specific parsers**.

Instead, NativeGate should normalize APIs into an intermediate representation.

Example:

```yaml
name: Calculator
language: cpp
namespace: math

constructors:
  - []

methods:
  - name: add
    parameters:
      - name: a
        type: float64
      - name: b
        type: float64
    returns: float64
```

For Fortran:

```yaml
name: calculate_pressure
language: fortran

parameters:
  - name: density
    type: float64
    direction: input

  - name: temperature
    type: float64
    direction: input

returns:
  type: float64
```

The IR becomes the common contract between language parsers and binding generators.

---

# 7. C++ Binding Strategy

C++ parsing will use **Clang AST/libTooling**.

Example:

```cpp
class Calculator {
public:
    double add(double a, double b);
    double multiply(double a, double b);
};
```

NativeGate generates:

```cpp
#include <pybind11/pybind11.h>
#include "calculator.hpp"

namespace py = pybind11;

PYBIND11_MODULE(calculator_cpp, m) {
    py::class_<Calculator>(m, "Calculator")
        .def(py::init<>())
        .def("add", &Calculator::add)
        .def("multiply", &Calculator::multiply);
}
```

Python:

```python
from calculator import Calculator

calculator = Calculator()

calculator.add(2, 3)
```

---

# 8. Fortran Binding Strategy

Fortran will initially use **f2py** as the default mechanism.

Example:

```fortran
module physics

contains

    function calculate_pressure(density, temperature) result(p)
        real(8), intent(in) :: density
        real(8), intent(in) :: temperature
        real(8) :: p

        p = density * temperature
    end function calculate_pressure

end module physics
```

NativeGate generates the build configuration necessary for f2py.

Python:

```python
from physics import calculate_pressure

pressure = calculate_pressure(10.0, 300.0)
```

For complex or long-lived APIs, NativeGate should support an alternative:

```text
Fortran
   │
   ▼
ISO_C_BINDING
   │
   ▼
Stable C ABI
   │
   ▼
NativeGate
   │
   ▼
Python
```

This is particularly useful for legacy scientific Fortran.

---

# 9. Explicit Exposure

NativeGate should **not automatically expose the entire native codebase**.

Support an annotation/configuration mechanism.

Example:

```cpp
[[nativegate::expose]]
class InferenceEngine {
public:

    [[nativegate::expose]]
    double predict(double value);
};
```

For Fortran:

```fortran
! nativegate: expose
function calculate_pressure(density, temperature)
```

Alternatively:

```yaml
# nativegate.yaml

expose:
  classes:
    - InferenceEngine

  functions:
    - calculate_pressure
    - calculate_temperature
```

Configuration should be the preferred mechanism for legacy code where modifying source is undesirable.

---

# 10. Generated Python Package

Generated structure:

```text
python/
└── inference/
    ├── __init__.py
    ├── _native/
    │   └── inference_cpp.so
    └── service.py
```

`__init__.py`:

```python
from ._native.inference_cpp import InferenceEngine

__all__ = ["InferenceEngine"]
```

The compiled extension remains an implementation detail.

---

# 11. Docker Generation

NativeGate automatically creates a multi-stage Dockerfile.

Conceptually:

```text
Builder
│
├── Python
├── GCC/GFortran
├── CMake
├── pybind11
├── f2py
├── native source
└── compile
        │
        ▼
Runtime
│
├── Python
├── compiled extensions
├── Python package
└── FastAPI
```

Example:

```dockerfile
FROM python:3.12-slim AS builder

RUN apt-get update && \
    apt-get install -y \
        build-essential \
        gfortran \
        cmake && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY . .

RUN pip install --no-cache-dir \
    pybind11 \
    numpy \
    scikit-build-core

RUN pip wheel . -w /dist


FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /dist /dist

RUN pip install --no-cache-dir /dist/*.whl

CMD ["python", "-m", "service"]
```

Production images should contain **only runtime dependencies**, not compilers.

---

# 12. FastAPI Service Layer

The generated native package should not automatically dictate the HTTP API.

Instead:

```text
HTTP
  ↓
FastAPI
  ↓
Service Layer
  ↓
Python Binding
  ↓
C++ / Fortran
```

Example:

```python
from fastapi import FastAPI
from inference import InferenceEngine

app = FastAPI()

engine = InferenceEngine()


@app.post("/predict")
def predict(value: float):
    return {
        "result": engine.predict(value)
    }
```

This preserves a clean separation between:

* transport
* business logic
* Python bindings
* native implementation

---

# 13. CLI

Initial commands:

```bash
ngate init
ngate create-service <name>
ngate detect <path>
ngate inspect <path>
ngate expose <path>
ngate generate <service>
ngate build <service>
ngate test <service>
ngate docker <service>
ngate clean <service>
```

Example:

```bash
ngate create-service reservoir --language fortran
```

Then:

```bash
ngate expose services/reservoir/native
```

Then:

```bash
ngate build reservoir
```

Then:

```bash
ngate docker reservoir
```

---

# 14. Build System

Use **CMake** as the native build abstraction wherever possible.

This provides a common build layer across:

```text
C
C++
Fortran
```

Python packaging can use **scikit-build-core** for C/C++/Fortran projects that need CMake integration.

The architecture becomes:

```text
pyproject.toml
       │
       ▼
scikit-build-core
       │
       ▼
CMake
       │
 ┌─────┼─────┐
 ▼     ▼     ▼
 C    C++  Fortran
```

---

# 15. Type Mapping

NativeGate needs a deterministic type mapping layer.

Example:

| Native            | Python                  |
| ----------------- | ----------------------- |
| `int`             | `int`                   |
| `int32_t`         | `int`                   |
| `int64_t`         | `int`                   |
| `float`           | `float`                 |
| `double`          | `float`                 |
| `bool`            | `bool`                  |
| `std::string`     | `str`                   |
| `std::vector<T>`  | `list[T]` / NumPy array |
| `double*`         | NumPy array             |
| Fortran `real(8)` | `float`                 |
| Fortran `integer` | `int`                   |
| Fortran arrays    | NumPy arrays            |

Unsupported types should generate a clear build-time error rather than silently producing an unsafe binding.

---

# 16. Arrays and Scientific Computing

For Fortran-heavy workloads, NumPy interoperability is a first-class requirement.

Example:

```fortran
subroutine normalize(values, n)
    real(8), intent(inout) :: values(n)
    integer, intent(in) :: n
end
```

Python:

```python
import numpy as np

values = np.array(
    [1.0, 2.0, 3.0],
    dtype=np.float64
)

normalize(values)
```

NativeGate should minimize unnecessary copies.

This is particularly important for:

* reservoir simulation
* numerical optimization
* ML inference
* physics
* financial calculations
* matrix operations

---

# 17. Performance Requirements

The binding layer must introduce minimal overhead.

For compute-heavy operations:

```text
Python
   │
   │ one binding call
   ▼
Native code
   │
   │ millions of operations
   ▼
Python
```

is preferred over:

```text
Python
 ↓
native
 ↓
Python
 ↓
native
 ↓
Python
```

NativeGate should encourage coarse-grained native APIs.

---

# 18. Testing

Generated services should automatically receive:

```text
tests/
├── test_python_api.py
├── test_binding.py
└── test_native.py
```

Minimum test:

```python
def test_calculator():
    from calculator import Calculator

    c = Calculator()

    assert c.add(2, 3) == 5
```

For Fortran:

```python
def test_pressure():
    from physics import calculate_pressure

    assert calculate_pressure(10.0, 300.0) == 3000.0
```

NativeGate should also provide an ABI/build smoke test.

---

# 19. CI/CD

Every generated service should support:

```text
git push
   │
   ▼
CI
   │
   ├── lint
   ├── native compile
   ├── binding generation
   ├── Python tests
   ├── native tests
   ├── package wheel
   ├── Docker build
   ├── security scan
   └── publish
           │
           ▼
       Container Registry
           │
           ▼
        Kubernetes
```

A service should be independently deployable without rebuilding unrelated services.

---

# 20. Versioning

NativeGate should generate a versioned Python API.

Example:

```text
inference-python
    1.0.0
```

The native implementation can evolve internally while maintaining:

```python
from inference import InferenceEngine
```

Breaking native API changes should result in a binding-generation/build failure unless explicitly approved.

---

# 21. Security

Generated native extensions execute arbitrary machine code.

Therefore:

* Never compile untrusted source in production.
* Build inside isolated CI workers.
* Pin compiler/toolchain versions.
* Scan dependencies.
* Generate SBOMs.
* Sign container images.
* Run containers as non-root.
* Use minimal runtime images.
* Restrict filesystem access.
* Avoid dynamic loading from untrusted paths.

---

# 22. Observability

The generated FastAPI service should support:

```text
OpenTelemetry
    │
    ├── traces
    ├── metrics
    └── logs
```

Native execution time should optionally be measurable separately:

```text
HTTP latency
Python overhead
Native execution time
Serialization time
```

This makes performance regressions visible during the migration.

---

# 23. Migration Strategy

The platform should support incremental migration.

### Phase 1 — Embed

```text
Python monolith
      ↓
Python binding
      ↓
C++ / Fortran
```

### Phase 2 — Containerize

```text
Python + native code
        ↓
Docker
```

### Phase 3 — Extract

```text
             Monolith
                 │
        ┌────────┴────────┐
        ▼                 ▼
   Python service    Native service
```

### Phase 4 — Independent scaling

```text
API Gateway
     │
     ├── inference-service
     │
     ├── reservoir-service
     │
     └── analytics-service
```

The native implementation doesn't need to change during Phase 1 → Phase 3.

---

# 24. Future RPC Mode

Eventually NativeGate could support:

```bash
ngate expose --mode grpc
```

Instead of:

```text
Python
  ↓
pybind11
  ↓
C++
```

you could generate:

```text
Python
  ↓
gRPC
  ↓
C++ service
```

This allows independent scaling.

The Python API could remain:

```python
result = inference.predict(data)
```

while the implementation changes underneath:

```text
Mode 1:
Python → pybind11 → C++

Mode 2:
Python → gRPC → C++
```

That is one of the strongest reasons to introduce the **intermediate API representation and service abstraction early**.

---

# 25. MVP

I would **not** build all of this at once.

### MVP 1

Support:

* C++
* pybind11
* Clang AST
* simple functions
* simple classes
* CMake
* Python package generation
* Docker generation
* CLI

Target experience:

```bash
ngate create-service calculator
ngate expose calculator.hpp
ngate build calculator
```

Then:

```python
from calculator import Calculator
```

### MVP 2

Add:

* Fortran
* f2py
* NumPy
* ISO_C_BINDING
* arrays
* structs/derived types

### MVP 3

Add:

* FastAPI generation
* Kubernetes manifests
* CI/CD templates
* observability
* service registry

### MVP 4

Add:

* gRPC mode
* remote native services
* ABI compatibility checks
* automatic API versioning
* performance profiling

---

# 26. Success Criteria

The project succeeds if a developer can take an existing native function such as:

```cpp
double calculate(double x);
```

or:

```fortran
function calculate(x) result(y)
```

and get to:

```python
from service import calculate

calculate(10)
```

with **no manually written binding code**.

The ultimate developer workflow should be:

```text
1. Put native code in service/native/
2. Run ngate expose
3. Run ngate build
4. Import from Python
5. Run ngate docker
6. Deploy
```

### The architectural principle

The most important design decision is to make **Python the stable developer-facing interface, not the implementation language**.

```text
                    Stable Python API
                           │
             ┌─────────────┼─────────────┐
             ▼             ▼             ▼
           C++           Fortran          C
             │             │              │
             └─────────────┼──────────────┘
                           ▼
                    NativeGate Runtime
                           │
                           ▼
                     Docker/K8s
```

That lets you modernize a large native codebase **without forcing a rewrite**, while still giving developers the ergonomics of a modern Python microservice architecture.
