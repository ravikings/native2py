"""Lifecycle management for a project's long-running service.

This module used to *be* the Docker implementation. It is now a thin facade
over console/backends/: the mechanics of starting and stopping a service
live in a DeployBackend (Docker on the host daemon, or Cloud Run), and what
stays here is the part that is the console's own concern rather than any
backend's — writing project status to the database.

That split is why the DB writes moved out of the backends: two backends
updating ``projects.status`` independently is two places to forget it, and
the status column means the same thing regardless of where the container
actually ran.

Every existing call site keeps working unchanged — ``deploy.start_service``,
``deploy.stop_service``, ``deploy.remove_image``, ``deploy.service_status``
have the same names, signatures and return shapes they always had. One
contract did widen, and callers must respect it: ``port`` is now ``int |
None``, because Cloud Run publishes a URL and no host port. Use ``url`` and
``mcp_url`` for anything that has to work on both backends.
"""

from __future__ import annotations

from console.backends import backend_name, get_backend
from console.db import update_project_status

__all__ = [
    "backend_name",
    "log_follow_command",
    "remove_image",
    "service_status",
    "start_service",
    "stop_service",
]


def start_service(slug: str, build_id: int | None = None) -> dict:
    """Start (or restart) the service for ``slug`` on the active backend.

    Idempotent: any existing instance is replaced. Raises ``RuntimeError``
    with an actionable message if the image has not been built (Docker) or
    published to Artifact Registry (Cloud Run).

    ``build_id`` is stamped into the service's environment (alongside the
    slug) so every call the service logs can be traced back to the build
    that produced it.
    """
    info = get_backend().start_service(slug, build_id=build_id)
    # After the backend, never before: a failed start must not leave the DB
    # claiming the project is running. The backend raises on failure, so
    # reaching this line is the only evidence that it actually came up.
    update_project_status(slug, "running")
    return info


def stop_service(slug: str) -> None:
    """Stop and remove the service for ``slug``, if it exists.

    No-op (no error) if nothing is running.
    """
    get_backend().stop_service(slug)
    update_project_status(slug, "stopped")


def remove_image(slug: str) -> None:
    """Delete the built image for ``slug``, if it exists. Never raises."""
    get_backend().remove_image(slug)


def service_status(slug: str) -> dict:
    """Return the running status of ``slug``'s service.

    Returns ``{"running": False, "port": None, "url": None, "mcp_url": None}``
    when nothing is running. Never raises for a missing service — the
    orchestrator polls this for every project in a loop, so an exception
    here would stop reconciliation for all of them.
    """
    return get_backend().service_status(slug)


def log_follow_command(
    slug: str,
    tail: str | None = None,
    since: str | None = None,
) -> list[str]:
    """argv that streams ``slug``'s service logs, following. See calls.py."""
    return get_backend().log_follow_command(slug, tail=tail, since=since)
