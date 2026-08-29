"""The generated middleware: auth, limits, request identity, draining.

Every test here builds a REAL app from the generated source and makes real
requests. Asserting on the generated text would prove the strings are present,
which is not the same claim as "an unauthenticated caller is refused".
"""

import gzip
import importlib
import json
import os
import signal
import sys

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from nativegate.config import ApiConfig, ConfigError, ServiceConfig
from nativegate.generators import middleware_gen


@pytest.fixture
def load_middleware(tmp_path, monkeypatch):
    """Write a generated middleware.py, import it, and hand back the module.

    Imported as a real module rather than exec'd into a dict because the
    generated code uses module-level state (the drain flag) and `global`, which
    only behaves correctly in a genuine module namespace.
    """
    monkeypatch.syspath_prepend(str(tmp_path))
    counter = {"n": 0}

    def _load(auth="none", **env):
        for key in (
            middleware_gen.ENV_API_KEYS,
            middleware_gen.ENV_RATE_LIMIT,
            middleware_gen.ENV_MAX_BODY,
            middleware_gen.ENV_PROJECT,
            middleware_gen.ENV_BUILD_ID,
            middleware_gen.ENV_IMAGE_DIGEST,
        ):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        counter["n"] += 1
        name = f"generated_mw_{counter['n']}"
        (tmp_path / f"{name}.py").write_text(
            middleware_gen.generate_middleware_py("demo", auth)
        )
        sys.modules.pop(name, None)
        return importlib.import_module(name)

    yield _load


def _app_with(module, auth_paths=True):
    app = FastAPI()

    @app.post("/compute")
    def compute(payload: dict):
        return {"result": 42}

    @app.get("/compute")
    def compute_get():
        return {"result": 42}

    module.install_middleware(app, "demo")
    module.readiness(app, "demo")
    return TestClient(app)


# --- authentication -------------------------------------------------------


def test_an_open_service_serves_but_says_so(load_middleware, caplog):
    # `api.auth: none` is a legitimate choice for an internal service, but it
    # must not be a quiet one — the warning is the only thing standing between
    # "deliberately internal" and "nobody realised".
    import logging

    with caplog.at_level(logging.WARNING, logger="nativegate.demo"):
        module = load_middleware("none")
        client = _app_with(module)

    assert client.get("/compute").status_code == 200
    assert any("WITHOUT authentication" in r.getMessage() for r in caplog.records)


