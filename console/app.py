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

from console import orchestrator
from console.auth import auth_router, get_current_user
from console.db import init_db
from console.routes.pages import router as pages_router
from console.routes.stream import router as stream_router

BASE_DIR = Path(__file__).resolve().parent

# NGATE_AUTH selects the auth mode: "none" (default, self-host/dev) or
# "github" (GitHub OAuth, projects scoped to the GitHub user id). See
# console/auth.py and docs/console-design.md §4.
NGATE_AUTH = os.environ.get("NGATE_AUTH", "none")


@asynccontextmanager
async def lifespan(app: FastAPI):
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
app.include_router(stream_router)
app.include_router(auth_router)


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
