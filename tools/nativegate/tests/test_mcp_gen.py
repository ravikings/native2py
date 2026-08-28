"""The generated MCP server, exercised as a real FastMCP server.

The MCP endpoint is not a second implementation of anything: FastMCP derives
one tool per route from the router's OpenAPI schema, so the tool surface and
the HTTP surface are the same routes. That is the property worth pinning, and
it is only observable by actually building the server and listing its tools —
a substring check over the generated source would pass even if FastMCP named
every tool something useless.

Three failure modes here are silent rather than loud, which is why each gets
its own test:

* **tool names.** FastMCP names a tool after the route's OpenAPI operationId,
  and FastAPI's default is `<function>_<path>_<method>`. Without an explicit
  `operation_id=`, `solution_gor` reaches the model as
  `solution_gor_endpoint_solution_gor_post`. The tools still *work*; they are
  just much harder for a model to use, and nothing warns.
* **tool descriptions.** With no docstring on the endpoint, FastMCP falls back
  to a title-cased function name ("Solution Gor Endpoint"), which says nothing
  about what the routine does. Again: works, useless, silent.
* **the lifespan.** FastMCP's streamable-HTTP session manager starts in
  `mcp_app.lifespan`. Mounting the app without adopting it fails every tool
  call at REQUEST time, not at startup, so a service can boot clean and be
  broken. Verified here against the real generated `service.py` source.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from nativegate.generators import gateway_gen, mcp_gen, python_pkg_gen
from nativegate.ir import FunctionDef, ModuleIR, Parameter

fastmcp = pytest.importorskip("fastmcp")

from fastapi import FastAPI  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


def fortran_module(**kwargs) -> ModuleIR:
    kwargs.setdefault("name", "petro")
    kwargs.setdefault("language", "fortran")
    kwargs.setdefault("source_file", "petro.f90")
    return ModuleIR(**kwargs)


PVT_IR = fortran_module(
    functions=[
        FunctionDef(
            name="solution_gor",
            parameters=[Parameter(name="pressure", type="float")],
            returns="float",
        ),
        FunctionDef(
            name="pvt_set_fluid",
            parameters=[Parameter(name="api_gravity", type="float")],
            returns="None",
            is_subroutine=True,
        ),
    ]
)


def _router_from(ir: ModuleIR, service: str, native: dict):
    """Exec a generated router with its native symbols injected.

    `from . import solution_gor, ...` is a relative import with no package to
    resolve against, so the names go straight into the module namespace. The
    real APIRouter, the real Pydantic models and the real lock are used — the
    router this returns is exactly what a deployed service mounts.
    """
    code = python_pkg_gen.generate_router_py(ir, service)
    stripped = "\n".join(
        line for line in code.splitlines() if not line.startswith("from . import")
    )
    namespace: dict = dict(native)
    exec(compile(stripped, "router.py", "exec"), namespace)  # noqa: S102
    return namespace["router"]


def _mcp_for(router, name="petro"):
    """The same construction the generated mcp_server.py performs."""
    inner = FastAPI(title=name)
    inner.include_router(router)
    return fastmcp.FastMCP.from_fastapi(app=inner, name=name)


NATIVE = {
    "solution_gor": lambda pressure: pressure * 2,
    "pvt_set_fluid": lambda api_gravity: None,
}


# --- the tool surface is the route surface --------------------------------


@pytest.mark.anyio
async def test_each_route_becomes_a_tool_named_after_the_native_symbol():
    router = _router_from(PVT_IR, "petro", NATIVE)

    async with fastmcp.Client(_mcp_for(router)) as client:
        names = {tool.name for tool in await client.list_tools()}

    # The service name plus the native spelling, NOT FastAPI's
    # `<fn>_<path>_<method>` default. This is what `operation_id=` on the
    # generated decorator buys; the service prefix keeps ids unique across a
    # gateway's OpenAPI document (see _EndpointNames.operation_id).
    assert "petro_solution_gor" in names
    assert "petro_pvt_set_fluid" in names
    assert not [n for n in names if n.endswith("_post")], (
        f"tool names fell back to FastAPI's default shape: {sorted(names)}"
    )


@pytest.mark.anyio
async def test_every_tool_carries_a_description():
    router = _router_from(PVT_IR, "petro", NATIVE)

    async with fastmcp.Client(_mcp_for(router)) as client:
        tools = await client.list_tools()

    for tool in tools:
        assert tool.description, f"tool {tool.name} has no description"
    # The description comes from the endpoint docstring, which names the
    # native routine — a title-cased function name would not.
    by_name = {tool.name: tool.description for tool in tools}
    assert "solution_gor" in by_name["petro_solution_gor"]


@pytest.mark.anyio
async def test_a_tool_call_reaches_the_native_routine_and_returns_its_result():
    calls: list[float] = []

    def solution_gor(pressure):
        calls.append(pressure)
        return pressure * 2

    router = _router_from(PVT_IR, "petro", {**NATIVE, "solution_gor": solution_gor})

    async with fastmcp.Client(_mcp_for(router)) as client:
        result = await client.call_tool("petro_solution_gor", {"pressure": 21.0})

    assert calls == [21.0]
    assert result.data == {"result": 42.0}


@pytest.mark.anyio
async def test_a_gateway_exposes_every_mounted_service_under_one_mcp_server():
    """The composed topology's "one URL" promise covers the LLM interface too."""
    inner = FastAPI(title="platform-api")
    inner.include_router(_router_from(PVT_IR, "pvt", NATIVE), prefix="/pvt")
    inner.include_router(
        _router_from(
            fortran_module(
                name="reservoir",
                functions=[
                    FunctionDef(
                        name="pore_volume",
                        parameters=[Parameter(name="h", type="float")],
                        returns="float",
                    )
                ],
            ),
            "reservoir",
            {"pore_volume": lambda h: h},
        ),
        prefix="/reservoir",
    )
    mcp = fastmcp.FastMCP.from_fastapi(app=inner, name="platform-api")

    async with fastmcp.Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}

    # Each tool is qualified by its own service, so two services exposing the
    # same native symbol stay separately addressable — and the model can tell
    # which service it is calling.
    assert {"pvt_solution_gor", "reservoir_pore_volume"} <= names


