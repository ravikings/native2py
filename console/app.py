"""FastAPI entry point for the Nativegate console.

Thin web skin over the ``ngate`` CLI (see docs/console-design.md). This
module wires up startup (DB init), static files, and templates, and defines
the landing route. Everything else (routes/, jobs.py, ngate.py, etc.) lands
separately.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from console.auth import auth_router
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
    yield


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
