"""Evidence-pack pages: generate a signed, forwardable record of a project's
recorded calls over a date range (see console/evidence.py)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from .. import db, evidence
from ..auth import get_current_user
from ..csrf import get_csrf_token, set_csrf_cookie, verify_csrf
from ..timerange import BadTimeBound, normalize_bound as _normalize_bound

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _owned_project(slug: str, user: sqlite3.Row) -> sqlite3.Row:
    """Same ownership rule as console/routes/pages.py: a bare slug lookup
    would let any authenticated user export another tenant's call history —
    and call history is exactly what must not leak across tenants."""
    project = db.get_project(slug)
    if project is None or project["owner_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@router.get("/p/{slug}/evidence")
def evidence_form(
    request: Request,
    slug: str,
    user: sqlite3.Row = Depends(get_current_user),
):
    project = _owned_project(slug, user)
    builds = db.list_builds(project["id"])
    token = get_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "evidence.html",
        {
            "project": project,
            "builds": builds,
            "total_calls": db.count_service_calls(project["id"]),
            "claims": evidence.ATTESTATION_CLAIMS,
            "disclaimers": evidence.ATTESTATION_DISCLAIMERS,
            "public_key": evidence.public_key_b64(
                evidence.load_or_create_key().public_key()
            ),
            "csrf_token": token,
        },
    )
    set_csrf_cookie(response, token)
    return response


@router.get("/p/{slug}/evidence/verifier")
def download_verifier(slug: str, user: sqlite3.Row = Depends(get_current_user)):
    """The standalone verifier, handed over alongside a pack."""
    _owned_project(slug, user)
    return Response(
        content=evidence.VERIFIER_SOURCE,
        media_type="text/x-python",
        headers={"Content-Disposition": 'attachment; filename="verify_evidence.py"'},
    )


@router.post("/p/{slug}/evidence", dependencies=[Depends(verify_csrf)])
def generate_pack(
    slug: str,
    since: str = Form(""),
    until: str = Form(""),
    build_id: str = Form(""),
    user: sqlite3.Row = Depends(get_current_user),
):
    project = _owned_project(slug, user)

    build_filter: int | None = None
    if build_id.strip():
        try:
            build_filter = int(build_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="build_id must be an integer")
        # Build ids are globally autoincrementing, so one from another project
        # is a valid integer that simply matches nothing here. Unchecked, that
        # returns a *signed* pack asserting zero calls — a far worse answer to
        # a typo than an error, since the recipient cannot tell "no activity"
        # from "wrong id". routes/pages.py's calls_page already 404s on this.
        build = db.get_build(build_filter)
        if build is None or build["project_id"] != project["id"]:
            raise HTTPException(status_code=404, detail="build not found")

    try:
        since_ts = _normalize_bound(since, end_of_day=False)
        until_ts = _normalize_bound(until, end_of_day=True)
    except BadTimeBound as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    calls = evidence.collect_calls(
        db, project["id"], since=since_ts, until=until_ts, build_id=build_filter
    )
    pack = evidence.build_pack(
        project_slug=project["slug"],
        project_name=project["name"],
        calls=calls,
        since=since_ts,
        until=until_ts,
        build_id=build_filter,
        private_key=evidence.load_or_create_key(),
    )

    # Indented for the human who opens it in an editor; the signature covers
    # the canonical form (console/evidence.py), never this rendering, so
    # pretty-printing here cannot break verification.
    body = json.dumps(pack, indent=2, ensure_ascii=False, sort_keys=True)
    filename = evidence.pack_filename(project["slug"], since_ts, until_ts)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