def test_a_gateway_has_no_duplicate_operation_ids(recwarn):
    """Every service carries `/_unexposed`, so unqualified ids collide.

    OperationIds must be unique across an OpenAPI document. Before the service
    qualification, a gateway mounting two services emitted two routes with
    operationId `_unexposed`: FastAPI warned, the OpenAPI document was invalid,
    and only one of the two reachable `_unexposed` routes became an MCP tool —
    so one service's refusal list was silently unreadable through MCP.
    """
    inner = FastAPI(title="platform-api")
    inner.include_router(_router_from(PVT_IR, "pvt", NATIVE), prefix="/pvt")
    inner.include_router(
        _router_from(PVT_IR, "reservoir", NATIVE), prefix="/reservoir"
    )

    ids = [
        operation_id
        for route in inner.routes
        if (operation_id := getattr(route, "operation_id", None))
    ]
    assert len(ids) == len(set(ids)), f"duplicate operationIds: {sorted(ids)}"

    # Generating the schema is what surfaces a duplicate, so do it and check.
    inner.openapi()
    duplicates = [
        w for w in recwarn if "Duplicate Operation ID" in str(w.message)
    ]
    assert not duplicates, [str(w.message) for w in duplicates]


@pytest.mark.anyio
async def test_double_underscore_symbols_do_not_collapse_onto_one_tool():
    """FastMCP truncates a tool name at `__`, which can merge two symbols.

    Measured against fastmcp 3.4.7: operationId `svc__unexposed` yields the
    tool `svc`, and `svc_a__b` yields `svc_a`. Two native symbols differing
    only after a `__` would therefore land on the same tool name and one would
    be silently uncallable — and `<service>_` + `_unexposed` would name the
    introspection tool after the service itself. The generator collapses
    underscore runs and de-duplicates, so every route keeps a distinct tool.
    """
    ir = fortran_module(
        name="petro",
        functions=[
            FunctionDef(name="flash__a", parameters=[], returns="float"),
            FunctionDef(name="flash_a", parameters=[], returns="float"),
        ],
    )
    router = _router_from(ir, "petro", {"flash__a": lambda: 1.0, "flash_a": lambda: 2.0})

    async with fastmcp.Client(_mcp_for(router)) as client:
        names = sorted(tool.name for tool in await client.list_tools())

    # Three routes (two functions plus /_unexposed), three distinct tools.
    assert len(names) == len(set(names)) == 3, names
    # The introspection tool is not named after the service.
    assert "petro" not in names
    assert "petro_unexposed" in names