def test_api_key_mode_refuses_callers_without_a_key(load_middleware):
    module = load_middleware("api_key", NATIVEGATE_API_KEYS="secret-one,secret-two")
    client = _app_with(module)

    assert client.get("/compute").status_code == 401
    assert client.get("/compute", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/compute", headers={"X-API-Key": "secret-two"}).status_code == 200
    # Bearer as well as X-API-Key: callers behind a standard gateway send one,
    # scripts tend to send the other.
    assert (
        client.get("/compute", headers={"Authorization": "Bearer secret-one"}).status_code
        == 200
    )


def test_auth_fails_closed_when_no_keys_are_configured(load_middleware):
    # THE most important test in this file. A service generated to require
    # authentication and started without keys must refuse to boot. Coming up
    # unauthenticated instead is the failure that gets discovered by an
    # attacker rather than by whoever deployed it.
    module = load_middleware("api_key")

    with pytest.raises(module.AuthNotConfigured, match="Refusing to start"):
        module.install_middleware(FastAPI(), "demo")


def test_health_and_readiness_never_require_a_key(load_middleware):
    # An orchestrator has no credentials. A liveness probe that 401s is a
    # service that looks dead and gets restarted forever.
    module = load_middleware("api_key", NATIVEGATE_API_KEYS="k")
    client = _app_with(module)

    assert client.get("/readyz").status_code == 200


def test_keys_are_compared_in_constant_time(load_middleware):
    # Not a timing measurement — that would be flaky. This pins the mechanism:
    # `hmac.compare_digest`, and a loop that does not break early, because
    # short-circuiting leaks which key matched and how far it matched.
    source = middleware_gen.generate_middleware_py("demo", "api_key")

    assert "hmac.compare_digest" in source
    assert "break" not in source.split("for key in api_keys:")[1].split("if not matched")[0]


# --- limits ---------------------------------------------------------------


def test_the_rate_limiter_rejects_past_the_window(load_middleware):
    module = load_middleware("none", NATIVEGATE_RATE_LIMIT_PER_MINUTE="3")
    client = _app_with(module)

    codes = [client.post("/compute", json={"x": 1}).status_code for _ in range(5)]

    assert codes == [200, 200, 200, 429, 429]
    rejected = client.post("/compute", json={"x": 1})
    assert rejected.headers["Retry-After"] == "60"
    # The id is on the rejection too: a client reporting "I got 429s" is
    # useless without one.
    assert rejected.json()["request_id"]


def test_rate_limiting_is_off_by_default(load_middleware):
    module = load_middleware("none")
    client = _app_with(module)

    assert all(
        client.post("/compute", json={"x": 1}).status_code == 200 for _ in range(20)
    )


def test_an_oversized_body_is_refused_before_it_is_read(load_middleware):
    module = load_middleware("none", NATIVEGATE_MAX_REQUEST_BYTES="200")
    client = _app_with(module)

    too_big = client.post("/compute", json={"blob": "z" * 5000})

    assert too_big.status_code == 413
    assert too_big.json()["max_request_bytes"] == 200
    assert client.post("/compute", json={"x": 1}).status_code == 200


# --- request identity -----------------------------------------------------


def test_every_response_carries_a_request_id(load_middleware):
    module = load_middleware("none")
    client = _app_with(module)

    response = client.get("/compute")

    assert len(response.headers["X-Request-ID"]) == 12
    assert response.headers["X-Response-Time-Ms"]


def test_an_inbound_request_id_is_honoured_but_bounded(load_middleware):
    # Honouring it keeps a trace intact across a gateway or service mesh hop.
    # Bounding it matters because the value is attacker-controlled and lands in
    # log lines: an unbounded one is log injection and a wrecked log file.
    module = load_middleware("none")
    client = _app_with(module)

    kept = client.get("/compute", headers={"X-Request-ID": "trace-abc-123"})
    assert kept.headers["X-Request-ID"] == "trace-abc-123"

    # Only values a well-behaved client will actually put on the wire: httpx
    # refuses to encode a non-ASCII header at all, so those cases cannot reach
    # the middleware through it. The `isascii()` guard in the generated code
    # still earns its place — a raw socket or a sloppy proxy is not bound by
    # httpx's politeness — but asserting it here would test httpx, not us.
    for hostile in ("x" * 500, "\x7f\x7f\x7f"):
        response = client.get("/compute", headers={"X-Request-ID": hostile})
        assert response.headers["X-Request-ID"] != hostile
        assert len(response.headers["X-Request-ID"]) == 12


# --- access logging --------------------------------------------------------


def test_access_log_emits_one_json_line_per_rest_call(load_middleware, capsys):
    module = load_middleware("none")
    client = _app_with(module)

    client.get("/compute")
    line = [l for l in capsys.readouterr().out.splitlines() if l.strip()][-1]
    row = json.loads(line)

    assert row["kind"] == "rest"
    assert row["service"] == "demo"
    assert row["method"] == "GET"
    assert row["path"] == "/compute"
    assert row["tool"] is None
    assert row["status"] == 200
    assert isinstance(row["duration_ms"], (int, float))
    assert row["request_id"]
    assert "ts" in row
    # Nulled, not omitted: the console parses one fixed schema.
    assert row["project"] is None
    assert row["build_id"] is None
    assert row["image_digest"] is None


def test_access_log_carries_provenance_from_the_environment(
    load_middleware, monkeypatch, capsys
):
    """Set at `docker run` by whatever deploys the image, read per call."""
    module = load_middleware("none")
    client = _app_with(module)

    monkeypatch.setenv(middleware_gen.ENV_PROJECT, "petro")
    monkeypatch.setenv(middleware_gen.ENV_BUILD_ID, "417")
    monkeypatch.setenv(middleware_gen.ENV_IMAGE_DIGEST, "sha256:abc123")

    client.get("/compute")
    row = json.loads([l for l in capsys.readouterr().out.splitlines() if l.strip()][-1])

    assert row["project"] == "petro"
    assert row["build_id"] == 417
    assert row["image_digest"] == "sha256:abc123"


def test_a_non_numeric_build_id_is_nulled_rather_than_emitted_as_a_string(
    load_middleware, monkeypatch, capsys
):
    # A consumer joining on build_id wants a number; half-typed is worse than
    # absent.
    module = load_middleware("none")
    client = _app_with(module)

    monkeypatch.setenv(middleware_gen.ENV_BUILD_ID, "not-a-number")

    client.get("/compute")
    row = json.loads([l for l in capsys.readouterr().out.splitlines() if l.strip()][-1])

    assert row["build_id"] is None


def _app_with_mcp_stub(module):
    app = FastAPI()

    @app.post("/mcp/")
    def mcp_stub():
        return {"ok": True}

    @app.get("/mcpconfig")
    def mcpconfig():
        return {"ok": True}

    @app.get("/mcp")
    def mcp_root():
        return {"ok": True}

    @app.post("/compute")
    def compute(payload: dict):
        return {"result": 42}

    module.install_middleware(app, "demo")
    return TestClient(app)


def _rows(capsys):
    return [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]


@pytest.mark.parametrize(
    "auth, env, headers, path, expected_status, expected_kind",
    [
        ("api_key", {"NATIVEGATE_API_KEYS": "k"}, {}, "/mcp/", 401, "mcp_http"),
        ("api_key", {"NATIVEGATE_API_KEYS": "k"}, {}, "/compute", 401, "rest"),
        ("none", {"NATIVEGATE_MAX_REQUEST_BYTES": "50"}, {}, "/compute", 413, "rest"),
    ],
    ids=["mcp-401", "rest-401", "rest-413"],
)
def test_a_rejected_request_is_still_logged(
    load_middleware, capsys, auth, env, headers, path, expected_status, expected_kind
):
    # An access log nested inside the layers that reject records only the
    # calls that got through, which is precisely backwards for an audit: a
    # 401 or a 413 against a governed service is the traffic most worth
    # seeing. The log layer therefore sits directly inside the request-id
    # layer and outside every rejecting layer.
    module = load_middleware(auth, **env)
    client = _app_with_mcp_stub(module)

    response = client.post(path, json={"blob": "z" * 500}, headers=headers)
    assert response.status_code == expected_status

    row = _rows(capsys)[-1]
    assert row["kind"] == expected_kind
    assert row["path"] == path
    assert row["status"] == expected_status
    # Correlatable with the rejection the caller saw — a 401 with no id is an
    # unanswerable support ticket.
    assert row["request_id"] == response.headers["X-Request-ID"]


def test_a_rest_call_records_its_request_and_response_payloads(load_middleware, capsys):
    # The console's calls page shows an Input and an Output column for every
    # row. They were populated for MCP tool calls only, which left them blank
    # for REST — the overwhelming majority of a service's traffic, and the
    # part an evidence pack is most often asked to account for.
    module = load_middleware("none")
    client = _app_with(module)

    response = client.post("/compute", json={"radius": 2.5})
    assert response.status_code == 200

    row = _rows(capsys)[-1]
    assert json.loads(row["input"]) == {"radius": 2.5}
    assert json.loads(row["output"]) == {"result": 42}


def test_reading_the_body_to_log_it_does_not_starve_the_endpoint(load_middleware):
    # Reading the body in middleware must not starve the endpoint. Starlette
    # is what guarantees that — BaseHTTPMiddleware hands dispatch a
    # `_CachedRequest` whose `wrapped_receive` replays a body `.body()` has
    # already cached — so this passes without any help from the capture
    # layer. It pins that contract rather than proving anything this layer
    # does: if an upgrade ever drops the caching, every POST to a generated
    # service breaks, and this is what says so.
    module = load_middleware("none")
    app = FastAPI()

    @app.post("/echo")
    def echo(payload: dict):
        return {"seen": payload}

    module.install_middleware(app, "demo")
    client = TestClient(app)

    response = client.post("/echo", json={"radius": 2.5})
    assert response.status_code == 200
    assert response.json() == {"seen": {"radius": 2.5}}


def test_a_non_json_body_is_not_captured(load_middleware, capsys):
    # A native-compute service's non-JSON payloads are binary (packed arrays,
    # files). Their bytes are noise in a log line and their size is what the
    # capture limit exists to keep out of memory.
    module = load_middleware("none")
    app = FastAPI()

    @app.post("/raw")
    async def raw(request: Request):
        body = await request.body()
        return Response(content=body, media_type="application/octet-stream")

    module.install_middleware(app, "demo")
    client = TestClient(app)

    response = client.post(
        "/raw", content=b"\x00\x01\x02", headers={"Content-Type": "application/octet-stream"}
    )
    assert response.status_code == 200
    assert response.content == b"\x00\x01\x02"

    row = _rows(capsys)[-1]
    assert row["input"] is None
    assert row["output"] is None


def test_a_streaming_response_to_a_body_carrying_request_still_works(load_middleware):
    # Regression test, and the sharpest one here. Reading the request body
    # for the log and then re-injecting a replay onto `request._receive` --
    # the obvious way to "hand the body back" -- broke this exact shape:
    # once the body is consumed, Starlette's `wrapped_receive` awaits that
    # same channel expecting only `http.disconnect`, and a replay that keeps
    # answering `http.request` makes it raise "Unexpected message received".
    # Every POST whose handler streamed its response died that way, while
    # the plain JSON POSTs the other tests cover kept passing.
    module = load_middleware("none")
    app = FastAPI()

    @app.post("/stream")
    async def stream(request: Request):
        await request.body()

        async def gen():
            yield b"chunk-1"
            yield b"chunk-2"

        return StreamingResponse(gen(), media_type="text/plain")

    module.install_middleware(app, "demo")
    client = TestClient(app)

    response = client.post("/stream", json={"radius": 2.5})
    assert response.status_code == 200
    assert response.content == b"chunk-1chunk-2"


def test_a_streaming_json_response_is_not_drained_by_the_capture(
    load_middleware, capsys
):
    # Draining a response to log it is only safe once the response is whole.
    # A handler streaming newline-delimited JSON has a JSON content type but
    # is still being produced, so draining it would hold every chunk until
    # the generator ended -- and never return at all for a generator that
    # does not. The Content-Length header is the discriminator: Starlette
    # sets it on a materialised body and cannot set it on a streaming one.
    #
    # Asserted as "the capture declined", not "the client got the first
    # chunk early": TestClient buffers the whole ASGI response before
    # returning, so any timing assertion here would be measuring the test
    # client rather than the middleware.
    module = load_middleware("none")
    app = FastAPI()

    @app.get("/ndjson")
    def ndjson():
        async def gen():
            for i in range(3):
                yield b'{"i": %d}\n' % i

        return StreamingResponse(gen(), media_type="application/json")

    module.install_middleware(app, "demo")
    client = TestClient(app)

    response = client.get("/ndjson")
    assert response.status_code == 200
    assert response.content == b'{"i": 0}\n{"i": 1}\n{"i": 2}\n'

    row = _rows(capsys)[-1]
    assert row["output"] is None


def test_a_boundary_containing_json_does_not_open_the_gate(load_middleware, capsys):
    # A multipart boundary is chosen by the client. A substring test for
    # "json" would let a binary upload name its way past the content-type
    # gate and into the log as mojibake -- the exact case the gate exists to
    # exclude, selectable by whoever is uploading.
    module = load_middleware("none")
    app = FastAPI()

    @app.post("/upload")
    async def upload(request: Request):
        await request.body()
        return {"ok": True}

    module.install_middleware(app, "demo")
    client = TestClient(app)

    response = client.post(
        "/upload",
        content=b"\x89PNG\r\n\x1a\n\x00\x00binary",
        headers={
            "Content-Type": "multipart/form-data; boundary=----WebKitFormBoundaryjson1a2b"
        },
    )
    assert response.status_code == 200

    row = _rows(capsys)[-1]
    assert row["input"] is None


def test_a_compressed_json_body_is_not_logged_as_mojibake(load_middleware, capsys):
    # Still JSON by content type, but its bytes are compressed. Decoding
    # them as text produces an unreadable string that looks like a real
    # captured payload — a corrupted audit record is worse than an absent
    # one, because nothing about it looks wrong.
    module = load_middleware("none")
    app = FastAPI()

    @app.post("/gz")
    async def gz(request: Request):
        await request.body()
        return {"ok": True}

    module.install_middleware(app, "demo")
    client = TestClient(app)

    response = client.post(
        "/gz",
        content=gzip.compress(b'{"radius": 2.5}'),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert response.status_code == 200

    row = _rows(capsys)[-1]
    assert row["input"] is None


def test_a_body_that_fails_mid_stream_is_not_silently_truncated(load_middleware):
    # Pins a property, and is deliberately honest that it does not currently
    # discriminate: it passes with and without the capture layer's re-raise,
    # because an exception from the body iterator comes from the downstream
    # app, which BaseHTTPMiddleware runs in a task group that re-raises on
    # unwind either way.
    #
    # It earns its place by pinning the outcome that matters — a mid-stream
    # failure reaches the caller as an error, never as a short body under a
    # 200, which from a numerical service is a wrong answer rather than a
    # failed one. If draining for the access log ever does start swallowing
    # that, this is what notices.
    module = load_middleware("none")
    app = FastAPI()

    @app.get("/half")
    def half():
        async def body():
            yield b'{"partial": '
            raise RuntimeError("stream died")

        return StreamingResponse(body(), media_type="application/json")

    module.install_middleware(app, "demo")
    client = TestClient(app)

    with pytest.raises(RuntimeError, match="stream died"):
        client.get("/half")


def test_an_oversized_payload_is_truncated_rather_than_logged_whole(
    load_middleware, capsys
):
    # These lines go to stdout, get parsed by the console's tailer and stored
    # in SQLite. One uploaded array must not be able to balloon the log
    # stream, or the evidence pack drawn from it.
    module = load_middleware("none")
    client = _app_with(module)

    client.post("/compute", json={"blob": "z" * 50_000})

    row = _rows(capsys)[-1]
    assert len(row["input"]) < 3000
    assert row["input"].endswith("chars total)")


def test_an_mcp_transport_request_records_no_payload(load_middleware, capsys):
    # Not an oversight. The MCP mount speaks a streaming transport that must
    # not be buffered, and the tool call underneath it logs its own decoded
    # arguments and result — capturing here too would store the same payload
    # twice under two kinds.
    client = _app_with_mcp_stub(load_middleware("none"))

    client.post("/mcp/", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})

    row = [r for r in _rows(capsys) if r["path"] == "/mcp/"][-1]
    assert row["kind"] == "mcp_http"
    assert row["input"] is None
    assert row["output"] is None


def test_an_mcp_transport_request_is_logged_under_its_own_kind(load_middleware, capsys):
    # The FastMCP hook only fires for tool dispatch, so if this layer skipped
    # the mount entirely an `initialize`, a `tools/list` or a malformed
    # JSON-RPC body would leave no record at all — an audit hole. The distinct
    # kind is what keeps a transport row from being mistaken for a tool row.
    client = _app_with_mcp_stub(load_middleware("none"))

    client.post("/mcp/")
    rows = _rows(capsys)

    mcp_http = [row for row in rows if row["path"] == "/mcp/"]
    assert mcp_http, f"no row for the /mcp transport request; got {rows}"
    row = mcp_http[-1]
    assert row["kind"] == "mcp_http"
    assert row["method"] == "POST"
    assert row["status"] == 200
    # A transport row never carries a tool name: the name lives in the
    # JSON-RPC body, which this layer does not parse.
    assert row["tool"] is None
    assert row["service"] == "demo"
    assert isinstance(row["duration_ms"], (int, float))
    assert row["request_id"]
    assert row["project"] is None
    assert row["build_id"] is None
    assert row["image_digest"] is None


def test_the_mcp_mount_itself_is_transport_traffic(load_middleware, capsys):
    client = _app_with_mcp_stub(load_middleware("none"))

    client.get("/mcp")

    assert _rows(capsys)[-1]["kind"] == "mcp_http"


def test_a_sibling_route_sharing_the_prefix_is_not_treated_as_mcp(
    load_middleware, capsys
):
    # `startswith("/mcp")` also swallows /mcpconfig and /mcp-status. Such a
    # route is ordinary REST traffic, and mislabelling it would describe a
    # layer the request never went through.
    client = _app_with_mcp_stub(load_middleware("none"))

    client.get("/mcpconfig")
    row = _rows(capsys)[-1]

    assert row["kind"] == "rest"
    assert row["path"] == "/mcpconfig"


# --- draining -------------------------------------------------------------


def test_readyz_reports_draining_after_sigterm(load_middleware):
    # Readiness exists for THIS, not for "has the app started": a generated
    # service cannot answer at all before its native extension imports, so a
    # started/not-started probe would just be a slower liveness check.
    # Reporting 503 the moment SIGTERM lands takes the worker out of the load
    # balancer before it stops accepting, which is what prevents a burst of
    # 502s on a rolling deploy.
    module = load_middleware("none")
    client = _app_with(module)
    assert client.get("/readyz").status_code == 200

    os.kill(os.getpid(), signal.SIGTERM)

    assert client.get("/readyz").status_code == 503
    # Still serving: draining means "stop sending me new work", not "stop".
    assert client.get("/compute").status_code == 200


def test_the_sigterm_handler_is_chained_not_replaced(load_middleware):
    # gunicorn and uvicorn install their own SIGTERM handlers to run graceful
    # shutdown. Replacing one would turn a graceful drain into an abrupt exit —
    # strictly worse than having no drain signal at all.
    called = []
    signal.signal(signal.SIGTERM, lambda *a: called.append("previous"))
    module = load_middleware("none")
    module.readiness(FastAPI(), "demo")

    os.kill(os.getpid(), signal.SIGTERM)

    assert called == ["previous"]


# --- configuration --------------------------------------------------------


def test_an_unknown_auth_mode_is_rejected_rather_than_defaulted(tmp_path):
    # `api: {auth: apikey}` is a plausible typo. Treating it as "none" would
    # produce exactly the silently-open service the setting exists to prevent.
    (tmp_path / "nativegate.yaml").write_text(
        "name: demo\nlanguage: cpp\nexpose:\n  all: true\napi:\n  auth: apikey\n"
    )
    (tmp_path / "native").mkdir()
    (tmp_path / "native" / "demo.hpp").write_text("int f();")

    with pytest.raises(ConfigError, match="api.auth"):
        ServiceConfig.load(tmp_path)


def test_auth_mode_is_baked_in_not_read_from_the_environment():
    # A service generated to require authentication must not be downgradable
    # by the environment it happens to start in.
    source = middleware_gen.generate_middleware_py("demo", "api_key")

    assert 'AUTH_MODE = "api_key"' in source
    assert "environ" not in source.split("AUTH_MODE =")[1].split("\n")[0]


def test_api_config_round_trips_through_yaml(tmp_path):
    (tmp_path / "native").mkdir()
    (tmp_path / "native" / "demo.hpp").write_text("int f();")
    ServiceConfig(
        name="demo", language="cpp", api=ApiConfig(auth="api_key")
    ).save(tmp_path)

    assert ServiceConfig.load(tmp_path).api.auth == "api_key"
    # The default stays out of the file, so an existing config does not grow a
    # key that says nothing.
    ServiceConfig(name="demo", language="cpp").save(tmp_path)
    assert "api:" not in (tmp_path / "nativegate.yaml").read_text()
