"""Multi-stage Dockerfile generator (design.md section 11).

Also emits the CycloneDX SBOM for a generated service — see `generate_sbom`.
Both live here because both are about what actually ends up in the shipped
image, and both have to be reproducible to be worth anything.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Iterable, Sequence

# --- Base image pin -------------------------------------------------------
#
# WHY A DIGEST AND NOT A TAG
#   `python:3.12-slim` is a MUTABLE tag. Docker Hub re-points it at a new image
#   on every patch/security rebuild, so two builds a month apart silently get
#   different interpreters, different libc, different libgfortran — with no
#   record of the change anywhere in the build. For a product whose entire
#   claim is "the re-hosted numerics return the same answers", an unpinned base
#   is a hole straight through the middle of that claim. A digest is immutable:
#   the same digest is byte-identical forever, or the pull fails.
#
#   The digest below is a multi-arch OCI image index, so it still resolves
#   correctly on amd64 and arm64 — pinning does not cost you cross-platform
#   builds.
#
# HOW TO REFRESH IT (deliberately — this is a reviewed change, not a chore)
#   With Docker available:
#       docker pull python:3.12-slim
#       docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
#   Without Docker, straight from the registry HTTP API:
#       TOKEN=$(curl -s "https://auth.docker.io/token\
#?service=registry.docker.io&scope=repository:library/python:pull" \
#         | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
#       curl -sI -H "Authorization: Bearer $TOKEN" \
#         -H "Accept: application/vnd.oci.image.index.v1+json" \
#         https://registry-1.docker.io/v2/library/python/manifests/3.12-slim \
#         | grep -i docker-content-digest
#   Then update BASE_IMAGE_DIGEST, note the date, rebuild, and re-run the
#   generated service's golden-value tests before shipping the new base.
#
# Resolved 2026-08-22 from registry-1.docker.io (OCI image index) for
# python:3.12-slim.
BASE_IMAGE = "python:3.12-slim"
BASE_IMAGE_DIGEST = "sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"
BASE_IMAGE_REF = f"{BASE_IMAGE}@{BASE_IMAGE_DIGEST}"

# --- apt pins -------------------------------------------------------------
#
# WHY THE VERSIONS ARE SPELLED OUT
#   `apt-get install gfortran` resolves against the LIVE Debian archive, so the
#   same Dockerfile, from the same digest-pinned base, installs a different
#   compiler the week after Debian publishes one. The image digest pins the
#   filesystem you start from; it does nothing about what you then download
#   into it. For a tool whose whole claim is "the re-hosted numerics return the
#   same answers", the compiler is the single most consequential input, and it
#   was the one thing nothing pinned.
#
#   These are the candidates in the base image named by BASE_IMAGE_DIGEST
#   (Debian trixie), read from the image itself rather than from a changelog.
#
# WHEN A PIN GOES STALE
#   Debian drops superseded versions from the live mirror, so an old pin
#   eventually fails with "Version '...' for '...' was not found". That is the
#   intended behaviour: a loud failure you resolve deliberately, rather than a
#   silent toolchain change nobody reviewed. Refresh with:
#
#       native2py toolchain --refresh
#
#   which reads the candidates out of the pinned base image and prints the
#   block to paste here. Update the base digest and these pins together, then
#   re-run the generated service's golden-value tests before shipping.
#
# WHY THE CONCRETE COMPILER PACKAGES ARE NAMED, NOT JUST `gfortran`
#   Pinning `gfortran=4:14.2.0-1` pins a METAPACKAGE. It exists to provide the
#   /usr/bin/gfortran symlink and to depend on the real compiler; the compiler
#   itself is `gfortran-14`, and apt only honours `=version` for packages named
#   on the command line. Measured in the built image:
#
#       gfortran       4:14.2.0-1     <- what the first version of this pinned
#       gfortran-14    14.2.0-19      <- the compiler that actually runs
#
#   Debian can ship gfortran-14 14.2.0-20 with the metapackage unchanged, and a
#   build that looked pinned would quietly change compilers. So the concrete
#   packages are named too. That hard-codes the GCC major version, which moves
#   with the Debian release — refresh it together with the base image digest.
#
# Read 2026-08-23 from python:3.12-slim@sha256:2c941e86... (Debian trixie).
APT_SUITE = "trixie"
APT_PINS = {
    "build-essential": "12.12",
    "cmake": "3.31.6-2",
    # Metapackage: provides the unsuffixed /usr/bin/gfortran.
    "gfortran": "4:14.2.0-1",
    # The compilers that actually run, and the runtimes they emit code against.
    "gcc-14": "14.2.0-19",
    "g++-14": "14.2.0-19",
    "gfortran-14": "14.2.0-19",
    "libgfortran5": "14.2.0-19",
    "libstdc++6": "14.2.0-19",
}


def _pinned(package: str) -> str:
    """`name=version` when the version is known, bare name otherwise.

    An unpinned name is not silently tolerated in the languages native2py
    generates for — every package in the tables below has an entry — but a
    caller passing `extra_build_deps` may name something native2py has never
    seen, and refusing to emit a Dockerfile for it would be worse than
    installing it unpinned and saying so in the SBOM.
    """
    version = APT_PINS.get(package)
    return f"{package}={version}" if version else package


# Both the metapackage and the concrete compiler are listed: the first gives
# the unsuffixed command every build system looks for, the second is what apt
# can actually pin. See APT_PINS.
_LANGUAGE_BUILD_DEPS = {
    "cpp": ["build-essential", "gcc-14", "g++-14", "cmake"],
    "fortran": [
        "build-essential", "gcc-14", "g++-14",
        "gfortran", "gfortran-14", "cmake",
    ],
}

_LANGUAGE_RUNTIME_DEPS = {
    "cpp": [],
    "fortran": ["libgfortran5"],
}

# Extra pip-installed build tools, on top of `build`.
#
# WHY MESON IS NOT OPTIONAL FOR FORTRAN
#   The generated CMake runs `python -m numpy.f2py -c`. distutils was removed
#   in Python 3.12, so numpy's f2py falls back to its meson backend and shells
#   out to a `meson` executable. BASE_IMAGE is python:3.12-slim. Without this,
#   every generated Fortran image fails to build with
#   `FileNotFoundError: [Errno 2] No such file or directory: 'meson'` — which
#   is exactly how CI found it, on the first run that ever executed.
#
#   Installed with pip rather than apt on purpose: Debian stable's `meson` is
#   older than what f2py's backend expects, and pinning the toolchain matters
#   more here than saving a layer. ninja comes along because meson drives it.
_LANGUAGE_PIP_BUILD_TOOLS = {
    "fortran": ["meson", "ninja"],
}

# PEP 517 build requirements, installed explicitly so the wheel can be built
# with --no-build-isolation. Mirrors pyproject_gen._BUILD_REQUIRES; kept as a
# local copy for the same reason the runtime list is, so drift shows up as a
# diff in review.
#
# WHY --no-build-isolation, WHICH IS THE OPPOSITE OF THE USUAL ADVICE
#   Two reasons, and the build failed on the first one.
#
#   1. It did not work. `pip wheel .` runs the backend in an isolated
#      environment, and f2py's meson backend then died with
#      `ModuleNotFoundError: No module named 'mesonbuild'` — the `meson`
#      executable was on PATH from the layer above, but the module it needs
#      was not importable from inside the isolated build.
#
#   2. Isolation is itself a reproducibility hole. pip *downloads* the build
#      requirements into that environment at build time, unpinned — so the
#      scikit-build-core and numpy that compile the extension could differ
#      between two builds of the same pinned Dockerfile, which is exactly what
#      this whole exercise is closing. Installing them explicitly makes them
#      visible, and lockable.
_LANGUAGE_BUILD_REQUIRES = {
    "cpp": ["scikit-build-core", "pybind11"],
    "fortran": ["scikit-build-core", "numpy"],
}

# --- Crash containment (ROADMAP 2.2) --------------------------------------
#
# Legacy numerical code outside its validated envelope segfaults, calls
# Fortran STOP, or hangs. None of those raise a Python exception; the process
# simply dies or stops answering, and no `except` clause anywhere can see it.
# A bare `uvicorn` is ONE process, so any of the three took the whole service
# down and left nothing running to restart it.
#
# THE TOPOLOGY THAT SHIPS: a supervised worker pool.
#   gunicorn's master process supervises N uvicorn workers. A worker that
#   segfaults or STOPs is reaped and respawned; the master keeps the listening
#   socket, so the service never stops accepting. `--timeout 60` is the part
#   that covers HANGS: gunicorn kills a worker that has not completed its
#   request in time, which no amount of exception handling can do.
#   `--max-requests` recycles workers periodically, bounding the damage from
#   native code that leaks rather than crashes.
#
# WHAT IT DOES NOT DO: per-request isolation. Requests sharing the crashed
#   worker die with it. Containing a crash to only the request that caused it
#   needs process-per-call, which puts marshalling cost on every request; that
#   is a separate, opt-in tier and is not implemented. Anything taking
#   untrusted input needs it — see docs/production-readiness.md.
#
# WHY THE FORTRAN DEFAULT IS ONE WORKER, AND IT IS NOT A THROUGHPUT OVERSIGHT
#   COMMON blocks are per-PROCESS storage. With one worker, a client's
#   `/pvt_set_fluid` and its follow-up `/solution_gor` at least reach the same
#   COMMON. With several, they can land on different processes and the compute
#   reads a COMMON that was never configured — turning an interleaving race
#   into a straightforwardly wrong answer. Raising WEB_CONCURRENCY is correct
#   ONLY for routines that carry no state across calls. gunicorn reads
#   WEB_CONCURRENCY itself, so this is an env change, not an image rebuild.
_LANGUAGE_DEFAULT_WORKERS = {
    # Per-request instances, no process-global state the parser can see.
    "cpp": 2,
    # See above: >1 changes the semantics of stateful routines.
    "fortran": 1,
}


def generate_dockerfile(
    service_name: str,
    language: str,
    package_name: str,
    extra_build_deps: list[str] | None = None,
    libraries: list[str] | None = None,
    locked: bool = False,
    needs_numpy: bool = False,
) -> str:
    """The service's multi-stage Dockerfile.

    `locked` is set when `requirements.lock` exists beside the service (see
    `native2py lock`). Without it the runtime `pip install` resolves the
    service's dependencies from PyPI at build time, so an image rebuilt a week
    later ships different Python code with no record of the change. With it,
    the dependencies are installed by hash and the wheel goes in `--no-deps`.
    """
    build_deps = _LANGUAGE_BUILD_DEPS.get(language, ["build-essential", "cmake"]) + (extra_build_deps or [])
    runtime_deps = _LANGUAGE_RUNTIME_DEPS.get(language, [])
    libraries = libraries or []

    runtime_install = ""
    if runtime_deps:
        deps = " \\\n        ".join(_pinned(d) for d in runtime_deps)
        runtime_install = (
            "\nRUN apt-get update && \\\n"
            f"    apt-get install -y --no-install-recommends {deps} && \\\n"
            "    rm -rf /var/lib/apt/lists/*\n"
        )

    build_deps_str = " \\\n        ".join(_pinned(d) for d in build_deps)
    pip_build_tools = " ".join(
        ["build"]
        + _LANGUAGE_BUILD_REQUIRES.get(language, _LANGUAGE_BUILD_REQUIRES["cpp"])
        + _LANGUAGE_PIP_BUILD_TOOLS.get(language, [])
        + (["numpy"] if language == "cpp" and needs_numpy else [])
    )

    # The lock lives beside the service, so the path it is COPYed from depends
    # on which directory the build context is rooted at.
    lock_source = (
        f"services/{service_name}/requirements.lock" if libraries else "requirements.lock"
    )
    lock_block = (
        f"""