# --- the generated source itself ------------------------------------------


def test_generated_service_mcp_module_compiles_and_wires_the_router():
    source = mcp_gen.generate_mcp_py("petro")

    compile(source, "mcp_server.py", "exec")
    assert "from .router import router" in source
    assert "_inner.include_router(router)" in source
    # Bare by design: middleware here would make the server authenticate to
    # itself, because from_fastapi calls this app back over ASGI.
    assert "install_middleware" not in source


def test_generated_gateway_mcp_module_mounts_each_service_under_its_prefix():
    source = mcp_gen.generate_mcp_py("platform-api", ["demo", "calculator"])

    compile(source, "mcp_server.py", "exec")
    assert "from demo.router import router as demo_router" in source
    assert '_inner.include_router(demo_router, prefix="/demo")' in source
    assert '_inner.include_router(calculator_router, prefix="/calculator")' in source


@pytest.mark.anyio
async def test_mcp_tool_calls_are_logged_as_json_with_the_real_tool_name(tmp_path, monkeypatch, capsys):
    """The FastMCP middleware in mcp_server.py logs the parsed tool name.

    Built the same way the generated file constructs it: `middleware.py` next
    to a hand-assembled `mcp_server.py` module executing the same source
    `mcp_gen.generate_mcp_py` would emit, with the router wired in directly
    rather than imported (there is no package on disk here to import from).
    """
    from nativegate.generators import middleware_gen

    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "middleware.py").write_text(
        middleware_gen.generate_middleware_py("petro", "none")
    )

    router = _router_from(PVT_IR, "petro", NATIVE)

    namespace: dict = {"router": router}
    source = mcp_gen.generate_mcp_py("petro").replace(
        "from .router import router", ""
    ).replace("from .middleware import log_call", "from middleware import log_call")
    exec(compile(source, "mcp_server.py", "exec"), namespace)  # noqa: S102

    async with fastmcp.Client(namespace["mcp"]) as client:
        await client.call_tool("petro_solution_gor", {"pressure": 21.0})

    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    rows = [json.loads(l) for l in lines]
    mcp_rows = [r for r in rows if r["kind"] == "mcp"]

    assert mcp_rows, f"no kind=mcp row emitted; got {rows}"
    row = mcp_rows[-1]
    assert row["tool"] == "petro_solution_gor"
    assert row["method"] is None
    assert row["path"] is None
    assert row["status"] == 200
    assert row["service"] == "petro"
    assert isinstance(row["duration_ms"], (int, float))
    assert row["request_id"]
    assert row["project"] is None
    assert row["build_id"] is None
    assert row["image_digest"] is None


