# The MCP endpoint

Every generated service and every composed gateway serves an
[MCP](https://modelcontextprotocol.io) endpoint at `/mcp`, alongside its REST
routes. Point Claude, or any MCP client, at it and the native routines become
callable tools.

There is nothing to switch on. `ngate generate` writes it, `service.py` mounts
it, and `fastmcp` is a dependency of the generated wheel.

```bash
ngate generate petro_api
ngate serve petro_api
# REST:  POST http://127.0.0.1:8000/solution_gor
# MCP:        http://127.0.0.1:8000/mcp
```

## It is the same routes, not a second implementation

FastMCP derives one tool per route from the router's OpenAPI schema. The
generated `mcp_server.py` builds a bare FastAPI app carrying the same
prefix-less `APIRouter` that `service.py` mounts, and hands it to
`FastMCP.from_fastapi()`:

```python
_inner = FastAPI(title="petro_api")
_inner.include_router(router)

mcp = FastMCP.from_fastapi(app=_inner, name="petro_api")
mcp_app = mcp.http_app(path="/")
```

So the tool surface and the HTTP surface cannot drift — they are the same
routes, and a routine added to the header appears in both on the next
`generate`. The router is the third consumer of the same object that
`service.py` and `gateway_gen.py` already share.

A tool call re-enters the same endpoint function in the same process, which
means **`_NATIVE_LOCK` still applies**. MCP does not open a second,
unsynchronised path into Fortran COMMON-block state. Everything
[the shared-state contract](production-readiness.md) says about serialisation
holds for MCP calls exactly as it does for HTTP calls.

## Tool names

The tool name is the route's OpenAPI `operationId`, which nativegate sets to
the service name plus the native symbol name:

| native symbol | route | MCP tool |
| --- | --- | --- |
| `solution_gor` | `POST /solution_gor` | `petro_api_solution_gor` |
| `pvt_set_fluid` | `POST /pvt_set_fluid` | `petro_api_pvt_set_fluid` |
| — | `GET /_unexposed` | `petro_api_unexposed` |

The service prefix is not decoration. OperationIds must be unique across an
OpenAPI *document*, and the same router is served by two documents: the
service's own app, and a gateway mounting several services at once. Without
the prefix, every service's `/_unexposed` collides in the gateway. It also
tells the model which service it is calling.

Two details are worth knowing because they are silent rather than loud, and
both are pinned by `tests/test_mcp_gen.py`:

- **Without an explicit `operation_id`**, FastAPI's default is
  `<function>_<path>_<method>`, so `solution_gor` would reach the model as
  `solution_gor_endpoint_solution_gor_post`. It works; it is just much harder
  to use.
- **FastMCP truncates a tool name at a double underscore** and strips leading
  underscores (measured against fastmcp 3.4.7: `svc__unexposed` becomes the
  tool `svc`). Two native symbols differing only after a `__` would land on the
  same tool name and one would become uncallable, so the generator collapses
  underscore runs and de-duplicates the result.

## Tool descriptions

Each generated endpoint carries a docstring, which FastAPI publishes as the
route's OpenAPI `description` and FastMCP hands the model as the tool
description. Without one, FastMCP falls back to a title-cased function name
("Solution Gor Endpoint"), which says nothing about the routine.

Today those descriptions are truthful skeletons — `Call the native routine
solution_gor.` — because **nativegate does not yet carry doc comments through
the parsers**; the IR has no field to put them in. Recovering the real
documentation from the header or the Fortran deck is tracked in the roadmap.
Until then, a service that wants better tool descriptions should improve the
native source's own documentation and wait for that work, rather than editing
the generated file, which `generate` will overwrite.

## Auth

The MCP app is mounted **under** the service's app, so the middleware
`service.py` installs guards `/mcp` exactly as it guards every REST route.
With `api.auth: api_key`, a request to `/mcp/` without a key gets a 401.

This is why the inner app is bare. `FastMCP.from_fastapi()` does not
introspect the app statically — it builds an httpx client over an in-process
ASGI transport and calls the app back for every tool invocation. Installing
auth middleware on the *inner* app would make the MCP server authenticate to
itself, and FastMCP's documented answer for that is a bearer token in
`httpx_client_kwargs` — a credential baked into generated source. Keeping the
inner app bare avoids the problem rather than working around it.

MCP clients that need a key send it as a normal header:

```json
{
  "mcpServers": {
    "petro": {
      "url": "http://localhost:8000/mcp",
      "headers": { "X-API-Key": "..." }
    }
  }
}
```

## Gateways

A composed gateway serves **one** MCP endpoint covering every mounted service —
the "one URL" promise applies to the LLM interface too:

```bash
ngate gateway platform-api --service pvt --service reservoir
# /pvt/*, /reservoir/*, and one /mcp with every service's tools
```

Tools stay qualified by service (`pvt_solution_gor`, `reservoir_pore_volume`),
so two services exposing the same native symbol remain separately addressable.

A service named `mcp` is refused, because mounting it at `/mcp` would shadow
the gateway's MCP endpoint — Starlette resolves the mount before the router, so
the service would simply stop answering with no error at startup.

## Notes and limits

- **The endpoint is `/mcp/`**; `/mcp` answers `307` to it, which MCP clients
  follow. FastMCP's own default path is also `/mcp`, so the generated code
  passes `http_app(path="/")` — otherwise the endpoint would land at
  `/mcp/mcp`.
- **`lifespan=mcp_app.lifespan` is required** and the generated apps set it.
  FastMCP's streamable-HTTP session manager starts in that lifespan; an app
  that mounts `mcp_app` without adopting it fails every tool call at *request*
  time with "task group was not initialized" — it starts up perfectly clean.
- **Crash containment is unchanged.** A segfault or Fortran `STOP` takes the
  worker down whether the call arrived over MCP or HTTP. See
  [production readiness](production-readiness.md).
- **An MCP client is an untrusted caller like any other.** The array-size caps,
  body-size limits and rate limiting in `middleware.py` apply, because the
  mount sits underneath them.