COPY {lock_source} ./requirements.lock

# --require-hashes: every dependency pinned by version AND sha256, so a
# rebuild either installs exactly this or fails. Generated by `native2py lock`.
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
"""
        if locked
        else ""
    )
    # Without the lock the wheel's dependencies must still be resolved, so
    # --no-deps is only correct once they are already installed from it.
    no_deps = " --no-deps" if locked else ""

    workers = _LANGUAGE_DEFAULT_WORKERS.get(language, 1)
    worker_note = (
        "# One worker: COMMON blocks are per-process, so a configure call and\n"
        "# the compute that reads it must reach the SAME process. Raise this\n"
        "# only for routines that keep no state between calls.\n"
        if language == "fortran"
        else "# Per-request instances and no process-global state, so workers scale freely.\n"
    )
    crash_containment = (
        "# Crash containment: gunicorn supervises the workers, so a segfault or\n"
        "# Fortran STOP in native code kills one worker instead of the service,\n"
        "# and --timeout reaps one that hangs. See docker_gen for the full note.\n"
        f"{worker_note}"
        f"ENV WEB_CONCURRENCY={workers}\n"
    )

    # A service that links shared libraries needs them inside the build
    # context, and they live outside its own directory — so the context has
    # to be the repo root and the paths become repo-relative. Without
    # libraries we keep the simpler service-dir context.
    if libraries:
        context_note = (
            f"# NOTE: build from the REPO ROOT, not services/{service_name}/:\n"
            f"#   docker build -f services/{service_name}/Dockerfile -t {service_name}:latest .\n"
            f"# The shared libraries under libraries/ must be in the build context.\n"
        )
        copy_lines = "\n".join(
            f"COPY libraries/{lib} ./libraries/{lib}" for lib in libraries
        )
        build_stage = f"""WORKDIR /build