@pytest.mark.anyio
async def test_mcp_rows_carry_the_same_provenance_fields_as_rest_rows(
    tmp_path, monkeypatch, capsys
):
    """Provenance is resolved in one place, so `kind: "mcp"` gets it too."""
    from nativegate.generators import middleware_gen

    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "middleware.py").write_text(
        middleware_gen.generate_middleware_py("petro", "none")
    )

    router = _router_from(PVT_IR, "petro", NATIVE)

    namespace: dict = {"router": router}
    source = mcp_gen.generate_mcp_py("petro").replace(
        "from .router import router", ""
    ).replace("from .middleware import log_call", "from middleware import log_call")
    exec(compile(source, "mcp_server.py", "exec"), namespace)  # noqa: S102

    monkeypatch.setenv(middleware_gen.ENV_PROJECT, "petro")
    monkeypatch.setenv(middleware_gen.ENV_BUILD_ID, "417")
    monkeypatch.setenv(middleware_gen.ENV_IMAGE_DIGEST, "sha256:abc123")

    async with fastmcp.Client(namespace["mcp"]) as client:
        await client.call_tool("petro_solution_gor", {"pressure": 21.0})

    rows = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    mcp_rows = [r for r in rows if r["kind"] == "mcp"]

    assert mcp_rows, f"no kind=mcp row emitted; got {rows}"
    row = mcp_rows[-1]
    assert row["project"] == "petro"
    assert row["build_id"] == 417
    assert row["image_digest"] == "sha256:abc123"


# --- the status an mcp row records ----------------------------------------
#
# A signed evidence pack is built from these rows, so a status of 200 has to
# mean the call actually succeeded. Three ways a call can fail to succeed, and
# only one of them used to be recorded honestly.


def _generated_mcp_module(tmp_path, monkeypatch, service="petro"):
    """The real generated mcp_server.py, executed with its imports rewired."""
    from nativegate.generators import middleware_gen

    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / "middleware.py").write_text(
        middleware_gen.generate_middleware_py(service, "none")
    )

    namespace: dict = {"router": _router_from(PVT_IR, service, NATIVE)}
    source = (
        mcp_gen.generate_mcp_py(service)
        .replace("from .router import router", "")
        .replace("from .middleware import log_call", "from middleware import log_call")
    )
    exec(compile(source, "mcp_server.py", "exec"), namespace)  # noqa: S102
    return namespace


def _last_mcp_row(capsys, tool):
    rows = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    matching = [r for r in rows if r["kind"] == "mcp" and r["tool"] == tool]
    assert matching, f"no kind=mcp row for {tool!r}; got {rows}"
    return matching[-1]


@pytest.mark.anyio
async def test_a_tool_that_raises_is_not_recorded_as_a_success(
    tmp_path, monkeypatch, capsys
):
    namespace = _generated_mcp_module(tmp_path, monkeypatch)
    mcp = namespace["mcp"]

    @mcp.tool
    def boom() -> float:
        """Always fails."""
        raise RuntimeError("native routine blew up")

    async with fastmcp.Client(mcp) as client:
        with pytest.raises(Exception):
            await client.call_tool("boom", {})

    assert _last_mcp_row(capsys, "boom")["status"] == 500


@pytest.mark.anyio
async def test_a_tool_returning_an_error_result_is_not_recorded_as_a_success(
    tmp_path, monkeypatch, capsys
):
    # This one never touches an except clause: the tool reports failure by
    # RETURNING a result flagged isError, so only inspecting the returned
    # ToolResult catches it. It used to log 200.
    from fastmcp.tools.base import ToolResult

    namespace = _generated_mcp_module(tmp_path, monkeypatch)
    mcp = namespace["mcp"]

    @mcp.tool
    def refused() -> str:
        """Reports failure in-band rather than raising."""
        return ToolResult(content="the native routine refused", is_error=True)

    async with fastmcp.Client(mcp) as client:
        with pytest.raises(Exception):
            await client.call_tool("refused", {})

    assert _last_mcp_row(capsys, "refused")["status"] == 500


