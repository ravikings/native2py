"""Backend selection.

``NGATE_DEPLOY_BACKEND`` picks where project services run: ``docker``
(default) or ``cloudrun``. The default is deliberate — the Docker backend is
the one that is actually exercised, by every self-host, every local dev run,
and the compose job in CI, while the Cloud Run backend has not yet run
against a live GCP project. An untested path must not be what somebody gets
by forgetting to set a variable.
"""

from __future__ import annotations

import os

from console.backends.base import DeployBackend

_BACKENDS = {"docker", "cloudrun"}

_cached: DeployBackend | None = None


def get_backend() -> DeployBackend:
    """Return the process-wide deploy backend, constructing it once.

    Cached because callers reach for this per request and per orchestrator
    tick; constructing a backend is cheap but re-reading env every time
    invites the two halves of one deploy landing on different backends if
    the variable is ever changed under a running process.

    Imports the implementation lazily so that selecting one backend never
    requires the other's module to import cleanly — relevant for the
    air-gapped build, where nothing GCP-shaped should be loaded at all.
    """
    global _cached
    if _cached is None:
        _cached = _construct(backend_name())
    return _cached


def backend_name() -> str:
    """The configured backend name, validated.

    Fails loudly on an unknown value rather than falling back to the
    default: a typo in NGATE_DEPLOY_BACKEND that silently deployed to the
    local Docker daemon instead of Cloud Run would be found by a user
    wondering why their hosted project is unreachable, which is far too
    late and very hard to diagnose from the outside.
    """
    name = os.environ.get("NGATE_DEPLOY_BACKEND", "docker").strip().lower() or "docker"
    if name not in _BACKENDS:
        raise RuntimeError(
            f"Unknown NGATE_DEPLOY_BACKEND={name!r}. Valid values: "
            f"{', '.join(sorted(_BACKENDS))}."
        )
    return name


def _construct(name: str) -> DeployBackend:
    if name == "cloudrun":
        from console.backends.cloudrun import CloudRunBackend

        return CloudRunBackend()

    from console.backends.docker import DockerBackend

    return DockerBackend()


def reset_cache() -> None:
    """Drop the cached backend. For tests that flip NGATE_DEPLOY_BACKEND."""
    global _cached
    _cached = None


__all__ = ["DeployBackend", "backend_name", "get_backend", "reset_cache"]
