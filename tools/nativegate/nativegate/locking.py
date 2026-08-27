"""Hash-pinned dependency locks for a generated service.

WHAT THIS CLOSES

The generated Dockerfile installed the service wheel and let pip resolve
`fastapi`, `uvicorn`, `gunicorn`, `numpy` and their transitive dependencies
from PyPI at build time, unpinned. So an image rebuilt a week later shipped
different Python code with no record of the change anywhere — the same hole
the base-image digest and the apt pins close on the other two layers.

`ngate lock <service>` resolves those dependencies once, records every
artifact's SHA-256, and the generated Dockerfile installs from the lock with
`--require-hashes`. After that, a rebuild either produces the same
dependencies or fails; it cannot quietly produce different ones.

WHY IT RESOLVES FOR THE IMAGE, NOT FOR THIS MACHINE

The lock is consumed inside `python:3.12-slim` on Linux. Resolving with the
local interpreter would pin macOS wheels, or wheels for whatever Python the
developer happens to run, and the build would fail — or worse, succeed with a
different set. So the resolution is done with pip's cross-environment flags:
`--python-version 3.12`, `--only-binary=:all:`, and an explicit platform.

BOTH ARCHITECTURES, IN ONE FILE

BASE_IMAGE is a multi-arch index that resolves on amd64 and arm64 alike, so a
lock naming only x86_64 wheels would break every arm64 build. Each dependency
is resolved for both platforms and the hashes are merged — `--require-hashes`
accepts several hashes for one requirement and matches whichever artifact it
actually downloads.

A package that resolves to *different versions* per architecture is refused
rather than silently locked to one of them: that is a genuine split in the
dependency graph, and picking a side for the user would produce an image whose
contents depend on where it was built.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

LOCK_FILENAME = "requirements.lock"

# Matched to docker_gen.BASE_IMAGE. Refresh them together.
LOCK_PYTHON_VERSION = "3.12"

# The manylinux tags the base image can actually run, newest first.
#
# `--platform` is an EXACT tag match, not a floor: asking only for
# manylinux2014 excludes a wheel tagged manylinux_2_28, which is how the first
# version of this locked numpy 2.2.6 while the image itself resolved 2.5.2 —
# numpy 2.5.2 publishes manylinux_2_28 only. A lock that pins something the
# image would not have chosen is worse than no lock: it looks authoritative
# and is quietly wrong.
#
# Measured in the base image: Debian trixie, glibc 2.41 — so manylinux_2_28
# and everything older is satisfied. Listing several lets pip pick whichever a
# project actually publishes.
LOCK_PLATFORMS = (
    "manylinux_2_28_x86_64",
    "manylinux2014_x86_64",
    "manylinux_2_28_aarch64",
    "manylinux2014_aarch64",
)


class LockError(RuntimeError):
    """Resolution failed, or produced something that cannot be locked."""


@dataclass
class LockedPackage:
    name: str
    version: str
    # SHA-256 of every artifact that may satisfy this requirement — one per
    # architecture, usually two, sometimes one for a pure-Python wheel.
    hashes: set[str] = field(default_factory=set)


def _resolve(
    requirements: list[str], platform: list[str], pip: list[str]
) -> dict[str, LockedPackage]:
    """One cross-environment pip resolution, as {normalised name: package}.

    `platform` is every compatible tag for ONE architecture; pip takes the set
    and picks whichever a project publishes.
    """
    # pip requires a target when resolving for a foreign platform; nothing is
    # written, because --dry-run. It still has to be a path pip can create, so
    # take one from the OS rather than hardcoding /tmp — that is not a
    # writable location on Windows.
    with tempfile.TemporaryDirectory(prefix="nativegate-lock-") as dry_run_target:
        command = [
            *pip,
            "install",
            "--dry-run",
            "--quiet",
            "--report",
            "-",
            "--only-binary=:all:",
            "--python-version",
            LOCK_PYTHON_VERSION,
            *[arg for tag in platform for arg in ("--platform", tag)],
            "--target",
            dry_run_target,
            *requirements,
        ]
        result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise LockError(
            f"pip could not resolve {' '.join(requirements)} for "
            f"{', '.join(platform)}:\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LockError(
            f"pip's --report output for {', '.join(platform)} was not JSON. "
            "pip 22.2+ is "
            f"required for hash locking. Got: {result.stdout[:200]!r}"
        ) from exc

    resolved: dict[str, LockedPackage] = {}
    for item in report.get("install", []):
        metadata = item.get("metadata", {})
        name = _normalise(metadata.get("name", ""))
        version = metadata.get("version", "")
        archive = item.get("download_info", {}).get("archive_info", {})
        digest = (archive.get("hashes") or {}).get("sha256") or ""
        if not digest and str(archive.get("hash", "")).startswith("sha256="):
            digest = archive["hash"].split("=", 1)[1]
        if not (name and version and digest):
            raise LockError(
                f"pip reported {name or '<unnamed>'} without a version or a "
                "sha256. A lock is only worth having if every entry has one; "
                "refusing to write a partial lock."
            )
        resolved[name] = LockedPackage(name=name, version=version, hashes={digest})
    return resolved


def _normalise(name: str) -> str:
    """PEP 503 normalisation, so `uvicorn-worker` and `uvicorn_worker` agree."""
    return "".join("-" if c in "-_." else c for c in name.lower())


def resolve_locked(
    requirements: list[str], pip: list[str] | None = None
) -> list[LockedPackage]:
    """Resolve `requirements` for every supported image platform, merged."""
    if not requirements:
        return []
    pip = pip or [sys.executable, "-m", "pip"]

    architectures = [
        [p for p in LOCK_PLATFORMS if p.endswith("x86_64")],
        [p for p in LOCK_PLATFORMS if p.endswith("aarch64")],
    ]
    merged: dict[str, LockedPackage] = {}
    for platform in architectures:
        for name, package in _resolve(requirements, platform, pip).items():
            existing = merged.get(name)
            if existing is None:
                merged[name] = package
                continue
            if existing.version != package.version:
                raise LockError(
                    f"'{name}' resolves to {existing.version} on one image "
                    f"architecture and {package.version} on another. Locking "
                    "one of them would make the image's contents depend on "
                    "where it was built. Pin it explicitly in the generated "
                    "pyproject.toml and re-run the lock."
                )
            existing.hashes |= package.hashes

    return [merged[name] for name in sorted(merged)]


def render_lock(packages: list[LockedPackage], service_name: str) -> str:
    """The `requirements.lock` text, in pip's `--require-hashes` format."""
    lines = [
        "# Generated by nativegate. Do not edit by hand — re-run "
        f"`ngate lock {service_name}`.",
        "#",
        "# Every dependency of this service, pinned by version and SHA-256, so",
        "# `docker build` cannot quietly install different code than it did",
        "# last time. Installed with --require-hashes: a mismatch fails the",
        "# build rather than shipping.",
        "#",
        f"# Resolved for the IMAGE, not for the machine that ran the lock:",
        f"# Python {LOCK_PYTHON_VERSION}, {', '.join(LOCK_PLATFORMS)}.",
        "# Several hashes per entry is normal — one artifact per architecture.",
        "",
    ]
    for package in packages:
        digests = sorted(package.hashes)
        entry = [f"{package.name}=={package.version}"]
        entry += [f"--hash=sha256:{d}" for d in digests]
        lines.append(" \\\n    ".join(entry))
    return "\n".join(lines) + "\n"


def write_lock(
    service_dir: Path, service_name: str, requirements: list[str], pip: list[str] | None = None
) -> Path:
    """Resolve and write `<service_dir>/requirements.lock`. Returns the path."""
    packages = resolve_locked(requirements, pip=pip)
    target = Path(service_dir) / LOCK_FILENAME
    target.write_text(render_lock(packages, service_name))
    return target
