# Deployment topologies & sharing

"Each service gets its own FastAPI app" is the *default*, not the only
option. There are three genuinely different things you might want to share,
and they have different answers — conflating them is the usual source of
pain.

| You want to share... | Mechanism | Status |
|---|---|---|
| **One API URL** across services | Composed gateway, or an ingress in front of separate images | ✅ both supported |
| **One deployable** (one image, one scale unit) | Composed gateway | ✅ `native2py gateway` |
| **Native code** across services (common C++/Fortran) | Shared CMake library target | ✅ C++ via `libraries:` — Fortran not yet |

## The enabling design: routers, not apps

Every generated service produces **two** files:

```python
# python/<name>/router.py  — the endpoints, deliberately WITHOUT a prefix
router = APIRouter(tags=["demo"])

@router.post("/circle_area")
def circle_area(radius: float): ...
```

```python
# python/<name>/service.py — standalone app, a thin wrapper
app = FastAPI(title="demo")
app.include_router(router)

@app.get("/healthz")
def healthz(): ...
```

Because the prefix isn't baked into the router, **the same generated code
serves both topologies unchanged**. That's the whole trick.

## Topology A — one image per service (default)

```
                    ┌──────────────┐
   client ────────► │   Ingress /  │───► demo-service:8000       (own image)
                    │  API Gateway │───► calculator-service:8000 (own image)
                    └──────────────┘
```

```bash
native2py docker demo         # demo:latest
native2py docker calculator   # calculator:latest
# deploy separately; route /demo/* and /calculator/* at the ingress
```

- ✅ Independent deploy, independent scaling, blast-radius isolation (a
  segfault in one native extension can't take down the other — see
  [production readiness](production-readiness.md), this is a real risk here)
- ✅ Independent image versions in your registry
- ❌ More infrastructure: N images, N deployments, ingress config

**This is design.md §23 Phase 4** and needs no code generation — just
routing rules. Each generated `service.py` already runs standalone.

## Topology B — one composed image, one URL

```bash
native2py gateway platform-api --service demo --service calculator
```

Generates `gateways/platform-api/`:

```python
from demo.router import router as demo_router
from calculator.router import router as calculator_router

app = FastAPI(title="platform-api")
app.include_router(demo_router, prefix="/demo")
app.include_router(calculator_router, prefix="/calculator")
```

and a `pyproject.toml` whose dependencies are **the service wheels
themselves**:

```toml
dependencies = ["fastapi", "uvicorn", "demo", "calculator"]
```

That last part is the important bit for enterprise: **each service is still
built, versioned, and published independently** as a normal wheel. The
gateway just consumes them — pin versions, upgrade one at a time, roll back
one at a time, exactly like any other Python dependency. Nothing is vendored
or copy-pasted.

- ✅ One URL, one image, one deploy — much simpler ops
- ✅ Services still built/versioned independently
- ✅ No network hop between "services" (it's one process)
- ❌ No independent scaling — they share CPU/memory
- ❌ **Shared blast radius**: a native segfault kills every mounted service

Use this when your services are low-traffic, related, and operationally
better off as one unit. This maps to design.md §23 Phase 2/3 — a sensible
waypoint before splitting into Phase 4.

### Verified

Both topologies were checked against real compiled extensions, not mocks —
two separately-built C++ wheels (`demo` with a `Geometry` class, `calculator`
with a `Calculator` class), installed together, served from one gateway:

```
GET  /healthz              -> {"status":"ok","services":["demo","calculator"]}
POST /demo/circle_area     -> {"result":12.566370614359172}
POST /demo/hypotenuse      -> {"result":5.0}
POST /calculator/add       -> {"result":5.0}
POST /calculator/multiply  -> {"result":20.0}
```

and the *same* `demo` wheel run standalone, paths unprefixed:

```
GET  /healthz        -> {"status":"ok","service":"demo"}
POST /circle_area    -> {"result":12.566370614359172}
```

## Switching between them

You don't regenerate anything. The service wheels are identical in both
cases:

- **A → B**: `native2py gateway ...`, deploy the gateway image instead of N
  service images.
- **B → A**: delete the gateway, deploy each `service.py` image, move the
  prefixes into your ingress rules.

This is why the router split matters — the topology decision stays reversible.

## Sharing native code — `libraries/common-cpp`

design.md §4's `libraries/` directory is implemented for C++. Declare the
library in a service's `native2py.yaml`:

```yaml
name: demo
language: cpp
libraries:
  - common-cpp        # -> libraries/common-cpp/
```

`native2py generate` then emits the CMake wiring for you:

```cmake
add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../../libraries/common-cpp
                 ${CMAKE_CURRENT_BINARY_DIR}/_libraries/common-cpp)
...
target_link_libraries(geometry_cpp PRIVATE common_cpp)
```

### What the library must provide

A shared library is a normal CMake project with its own `CMakeLists.txt`:

```
libraries/common-cpp/
├── CMakeLists.txt
├── include/units.hpp      # PUBLIC include dir — consumers see this
└── src/units.cpp
```

```cmake
add_library(common_cpp STATIC src/units.cpp)
set_target_properties(common_cpp PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(common_cpp PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>)
```

Two details that will bite you if you skip them:

- **`STATIC`, not `SHARED`** — the library links directly into each
  service's Python extension, so there's no second `.so` to install or keep
  on `LD_LIBRARY_PATH` in the container.
- **`POSITION_INDEPENDENT_CODE ON`** is required, because the consuming
  target is a shared module. Without it the link fails.
- **Target name has no hyphen.** The directory is `common-cpp`; the CMake
  target is `common_cpp`. native2py translates between them.

Consuming services just `#include "units.hpp"` — the include path arrives
through the library's `PUBLIC` usage requirement, nothing to configure.

### Docker: the build context changes

This is the one non-obvious consequence. `libraries/` sits *outside* the
service directory, so `COPY` can't reach it from a service-dir build
context. When a service declares libraries, the generated Dockerfile
switches to **repo-root context** with repo-relative paths:

```dockerfile
COPY libraries/common-cpp ./libraries/common-cpp
COPY services/demo ./services/demo
WORKDIR /build/services/demo
```

and must be built accordingly — `native2py docker demo` prints this, and
`--build` does it for you:

```bash
docker build -f services/demo/Dockerfile -t demo:latest .   # note the trailing "."
```

Services *without* libraries keep the simpler service-dir context and
`COPY . .`, unchanged.

### Trade-off: static linking duplicates code

Each service gets its own copy of the library compiled into its extension.
That's the right default here — self-contained wheels, no runtime library
resolution — but it means a fix in `common-cpp` requires **rebuilding every
service that links it**, not just redeploying one shared `.so`. There's no
version negotiation: whatever is in `libraries/common-cpp/` at build time is
what you get. Treat it as source-level sharing, not binary-level.

### Verified

`libraries/common-cpp` (unit conversions) linked into two services built
independently, both loaded in one interpreter:

```
demo       circle_area_from_feet(1.0) = 0.2918635079601587   # uses feet_to_metres
calculator add_psi_as_pascal(1, 1)    = 13789.514586336722   # uses psi_to_pascal
```

and the same thing through a real Docker image (fresh Linux compile,
non-root):

```
POST /circle_area_from_feet -> {"result":0.2918635079601587}
```

### Not yet: shared Fortran libraries

`libraries:` is wired into the C++/CMake path only. Fortran services build
through `f2py -c`, which doesn't take the same `add_subdirectory` treatment
— sharing Fortran code across services still means duplicating source or
managing a prebuilt library yourself.
