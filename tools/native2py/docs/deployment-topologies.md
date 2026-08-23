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

## Topology C — air-gapped / regulated build

The environments where 1988 Fortran actually lives are usually the ones with
no outbound network and a change-control board that wants to know exactly
what went into a binary. This section is about what works there, and — more
usefully — what does not yet.

### Reproducibility: the three pins

Reproducibility here means three separate things, pinned in three places.
Miss any one and the other two buy you nothing.

| What | Pinned by | Mutable if unpinned |
|---|---|---|
| native2py's own Python deps | `constraints.txt` (exact `==` + SHA-256) | resolution drifts daily |
| The container base image | `docker_gen.BASE_IMAGE_DIGEST` (`@sha256:`) | Docker Hub re-points `python:3.12-slim` on every rebuild |
| The native sources compiled in | the generated SBOM (path + SHA-256) | nothing records which revision of the deck is inside the wheel |

**1. The tool.** `pyproject.toml` keeps `>=` ranges because native2py is a
library and pinning a library's `dependencies` propagates conflicts to
consumers. Reproducibility lives beside it, in `constraints.txt`:

```bash
pip install --require-hashes -r constraints.txt
pip install --no-deps -e .
```

`--require-hashes` makes pip refuse anything whose SHA-256 does not match,
which is the difference between a version pin and a supply-chain control. Its
header documents regeneration. Note the one dependency that puts vendor-built
native code in your process: **`libclang` ships a prebuilt libclang shared
library inside the wheel**. Hash-pinning is the only control short of
building libclang yourself; `NATIVE2PY_LIBCLANG` points at a system libclang
if your policy forbids vendored binaries.

**2. The base image.** Generated Dockerfiles use
`FROM python:3.12-slim@sha256:...` for *both* stages. A tag is a moving
pointer; a digest is the image. Two builds a month apart off a tag get
different interpreters and a different libgfortran with no record anywhere —
which quietly undoes the numerical-reproducibility story the golden-value
tests exist to defend. Refresh the digest deliberately (the constant's
comment has the `docker inspect` and the registry-API `curl` recipes), then
re-run the golden tests before shipping the new base.

### SBOM: `sbom.cdx.json`

A wheel is an opaque binary. Without source digests there is no way to answer
"which revision of the deck is in this image?" during an audit, which is the
question a regulator actually asks. The generated SBOM records both halves:
the Python dependency surface *and* every native source compiled in, each
with its path and SHA-256.

```bash
python -c "
from pathlib import Path
from native2py.generators import docker_gen
print(docker_gen.write_sbom(Path('services/reservoir'), 'reservoir', 'fortran', 'physics'))
"
```

CycloneDX 1.5 JSON, roughly:

```json
{
  "bomFormat": "CycloneDX",
  "specVersion": "1.5",
  "version": 1,
  "metadata": {
    "tools": [{"vendor": "native2py", "name": "native2py"}],
    "component": {
      "type": "application", "name": "reservoir", "version": "1.0.0",
      "purl": "pkg:pypi/reservoir@1.0.0",
      "properties": [
        {"name": "native2py:language", "value": "fortran"},
        {"name": "native2py:baseImage", "value": "python:3.12-slim@sha256:..."}
      ]
    }
  },
  "components": [
    {"type": "library", "name": "fastapi", "purl": "pkg:pypi/fastapi"},
    {"type": "file", "name": "src/flow.f90",
     "hashes": [{"alg": "SHA-256", "content": "00ff1fce..."}]}
  ],
  "serialNumber": "urn:uuid:d35f3b74-..."
}
```

**Deterministic on purpose.** No `metadata.timestamp`, no random serial
number, components sorted. The serial is a UUIDv5 over the BOM's own content,
so it is stable for identical inputs and changes the moment a source does.
That means you can commit `sbom.cdx.json` and review its diff like any other
file — an SBOM you can't diff is an SBOM nobody reads. Build output
(`build/`, `dist/`, `_skbuild/`) is excluded so a stale artefact can't sneak
a component into the manifest.

