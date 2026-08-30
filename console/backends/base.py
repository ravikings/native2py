"""The deploy-backend interface.

One project's service has to run *somewhere*, and there are two somewheres
this console has to support:

* the host's own Docker daemon — self-hosting, local development, and the
  air-gapped install (docs/design-air-gapped-deployment.md §3), where the
  customer's infrastructure is whatever Linux box they put the console on;
* Google Cloud Run — the hosted tier
  (docs/design-cloud-run-lifecycle.md §2).

Those are the only two, deliberately. A "pick your cloud provider" selector
would mean holding the customer's cloud credentials and maintaining a
backend per provider, and neither is worth it while the Docker backend
already runs on anybody's infrastructure. A third implementation is a
day's work behind this interface if a contract ever justifies one.

Two shapes cross this boundary, and both are dictionaries rather than
dataclasses because the console's routes and templates consume them
directly and the existing call sites already expect ``dict``:

``start_service`` returns
    ``{"container_id": str, "port": int | None, "url": str, "mcp_url": str}``

``service_status`` returns
    ``{"running": bool, "port": int | None, "url": str | None, "mcp_url": str | None}``

``port`` is ``None`` on any backend that does not publish a host port —
Cloud Run hands out a URL, not a port, so callers must treat ``port`` as
display detail and use ``url``/``mcp_url`` for anything that has to work
on both. ``container_id`` is an opaque identity string (a container ID on
Docker, a revision name on Cloud Run); nothing may parse it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class DeployBackend(ABC):
    """Where a project's built service actually runs.

    Implementations must be safe to call from a thread — the console drives
    them through ``asyncio.to_thread`` (console/orchestrator.py,
    console/routes/stream.py) — and must not hold per-call state, since one
    instance is shared process-wide.
    """

    #: Short identifier, surfaced in errors and in ``/healthz`` so a
    #: misconfigured deployment is visible without reading env vars.
    name: str = "unknown"

    @abstractmethod
    def start_service(self, slug: str, build_id: int | None = None) -> dict:
        """Start, or restart, the service for ``slug``.

        Must be idempotent: an existing instance is replaced rather than
        duplicated or refused. Raises ``RuntimeError`` with a message that
        names the fix when the image has not been built or published yet —
        that error reaches the user's screen, so "not found" alone is not
        an acceptable message.

        ``build_id`` is stamped into the service's environment so every
        call it later logs can be traced to the build that produced it.
        """

    @abstractmethod
    def stop_service(self, slug: str) -> None:
        """Stop and remove the service for ``slug``.

        A no-op, not an error, when nothing is running — callers use this
        to converge state and must be able to call it unconditionally.
        """

    @abstractmethod
    def remove_image(self, slug: str) -> None:
        """Delete the built image for ``slug``.

        Never raises. A left-behind image is a storage cost, not a
        correctness problem, and project deletion must not fail because a
        registry refused a delete (docs/design-cloud-run-lifecycle.md §5
        makes reclamation a background concern for exactly this reason).
        """

    @abstractmethod
    def service_status(self, slug: str) -> dict:
        """Report whether ``slug``'s service is currently running.

        Never raises for a missing service; returns the not-running shape
        described in this module's docstring instead. The orchestrator polls
        this in a loop, so an exception here would take down reconciliation
        for every project, not just this one.
        """

    @abstractmethod
    def log_follow_command(
        self,
        slug: str,
        tail: str | None = None,
        since: str | None = None,
    ) -> list[str]:
        """Return an argv that streams this service's logs to stdout, following.

        The console runs this under ``subprocess.Popen`` and parses the
        stream line by line (console/calls.py), so the command must write
        one log line per output line and keep running until killed.

        ``since`` resumes from a timestamp after a tailer outage. ``tail``
        bounds the initial backfill and **must be honoured as unbounded when
        None** — capping a resume would silently drop everything older than
        the last N lines from a window the resume existed to cover, which
        produces an incomplete evidence pack with no error anywhere.
        """
