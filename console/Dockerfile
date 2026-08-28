# Console app image. Runs the FastAPI app that lets users upload native code,
# review the inferred contract, build it, and deploy it as a service — all by
# shelling out to `ngate` and `docker`, both of which this image needs.
FROM python:3.11-slim

# Only the `docker` CLI client is needed — this container talks to the
# host's Docker daemon over the socket mount (console/README.md), it never
# runs its own. On Debian trixie (the python:3.11-slim base as of this
# writing) the client binary lives in docker-cli, not docker.io: docker.io
# alone installs an unused dockerd and leaves every `docker` subprocess
# call in deploy.py failing with "No such file or directory: 'docker'".
# build-essential (gcc/g++) + gfortran + cmake are needed to build native
# extension wheels during `ngate build` — matches the build toolchain
# nativegate itself requires for cpp/fortran projects (see
# tools/nativegate/nativegate/generators/docker_gen.py _LANGUAGE_BUILD_DEPS).
# Unlike that generator, versions aren't pinned here: this image is the
# console's own build sandbox, not a shipped/reproducible service artifact.
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker-cli \
    git \
    build-essential \
    gfortran \
    cmake \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install nativegate itself (editable) plus the console's own runtime deps.
COPY tools/nativegate /app/tools/nativegate
#
# `ngate build`/`ngate verify`/`ngate test` (nativegate/driverbuild.py) run
# the C++/Fortran extension build and its generated test suite directly in
# this interpreter, outside pip's build isolation — so everything they shell
# out to (pybind11's CMake package config dir, numpy's f2py, the meson+ninja
# backend f2py drives, scikit-build-core, pytest for the generated tests)
# has to be importable/on PATH here, not just declared in the generated
# project's own [build-system].requires.
#
# Install via nativegate's own `build`/`test` extras (tools/nativegate/
# pyproject.toml), not a hand-picked package list — a hand-picked list is
# exactly what went stale before (missing pybind11/numpy caused every C++/
# Fortran build in this console to fail); the extras are what nativegate
# itself declares it needs, so this can't drift out of sync with that again.
RUN pip install --no-cache-dir -e "/app/tools/nativegate[build,test]" \
    && pip install --no-cache-dir fastapi uvicorn jinja2 python-multipart httpx sse-starlette

COPY console /app/console

# The repo's shared native libraries (e.g. libraries/common-cpp) — needed so
# the console can copy a library into an uploaded project's workspace when
# that project's sources declare a dependency on it (console/routes/pages.py
# _detect_libraries). Without this, `#include "units.hpp"` and similar fail
# even though the toolchain to build them is present.
COPY libraries /app/libraries

EXPOSE 8000

CMD ["uvicorn", "console.app:app", "--host", "0.0.0.0", "--port", "8000"]