**Validation.** Checked with `jsonschema` against the official
[CycloneDX 1.5 JSON schema](https://github.com/CycloneDX/specification/blob/master/schema/bom-1.5.schema.json)
(plus its `spdx` and `jsf-0.82` `$ref` targets), Draft-7: **0 errors**.

```bash
pip install jsonschema
curl -O https://raw.githubusercontent.com/CycloneDX/specification/master/schema/bom-1.5.schema.json
# also fetch spdx.schema.json and jsf-0.82.schema.json — bom-1.5 $refs them
```

What that check does **not** prove:

- **Semantic correctness.** The schema accepts any well-shaped document. It
  cannot tell you the SHA-256 belongs to the file that was actually compiled;
  nothing outside the build re-verifies that link.
- **Python dependency versions.** Library components carry a name and a bare
  `pkg:pypi/<name>` purl with **no version**, because the generated
  `pyproject.toml` declares those deps unpinned — asserting a version here
  would be a fabrication. To get resolved versions you must scan the *built
  image* (`syft`, `trivy`, or `pip freeze` inside the container) and merge
  that BOM with this one. This SBOM is authoritative about native sources; it
  is only indicative about Python.
- **Transitive native dependencies.** System packages installed by `apt-get`
  in the Dockerfile (`libgfortran5`, `build-essential`) are not components
  here. Scan the image for those.
- **The base image's own contents.** Recorded only as the pinned reference in
  `native2py:baseImage`, not expanded into components.

### What a build machine needs with no PyPI access

1. **A wheelhouse.** On a connected machine, mirror the pinned set and carry
   it across:
   ```bash
   pip download --require-hashes -r constraints.txt -d wheelhouse/ \
       --platform manylinux2014_x86_64 --python-version 312 --only-binary=:all:
   ```
   then on the air-gapped side:
   ```bash
   pip install --no-index --find-links=wheelhouse/ --require-hashes -r constraints.txt
   ```
   `constraints.txt` is resolved on macOS/arm64 CPython 3.12 — the *hash lists*
   cover every published file for each pinned version, so Linux wheels are
   included, but a dependency gated behind a marker that only fires on another
   platform would be missing. Verify the wheelhouse on the target platform
   before you seal it.
2. **An internal PyPI mirror or registry proxy** (devpi, Artifactory, Nexus),
   if you'd rather not carry tarballs. Point `PIP_INDEX_URL` at it.
3. **The base image, pre-pulled by digest** into your internal registry:
   ```bash
   docker pull python:3.12-slim@sha256:...
   docker tag  python:3.12-slim@sha256:... registry.internal/python:3.12-slim
   ```
   Re-tagging preserves the digest of the *content*; keep the original digest
   recorded so the provenance survives the retag.
4. **A Debian package mirror** for the `apt-get install` lines
   (`build-essential`, `gfortran`, `cmake`, `libgfortran5`). These are not
   version-pinned at all — see below.
5. **The system toolchain outside pip entirely**: cmake, gcc/gfortran, ninja.
   `constraints.txt` says nothing about these and cannot.

### Steps that currently REQUIRE network — not solved

Being blunt about this is more useful than a checklist that looks complete.

- **`pip install` inside the generated Dockerfile.** Both stages run
  `pip install` / `pip wheel` against the default index. There is no
  `--no-index`, no `--find-links`, no `--require-hashes`, and the service's
  own `pyproject.toml` declares `fastapi`/`uvicorn`/`numpy` **unpinned**. On
  an air-gapped builder you must supply an index yourself today — bake
  `PIP_INDEX_URL`/`PIP_FIND_LINKS` into the build environment, or edit the
  generated Dockerfile. **native2py does not generate an offline-capable
  Dockerfile.** The base image is pinned; what gets installed into it is not.
- **`apt-get update && apt-get install`** in both stages, unpinned, against
  whatever `deb.debian.org` currently serves. So the compiler that builds
  your numerics is *not* reproducible even though the base image is. Point
  `sources.list` at an internal snapshot mirror if you need this closed;
  nothing in the generator does it for you.
- **`f2py -c` shelling out** to the system `gfortran` (and, on newer NumPy, a
  `meson`/`ninja` build). No network of its own, but it depends entirely on a
  toolchain pip never installed and the SBOM never records. Two machines with
  different gfortran versions produce different binaries from identical
  sources — the SBOM will look identical.
- **CMake `find_package`** resolving pybind11/Python from whatever is on the
  build machine. Offline once the packages are present, but the *resolution*
  is environment-dependent and unrecorded.
- **`pip download` / digest refresh / regenerating `constraints.txt`.** All
  connected-side operations by definition. Air-gapped sites need a documented
  sneakernet cadence for them, not a one-off transfer.

The short version: **the tool's own install and the container base are
reproducible today; the generated service's build is not.** Closing that gap
means teaching the generated Dockerfile and `pyproject.toml` about pinned,
hash-verified installs and a snapshot apt mirror. That work has not been
done.
