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
