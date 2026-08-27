"""Double-submit-cookie CSRF protection for the console's state-changing forms.

No JS involved: the token is generated server-side, set in an httponly
cookie, and also embedded server-side as a hidden form field (Jinja renders
both from the same request). A cross-origin attacker page can trigger the
victim's browser into submitting the cookie automatically, but same-origin
policy stops it from ever reading the cookie's value to also put in a hidden
field — so a forged cross-site POST carries the cookie but not a matching
token, and `verify_csrf` rejects it.

This is a second, independent layer on top of the session cookie's
`SameSite=Lax` (console/auth.py) — Lax already blocks most cross-site POSTs
in modern browsers, but doesn't cover every client/proxy configuration, so
state-changing routes (delete, in particular) get this belt-and-suspenders
check too.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import Form, HTTPException, Request
from starlette.responses import Response

CSRF_COOKIE_NAME = "ngate_csrf"
CSRF_COOKIE_MAX_AGE = 7 * 24 * 3600  # 1 week; regenerated on demand if missing/expired


def get_csrf_token(request: Request) -> str:
    """Return this browser's CSRF token, generating one if it has none yet.

    Call *before* constructing a TemplateResponse — Starlette renders a
    TemplateResponse's body synchronously in its constructor, so mutating
    `.context` afterward has no effect on the already-rendered HTML. Pass the
    returned token into the template context as `csrf_token`, then call
    `set_csrf_cookie` on the constructed response.
    """
    return request.cookies.get(CSRF_COOKIE_NAME) or secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str) -> None:
    """Set/refresh the CSRF cookie on a response. Idempotent and cheap to call
    unconditionally, even if the token was already present in the request."""
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        max_age=CSRF_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def verify_csrf(request: Request, csrf_token: str = Form(...)) -> None:
    """FastAPI dependency: reject a POST whose form token doesn't match the cookie."""
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token or not hmac.compare_digest(cookie_token, csrf_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")
