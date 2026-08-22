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