@pytest.mark.anyio
async def test_a_cancelled_tool_call_is_recorded_as_neither_success_nor_crash(
    tmp_path, monkeypatch, capsys
):
    # CancelledError derives from BaseException, so `except Exception` never
    # saw it and a client disconnecting mid-call logged a completed 200. 499
    # rather than 500 because an abandoned call is not a failed one, and the
    # exception must propagate — swallowing a cancellation breaks the
    # structured-concurrency contract above this hook.
    namespace = _generated_mcp_module(tmp_path, monkeypatch)
    mcp = namespace["mcp"]

    @mcp.tool
    def abandoned() -> float:
        """Simulates the client vanishing mid-call."""
        raise asyncio.CancelledError()

    async with fastmcp.Client(mcp) as client:
        with pytest.raises(BaseException):
            await client.call_tool("abandoned", {})

    assert _last_mcp_row(capsys, "abandoned")["status"] == 499


def test_the_cancellation_path_is_not_caught_as_a_plain_exception():
    # Pins the mechanism, because the bug is invisible in a passing call: a
    # bare `except Exception` silently reinstates it.
    source = mcp_gen.generate_mcp_py("petro")

    assert "except asyncio.CancelledError:" in source
    assert "except Exception:" not in source


def test_the_mcp_app_is_built_at_the_root_path_not_at_the_default():
    """`http_app()` would serve the endpoint at /mcp/mcp once mounted at /mcp.

    FastMCP's own default `streamable_http_path` is "/mcp", and the generated
    apps mount the ASGI app at "/mcp" as well, so the path has to be reset to
    "/" or the endpoint moves and every configured client 404s.
    """
    source = mcp_gen.generate_mcp_py("petro")
    assert 'mcp.http_app(path="/")' in source


@pytest.mark.parametrize(
    "source",
    [
        python_pkg_gen.generate_service_py("demo"),
        gateway_gen.generate_gateway_app("platform-api", ["demo"]),
    ],
    ids=["standalone-service", "composed-gateway"],
)
def test_both_app_topologies_adopt_the_mcp_lifespan_and_mount_it(source):
    # Both, because the mount is per app: a router mounted into the gateway
    # runs under the GATEWAY's app, so wiring only service.py would leave the
    # composed deployment — the one more likely to be internet-facing —
    # serving an MCP endpoint that fails on every call.
    compile(source, "app.py", "exec")
    assert "lifespan=mcp_app.lifespan" in source, (
        "without the lifespan every tool call fails at request time"
    )
    assert 'app.mount("/mcp", mcp_app)' in source
    assert mcp_gen.MCP_APP_IMPORT in source


def test_the_mcp_mount_sits_behind_the_apps_middleware():
    """Auth guards /mcp because the mount is under the app that installs it.

    This is the whole reason the inner app is bare. Order matters: middleware
    has to be installed before the mount line for the intent to be legible,
    and the mount must not appear before install_middleware.
    """
    source = python_pkg_gen.generate_service_py("demo")
    assert source.index("install_middleware(app") < source.index('app.mount("/mcp"')


def test_a_service_named_mcp_is_refused_rather_than_silently_shadowed():
    # Starlette resolves the mount before the router, so a service mounted at
    # /mcp would simply stop answering — with no error at startup.
    with pytest.raises(ValueError, match="collides with the gateway's MCP endpoint"):
        gateway_gen.generate_gateway_app("platform-api", ["mcp"])


def test_mcp_generation_is_byte_identical_when_repeated():
    """Generated output is compared by CI; iteration order must not leak in."""
    services = ["demo", "calculator", "pvt"]
    for build in (
        lambda: mcp_gen.generate_mcp_py("petro"),
        lambda: mcp_gen.generate_mcp_py("gw", services),
        lambda: mcp_gen.generate_mcp_smoke_test("petro"),
    ):
        assert build() == build()


def test_the_generated_smoke_test_is_valid_python():
    compile(mcp_gen.generate_mcp_smoke_test("petro"), "test_mcp.py", "exec")
