"""Lifecycle management for a project's long-running service container.

Assumes the build pipeline already ran ``ngate docker <slug> --build`` (see
console/jobs.py) and produced a local image tagged ``<slug>:latest`` — that
is the tag ``ngate docker --build`` actually produces
(tools/nativegate/docs/cli-reference.md, "ngate docker <name>"), not a
console-invented ``ngate-svc-<slug>`` prefix. This module only starts, stops,
and inspects the resulting container — it does not build images.

Shells out to the ``docker`` CLI via ``subprocess`` rather than depending on
the ``docker`` Python SDK, since the CLI is always available wherever the
Docker daemon is reachable and this keeps the console free of an extra
dependency.

Limits per docs/console-design.md §5: 512 MB memory, 1 CPU core. The service
container runs on the default bridge network (unlike the build container,
which runs with ``--network none`` — that is handled elsewhere).
"""

from __future__ import annotations

import socket
import subprocess

from console.db import update_project_status

DOCKER_TIMEOUT = 30
PORT_RANGE_START = 20000
PORT_RANGE_END = 29999


def _container_name(slug: str) -> str:
    return f"ngate-svc-{slug}"


def _image_name(slug: str) -> str:
    return f"{slug}:latest"


def _run_docker(args: list[str], timeout: int = DOCKER_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a docker CLI command, returning the completed process.

    Callers are responsible for interpreting the return code; this helper
    only handles invocation and captures stdout/stderr as text.
    """
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def allocate_port() -> int:
    """Find a free TCP port in the 20000-29999 range.

    Tries binding to candidate ports in the range until one succeeds, then
    releases it immediately and returns it. This accepts a small TOCTOU
    race (something else could grab the port before ``docker run`` binds
    it) which is acceptable for an MVP.
    """
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
        except OSError:
            continue
        else:
            return port
        finally:
            sock.close()
    raise RuntimeError(
        f"No free TCP port available in range {PORT_RANGE_START}-{PORT_RANGE_END}"
    )


def _image_exists(slug: str) -> bool:
    proc = _run_docker(["image", "inspect", _image_name(slug)])
    return proc.returncode == 0


def _remove_existing_container(slug: str) -> None:
    """Stop and remove any existing container for this slug, ignoring absence."""
    name = _container_name(slug)
    _run_docker(["stop", name])
    proc = _run_docker(["rm", "-f", name])
    if proc.returncode != 0 and "No such container" not in proc.stderr:
        raise RuntimeError(f"Failed to remove existing container {name!r}: {proc.stderr.strip()}")


def start_service(slug: str) -> dict:
    """Start (or restart) the service container for ``slug``.

    Idempotent: any existing container with the same name is stopped and
    removed first. Raises ``RuntimeError`` with an actionable message if the
    image has not been built yet.
    """
    image = _image_name(slug)
    if not _image_exists(slug):
        raise RuntimeError(
            f"Docker image {image!r} not found locally. Build it first by running "
            f"'ngate docker {slug} --build' (--build is required to actually build "
            f"the image; without it, 'ngate docker' only writes the Dockerfile)."
        )

    _remove_existing_container(slug)

    port = allocate_port()
    name = _container_name(slug)
    proc = _run_docker(
        [
            "run",
            "-d",
            "--name",
            name,
            "-m",
            "512m",
            "--cpus",
            "1",
            "--restart",
            "unless-stopped",
            "-p",
            f"{port}:8000",
            image,
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to start container {name!r}: {proc.stderr.strip()}")

    container_id = proc.stdout.strip()
    update_project_status(slug, "running")

    return {
        "container_id": container_id,
        "port": port,
        "url": f"http://localhost:{port}/",
        "mcp_url": f"http://localhost:{port}/mcp/",
    }


def stop_service(slug: str) -> None:
    """Stop and remove the service container for ``slug``, if it exists.

    No-op (no error) if the container does not exist.
    """
    name = _container_name(slug)

    proc = _run_docker(["stop", name])
    if proc.returncode != 0 and "No such container" not in proc.stderr:
        raise RuntimeError(f"Failed to stop container {name!r}: {proc.stderr.strip()}")

    proc = _run_docker(["rm", name])
    if proc.returncode != 0 and "No such container" not in proc.stderr:
        raise RuntimeError(f"Failed to remove container {name!r}: {proc.stderr.strip()}")

    update_project_status(slug, "stopped")


def remove_image(slug: str) -> None:
    """Delete the built image for ``slug``, if it exists.

    Called on project deletion so repeated upload/build/delete cycles during
    testing don't silently accumulate images forever — `deploy.start_service`
    tags every build's image `<slug>:latest`, and nothing else in this
    console removes an old one. No-op (no error) if the image doesn't exist
    or is still in use by a container the caller hasn't stopped yet (that
    case surfaces as a docker error in the log rather than raising, since a
    left-behind image is a disk-space nuisance, not a correctness problem).
    """
    image = _image_name(slug)
    proc = _run_docker(["rmi", "-f", image])
    if proc.returncode != 0 and "No such image" not in proc.stderr:
        # Not fatal — deletion should still proceed even if docker refuses
        # to drop the image (e.g. another tag/container still references it).
        pass


def service_status(slug: str) -> dict:
    """Return the running status of the service container for ``slug``.

    Returns ``{"running": False, "port": None, "url": None, "mcp_url": None}``
    if the container does not exist or is not running. Never raises for a
    missing container.
    """
    name = _container_name(slug)
    not_running = {"running": False, "port": None, "url": None, "mcp_url": None}

    proc = _run_docker(
        [
            "inspect",
            "--format",
            "{{.State.Running}}|{{(index (index .NetworkSettings.Ports \"8000/tcp\") 0).HostPort}}",
            name,
        ]
    )
    if proc.returncode != 0:
        return not_running

    output = proc.stdout.strip()
    if "|" not in output:
        return not_running

    running_str, port_str = output.split("|", 1)
    running = running_str.strip() == "true"
    if not running or not port_str.strip():
        return {"running": running, "port": None, "url": None, "mcp_url": None}

    port = int(port_str.strip())
    return {
        "running": True,
        "port": port,
        "url": f"http://localhost:{port}/",
        "mcp_url": f"http://localhost:{port}/mcp/",
    }
