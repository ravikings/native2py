"""MCP (Model Context Protocol) server generator.

A third consumer of the same prefix-less `APIRouter` that `service.py` and the
gateway app already mount. Nothing here is generated per native symbol: FastMCP
reads the router's OpenAPI schema and derives one tool per route, so the tool
surface cannot drift from the HTTP surface — they are the same routes.

WHY THE MCP SERVER IS BUILT FROM A *BARE* APP

`FastMCP.from_fastapi()` does not introspect the app statically. It builds an
httpx client over an in-process `ASGITransport` and calls the app back over
HTTP for every tool invocation. That detail decides the whole design:

* The inner app carries the router and NOTHING ELSE — no `install_middleware`.
  If auth middleware were installed on the inner app, the MCP server would have
  to authenticate to *itself*, and FastMCP's own documented answer for that is
  a bearer token hardcoded into `httpx_client_kwargs` — a credential baked into
  generated source, in a file nobody is allowed to hand-edit. Instead the outer
  app (service.py or the gateway) mounts `mcp_app` and its existing middleware
  guards `/mcp` exactly as it guards every REST route. Verified: with API-key
  middleware on the outer app, `POST /mcp/` without a key returns 401.

* The `_NATIVE_LOCK` in router.py still applies. A tool call re-enters the same
  endpoint function in the same process, so COMMON-block serialisation is
  unaffected — MCP does not open a second, unsynchronised path into the native
  code. This is the first question a reader has, so the generated file says so.

WHY THE ROUTES CARRY `operation_id`

FastMCP names each tool after the route's OpenAPI `operationId`. FastAPI's
default is `<function>_<path>_<method>`, which turns `solution_gor` into
`solution_gor_endpoint_solution_gor_post`, and derives a title-cased
description from the function name when there is no docstring. Both were
measured against fastmcp 3.4.7 before this module was written. python_pkg_gen
therefore emits an explicit `operation_id=` and a docstring on every generated
route, which is also why the OpenAPI schema and `/docs` read better.

The operation_id is qualified by service name, and underscore runs in it are
collapsed. Both are forced: ids must be unique across an OpenAPI document (a
gateway mounts several services into one), and FastMCP TRUNCATES a tool name
at `__` — `svc__unexposed` becomes the tool `svc`. See
_EndpointNames.operation_id in python_pkg_gen and docs/mcp.md.

MOUNT PATH

`http_app(path="/")` mounted at `/mcp`, NOT `http_app()` mounted at `/mcp`.
FastMCP's default `streamable_http_path` is itself `/mcp`, so the latter serves
the endpoint at `/mcp/mcp`. The served path is `/mcp/`; `/mcp` answers 307 to
it, which MCP clients follow.
"""

from __future__ import annotations

from ..ir import is_valid_python_name

MCP_FILENAME = "mcp_server.py"

# The path the generated apps mount the MCP ASGI app at.
MCP_MOUNT_PATH = "/mcp"

# Not `mcp.py`: fastmcp itself imports the top-level `mcp` package, and while
# Python 3's absolute imports mean a sibling module could not actually shadow
# it, a package containing both `mcp` and a dependency named `mcp` is a trap
# for the next person reading it. The extra six characters are cheaper.

# Lines the generated app modules add to mount the MCP app. Emitted from here
# so service.py and the gateway app cannot drift — the same reason the error
# handler lives in error_gen.
MCP_APP_IMPORT = f"from .{MCP_FILENAME[:-3]} import mcp_app"


def mcp_mount_lines() -> str:
    """The mount call for an app that already took `lifespan=mcp_app.lifespan`."""
    return f'''
# The MCP view of these same endpoints, for an LLM client. Served at
# "{MCP_MOUNT_PATH}/"; "{MCP_MOUNT_PATH}" answers 307 to it. Auth, rate limiting and request
# ids come from the middleware installed above, because that attaches to THIS
# app and the mount sits underneath it.
app.mount("{MCP_MOUNT_PATH}", mcp_app)'''


