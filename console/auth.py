"""Authentication for the console app.

Controlled by the ``NGATE_AUTH`` env var:

- ``none`` (default): no login. Every request is owned by the single local
  user (self-host / dev). See docs/console-design.md §4.
- ``github``: GitHub OAuth. Projects are scoped to the GitHub user id.

``get_current_user`` is the FastAPI dependency every route should use to
resolve "who is making this request" — it is a plain sync function so it
stays cheap to call on every request.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from console import db

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_API_URL = "https://api.github.com/user"

SESSION_COOKIE_NAME = "ngate_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 days

OAUTH_STATE_COOKIE_NAME = "ngate_oauth_state"
OAUTH_STATE_MAX_AGE_SECONDS = 600  # 10 minutes to complete the OAuth round trip


def _auth_mode() -> str:
    return os.environ.get("NGATE_AUTH", "none").strip().lower()


def _require_github_env() -> tuple[str, str]:
    client_id = os.environ.get("GITHUB_CLIENT_ID")
    client_secret = os.environ.get("GITHUB_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "NGATE_AUTH=github requires GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET "
            "to be set in the environment. Register an OAuth app at "
            "https://github.com/settings/developers and set both env vars — "
            "the console will not silently fall back to unauthenticated mode."
        )
    return client_id, client_secret


def _secret_key() -> str:
    key = os.environ.get("NGATE_SECRET_KEY")
    if not key:
        raise RuntimeError(
            "NGATE_AUTH=github requires NGATE_SECRET_KEY to be set (used to sign "
            "session cookies). Set it to any long random string."
        )
    return key


def _sign_session(github_id: str) -> str:
    """Produce a signed session cookie value encoding the github_id + timestamp.

    Plain stdlib HMAC, not itsdangerous: console/Dockerfile never installs
    it, so a code path depending on it would be permanently dead in every
    real deployment of this image (verified: it's not in the Docker image's
    pip install list) — indistinguishable from a supported option to anyone
    reading this file, but silently unreachable in practice.
    """
    ts = str(int(time.time()))
    payload = f"{github_id}.{ts}"
    sig = hmac.new(_secret_key().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_session(value: str) -> str | None:
    """Return the github_id encoded in a signed session cookie, or None if invalid/expired."""
    try:
        github_id, ts, sig = value.split(".", 2)
    except ValueError:
        return None
    payload = f"{github_id}.{ts}"
    expected = hmac.new(_secret_key().encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    if time.time() - int(ts) > SESSION_MAX_AGE_SECONDS:
        return None
    return github_id


def get_current_user(request: Request) -> sqlite3.Row:
    """FastAPI dependency: resolve the user making this request.

    ``none`` mode: always the single local user, no cookie/session involved.
    ``github`` mode: reads the signed session cookie set by
    ``/auth/github/callback``; raises 401 if missing/invalid.
    """
    mode = _auth_mode()

    if mode == "none":
        return db.get_or_create_local_user()

    if mode == "github":
        _require_github_env()
        try:
            _secret_key()
        except RuntimeError as exc:
            # NGATE_SECRET_KEY missing/removed after a session cookie was
            # already issued (env changed at runtime) — surface the same
            # clear config error _require_github_env() gives, not an
            # unhandled 500 from deep inside _verify_session.
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        cookie = request.cookies.get(SESSION_COOKIE_NAME)
        if not cookie:
            raise HTTPException(status_code=401, detail="Not logged in. Visit /auth/github/login.")
        github_id = _verify_session(cookie)
        if not github_id:
            raise HTTPException(status_code=401, detail="Session invalid or expired.")
        conn = db.get_db()
        try:
            row = conn.execute("SELECT * FROM users WHERE github_id = ?", (github_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=401, detail="Unknown session user.")
        return row

    raise RuntimeError(
        f"Unknown NGATE_AUTH={mode!r}. Expected 'none' or 'github'."
    )


auth_router = APIRouter(prefix="/auth/github", tags=["auth"])


@auth_router.get("/login")
def github_login() -> RedirectResponse:
    client_id, _ = _require_github_env()
    # Fail fast, before sending the user to GitHub at all: without this,
    # a misconfigured NGATE_SECRET_KEY only surfaces as an unhandled
    # RuntimeError -> 500 at the very end of github_callback (in
    # _sign_session), after the user has already completed the external
    # GitHub login — a much worse place to discover a config error.
    _secret_key()
    # CSRF protection on the OAuth flow: a random state is bound to this
    # browser via a short-lived httponly cookie and echoed back through
    # GitHub. Without this, an attacker can start their own OAuth flow,
    # capture the callback URL, and trick a victim into visiting it — logging
    # the victim into the attacker's GitHub-linked session (a login-CSRF /
    # session-fixation style attack).
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": client_id,
        "scope": "read:user",
        "state": state,
    }
    response = RedirectResponse(f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}")
    response.set_cookie(
        OAUTH_STATE_COOKIE_NAME,
        state,
        max_age=OAUTH_STATE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return response


@auth_router.get("/callback")
def github_callback(request: Request) -> RedirectResponse:
    client_id, client_secret = _require_github_env()

    expected_state = request.cookies.get(OAUTH_STATE_COOKIE_NAME)
    got_state = request.query_params.get("state")
    if not expected_state or not got_state or not hmac.compare_digest(expected_state, got_state):
        raise HTTPException(status_code=400, detail="Invalid or missing OAuth state.")

    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Missing 'code' from GitHub OAuth callback.")

    token_resp = httpx.post(
        GITHUB_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        },
        headers={"Accept": "application/json"},
        timeout=10.0,
    )
    token_resp.raise_for_status()
    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail=f"GitHub OAuth token exchange failed: {token_data}")

    user_resp = httpx.get(
        GITHUB_USER_API_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10.0,
    )
    user_resp.raise_for_status()
    gh_user = user_resp.json()
    github_id = str(gh_user["id"])
    username = gh_user["login"]

    db.get_or_create_github_user(github_id, username)

    response = RedirectResponse("/")
    response.set_cookie(
        SESSION_COOKIE_NAME,
        _sign_session(github_id),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(OAUTH_STATE_COOKIE_NAME)
    return response