{copy_lines}
COPY services/{service_name} ./services/{service_name}

WORKDIR /build/services/{service_name}

# A wheel is a zip, and a zip records a modification time per entry, so two
# builds of identical sources produced wheels with different SHA-256s. The
# compiled extension inside was byte-identical both times — measured — but the
# artifact you actually ship and sign was not. SOURCE_DATE_EPOCH is the
# reproducible-builds convention every Python packaging tool honours for
# exactly this. The value is arbitrary and fixed; 1980-01-01 is the earliest a
# zip can represent.
ENV SOURCE_DATE_EPOCH=315532800

RUN pip install --no-cache-dir {pip_build_tools}

# --no-deps, and it is load-bearing: without it `pip wheel` downloads every
# DEPENDENCY into /dist as well, and the `pip install /dist/*.whl` below then
# installs those instead of the hash-locked ones — silently defeating the lock.
# Caught by building the image and reading which numpy actually landed.
RUN pip wheel . -w /dist --no-build-isolation --no-deps
"""
    else:
        context_note = (
            f"# Build from services/{service_name}/:\n"
            f"#   docker build -t {service_name}:latest .\n"
        )
        build_stage = f"""WORKDIR /build

COPY . .

# A wheel is a zip, and a zip records a modification time per entry, so two
# builds of identical sources produced wheels with different SHA-256s. The
# compiled extension inside was byte-identical both times — measured — but the
# artifact you actually ship and sign was not. SOURCE_DATE_EPOCH is the
# reproducible-builds convention every Python packaging tool honours for
# exactly this. The value is arbitrary and fixed; 1980-01-01 is the earliest a
# zip can represent.
ENV SOURCE_DATE_EPOCH=315532800

