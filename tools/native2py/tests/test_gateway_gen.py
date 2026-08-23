from native2py.generators import gateway_gen, python_pkg_gen


def test_router_has_no_prefix_baked_in(calculator_header):
    # The prefix must be chosen by whoever mounts the router, so the same
    # generated code serves standalone (/circle_area) and composed
    # (/demo/circle_area) deployments. A prefix here would break standalone.
    from native2py.config import ExposeConfig
    from native2py.parsers import cpp as cpp_parser
    from pathlib import Path

    module = cpp_parser.parse_header(calculator_header, ExposeConfig(classes=["Calculator"]))
    router_py = python_pkg_gen.generate_router_py(module, "calculator")

    assert 'APIRouter(tags=["calculator"])' in router_py
    assert "prefix=" not in router_py


def test_service_py_is_thin_wrapper_with_health():
    service_py = python_pkg_gen.generate_service_py("calculator")

    assert "from .router import router" in service_py
    assert "app.include_router(router)" in service_py
    assert "/healthz" in service_py


def test_gateway_mounts_each_service_under_its_own_prefix():
    app_py = gateway_gen.generate_gateway_app("platform-api", ["demo", "calculator"])

    assert "from demo.router import router as demo_router" in app_py
    assert "from calculator.router import router as calculator_router" in app_py
    assert 'app.include_router(demo_router, prefix="/demo")' in app_py
    assert 'app.include_router(calculator_router, prefix="/calculator")' in app_py


def test_gateway_pyproject_depends_on_each_service_wheel():
    # Services stay independently built/versioned — the gateway consumes
    # them as ordinary wheel dependencies, not as vendored source.
    pyproject = gateway_gen.generate_gateway_pyproject(
        "platform-api", "platform_api", ["demo", "calculator"]
    )

    assert '"demo"' in pyproject
    assert '"calculator"' in pyproject
    # Distribution name keeps the hyphen; the importable package must not.
    assert 'name = "platform-api"' in pyproject
    assert 'packages = ["platform_api"]' in pyproject


# --- unhandled exceptions (ROADMAP 2.4) ----------------------------------
#
# What the generated app did BEFORE this handler was measured, not assumed:
# with debug=False it answered `Internal Server Error` as text/plain. No
# traceback, no leak. The docs' claim of a leaked traceback was wrong.
#
# So these pin what the handler actually adds — a JSON body and an error_id
# the caller can quote back, matching a log line that has the traceback — and,
# just as importantly, the two things it does NOT do: it cannot save a service
# running with debug=True, and it never sees a native crash.
#
# They build a REAL app from the generated source and make real requests,
# because all of this is a property of what goes over the wire.

import logging

import pytest
from fastapi import APIRouter, HTTPException
from fastapi.testclient import TestClient

_SECRET = "/opt/prod/decks/fluid.dat"


def _app_from(source: str):
    """Exec generated app source with its service imports stubbed out.

    The generated module opens `from .router import router` / `from demo.router
    import ...`, which cannot resolve with no installed service package. The
    imports and the matching include_router lines are dropped and a probe
    router is mounted instead; the app, the handler and the middleware stack
    are exactly as generated.
    """
    # The middleware layer is stripped too, not just the routers: these tests
    # are about the error handler, and auth/limits/request-ids in front of it
    # would change what reaches it. tests/test_middleware_gen.py covers those
    # against a real generated middleware module.
    dropped = (".router import", ".middleware import")
    called = ("app.include_router(", "install_middleware(", "readiness(")
    kept = [
        line
        for line in source.splitlines()
        if not any(d in line for d in dropped) and not line.startswith(called)
    ]
    namespace: dict = {}
    exec(compile("\n".join(kept), "app.py", "exec"), namespace)  # noqa: S102

    probe = APIRouter()

    @probe.post("/boom")
    def boom():
        raise ValueError(f"could not open {_SECRET}")

    @probe.post("/guard")
    def guard():
        # Stands in for the generated size guard, which must keep its own
        # status and detail rather than being flattened into a 500.
        raise HTTPException(status_code=422, detail="n must equal len(props)")

    app = namespace["app"]
    app.include_router(probe)
    # raise_server_exceptions=False so the client behaves like a real server:
    # Starlette re-raises after sending, purely so the server logs it too.
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "source",
    [
        python_pkg_gen.generate_service_py("demo"),
        gateway_gen.generate_gateway_app("platform-api", ["demo"]),
    ],
    ids=["standalone-service", "composed-gateway"],
)
def test_an_uncaught_exception_becomes_a_correlatable_json_500(source):
    # Both topologies, because a handler is registered per app: a router
    # mounted into the gateway runs under the GATEWAY's app, so a handler
    # installed only on service.py would not apply to the deployment more
    # likely to be internet-facing.
    response = _app_from(source).post("/boom")

    assert response.status_code == 500
    assert _SECRET not in response.text
    assert "Traceback" not in response.text
    assert "ValueError" not in response.text
    assert response.json()["error"] == "internal server error"
    # An id the caller can quote, which is the only link back to the log line.
    assert len(response.json()["error_id"]) == 12


def test_the_traceback_still_reaches_the_log(caplog):
    # Suppressing it in the response is only correct if it is preserved
    # somewhere. Losing it would trade an information leak for a blind service.
    with caplog.at_level(logging.ERROR, logger="native2py.demo"):
        response = _app_from(python_pkg_gen.generate_service_py("demo")).post("/boom")

    record = next(r for r in caplog.records if r.name == "native2py.demo")
    assert response.json()["error_id"] in record.getMessage()
    assert record.exc_info is not None
    assert _SECRET in logging.Formatter().formatException(record.exc_info)


def test_http_exceptions_keep_their_status_and_detail():
    # The generated argument-validation errors are deliberate, caller-facing
    # and safe. Catching Exception broadly must not swallow them into a 500.
    response = _app_from(python_pkg_gen.generate_service_py("demo")).post("/guard")

    assert response.status_code == 422
    assert response.json()["detail"] == "n must equal len(props)"


def test_debug_errors_is_opt_in(monkeypatch):
    # The escape hatch exists for non-production debugging, and must be off
    # unless deliberately switched on.
    default = _app_from(python_pkg_gen.generate_service_py("demo")).post("/boom")
    assert "detail" not in default.json()

    monkeypatch.setenv("NATIVE2PY_DEBUG_ERRORS", "1")
    debug = _app_from(python_pkg_gen.generate_service_py("demo")).post("/boom")
    assert _SECRET in debug.json()["detail"]


def test_debug_true_defeats_the_handler_and_is_not_used():
    # Measured, and it is the reason no doc here claims the handler "prevents
    # traceback leaks": Starlette consults `debug` BEFORE the handler, so a
    # debug app returns its traceback response instead of ours. Nothing in the
    # generated code can override that — the only protection is not enabling
    # it, so pin that the generated app does not.
    source = python_pkg_gen.generate_service_py("demo")
    # Asserted on the constructor, not on the whole file: the handler's own
    # docstring names `debug=True` to warn about it, and a substring check
    # over the source would fail on that warning.
    assert 'app = FastAPI(title="demo")\n' in source

    leaky = source.replace('FastAPI(title="demo")', 'FastAPI(title="demo", debug=True)')
    response = _app_from(leaky).post("/boom")

    assert _SECRET in response.text  # the handler did NOT get a say
