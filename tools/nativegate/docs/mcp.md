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

The text is **the native source's own documentation**, recovered by the
parsers: a Doxygen or `///` block over a C++ declaration, the `C`/`*`/`!`
comment header on a Fortran routine. nativegate's own one-line statement of
what the endpoint does is appended, because a native comment rarely names the
symbol it belongs to:

```
TOOL petro_api_last_error
     Last error code raised by the legacy layer. Clears the state.

     Call the native routine `last_error`.
```

That "Clears the state" is the point of the whole feature — it is a side
effect a model must know about before calling, and it exists only in the deck.

Details worth knowing:

- **It is verbatim.** nativegate does not paraphrase, summarise or tidy a
  routine's contract; a plausible-sounding paraphrase of a numerical routine is
  worse than no description. Legacy headers do carry stale comments, and
  passing them through unchanged keeps the staleness attributable to the
  source.
- **Doxygen structure is kept as prose.** `@param`/`@return` lines survive
  rather than being parsed into fields — a model reads them fine.
- **Separator banners are dropped** (`C=====`, `C*****`), but a dated rule with
  words in it is kept. A header longer than 30 lines is truncated: past that it
  is a change log, and pasting one into a tool description hurts the model
  reading it.
- **A file banner can become the first routine's description.** Only the first
  routine in a deck can pick one up, which is slightly arbitrary, and it is
  deliberate: `simcor.f`'s banner carries "SIMINI MUST BE CALLED AFTER PVTINI
  AND KRINI. THERE IS NO CHECK.", which is exactly what a caller needs.
- **The C++ regex fallback yields no documentation.** It has no reliable way to
  tell which declaration a comment belongs to; Clang's attachment rules do that
  properly, so only the AST backend populates it.

### Comment text cannot break the generated service

A comment is unreviewed text from a header or a deck, and it lands inside a
Python docstring. A `"""` in it would close that docstring early and turn the
rest of the comment into executable code; a trailing backslash would escape the
closing quotes. Both occur in real sources — `\` continues a macro line, 1988
programmers drew boxes — so this is a live injection channel.

The defence is entirely in the generator, which is the layer that knows what
quoting context it is emitting into. Plain prose becomes a readable
`"""..."""` block; anything carrying a quote, a backslash or a control
character is emitted as `repr(text)`, which Python itself guarantees is
correctly encoded. The parsers deliberately do **not** pre-sanitise, because
neutralising a `"""` there would silently rewrite documentation for every other
consumer of the IR too.

`tests/test_endpoint_docstrings.py` pins this by *executing* the generated
module, not merely compiling it — source with an escaped-out docstring still
parses; the question is whether the text can run.

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

## Access logging

Every call — REST or MCP — writes one JSON line to stdout, via
`nativegate.<service>.access`:

```json
{"ts": "2026-01-01T00:00:00+00:00", "service": "petro_api", "kind": "rest", "method": "POST", "path": "/solution_gor", "tool": null, "status": 200, "duration_ms": 4.2, "request_id": "a1b2c3d4e5f6", "project": "petro", "build_id": 417, "image_digest": "sha256:abc123"}
{"ts": "2026-01-01T00:00:01+00:00", "service": "petro_api", "kind": "mcp", "method": null, "path": null, "tool": "petro_api_solution_gor", "status": 200, "duration_ms": 5.1, "request_id": "6f5e4d3c2b1a", "project": "petro", "build_id": 417, "image_digest": "sha256:abc123"}
```

`project`, `build_id` and `image_digest` are provenance — which project, which
build, which image answered the call. They come from `NATIVEGATE_PROJECT`,
`NATIVEGATE_BUILD_ID` and `NATIVEGATE_IMAGE_DIGEST`, set by whatever runs the
container, and are read per call rather than at import, since a container's
environment is set at `docker run`. All three are `null` when unset — never
omitted — and `build_id` is an integer (`null` if it does not parse as one).

There are three kinds, one per layer a call can pass through:

| `kind` | Emitted by | One row per |
| --- | --- | --- |
| `rest` | `middleware.py`'s access-log layer | HTTP request to a REST route |
| `mcp_http` | the same layer | HTTP request to the `/mcp` mount |
| `mcp` | `mcp_server.py`'s FastMCP `Middleware` | tool dispatch |

`method` and `path` are set on `rest` and `mcp_http` rows; `tool` is set on
`mcp` rows. Only the `on_call_tool` hook sees the parsed tool name — it is
inside the JSON-RPC body, which plain HTTP middleware does not read.

**A tool call produces both an `mcp_http` row and an `mcp` row.** That is not
double counting: they are two events at two layers, and both are needed. The
transport row is the only record of an MCP request that never reaches a tool —
`initialize`, `tools/list`, a malformed JSON-RPC body, a request rejected with
401, 413 or 429 before any tool ran — and the tool row is the only record
carrying the tool
name and the outcome of the native call. Filter on `kind` to pick a layer;
never sum the two. They are deliberately not correlated by a shared id: the
tool call re-enters through an in-process ASGI transport and may run in a
different task, so a contextvar set in the hook would not reliably reach it.

`/mcp` is matched at the mount boundary (exactly `/mcp`, or `/mcp/…`), so a
sibling route such as `/mcpconfig` stays `kind: "rest"`.

An `mcp` row's `status` is `200` only when the tool returned a result that is
not flagged `isError`. A tool that raises logs `500`; a tool that returns an
error result logs `500`; a call cancelled by a client disconnecting mid-flight
logs `499`, since an abandoned call is neither a success nor a crash.

Every layer calls the same `log_call()` helper in `middleware.py`, so the
schema is one place, not three.

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
