from native2py.generators import docker_gen


def test_dockerfile_runs_as_non_root():
    dockerfile = docker_gen.generate_dockerfile("calculator", "cpp", "calculator")

    assert "USER appuser" in dockerfile
    # USER must come after the pip install (root needs to own the install)
    # and before CMD, otherwise the app runs as root anyway.
    install_idx = dockerfile.index("pip install --no-cache-dir /dist")
    user_idx = dockerfile.index("USER appuser")
    cmd_idx = dockerfile.index("CMD [")
    assert install_idx < user_idx < cmd_idx


def test_dockerfile_has_healthcheck():
    dockerfile = docker_gen.generate_dockerfile("calculator", "cpp", "calculator")

    assert "HEALTHCHECK" in dockerfile


def test_dockerfile_fortran_runtime_deps():
    dockerfile = docker_gen.generate_dockerfile("reservoir", "fortran", "physics")

    assert "libgfortran5" in dockerfile
    assert "gfortran" in dockerfile  # build stage


import json
import re

import pytest


# --- Base image is digest-pinned -----------------------------------------
#
# Asserted on the SHAPE (`python:3.12-slim@sha256:<64 hex>`), never on a
# literal digest: the digest is meant to be refreshed, and a test that hard
# codes it would fail on every legitimate refresh instead of catching the
# thing that actually matters — someone dropping back to a mutable tag.

_DIGEST_PINNED = re.compile(r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}", re.M)


@pytest.mark.parametrize("libraries", [None, ["common-cpp"]])
def test_dockerfile_base_image_is_digest_pinned(libraries):
    dockerfile = docker_gen.generate_dockerfile(
        "calculator", "cpp", "calculator", libraries=libraries
    )

    from_lines = [
        line for line in dockerfile.splitlines() if line.startswith("FROM ")
    ]
    assert from_lines, "no FROM line in the generated Dockerfile"
    # Both the builder and the runtime stage must be pinned; a pinned builder
    # with an unpinned runtime still ships a nondeterministic image.
    assert len(_DIGEST_PINNED.findall(dockerfile)) == len(from_lines) == 2
    for line in from_lines:
        assert "@sha256:" in line, f"mutable tag in {line!r}"


def test_dockerfile_never_uses_the_mutable_tag_alone():
    dockerfile = docker_gen.generate_dockerfile("reservoir", "fortran", "physics")

    assert "FROM python:3.12-slim\n" not in dockerfile
    assert "FROM python:3.12-slim AS builder" not in dockerfile


def test_base_image_constant_shape():
    assert docker_gen.BASE_IMAGE_DIGEST.startswith("sha256:")
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", docker_gen.BASE_IMAGE_DIGEST)
    assert docker_gen.BASE_IMAGE_REF == (
        f"{docker_gen.BASE_IMAGE}@{docker_gen.BASE_IMAGE_DIGEST}"
    )


# --- SBOM -----------------------------------------------------------------


def _service_tree(tmp_path):
    service_dir = tmp_path / "reservoir"
    (service_dir / "src").mkdir(parents=True)
    (service_dir / "src" / "flow.f90").write_text("      SUBROUTINE FLOW\n      END\n")
    (service_dir / "src" / "decl.inc").write_text("      COMMON /BLK/ X\n")
    # Build output must not leak into the SBOM.
    (service_dir / "build").mkdir()
    (service_dir / "build" / "junk.c").write_text("int main(void){return 0;}\n")
    return service_dir


def test_sbom_is_valid_json_with_cyclonedx_top_level_fields(tmp_path):
    service_dir = _service_tree(tmp_path)

    document = json.loads(
        docker_gen.generate_sbom(
            "reservoir", "fortran", "physics", source_root=service_dir
        )
    )

    assert document["bomFormat"] == "CycloneDX"
    assert document["specVersion"] == docker_gen.CYCLONEDX_SPEC_VERSION
    assert isinstance(document["version"], int)
    assert document["serialNumber"].startswith("urn:uuid:")
    assert document["metadata"]["component"]["name"] == "reservoir"
    assert isinstance(document["components"], list)


