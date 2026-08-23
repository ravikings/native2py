"""Loader for a service's native2py.yaml (design.md section 9)."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIG_FILENAME = "native2py.yaml"

SUPPORTED_LANGUAGES = ("cpp", "fortran")


class ConfigError(ValueError):
    """native2py.yaml is missing something native2py refuses to guess."""


class ExposeWarning(UserWarning):
    """An empty `expose:` block was read as "bind everything" without an opt-in."""


@dataclass
class ExposeConfig:
    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    # Explicit opt-in to "bind the whole public surface": `expose: all` or
    # `expose: {all: true}` in native2py.yaml. None means "not stated" — which
    # is still permissive for direct construction (the parsers rely on that,
    # falling back to `[[native2py::expose]]` annotations), but ServiceConfig.load
    # warns about it so the intent isn't left implicit in a yaml file.
    all: bool | None = None

    def is_exposed(self, name: str) -> bool:
        if self.all:
            return True
        if not self.classes and not self.functions:
            # Nothing named: permissive unless the user explicitly said `all: false`.
            return self.all is not False
        return name in self.classes or name in self.functions

    @property
    def is_empty(self) -> bool:
        return not self.classes and not self.functions


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
class ApiConfig:
    """How the generated HTTP API authenticates callers.

    Only the *mode* lives here. Keys never do: `native2py.yaml` is committed,
    and a credential in a committed file is a credential in every clone and
    every image layer. The generated middleware reads keys from
    `NATIVE2PY_API_KEYS` at startup, and refuses to start if the mode requires
    them and none are present — failing closed, because a service that
    silently drops authentication is discovered by an attacker rather than by
    whoever deployed it.
    """

    # "none" (open — logged loudly at startup) or "api_key".
    auth: str = "none"


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
    # Authentication for the generated HTTP API. See ApiConfig.
    api: ApiConfig = field(default_factory=ApiConfig)

    @classmethod
    def load(cls, service_dir: Path) -> "ServiceConfig":
        config_path = service_dir / CONFIG_FILENAME
        if not config_path.exists():
            raise FileNotFoundError(
                f"No {CONFIG_FILENAME} found in {service_dir}. "
                "Run `native2py create-service` first."
            )
        data = yaml.safe_load(config_path.read_text()) or {}
        expose = _load_expose(data.get("expose"), config_path)
        clang_data = data.get("clang") or {}
        return cls(
            name=data.get("name", service_dir.name),
            language=_load_language(data.get("language"), service_dir, config_path),
            expose=expose,
            parser=str(data.get("parser") or "auto"),
            clang=ClangConfig(
                std=str(clang_data.get("std") or "c++17"),
                include_paths=list(clang_data.get("include_paths") or []),
                defines=list(clang_data.get("defines") or []),
                extra_args=list(clang_data.get("extra_args") or []),
            ),
            libraries=list(data.get("libraries") or []),
            include_paths=list(data.get("include_paths") or []),
            api=_load_api(data.get("api"), config_path),
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
        if self.expose.all is not None:
            data["expose"]["all"] = self.expose.all
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
        # Only written when it differs from the default, so an existing
        # native2py.yaml does not grow a key that says nothing.
        if self.api.auth != "none":
            data["api"] = {"auth": self.api.auth}
        (service_dir / CONFIG_FILENAME).write_text(yaml.dump(data, sort_keys=False))


def _load_api(api_data, config_path: Path) -> ApiConfig:
    """Read the `api:` block.

    An unrecognised auth mode is an error, not a fallback to `none`. Quietly
    treating `api: {auth: apikey}` (a plausible typo) as "no authentication"
    would produce exactly the silently-open service this setting exists to
    prevent.
    """
    api_data = api_data or {}
    if not isinstance(api_data, dict):
        raise ConfigError(
            f"{config_path}: `api:` must be a block, got {type(api_data).__name__}."
        )
    auth = str(api_data.get("auth") or "none").strip().lower()
    if auth not in ("none", "api_key"):
        raise ConfigError(
            f"{config_path}: `api.auth: {auth}` is not understood. Use "
            "`none` or `api_key`. Refusing to default to `none`, which would "
            "leave the service open on a typo."
        )
    return ApiConfig(auth=auth)


def _load_expose(expose_data, config_path: Path) -> ExposeConfig:
    """Read an `expose:` block, requiring an explicit opt-in for "bind everything".

    `expose: all` (or `expose: {all: true}`) states the intent. An empty block
    keeps the historical permissive behaviour — the C++ parsers depend on it,
    and on `[[native2py::expose]]` annotations in the source — but says so out
    loud rather than binding a whole header on an unstated default.
    """
    if isinstance(expose_data, str):
        if expose_data.strip().lower() != "all":
            raise ConfigError(
                f"{config_path}: `expose: {expose_data}` is not understood. Use "
                "`expose: all`, or a block with `classes:`/`functions:` lists."
            )
        return ExposeConfig(all=True)

    # Checked before the `or {}` below, which cannot tell `False` from an
    # omitted key — `False or {}` is `{}`. That coercion turned an explicit
    # `expose: false` ("bind nothing") into "not stated", which then took the
    # permissive branch and bound the entire native API: the exact opposite of
    # what was written, with only a warning suggesting the user say `expose:
    # all`.
    if isinstance(expose_data, bool):
        return ExposeConfig(all=expose_data)

    expose_data = expose_data or {}
    if not isinstance(expose_data, dict):
        raise ConfigError(
            f"{config_path}: `expose:` must be a mapping or the word `all`, "
            f"not {type(expose_data).__name__}."
        )

    all_value = expose_data.get("all")
    if all_value is not None and not isinstance(all_value, bool):
        raise ConfigError(
            f"{config_path}: `expose.all` must be true or false, got {all_value!r}."
        )

    expose = ExposeConfig(
        classes=list(expose_data.get("classes") or []),
        functions=list(expose_data.get("functions") or []),
        all=all_value,
    )
    if expose.is_empty and expose.all is None:
        warnings.warn(
            f"{config_path}: `expose:` is empty, so every symbol native2py finds "
            "will be bound unless the source carries [[native2py::expose]] "
            "annotations. Say so explicitly with `expose: all`, or list the "
            "classes/functions you want under `expose.classes:` / "
            "`expose.functions:`.",
            ExposeWarning,
            stacklevel=3,
        )
    return expose


def _load_language(value, service_dir: Path, config_path: Path) -> str:
    """Require `language:`, or infer it from the sources — never default to cpp."""
    from .discovery import detect_language  # local: discovery has no config deps

    def discovered() -> set[str]:
        # Only native/ — bindings/generated/ holds generated .cpp for every
        # service, Fortran ones included, and would poison the inference.
        native_dir = service_dir / "native"
        if not native_dir.is_dir():
            return set()
        return {
            lang
            for path in native_dir.rglob("*")
            if path.is_file() and (lang := detect_language(path)) is not None
        }

    if value is None:
        found = discovered()
        if len(found) == 1:
            return found.pop()
        if not found:
            raise ConfigError(
                f"{config_path}: `language:` is missing and no native sources were "
                f"found under {service_dir} to infer it from. Set `language:` to one "
                f"of {', '.join(SUPPORTED_LANGUAGES)}."
            )
        raise ConfigError(
            f"{config_path}: `language:` is missing and the sources under "
            f"{service_dir} are mixed ({', '.join(sorted(found))}). Set `language:` "
            "explicitly."
        )

    language = str(value).strip().lower()
    if language not in SUPPORTED_LANGUAGES:
        raise ConfigError(
            f"{config_path}: unsupported `language: {value}`. Supported languages "
            f"are {', '.join(SUPPORTED_LANGUAGES)}."
        )

    found = discovered()
    if found and language not in found:
        raise ConfigError(
            f"{config_path}: `language: {language}` conflicts with the sources "
            f"under {service_dir}, which are {', '.join(sorted(found))}. Fix "
            "`language:` or remove the sources that do not belong."
        )
    return language
