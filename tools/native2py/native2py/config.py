"""Loader for a service's native2py.yaml (design.md section 9)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_FILENAME = "native2py.yaml"


@dataclass
class ExposeConfig:
    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)

    def is_exposed(self, name: str) -> bool:
        # Empty expose block means "fall back to source annotations only".
        if not self.classes and not self.functions:
            return True
        return name in self.classes or name in self.functions


@dataclass
class ClangConfig:
    """Compiler flags the C++ AST parser needs to read this service's headers.

    A real front end has to be told what a compiler would be told: where the
    other headers live, which macros the build defines, which standard the
    code is written against. Getting these wrong doesn't fail loudly — clang
    recovers from an unknown type by pretending it was `int` — so native2py
    reports every parse error it sees rather than binding the wreckage.
    """

    std: str = "c++17"
    include_paths: list[str] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)


@dataclass
class ServiceConfig:
    name: str
    language: str
    expose: ExposeConfig = field(default_factory=ExposeConfig)
    # "auto" (Clang AST when libclang is importable, else the regex reader),
    # "clang" (require the AST parser), or "regex" (force the fallback).
    parser: str = "auto"
    clang: ClangConfig = field(default_factory=ClangConfig)
    # Shared native libraries under libraries/ that this service links
    # against (design.md section 4). Each entry is a directory name, e.g.
    # "common-cpp" -> libraries/common-cpp/ with its own CMakeLists.txt.
    libraries: list[str] = field(default_factory=list)
    # Directories searched for Fortran INCLUDE files (.INC), relative to the
    # repo root. Legacy F77 keeps COMMON blocks and IMPLICIT statements there.
    include_paths: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, service_dir: Path) -> "ServiceConfig":
        config_path = service_dir / CONFIG_FILENAME
        if not config_path.exists():
            raise FileNotFoundError(
                f"No {CONFIG_FILENAME} found in {service_dir}. "
                "Run `native2py create-service` first."
            )
        data = yaml.safe_load(config_path.read_text()) or {}
        expose_data = data.get("expose") or {}
        clang_data = data.get("clang") or {}
        return cls(
            name=data.get("name", service_dir.name),
            language=data.get("language", "cpp"),
            expose=ExposeConfig(
                classes=list(expose_data.get("classes", [])),
                functions=list(expose_data.get("functions", [])),
            ),
            parser=str(data.get("parser") or "auto"),
            clang=ClangConfig(
                std=str(clang_data.get("std") or "c++17"),
                include_paths=list(clang_data.get("include_paths") or []),
                defines=list(clang_data.get("defines") or []),
                extra_args=list(clang_data.get("extra_args") or []),
            ),
            libraries=list(data.get("libraries") or []),
            include_paths=list(data.get("include_paths") or []),
        )

    def save(self, service_dir: Path) -> None:
        data = {
            "name": self.name,
            "language": self.language,
            "expose": {
                "classes": self.expose.classes,
                "functions": self.expose.functions,
            },
        }
        if self.parser != "auto":
            data["parser"] = self.parser
        clang = {
            key: value
            for key, value in {
                "std": self.clang.std if self.clang.std != "c++17" else None,
                "include_paths": self.clang.include_paths,
                "defines": self.clang.defines,
                "extra_args": self.clang.extra_args,
            }.items()
            if value
        }
        if clang:
            data["clang"] = clang
        if self.libraries:
            data["libraries"] = self.libraries
        if self.include_paths:
            data["include_paths"] = self.include_paths
        (service_dir / CONFIG_FILENAME).write_text(yaml.dump(data, sort_keys=False))
