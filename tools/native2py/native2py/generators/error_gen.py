"""The generated apps' unhandled-exception handler.

Lives in its own module because BOTH app generators need the identical block:
`python_pkg_gen.generate_service_py` for the standalone container, and
`gateway_gen.generate_gateway_app` for the composed topology. An exception
handler is registered per FastAPI *app*, and a service router mounted into a
gateway runs under the gateway's app — so a handler installed only on
`service.py` would silently not apply in the composed deployment, which is the
one more likely to be internet-facing. Emitting from one place is what stops
those two paths drifting.

WHAT THIS DOES AND DOES NOT COVER — measured, not assumed

`docs/production-readiness.md` used to claim an uncaught exception returned "a
raw traceback in the response". **That was wrong, and it was checked before
this module was written.** With `debug=False` (FastAPI's default, and what the
generated app uses), Starlette's ServerErrorMiddleware returns exactly
`Internal Server Error` as `text/plain`. No traceback, no exception message,
no paths. There was no information leak to close.

So this handler is NOT a leak fix. What it actually buys:

* **a correlatable id.** The bare 500 is untraceable: a caller reporting "it
  returned 500 at about 2pm" cannot be matched to a log line. Every failure
  now carries an `error_id` that appears in both the response and the log.
* **a consistent body.** `text/plain` for failures and JSON for everything
  else means a client needs two parsers. Errors are now JSON like the rest.
* **a named logger.** `native2py.<service>` can be raised or silenced without
  touching the rest of the process.

Two things it does not cover, both verified:

* **`debug=True` still leaks the full traceback, and this handler does not
  stop it** — Starlette checks `debug` *before* consulting the handler, so the
  debug response wins. The protection against that is not enabling debug in a
  deployed service; there is no code here that can override it.
* **Python-level failures only** — an f2py argument error, a ValueError out of
  a wrapper, a numpy conversion failure. Native code that segfaults, calls
  Fortran `STOP`, or hangs takes the worker process down with it and never
  reaches any Python handler. That is ROADMAP 2.2 (crash containment) and
  needs process isolation, not a try/except.
"""

from __future__ import annotations

# Imports the emitted block needs, on top of whatever the app module already
# imports. Both call sites splice these in rather than hard-coding them, so
# adding a dependency here cannot leave one generated app un-importable.
# `Request` is deliberately NOT here: both app modules already import from
# `fastapi` for `FastAPI`, and a second `from fastapi import ...` line would
# be a lint finding in generated code nobody is allowed to edit by hand. The
# call sites add `Request` to the import they already write.
ERROR_HANDLER_IMPORTS = (
    "import logging",
    "import os",
    "import uuid",
    "",
    "from fastapi.responses import JSONResponse",
)


def generate_error_handler(logger_name: str) -> str:
    """The handler block, to be appended after `app = FastAPI(...)`.

    `logger_name` names the logger so an operator can raise or silence one
    service's errors without touching the rest of the process.
    """
    return f'''

_LOG = logging.getLogger("native2py.{logger_name}")

# Off by default. A native routine's exception text can carry deck values,
# array contents or absolute paths from the build machine; none of that should
# leave the process just because something failed. Turn it on deliberately,
# in a non-production environment, when a caller needs to see why.
_DEBUG_ERRORS = os.environ.get("NATIVE2PY_DEBUG_ERRORS", "").lower() not in ("", "0", "false", "no")


@app.exception_handler(Exception)
async def _unhandled_native_error(request: Request, exc: Exception):
    """Give a failed request a JSON body and an id that matches the log line.

    Without this, Starlette answers `Internal Server Error` as plain text —
    safe, but untraceable and a second content type for clients to parse.

    HTTPException does NOT reach here — FastAPI handles it first — so the
    generated argument-validation errors keep their own status and detail.

    Caveats, both real: with `debug=True` Starlette returns its traceback
    response *instead* of this one, so do not enable debug on a deployed
    service; and a segfault, Fortran STOP or hang kills the worker process
    before any Python handler runs. See docs/production-readiness.md.
    """
    error_id = uuid.uuid4().hex[:12]
    # .exception() so the traceback reaches the log exactly once, keyed by the
    # same id the caller was given — that pairing is the whole point.
    _LOG.exception(
        "unhandled error %s on %s %s", error_id, request.method, request.url.path
    )
    body = {{"error": "internal server error", "error_id": error_id}}
    if _DEBUG_ERRORS:
        body["detail"] = f"{{type(exc).__name__}}: {{exc}}"
    return JSONResponse(status_code=500, content=body)
'''
