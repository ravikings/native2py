"""FastAPI entry point for the Nativegate console.

Thin web skin over the ``ngate`` CLI (see docs/console-design.md). This
module wires up startup (DB init), static files, and templates, and defines
the landing route. Everything else (routes/, jobs.py, ngate.py, etc.) lands
separately.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from console import deploy, orchestrator
from console.auth import auth_router, get_current_user
from console.db import get_db, init_db
from console.routes.evidence import router as evidence_router
from console.routes.pages import router as pages_router
from console.routes.stream import router as stream_router

BASE_DIR = Path(__file__).resolve().parent

# NGATE_AUTH selects the auth mode: "none" (default, self-host/dev) or
# "github" (GitHub OAuth, projects scoped to the GitHub user id). See
# console/auth.py and docs/console-design.md §4.
NGATE_AUTH = os.environ.get("NGATE_AUTH", "none")


# The deploy backend, resolved once at startup. Validating here rather than
# per-request is deliberate: an unknown NGATE_DEPLOY_BACKEND is a
# configuration error, and the two ways to surface one are not equal. Raising
# inside /healthz would turn every probe into a 500, and an orchestrator that
# restarts on failed health checks (compose's `restart: unless-stopped`,
# Cloud Run, k8s) would loop-restart the container forever with no statement
# of what is wrong. Raising in lifespan kills the container once, with the
# message naming the bad value, and leaves /healthz a pure readiness signal
# that configuration cannot break.
DEPLOY_BACKEND = "unresolved"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global DEPLOY_BACKEND
    DEPLOY_BACKEND = deploy.backend_name()
    init_db()
    # Bring container reality back in line with the DB (host reboot, a
    # container removed out from under the console, etc.) before serving
    # any requests, then keep it that way in the background — see
    # console/orchestrator.py.
    orchestrator.reconcile_on_startup()
    monitor_task = asyncio.create_task(orchestrator.monitor_loop())
    try:
        yield
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Nativegate Console", lifespan=lifespan)

app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static"), check_dir=False),
    name="static",
)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app.include_router(pages_router)
app.include_router(evidence_router)
app.include_router(stream_router)
app.include_router(auth_router)


@app.get("/healthz")
def healthz():
    """Unauthenticated readiness probe.

    Deliberately outside the auth dependency: this is what the compose
    healthcheck and CI's ``docker compose up --wait`` poll, and both run
    before any login exists. It must therefore never disclose anything
    tenant-scoped — no project slugs, no counts, no user data.

    It is a *readiness* probe, not a liveness one: it touches SQLite so a
    container that came up with an unwritable ``console-data`` volume
    reports unhealthy instead of serving 500s on the first real request.
    That failure mode is the whole reason this returns more than a
    hardcoded ``{"status": "ok"}`` — a probe that cannot fail proves
    nothing, and every one of these bugs is a volume-permission bug.
    """
    conn = get_db()
    try:
        conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    finally:
        conn.close()
    # DEPLOY_BACKEND is read, not recomputed: this route must not be able to
    # fail on configuration. A bad NGATE_DEPLOY_BACKEND already stopped the
    # container in lifespan, so anything answering here has a valid one, and
    # reporting it makes "which backend is this deployment actually using"
    # answerable without shell access.
    return {"status": "ok", "auth": NGATE_AUTH, "backend": DEPLOY_BACKEND}


@app.get("/")
def landing(request: Request):
    return templates.TemplateResponse(
        request, "landing.html", {"title": "Nativegate Console"}
    )


@app.get("/api/services")
def services_status(user: sqlite3.Row = Depends(get_current_user)):
    """Live status of the current user's deployed project containers.

    Read-only view over console/orchestrator.py's reconciliation state —
    what the DB thinks is running, what Docker actually reports, and how
    many consecutive health checks a project has failed. Scoped to the
    logged-in user the same way every other project listing in the console
    is (console/routes/pages.py's ``_owned_project``) — under
    ``NGATE_AUTH=github`` this must never return another tenant's project
    slugs or live URLs.
    """
    return {"services": orchestrator.fleet_status(owner_id=user["id"])}
