"""Generated pyproject.toml for a service (design.md sections 10, 14)."""

from __future__ import annotations

_BUILD_REQUIRES = {
    "cpp": '["scikit-build-core", "pybind11"]',
    "fortran": '["scikit-build-core", "numpy"]',
}

_DEPENDENCIES = {
    "cpp": '["fastapi", "uvicorn"]',
    "fortran": '["fastapi", "uvicorn", "numpy"]',
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