def test_sbom_records_native_sources_with_sha256(tmp_path):
    service_dir = _service_tree(tmp_path)

    document = json.loads(
        docker_gen.generate_sbom(
            "reservoir", "fortran", "physics", source_root=service_dir
        )
    )
    files = {
        component["name"]: component
        for component in document["components"]
        if component["type"] == "file"
    }

    # The whole point: the Fortran deck is traceable from the image.
    assert set(files) == {"src/decl.inc", "src/flow.f90"}
    for component in files.values():
        hashes = component["hashes"]
        assert hashes[0]["alg"] == "SHA-256"
        assert re.fullmatch(r"[0-9a-f]{64}", hashes[0]["content"])

    # Build output is excluded, not silently hashed in.
    assert "build/junk.c" not in files


def test_sbom_records_the_pinned_base_image(tmp_path):
    document = json.loads(
        docker_gen.generate_sbom("demo", "cpp", "demo", source_root=tmp_path)
    )
    properties = {
        item["name"]: item["value"]
        for item in document["metadata"]["component"]["properties"]
    }

    assert properties["native2py:baseImage"] == docker_gen.BASE_IMAGE_REF
    assert "@sha256:" in properties["native2py:baseImage"]


def test_sbom_is_byte_identical_across_runs(tmp_path):
    service_dir = _service_tree(tmp_path)

    first = docker_gen.generate_sbom(
        "reservoir", "fortran", "physics", source_root=service_dir
    )
    second = docker_gen.generate_sbom(
        "reservoir", "fortran", "physics", source_root=service_dir
    )

    # No timestamp, no random serial: an SBOM you cannot diff is an SBOM you
    # cannot review.
    assert first == second
    assert "timestamp" not in json.loads(first)["metadata"]


def test_sbom_serial_number_changes_when_a_source_changes(tmp_path):
    service_dir = _service_tree(tmp_path)
    before = json.loads(
        docker_gen.generate_sbom("reservoir", "fortran", "physics", source_root=service_dir)
    )

    (service_dir / "src" / "flow.f90").write_text("      SUBROUTINE FLOW2\n      END\n")
    after = json.loads(
        docker_gen.generate_sbom("reservoir", "fortran", "physics", source_root=service_dir)
    )

    assert before["serialNumber"] != after["serialNumber"]


def test_write_sbom_writes_deterministic_file(tmp_path):
    service_dir = _service_tree(tmp_path)

    path = docker_gen.write_sbom(service_dir, "reservoir", "fortran", "physics")
    first = path.read_bytes()
    docker_gen.write_sbom(service_dir, "reservoir", "fortran", "physics")

    assert path.name == docker_gen.SBOM_FILENAME
    assert path.read_bytes() == first
    assert json.loads(first)["bomFormat"] == "CycloneDX"


# --- the meson backend (found by CI's first-ever run) ---------------------
#
# The generated CMake runs `python -m numpy.f2py -c`. distutils was removed in
# Python 3.12, so numpy's f2py switches to its meson backend and shells out to
# a `meson` executable — and BASE_IMAGE is python:3.12-slim. Before this, every
# generated Fortran image failed with
# `FileNotFoundError: [Errno 2] No such file or directory: 'meson'`.
#
# Reproduced directly, outside the container: on a 3.12 interpreter with numpy
# 2.5 and no meson on PATH, `f2py -c` exits 1 and produces no .so; with meson
# and ninja pip-installed it exits 0 and the extension imports and computes.


