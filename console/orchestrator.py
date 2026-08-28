"""Multi-service lifecycle orchestration, single host.

`deploy.py` knows how to start/stop/inspect *one* project's container. This
module is the layer above it that keeps every deployed project's container
matching what the database thinks is running, without a human clicking
"restart" on each one by hand:

- ``reconcile_on_startup`` — runs once when the console boots. A project
  marked "running" in the DB whose container is gone (host reboot, someone
  ran ``docker rm``, the daemon lost state) gets started back up if its
  image still exists locally; otherwise it's marked "stopped" so the UI
  doesn't lie about it.
- ``monitor_loop`` — a background asyncio task that polls every deployed
  project's ``/healthz`` on an interval, and restarts a project whose
  container is unhealthy or missing, up to a small retry cap. This is
  distinct from Docker's own ``--restart unless-stopped`` policy (still set
  in ``deploy.start_service``): that policy only recovers a container that
  *crashed*, not one that's running but wedged and failing health checks.

Deliberately still single-host: this walks ``docker ps``/``docker inspect``
on the one Docker daemon the console's socket is mounted from
(console/README.md), same as deploy.py. No scheduler, no cluster, no
cross-host state — just making sure what's supposed to be up, is.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

from console import calls, db, deploy

logger = logging.getLogger("console.orchestrator")

# How often the background monitor polls each running project's /healthz.
HEALTH_INTERVAL_SECONDS = int(os.environ.get("NGATE_HEALTH_INTERVAL", "30"))

# Give up restarting a given project after this many consecutive failed
# health checks in a row, so a permanently broken image can't be
# restart-looped forever — it's marked "crashed" instead and left for a
# human to look at.
MAX_CONSECUTIVE_FAILURES = 3

HEALTH_TIMEOUT_SECONDS = 5.0

# project slug -> consecutive failed-health-check count. In-memory is fine:
# this resets on console restart, which is exactly when reconcile_on_startup
# re-establishes ground truth anyway.
_failure_counts: dict[str, int] = {}


def forget(slug: str) -> None:
    """Drop `slug`'s tracked failure count.

    Call this when a project is deleted — otherwise `_failure_counts` grows
    unbounded over the life of the process (one entry per slug that ever
    failed a health check, never removed), and a *new* project that reuses a
    deleted slug would inherit the old one's stale count instead of starting
    clean.
    """
    _failure_counts.pop(slug, None)
    # A deleted project's container is gone, but its supervised tailer would
    # otherwise keep respawning `docker logs` against a name that no longer
    # exists for the life of the process.
    calls.stop_tailer(slug)


async def _check_health(port: int) -> bool:
    """Async health check — must not block the event loop.

    Uses httpx.AsyncClient rather than the synchronous httpx.get: this runs
    from within monitor_loop's asyncio task, and a blocking call here would
    stall every other coroutine in the app (all incoming console requests)
    for up to HEALTH_TIMEOUT_SECONDS per unhealthy project checked each tick.
    """
    try:
        async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT_SECONDS) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/healthz")
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def reconcile_on_startup() -> None:
    """Bring container reality in line with the DB's "running" projects.

    Called once from the app's lifespan, before the health-monitor loop
    starts. Best-effort per project: one broken project must not stop the
    rest from being reconciled.
    """
    projects = [p for p in db.list_projects() if p["status"] == "running"]
    for project in projects:
        slug = project["slug"]
        status = deploy.service_status(slug)
        if status["running"]:
            continue
        try:
            build_id = db.get_latest_successful_build_id(project["id"])
            info = deploy.start_service(slug, build_id=build_id)
            logger.info("reconcile: restarted %s at %s", slug, info["url"])
        except RuntimeError as exc:
            logger.warning("reconcile: could not restart %s: %s", slug, exc)
            db.update_project_status(slug, "stopped")


async def _sync_tailer(project, running: bool) -> None:
    """Keep exactly one call-log tailer alive per running service.

    Failures here are swallowed: durable call history is a nice-to-have next
    to the monitor loop's actual job of keeping services up, and must never
    be the reason a health check doesn't happen.
    """
    try:
        if running:
            await asyncio.to_thread(calls.ensure_tailer, project["slug"], project["id"])
        else:
            await asyncio.to_thread(calls.stop_tailer, project["slug"])
    except Exception:  # noqa: BLE001
        logger.exception("call tailer bookkeeping failed for %s", project["slug"])


async def _monitor_tick() -> None:
    projects = [p for p in db.list_projects() if p["status"] in ("running", "crashed")]
    for project in projects:
        slug = project["slug"]
        # `docker inspect` under the hood: seconds of blocking I/O per
        # project, which run directly on the event loop would stall every
        # in-flight console request for the whole tick.
        status = await asyncio.to_thread(deploy.service_status, slug)
        await _sync_tailer(project, status["running"])
        healthy = status["running"] and await _check_health(status["port"])

        if healthy:
            _failure_counts[slug] = 0
            if project["status"] != "running":
                db.update_project_status(slug, "running")
            continue

        failures = _failure_counts.get(slug, 0) + 1
        _failure_counts[slug] = failures
        logger.warning(
            "health check failed for %s (%d/%d)", slug, failures, MAX_CONSECUTIVE_FAILURES
        )

        if failures > MAX_CONSECUTIVE_FAILURES:
            continue  # already given up on this one; wait for a human

        if failures == MAX_CONSECUTIVE_FAILURES:
            try:
                build_id = db.get_latest_successful_build_id(project["id"])
                info = await asyncio.to_thread(
                    deploy.start_service, slug, build_id=build_id
                )
                logger.info("auto-restarted %s at %s", slug, info["url"])
                _failure_counts[slug] = 0
            except RuntimeError as exc:
                logger.error("giving up on %s after %d failures: %s", slug, failures, exc)
                db.update_project_status(slug, "crashed")


async def monitor_loop() -> None:
    """Poll every running project's health forever, restarting as needed.

    Runs as a background asyncio task for the lifetime of the app (started
    from the lifespan context in app.py). Exceptions from a single tick are
    logged and swallowed so one bad tick doesn't kill monitoring entirely.
    """
    try:
        while True:
            try:
                await _monitor_tick()
            except Exception:  # noqa: BLE001 - the loop must survive any single tick's failure
                logger.exception("orchestrator monitor tick failed")
            await asyncio.sleep(HEALTH_INTERVAL_SECONDS)
    finally:
        # This task being cancelled *is* console shutdown; the tailers it
        # started hold `docker logs -f` subprocesses that would otherwise
        # outlive the process that owns them.
        calls.stop_all_tailers()


def fleet_status(owner_id: int | None = None) -> list[dict]:
    """Live status for every project owned by ``owner_id``.

    ``owner_id`` scopes this the same way ``console.db.list_projects`` does
    for every other per-user listing in the console (see
    ``console/routes/pages.py``'s ``_owned_project``) — callers must pass
    the current user's id, not leave it ``None``, or every tenant's project
    slugs and live URLs leak to every other tenant under ``NGATE_AUTH=github``.
    """
    out = []
    for project in db.list_projects(owner_id=owner_id):
        slug = project["slug"]
        status = deploy.service_status(slug)
        out.append(
            {
                "slug": slug,
                "name": project["name"],
                "db_status": project["status"],
                "running": status["running"],
                "port": status["port"],
                "url": status["url"],
                "mcp_url": status["mcp_url"],
                "consecutive_failures": _failure_counts.get(slug, 0),
            }
        )
    return out
