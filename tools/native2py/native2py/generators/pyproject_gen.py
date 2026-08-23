"""Generated pyproject.toml for a service (design.md sections 10, 14)."""

from __future__ import annotations

_BUILD_REQUIRES = {
    "cpp": '["scikit-build-core", "pybind11"]',
    "fortran": '["scikit-build-core", "numpy"]',
}

# gunicorn supervises the worker processes; see docker_gen's CMD and
# docs/deployment-topologies.md. It is a RUNTIME dependency, not a dev one:
# a bare `uvicorn` is a single process, so one segfault in native code takes
# the whole service down with nothing left to restart it.
#
# `uvicorn-worker` rather than `uvicorn.workers.UvicornWorker`: the in-uvicorn
# worker class is deprecated and slated for removal, and shipping a deprecated
# import path inside generated code nobody is allowed to hand-edit is how a
# service breaks on a routine dependency bump.
_DEPENDENCIES = {
    "cpp": '["fastapi", "uvicorn", "gunicorn", "uvicorn-worker"]',
    "fortran": '["fastapi", "uvicorn", "gunicorn", "uvicorn-worker", "numpy"]',
}


def generate_pyproject(service_name: str, language: str) -> str:
    build_requires = _BUILD_REQUIRES.get(language, _BUILD_REQUIRES["cpp"])
    dependencies = _DEPENDENCIES.get(language, _DEPENDENCIES["cpp"])

    return f"""[build-system]
requires = {build_requires}
build-backend = "scikit_build_core.build"

[project]
name = "{service_name}"
version = "1.0.0"
requires-python = ">=3.10"
dependencies = {dependencies}

[tool.scikit-build]
wheel.packages = ["python/{service_name}"]
cmake.source-dir = "."
"""