@pytest.mark.parametrize("libraries", [None, ["petro"]])
def test_fortran_builder_installs_meson_and_ninja(libraries):
    # Both build-context shapes: the library-linking branch and the plain one
    # are separate f-strings, and the first fix patched only one of them.
    dockerfile = docker_gen.generate_dockerfile(
        "petro_api", "fortran", "petro_api", libraries=libraries
    )

    assert "pip install --no-cache-dir build meson ninja" in dockerfile
    # Must be installed BEFORE the wheel build that invokes f2py, not after.
    assert dockerfile.index("meson ninja") < dockerfile.index("pip wheel .")


def test_cpp_builder_does_not_pull_in_meson():
    # pybind11/CMake needs nothing from f2py. Installing meson everywhere would
    # be cargo-culting a Fortran-specific fix into every image.
    dockerfile = docker_gen.generate_dockerfile("calculator", "cpp", "calculator")

    assert "pip install --no-cache-dir build\n" in dockerfile
    assert "meson" not in dockerfile


# --- crash containment (ROADMAP 2.2) -------------------------------------
#
# Segfaults, Fortran STOP and hangs raise no Python exception — the process
# dies or stops answering, and no `except` can see it. A bare `uvicorn` is one
# process, so any of the three took the whole service down.
#
# Verified against a real gunicorn, not just asserted on the text: a genuine
# SIGSEGV (ctypes.string_at(0)) killed a worker, gunicorn logged
# "Worker (pid:...) was sent SIGSEGV!", booted a replacement, and the service
# kept answering. A 600s hang under `--timeout 5` likewise left the service up.


@pytest.mark.parametrize("language", ["fortran", "cpp"])
def test_the_service_runs_under_a_supervisor_not_bare_uvicorn(language):
    dockerfile = docker_gen.generate_dockerfile("svc", language, "svc")

    assert '"gunicorn"' in dockerfile
    # A bare `uvicorn` entrypoint is exactly the single-process shape this
    # replaces; if it comes back, crash containment is silently gone.
    assert 'CMD ["uvicorn"' not in dockerfile
    # Deprecated in uvicorn and slated for removal — generated code must not
    # depend on it.
    assert "uvicorn.workers" not in dockerfile
    assert "uvicorn_worker.UvicornWorker" in dockerfile


@pytest.mark.parametrize("language", ["fortran", "cpp"])
def test_a_hung_request_is_reaped(language):
    # The ONLY mechanism here that addresses hangs. Exception handling cannot:
    # a worker stuck inside native code never returns to Python.
    dockerfile = docker_gen.generate_dockerfile("svc", language, "svc")

    assert '"--timeout", "60"' in dockerfile
    assert '"--graceful-timeout", "30"' in dockerfile
    # Bounds damage from native code that leaks instead of crashing.
    assert '"--max-requests", "1000"' in dockerfile


def test_fortran_defaults_to_one_worker_because_common_is_per_process():
    # Not a throughput oversight. With several workers a client's configure
    # call and the compute that reads it can land on DIFFERENT processes, so
    # the compute reads a COMMON block that was never configured — an
    # interleaving race becomes a plainly wrong answer.
    dockerfile = docker_gen.generate_dockerfile("petro_api", "fortran", "petro_api")

    assert "ENV WEB_CONCURRENCY=1" in dockerfile
    assert "COMMON" in dockerfile  # and the reason travels with the image


def test_cpp_scales_workers_freely():
    dockerfile = docker_gen.generate_dockerfile("calculator", "cpp", "calculator")

    assert "ENV WEB_CONCURRENCY=2" in dockerfile


def test_the_supervisor_is_a_declared_runtime_dependency():
    # A CMD naming gunicorn in an image that never installed it is a container
    # that exits immediately. The generated pyproject and the SBOM must agree.
    from native2py.generators import pyproject_gen

    for language in ("cpp", "fortran"):
        pyproject = pyproject_gen.generate_pyproject("svc", language)
        assert '"gunicorn"' in pyproject
        assert '"uvicorn-worker"' in pyproject
        assert "gunicorn" in docker_gen._LANGUAGE_RUNTIME_PYTHON_DEPS[language]