RUN pip install --no-cache-dir {pip_build_tools}

# --no-deps, and it is load-bearing: without it `pip wheel` downloads every
# DEPENDENCY into /dist as well, and the `pip install /dist/*.whl` below then
# installs those instead of the hash-locked ones — silently defeating the lock.
RUN pip wheel . -w /dist --no-build-isolation --no-deps
"""

    return f"""# Generated by native2py. Do not edit by hand — re-run `native2py docker {service_name}`.
{context_note}# Base image is DIGEST-PINNED, not tag-pinned — see docker_gen.BASE_IMAGE_DIGEST
# for why, and for how to refresh it.
FROM {BASE_IMAGE_REF} AS builder

RUN apt-get update && \\
    apt-get install -y \\
        {build_deps_str} && \\
    rm -rf /var/lib/apt/lists/*

{build_stage}


FROM {BASE_IMAGE_REF}
{runtime_install}
WORKDIR /app

COPY --from=builder /dist /dist
{lock_block}
RUN pip install --no-cache-dir{no_deps} /dist/*.whl

# design.md section 21: run as non-root. UID is explicit so a Kubernetes
# `runAsUser: 1000` and `runAsNonRoot: true` match the image rather than
# fighting it.
RUN useradd --create-home --uid 1000 appuser
USER appuser

# The generated Kubernetes manifests set readOnlyRootFilesystem: true. Python
# writing .pyc files next to the source would fail against that, so the
# interpreter is told not to. PYTHONUNBUFFERED so logs reach the collector as
# they happen rather than when a buffer fills — a service that crashes with
# its last log lines still buffered is one you cannot debug.
ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/docs')" || exit 1

{crash_containment}
CMD ["gunicorn", "{package_name}.service:app", \\
     "--worker-class", "uvicorn_worker.UvicornWorker", \\
     "--bind", "0.0.0.0:8000", \\
     "--timeout", "60", \\
     "--graceful-timeout", "30", \\
     "--max-requests", "1000", \\
     "--max-requests-jitter", "100"]
"""


# --- CycloneDX SBOM -------------------------------------------------------

SBOM_FILENAME = "sbom.cdx.json"

# CycloneDX 1.5 is the last spec revision with broad tool support (Grype,
# Trivy, Dependency-Track, the GitHub dependency graph). Bump deliberately.
CYCLONEDX_SPEC_VERSION = "1.5"

# Namespace for deriving the BOM serial number from the BOM's own content.
# CycloneDX wants a `serialNumber`, but a random UUID would make two SBOMs of
# identical inputs differ, which destroys the point of diffing them in review.
# A UUIDv5 over the content is stable AND still unique per distinct BOM.
_SERIAL_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # RFC 4122 URL ns

# Runtime Python dependencies of a generated service, mirroring
# pyproject_gen's [project.dependencies]. Kept as a local copy on purpose:
# the SBOM should record what the generated pyproject actually declares, and
# a silent drift between the two is something a reviewer should see as a diff.
_LANGUAGE_RUNTIME_PYTHON_DEPS = {
    "cpp": ["fastapi", "uvicorn", "gunicorn", "uvicorn-worker"],
    "fortran": ["fastapi", "uvicorn", "gunicorn", "uvicorn-worker", "numpy"],
}

# Files the SBOM treats as "native sources compiled in". A wheel wrapping a
# 1988 Fortran deck is only auditable if the deck itself is in the manifest.
_NATIVE_SOURCE_SUFFIXES = {
    ".f", ".for", ".f77", ".f90", ".f95", ".f03", ".f08", ".inc",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_native_sources(root: Path) -> list[Path]:
    """Every native source under `root`, sorted, build output excluded.

    Sorted so the SBOM component order does not depend on filesystem
    iteration order — a directory listing is not stable across machines.
    """
    root = Path(root)
    skip = {"build", "dist", "_skbuild", ".git", "__pycache__", ".venv"}
    found = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in _NATIVE_SOURCE_SUFFIXES
        and not any(part in skip for part in path.relative_to(root).parts)
    ]
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def generate_sbom(
    service_name: str,
    language: str,
    package_name: str,
    *,
    version: str = "1.0.0",
    native_sources: Iterable[Path] | None = None,
    source_root: Path | None = None,
    libraries: Sequence[str] | None = None,
    python_dependencies: Sequence[str] | None = None,
) -> str:
    """A deterministic CycloneDX 1.5 SBOM (JSON) for one generated service.

    Records BOTH halves of what ships:

      * the Python dependency surface (fastapi, uvicorn, numpy), and
      * every native source compiled into the extension, each with its
        SHA-256 and its path relative to the service directory.

    The second half is the reason this exists. A wheel is an opaque binary;
    without the source digests there is no way to answer "which revision of
    the Fortran deck is inside this image?" during an audit.

    Determinism: no timestamps, no random serial number, sorted components.
    Two runs over unchanged inputs produce byte-identical output, so the SBOM
    can be committed and diffed like any other reviewed artifact.
    """
    source_root = Path(source_root) if source_root is not None else None
    if native_sources is None:
        native_sources = find_native_sources(source_root) if source_root else []
    if python_dependencies is None:
        python_dependencies = _LANGUAGE_RUNTIME_PYTHON_DEPS.get(
            language, _LANGUAGE_RUNTIME_PYTHON_DEPS["cpp"]
        )

    components: list[dict] = []

    # The build toolchain, which nothing recorded until now. The base image
    # digest pinned the filesystem you start from and said nothing about what
    # gets installed into it, so two images built a month apart could carry
    # different compilers with byte-identical SBOMs — precisely the gap the
    # golden values exist to close, left open in the artifact that is supposed
    # to document it.
    #
    # Deterministic because APT_PINS is static. Scope distinguishes what ships
    # from what only built it: the compilers stay in the builder stage
    # ("excluded"), the runtime libraries are in the final image ("required").
    build_only = set(_LANGUAGE_BUILD_DEPS.get(language, [])) - set(
        _LANGUAGE_RUNTIME_DEPS.get(language, [])
    )
    apt_packages = sorted(
        build_only | set(_LANGUAGE_RUNTIME_DEPS.get(language, []))
    )
    for name in apt_packages:
        version = APT_PINS.get(name)
        components.append(
            {
                "type": "library",
                "bom-ref": f"deb/{name}",
                "name": name,
                **({"version": version} if version else {}),
                "purl": (
                    f"pkg:deb/debian/{name}@{version}?distro={APT_SUITE}"
                    if version
                    else f"pkg:deb/debian/{name}?distro={APT_SUITE}"
                ),
                "scope": "excluded" if name in build_only else "required",
            }
        )

    for name in sorted(python_dependencies):
        components.append(
            {
                "type": "library",
                "bom-ref": f"pypi/{name}",
                "name": name,
                # No version: the generated pyproject.toml declares these
                # unpinned, so claiming a version here would be a lie. Resolve
                # the built image with `pip freeze`/syft to get versions, and
                # see docs/deployment-topologies.md.
                "purl": f"pkg:pypi/{name}",
                "scope": "required",
            }
        )

    for path in native_sources:
        path = Path(path)
        relative = (
            path.relative_to(source_root).as_posix()
            if source_root is not None and path.is_absolute()
            else Path(path).as_posix()
        )
        components.append(
            {
                "type": "file",
                "bom-ref": f"native-source/{relative}",
                "name": relative,
                "hashes": [{"alg": "SHA-256", "content": _sha256_file(path)}],
            }
        )

    for library in sorted(libraries or []):
        components.append(
            {
                "type": "library",
                "bom-ref": f"native-library/{library}",
                "name": library,
                "description": (
                    "Shared native library from libraries/ linked statically "
                    "into this service's extension module."
                ),
            }
        )

    document = {
        "bomFormat": "CycloneDX",
        "specVersion": CYCLONEDX_SPEC_VERSION,
        "version": 1,
        "metadata": {
            # No "timestamp" key: it would change on every run and make the
            # SBOM undiffable. CycloneDX makes it optional for exactly this.
            "tools": [{"vendor": "native2py", "name": "native2py"}],
            "component": {
                "type": "application",
                "bom-ref": f"pypi/{service_name}@{version}",
                "name": service_name,
                "version": version,
                "purl": f"pkg:pypi/{service_name}@{version}",
                "properties": [
                    {"name": "native2py:language", "value": language},
                    {"name": "native2py:package", "value": package_name},
                    {"name": "native2py:baseImage", "value": BASE_IMAGE_REF},
                ],
            },
        },
        "components": components,
    }

    # Serial number derived from the content above, so it is stable for
    # identical inputs and changes when anything in the BOM changes.
    payload = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
    document["serialNumber"] = "urn:uuid:" + str(
        uuid.uuid5(_SERIAL_NAMESPACE, payload)
    )

    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def write_sbom(service_dir: Path, service_name: str, language: str, package_name: str, **kwargs) -> Path:
    """Write `sbom.cdx.json` into the service directory and return its path."""
    service_dir = Path(service_dir)
    kwargs.setdefault("source_root", service_dir)
    path = service_dir / SBOM_FILENAME
    path.write_text(generate_sbom(service_name, language, package_name, **kwargs))
    return path