def generate_mcp_py(name: str, service_names: list[str] | None = None) -> str:
    """The `mcp_server.py` for one service, or for a composed gateway.

    `service_names` is None for a standalone service (its own router, no
    prefix) and a list for a gateway (each service's router under `/<name>`,
    matching exactly what the gateway app mounts).
    """
    if service_names is None:
        imports = "from .router import router"
        includes = "_inner.include_router(router)"
        scope = "this service's"
    else:
        for service_name in service_names:
            if not is_valid_python_name(service_name):
                raise ValueError(
                    f"Service name '{service_name}' cannot be imported from an MCP "
                    "server: it is not a valid Python module name (a keyword, or "
                    "not an identifier). Rename the service."
                )
        imports = "\n".join(
            f"from {s}.router import router as {s}_router" for s in service_names
        )
        includes = "\n".join(
            f'_inner.include_router({s}_router, prefix="/{s}")' for s in service_names
        )
        scope = "every mounted service's"

    return f'''# Generated by nativegate. Do not edit by hand — re-run `ngate generate`.
"""MCP server exposing {scope} endpoints as tools for an LLM client.

Not a second implementation of anything: FastMCP derives one tool per route
from the router's OpenAPI schema, so the tool surface and the HTTP surface are
the same routes and cannot drift. Tool names are the routes' `operation_id`
(the service name plus the native symbol name, so ids stay unique when several
services are composed) and tool descriptions are the endpoints' docstrings.

The native call still runs under router.py's `_NATIVE_LOCK`: a tool call
re-enters the same endpoint function in the same process, so MCP does NOT open
a second, unsynchronised path into COMMON-block state.
"""
import asyncio
import time
import uuid

from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware, MiddlewareContext

from .middleware import log_call

{imports}

# Deliberately bare — the router and nothing else. `FastMCP.from_fastapi` calls
# this app back over an in-process ASGI transport for every tool invocation, so
# installing auth middleware HERE would mean the server authenticating to
# itself with a credential hardcoded in generated source. Auth belongs on the
# outer app that mounts `mcp_app`, where it already guards the REST routes.
_inner = FastAPI(title="{name}")
{includes}

mcp = FastMCP.from_fastapi(app=_inner, name="{name}")


class _AccessLogMiddleware(Middleware):
    """Logs each tool call with its real name, not the outer app's generic
    "POST /mcp/" — the outer HTTP middleware sees only the JSON-RPC envelope,
    the tool name is inside the body, and this hook is where FastMCP has
    already parsed it out."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        # Pessimistic until the call demonstrably succeeds: an audit trail a
        # signed evidence pack rests on must never claim a 200 for a call that
        # did not finish. Any exception that is not a cancellation propagates
        # uncaught and leaves this value standing, so no future failure mode
        # can escape by simply not having a clause here.
        status = 500
        try:
            result = await call_next(context)
        except asyncio.CancelledError:
            # CancelledError derives from BaseException, so `except Exception`
            # never saw it: a client disconnecting mid-call used to record a
            # completed 200. Distinct from 500 because an abandoned call is not
            # a failed one — 499 is the same "client went away" sense nginx
            # gives it. Re-raised, never swallowed: swallowing a cancellation
            # breaks the structured-concurrency contract above us.
            status = 499
            raise
        else:
            # A tool can report failure by RETURNING a result flagged
            # `is_error` (mapped to CallToolResult.isError on the wire) instead
            # of raising, and that path never touches an except clause.
            status = 500 if getattr(result, "is_error", False) else 200
            return result
        finally:
            log_call(
                kind="mcp",
                request_id=request_id,
                # getattr, not attribute access: this runs in a `finally`
                # while an exception may be unwinding, and an AttributeError
                # raised here would replace the real tool failure with a
                # confusing error from the logging hook. FastMCP's own
                # timing middleware hedges the same way.
                tool=getattr(context.message, "name", "unknown"),
                status=status,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                service_name="{name}",
            )


mcp.add_middleware(_AccessLogMiddleware())

# path="/" because the app that mounts this does so at "/mcp", and FastMCP's
# own default path is ALSO "/mcp" — passing neither serves the endpoint at
# "/mcp/mcp". The app mounting this MUST take `lifespan=mcp_app.lifespan`:
# FastMCP's session manager starts there, and without it every tool call fails
# at runtime with "task group was not initialized".
mcp_app = mcp.http_app(path="/")
'''


def generate_mcp_smoke_test(service_name: str) -> str:
    """A test that the generated MCP server lists a tool per route and calls one.

    Asserts the two properties that silently degrade rather than fail loudly:
    tool names are the native symbol names (not FastAPI's
    `<fn>_<path>_<method>` default), and every tool carries a description.
    An unnamed, undescribed tool still "works" — it just cannot be used
    correctly by the model that is the entire point of this interface.
    """
    return f'''# Generated by nativegate. Do not edit by hand — re-run `ngate generate`.
"""The MCP view of this service exposes usable tools."""
import pytest

fastmcp = pytest.importorskip("fastmcp")

from {service_name}.mcp_server import mcp  # noqa: E402
from {service_name}.router import router  # noqa: E402


def _post_routes():
    return [
        route
        for route in router.routes
        if "POST" in getattr(route, "methods", set())
    ]


@pytest.mark.anyio
async def test_every_post_route_becomes_a_named_tool():
    async with fastmcp.Client(mcp) as client:
        tools = {{tool.name for tool in await client.list_tools()}}

    # The tool name is the route's operation_id, which is the native symbol
    # name. If this fails with names like "foo_endpoint_foo_post", the
    # operation_id= on the generated route decorator was lost — the tools
    # still work, but the model sees mangled names.
    for route in _post_routes():
        assert route.operation_id in tools, (
            f"route {{route.path}} produced no tool named "
            f"{{route.operation_id!r}}; got {{sorted(tools)}}"
        )


@pytest.mark.anyio
async def test_every_tool_has_a_description():
    async with fastmcp.Client(mcp) as client:
        undescribed = [t.name for t in await client.list_tools() if not t.description]
    # FastMCP falls back to a title-cased function name when a route has no
    # docstring, so an empty description means the generated docstring is gone.
    assert not undescribed, f"tools with no description: {{undescribed}}"


@pytest.fixture
def anyio_backend():
    return "asyncio"
'''
