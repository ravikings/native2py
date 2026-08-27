"""The shared-native-state contract of a generated service, pinned by CI.

tests/test_service_gen.py already covers the lock's *shape* on small,
hand-built IRs and tests/test_gateway_gen.py covers the error handler. This
module covers what those leave open, and it is deliberately written as
"characterise, do not fix":

* an EXHAUSTIVE, AST-based guard that no generated endpoint reaches a native
  symbol outside `_NATIVE_LOCK` — the earlier tests assert a handful of
  literal `with _NATIVE_LOCK:\\n        NAME(...)` strings, which passes even
  if a newly-supported signature shape grows a second, unlocked call site;
* serialisation observed through a REAL `TestClient` and real threads, rather
  than by calling endpoint functions directly;
* `MAX_ARRAY_ITEMS` exercised at its real default and through its real
  environment override, rather than by rewriting the constant in the source;
* `/healthz` and `/readyz` served by the real generated `service.py` and the
  real generated `middleware.py` at the same time, which is the only place
  their differing semantics are actually observable;
* the limits that are NOT enforced, recorded as named tests rather than prose.

KNOWN GAPS, recorded here so they are visible in the suite (see the tests at
the bottom of this file):

1. `_NATIVE_LOCK` is a `threading.RLock` in ONE interpreter. It serialises
   FastAPI's threadpool inside a single worker and nothing else. Two gunicorn
   workers, two containers or two replicas each get their own lock and their
   own copy of COMMON, so the COMMON-block hazard returns at that scale. The
   supported answer is one worker per process holding the state, not a bigger
   lock; nativegate cannot express that for you.
2. `MAX_ARRAY_ITEMS` bounds the number of elements a request may carry. It is
   not the extent the native routine declares — the IR does not record that
   (ROADMAP 1.4) — so it prevents memory exhaustion and nothing else.
3. The lock is held for one native call, so a configure-then-read pair spread
   over two requests is still not atomic. That one is already pinned in
   tests/test_service_gen.py.
"""

import ast
import logging
import os
import signal
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nativegate.generators import middleware_gen, python_pkg_gen
from nativegate.ir import FunctionDef, ModuleIR, Parameter, SkippedSymbol


def fortran_module(**kwargs) -> ModuleIR:
    # setdefault rather than a `dict(...)` literal updated with kwargs: the
    # literal narrows to dict[str, str], and splatting it at ModuleIR's typed
    # list fields is a type error even though every call site is correct.
    kwargs.setdefault("name", "petro")
    kwargs.setdefault("language", "fortran")
    kwargs.setdefault("source_file", "petro.f")
    return ModuleIR(**kwargs)


def _exec_router(code: str, native: dict) -> dict:
    """Exec a generated router with the native symbols injected.

    `from . import PVTINI, ...` is a relative import with no package to
    resolve against, so the names go straight into the module namespace. The
    real APIRouter, the real Pydantic models and the real lock are used.
    """
    stripped = "\n".join(
        line for line in code.splitlines() if not line.startswith("from . import")
    )
    namespace = dict(native)
    exec(compile(stripped, "router.py", "exec"), namespace)  # noqa: S102
    return namespace


def _client_for(namespace: dict) -> TestClient:
    app = FastAPI()
    app.include_router(namespace["router"])
    return TestClient(app)


# --- 1. every native call is inside the lock (AST, not substrings) ---------


def _native_names(code: str) -> set[str]:
    """The symbols the generated router imports from the native package."""
    tree = ast.parse(code)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None:
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _holds_native_lock(node: ast.With) -> bool:
    return any(
        isinstance(item.context_expr, ast.Name)
        and item.context_expr.id == "_NATIVE_LOCK"
        for item in node.items
    )


def _unlocked_native_calls(code: str) -> list[str]:
    """Native calls reachable from an endpoint body without holding the lock.

    Returns "<endpoint>:<symbol>" for each offender, so a failure names the
    route that regressed rather than just saying the count changed.
    """
    tree = ast.parse(code)
    native = _native_names(code)
    offenders: list[str] = []

    def is_route(fn: ast.FunctionDef) -> bool:
        for dec in fn.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "router"
            ):
                return True
        return False

    def visit(node: ast.AST, endpoint: str, locked: bool) -> None:
        if isinstance(node, ast.With):
            locked = locked or _holds_native_lock(node)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in native
            and not locked
        ):
            offenders.append(f"{endpoint}:{node.func.id}")
        for child in ast.iter_child_nodes(node):
            visit(child, endpoint, locked)

    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef) and is_route(fn):
            for stmt in fn.body:
                visit(stmt, fn.name, locked=False)
    return offenders


# A deliberately wide Fortran IR: plain function, subroutine, array plus its
# size argument, a routine whose result is an array, and one refused symbol.
# The point is that the guard runs over EVERY shape the generator supports,
# not the two-endpoint module the shape tests use.
WIDE_FORTRAN_IR = fortran_module(
    functions=[
        FunctionDef(
            name="PVTINI",
            parameters=[
                Parameter(name="api", type="float"),
                Parameter(name="icorr", type="int"),
            ],
            is_subroutine=True,
        ),
        FunctionDef(
            name="PVTRS",
            parameters=[Parameter(name="p", type="float")],
            returns="float",
        ),
        FunctionDef(name="PVTVER", returns="float"),
        FunctionDef(
            name="PVTSTATE",
            parameters=[
                Parameter(name="pressure", type="float"),
                Parameter(name="props", type="float", is_array=True),
                Parameter(name="n", type="int"),
            ],
            returns="float",
        ),
        FunctionDef(
            name="NORMALIZE",
            parameters=[
                Parameter(name="values", type="float", is_array=True),
                Parameter(name="nvalues", type="int"),
            ],
            is_subroutine=True,
        ),
    ],
    skipped=[SkippedSymbol(name="PVTPACK", reason="derived-type Fortran result")],
)


def test_no_generated_endpoint_reaches_native_code_outside_the_lock():
    """The regression guard: a dropped lock on ANY endpoint fails CI.

    Substring assertions elsewhere check the two call shapes they were
    written for. This walks the AST of the whole generated file, so a new
    signature shape that grows an unlocked call site is caught the day it is
    added rather than the day two requests corrupt each other's COMMON.
    """
    code = python_pkg_gen.generate_router_py(WIDE_FORTRAN_IR, "petro")

    # Sanity: the guard is looking at something. If the import shape ever
    # changes, an empty `native` set would make this test vacuously green.
    assert _native_names(code) >= {
        "PVTINI",
        "PVTRS",
        "PVTVER",
        "PVTSTATE",
        "NORMALIZE",
    }
    assert _unlocked_native_calls(code) == []


def test_the_unlocked_call_guard_actually_detects_a_dropped_lock():
    """The guard above is only worth having if it can fail.

    A maintainer deleting one `with _NATIVE_LOCK:` "just for the hot path" is
    the exact change this suite exists to stop, so prove the detector sees it.
    """
    code = python_pkg_gen.generate_router_py(WIDE_FORTRAN_IR, "petro")
    without_one = code.replace(
        "    with _NATIVE_LOCK:\n        result = PVTRS(p)\n",
        "    result = PVTRS(p)\n",
        1,
    )
    assert without_one != code, "the PVTRS call shape changed; update this test"

    assert _unlocked_native_calls(without_one) == ["PVTRS_endpoint:PVTRS"]


def test_every_fortran_endpoint_carries_exactly_one_critical_section():
    # Not two: a second `with _NATIVE_LOCK:` inside one endpoint would mean the
    # lock was released between two calls that share COMMON.
    code = python_pkg_gen.generate_router_py(WIDE_FORTRAN_IR, "petro")
    tree = ast.parse(code)

    posts = [
        fn
        for fn in tree.body
        if isinstance(fn, ast.FunctionDef) and fn.name.endswith("_endpoint")
    ]
    assert len(posts) == 5
    for fn in posts:
        held = [n for n in ast.walk(fn) if isinstance(n, ast.With) and _holds_native_lock(n)]
        assert len(held) == 1, f"{fn.name} has {len(held)} critical sections"


def test_the_introspection_route_is_not_serialised_behind_native_work():
    # /_unexposed reads a dict. Putting it behind the lock would make a
    # long-running native call block introspection, for no safety gain.
    code = python_pkg_gen.generate_router_py(WIDE_FORTRAN_IR, "petro")
    tree = ast.parse(code)

    unexposed = next(
        fn
        for fn in tree.body
        if isinstance(fn, ast.FunctionDef) and "unexposed" in fn.name
    )
    assert not [n for n in ast.walk(unexposed) if isinstance(n, ast.With)]


# --- 2. serialisation observed over HTTP ----------------------------------
#
# The existing race tests call the generated endpoint functions directly.
# These go through a real TestClient, so FastAPI's own threadpool dispatch for
# synchronous `def` endpoints is part of what is under test — that dispatch is
# the reason the lock is needed at all.


class _CallRecorder:
    """A stand-in native routine that records when it is entered and left."""

    def __init__(self, dwell=0.05):
        self.events: list[tuple[str, float]] = []
        self._guard = threading.Lock()
        self._dwell = dwell

    def __call__(self, api):
        self._log("enter", api)
        time.sleep(self._dwell)
        self._log("exit", api)
        return api

    def _log(self, kind, api):
        # Its own lock, so the RECORDING is never the thing that serialises.
        with self._guard:
            self.events.append((kind, api))

    def overlaps(self) -> list[tuple[str, float]]:
        """The prefix of events up to the first nested enter, or []."""
        depth = 0
        for index, (kind, _api) in enumerate(self.events):
            depth += 1 if kind == "enter" else -1
            if depth > 1:
                return self.events[: index + 1]
        return []


PVTSOLVE_IR = fortran_module(
    functions=[
        FunctionDef(
            name="PVTSOLVE",
            parameters=[Parameter(name="api", type="float")],
            returns="float",
        )
    ]
)


def _hammer(app, apis, workers=6):
    """Post to /PVTSOLVE from several threads at once.

    A TestClient per thread, not one shared client: a TestClient owns a portal
    into its own event loop and is not built for concurrent use. The APP — and
    therefore the router module globals, the lock and the fake native routine —
    is shared, which is the part that matters.
    """
    results: dict[float, float] = {}
    start = threading.Barrier(workers)
    errors: list[BaseException] = []

    def call(api):
        try:
            start.wait(timeout=10)
            with TestClient(app) as client:
                response = client.post(f"/PVTSOLVE?api={api}")
            results[api] = response.json()["result"]
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=call, args=(api,)) for api in apis]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    return results


def test_concurrent_requests_never_overlap_inside_the_native_call():
    recorder = _CallRecorder()
    namespace = _exec_router(
        python_pkg_gen.generate_router_py(PVTSOLVE_IR, "petro"),
        {"PVTSOLVE": recorder},
    )
    app = FastAPI()
    app.include_router(namespace["router"])

    apis = [30.0, 35.0, 40.0, 45.0, 50.0, 55.0]
    results = _hammer(app, apis)

    # Every request was served...
    assert results == {api: api for api in apis}
    # ...and each native call ran alone: enter/exit strictly alternate.
    assert recorder.overlaps() == []
    assert [kind for kind, _ in recorder.events] == ["enter", "exit"] * len(apis)


def test_the_overlap_detector_would_see_a_race_if_there_were_one():
    """Without the lock the same recorder shows nested enters.

    Otherwise the test above proves only that the requests happened to be
    slow enough to queue. Driven through raw threads rather than a modified
    router so nothing here depends on the generated source's exact text.
    """
    recorder = _CallRecorder()
    threads = [threading.Thread(target=recorder, args=(api,)) for api in (1.0, 2.0)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert recorder.overlaps(), f"detector missed the overlap: {recorder.events}"


def test_the_lock_is_actually_held_while_the_native_routine_runs():
    """Direct evidence, independent of scheduling.

    A concurrency test can be defeated by a fast machine; this cannot. The
    fake routine tries to take the lock from ANOTHER thread while it is
    executing, and must not get it.
    """
    namespace: dict = {}
    observed: dict[str, bool] = {}

    def PVTSOLVE(api):
        lock = namespace["_NATIVE_LOCK"]
        # Not the calling thread: an RLock is re-entrant, so asking on this
        # thread would always succeed and prove nothing.
        def probe():
            acquired = lock.acquire(blocking=False)
            observed["free"] = acquired
            if acquired:
                lock.release()

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(timeout=10)
        return api

    namespace.update(
        _exec_router(
            python_pkg_gen.generate_router_py(PVTSOLVE_IR, "petro"),
            {"PVTSOLVE": PVTSOLVE},
        )
    )
    client = _client_for(namespace)

    assert client.post("/PVTSOLVE?api=30").json()["result"] == 30.0
    assert observed == {"free": False}
    # And released afterwards — a lock leaked on the success path would wedge
    # the worker on the next request rather than failing anything here.
    assert namespace["_NATIVE_LOCK"].acquire(blocking=False)
    namespace["_NATIVE_LOCK"].release()


def test_the_lock_is_released_when_the_native_call_raises():
    """`with` guarantees it, but a regression to try/acquire would not.

    A lock leaked on the error path deadlocks every later request in the
    worker, which presents as a hang rather than as an error — the hardest
    possible failure to diagnose in production.
    """
    def PVTSOLVE(api):
        raise RuntimeError("gfortran: STOP 1")

    namespace = _exec_router(
        python_pkg_gen.generate_router_py(PVTSOLVE_IR, "petro"),
        {"PVTSOLVE": PVTSOLVE},
    )
    client = TestClient(_app_including(namespace), raise_server_exceptions=False)

    assert client.post("/PVTSOLVE?api=30").status_code == 500
    assert namespace["_NATIVE_LOCK"].acquire(blocking=False)
    namespace["_NATIVE_LOCK"].release()


def _app_including(namespace: dict) -> FastAPI:
    app = FastAPI()
    app.include_router(namespace["router"])
    return app


# --- 3. MAX_ARRAY_ITEMS, at its real default and via its real env var ------

ARRAY_IR = fortran_module(
    functions=[
        FunctionDef(
            name="PVTSTATE",
            parameters=[
                Parameter(name="pressure", type="float"),
                Parameter(name="props", type="float", is_array=True),
                Parameter(name="n", type="int"),
            ],
            returns="float",
        )
    ]
)


@pytest.fixture
def array_router(monkeypatch):
    """Exec the array router, with NATIVEGATE_MAX_ARRAY_ITEMS under test control.

    The constant is read at import time, so the environment has to be set
    BEFORE the exec — which is exactly how a deployed worker sees it.
    """
    calls: list[int] = []

    def build(env=None):
        monkeypatch.delenv("NATIVEGATE_MAX_ARRAY_ITEMS", raising=False)
        if env is not None:
            monkeypatch.setenv("NATIVEGATE_MAX_ARRAY_ITEMS", env)

        def PVTSTATE(pressure, props, n):
            calls.append(len(props))
            return 1.0

        namespace = _exec_router(
            python_pkg_gen.generate_router_py(ARRAY_IR, "petro"), {"PVTSTATE": PVTSTATE}
        )
        return namespace, _client_for(namespace), calls

    return build


def test_the_default_cap_is_65536_elements(array_router):
    namespace, _client, _calls = array_router()

    assert namespace["MAX_ARRAY_ITEMS"] == 65536


def test_the_default_cap_is_enforced_at_exactly_that_boundary(array_router):
    # Posting 65_537 floats is the honest version of this test: the earlier
    # suite shrinks the constant in the source, which cannot catch a default
    # that silently changed.
    _namespace, client, calls = array_router()
    cap = 65536

    ok = client.post(f"/PVTSTATE?pressure=2000&n={cap}", json=[1.0] * cap)
    assert ok.status_code == 200
    assert calls == [cap]

    too_big = client.post(f"/PVTSTATE?pressure=2000&n={cap + 1}", json=[1.0] * (cap + 1))
    assert too_big.status_code == 422
    # Rejected during request validation. The native routine never ran and the
    # oversized list was never materialised as a numpy array.
    assert calls == [cap]


def test_the_environment_variable_really_overrides_the_default(array_router):
    namespace, client, calls = array_router(env="3")

    assert namespace["MAX_ARRAY_ITEMS"] == 3
    assert client.post("/PVTSTATE?pressure=2000&n=3", json=[1.0] * 3).status_code == 200
    assert client.post("/PVTSTATE?pressure=2000&n=4", json=[1.0] * 4).status_code == 422
    assert calls == [3]


def test_the_422_says_the_body_was_too_long_rather_than_just_failing(array_router):
    _namespace, client, _calls = array_router(env="2")

    body = client.post("/PVTSTATE?pressure=2000&n=5", json=[1.0] * 5).json()

    error = body["detail"][0]
    assert error["type"] == "too_long"
    # NOTE: `loc` is ["body"], not ["body", "props"]. The array is the whole
    # request body — the other arguments are query parameters — so Pydantic
    # has no field name to attribute it to. A caller sees which limit it
    # exceeded, not which parameter carried it. Recorded rather than asserted
    # away, because it is the message an integrator will actually get.
    assert error["loc"] == ["body"]
    assert error["ctx"]["max_length"] == 2


def test_a_larger_cap_can_be_configured_upwards_not_only_down(array_router):
    # The comment in the generated file says "raise it for a service with
    # genuinely large inputs; do not remove it". Pin that raising it works.
    namespace, _client, _calls = array_router(env="1000000")

    assert namespace["MAX_ARRAY_ITEMS"] == 1_000_000


# --- 4. the error contract, on a router that really calls native code -----
#
# tests/test_gateway_gen.py pins the handler against a probe route. This pins
# the same contract for an exception coming out of a NATIVE call inside the
# lock, which is the case the handler was written for.


def _service_app(monkeypatch, tmp_path, namespace: dict, service="petro") -> FastAPI:
    """The real generated service.py, wired to a generated router namespace.

    `from .router import router` and `from .middleware import ...` cannot
    resolve with no installed package, so the middleware is imported from a
    real module written to tmp_path and the router is taken from `namespace`.
    Everything else — the exception handler, the health route, the middleware
    stack — is exactly as generated.
    """
    monkeypatch.syspath_prepend(str(tmp_path))
    mw_name = f"generated_mw_{abs(hash(tmp_path)) % 10**8}"
    (tmp_path / f"{mw_name}.py").write_text(
        middleware_gen.generate_middleware_py(service, "none")
    )

    source = python_pkg_gen.generate_service_py(service)
    rewritten = source.replace(
        "from .middleware import install_middleware, readiness",
        f"from {mw_name} import install_middleware, readiness",
    ).replace("from .router import router", "").replace(
        "app.include_router(router)", ""
    # The MCP mount is dropped for the same reason the router import is: it
    # cannot resolve without an installed package. These tests are about the
    # health probes and the error handler; tests/test_mcp_gen.py owns the MCP
    # wiring. The lifespan= keyword has to go too, or FastAPI() never builds.
    ).replace("from .mcp_server import mcp_app", "").replace(
        ", lifespan=mcp_app.lifespan", ""
    ).replace('app.mount("/mcp", mcp_app)', "")
    assert mw_name in rewritten, "service.py's middleware import shape changed"
    # Comments legitimately mention mcp_app (they explain why the lifespan is
    # not optional), so only executable lines count here.
    assert not [
        line
        for line in rewritten.splitlines()
        if "mcp_app" in line and not line.lstrip().startswith("#")
    ], "service.py's MCP wiring shape changed"

    module_globals: dict = {}
    exec(compile(rewritten, "service.py", "exec"), module_globals)  # noqa: S102
    app = module_globals["app"]
    app.include_router(namespace["router"])
    return app


def test_a_native_exception_becomes_a_correlatable_json_500(monkeypatch, tmp_path, caplog):
    def PVTSOLVE(api):
        raise RuntimeError("could not open /opt/prod/decks/fluid.dat")

    namespace = _exec_router(
        python_pkg_gen.generate_router_py(PVTSOLVE_IR, "petro"), {"PVTSOLVE": PVTSOLVE}
    )
    app = _service_app(monkeypatch, tmp_path, namespace)
    client = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level(logging.ERROR, logger="nativegate.petro"):
        response = client.post("/PVTSOLVE?api=30")

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "internal server error"
    assert len(body["error_id"]) == 12
    # The native text — which can carry deck paths and array contents — does
    # not leave the process by default.
    assert "fluid.dat" not in response.text
    assert "RuntimeError" not in response.text
    assert "detail" not in body

    # The ERROR record specifically: the middleware logs a WARNING to the same
    # logger when the service is generated without authentication.
    record = next(
        r
        for r in caplog.records
        if r.name == "nativegate.petro" and r.levelno == logging.ERROR
    )
    assert body["error_id"] in record.getMessage()
    assert record.exc_info is not None
    # The traceback is kept where it belongs — in the log, not the response.
    assert "fluid.dat" in logging.Formatter().formatException(record.exc_info)


def test_debug_errors_opens_the_native_text_up_deliberately(monkeypatch, tmp_path):
    monkeypatch.setenv("NATIVEGATE_DEBUG_ERRORS", "1")

    def PVTSOLVE(api):
        raise RuntimeError("could not open /opt/prod/decks/fluid.dat")

    namespace = _exec_router(
        python_pkg_gen.generate_router_py(PVTSOLVE_IR, "petro"), {"PVTSOLVE": PVTSOLVE}
    )
    client = TestClient(
        _service_app(monkeypatch, tmp_path, namespace), raise_server_exceptions=False
    )

    body = client.post("/PVTSOLVE?api=30").json()

    assert body["detail"] == "RuntimeError: could not open /opt/prod/decks/fluid.dat"
    # Still correlatable — the escape hatch adds a field, it does not replace
    # the contract.
    assert len(body["error_id"]) == 12


def test_the_generated_size_guard_keeps_its_own_422_and_detail(monkeypatch, tmp_path):
    """An HTTPException raised by the router must not become a 500.

    This is the generated `n != len(props)` guard going through the SAME app
    that installs the catch-all handler, which is the arrangement that could
    flatten it.
    """
    namespace = _exec_router(
        python_pkg_gen.generate_router_py(ARRAY_IR, "petro"),
        {"PVTSTATE": lambda *a: 1.0},
    )
    client = TestClient(
        _service_app(monkeypatch, tmp_path, namespace), raise_server_exceptions=False
    )

    response = client.post("/PVTSTATE?pressure=2000&n=9", json=[1.0, 2.0, 3.0])

    assert response.status_code == 422
    assert "len(props)" in response.json()["detail"]


# --- 5. /healthz is liveness, /readyz is readiness -------------------------


@pytest.fixture
def restore_sigterm():
    """Put SIGTERM back however it was found.

    These tests signal themselves. Leaving a handler installed would change
    the behaviour of whatever runs next in the session — which is a real
    hazard here, because another module in this suite does exactly that.
    """
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


@pytest.fixture
def health_client(monkeypatch, tmp_path, restore_sigterm):
    namespace = _exec_router(
        python_pkg_gen.generate_router_py(PVTSOLVE_IR, "petro"),
        {"PVTSOLVE": lambda api: api},
    )
    return TestClient(_service_app(monkeypatch, tmp_path, namespace))


def test_healthz_and_readyz_are_distinct_routes_with_distinct_bodies(health_client):
    live = health_client.get("/healthz")
    ready = health_client.get("/readyz")

    assert live.status_code == 200
    assert live.json() == {"status": "ok", "service": "petro"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "service": "petro"}
    # Different words on purpose: "ok" is "this process is alive", "ready" is
    # "send me work". An operator reading a probe log must be able to tell
    # which question was answered.
    assert live.json()["status"] != ready.json()["status"]


def test_liveness_stays_up_while_readiness_reports_draining(health_client):
    """The whole reason they are two routes.

    A draining worker is deliberately not ready and perfectly alive. If
    liveness followed readiness the orchestrator would kill it mid-request
    instead of letting it finish, turning every rolling deploy into dropped
    work. tests/test_k8s_gen.py pins the manifests pointing each probe at the
    right route; this pins the routes themselves behaving differently.
    """
    assert health_client.get("/healthz").status_code == 200
    assert health_client.get("/readyz").status_code == 200

    os.kill(os.getpid(), signal.SIGTERM)

    assert health_client.get("/readyz").status_code == 503
    assert health_client.get("/healthz").status_code == 200
    assert health_client.get("/healthz").json()["status"] == "ok"
    # And native work is still served: draining means "no new work from the
    # load balancer", not "stop answering".
    assert health_client.post("/PVTSOLVE?api=30").json()["result"] == 30.0


def test_liveness_does_not_probe_the_native_extension_on_every_call(health_client):
    """Pinned because the docstring claims it, and it is load-bearing.

    /healthz says "the process is up and the native extension imported". It
    proves that by EXISTING — router.py imports the extension at module scope,
    so a worker that failed to load it never serves anything. Making the probe
    call a native routine would put every liveness check behind the process
    wide lock, so a slow simulation would look like a dead worker and get the
    container killed.
    """
    source = python_pkg_gen.generate_service_py("petro")

    assert "_NATIVE_LOCK" not in source
    # Asserted on the parsed body, not the text: the docstring says "the
    # native extension imported", which any substring check would trip over.
    healthz = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "healthz"
    )
    statements = [n for n in healthz.body if not _is_docstring(n)]
    assert len(statements) == 1
    assert isinstance(statements[0], ast.Return)
    # A literal dict: no call, so nothing that could touch native code.
    assert not [n for n in ast.walk(statements[0]) if isinstance(n, ast.Call)]


def _is_docstring(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)


# --- 6. /_unexposed is part of the contract -------------------------------


def test_unexposed_is_served_by_the_full_service_not_only_the_router(
    monkeypatch, tmp_path
):
    # It is a route on the generated router, so it survives being mounted into
    # the real app behind the real middleware. That is what makes it a contract
    # a caller can rely on rather than a debugging aid.
    # Generate once: two calls could drift, and the stubs must be built for the
    # same source that gets exec'd.
    code = python_pkg_gen.generate_router_py(WIDE_FORTRAN_IR, "petro")
    namespace = _exec_router(
        code, {name: (lambda *a, **k: 1.0) for name in _native_names(code)}
    )
    client = TestClient(_service_app(monkeypatch, tmp_path, namespace))

    response = client.get("/_unexposed")

    assert response.status_code == 200
    assert response.json() == {"PVTPACK": "derived-type Fortran result"}


def test_an_empty_unexposed_mapping_is_a_200_and_not_a_404(monkeypatch, tmp_path):
    """"refused nothing" and "cannot tell you" must be different answers.

    A caller integrating against a generated service needs to distinguish a
    build that bound everything from one too old to have the route at all.
    200 {} versus 404 is that distinction, and it only holds if the route is
    emitted unconditionally.
    """
    namespace = _exec_router(
        python_pkg_gen.generate_router_py(PVTSOLVE_IR, "petro"),
        {"PVTSOLVE": lambda api: api},
    )
    client = TestClient(_service_app(monkeypatch, tmp_path, namespace))

    assert client.get("/_unexposed").status_code == 200
    assert client.get("/_unexposed").json() == {}
    # The negative control for the claim above.
    assert client.get("/_no_such_introspection_route").status_code == 404


def test_unexposed_is_declared_in_the_openapi_schema(monkeypatch, tmp_path):
    # Part of the contract means part of the published schema, not an
    # undocumented URL that happens to answer.
    # Generate once: two calls could drift, and the stubs must be built for the
    # same source that gets exec'd.
    code = python_pkg_gen.generate_router_py(WIDE_FORTRAN_IR, "petro")
    namespace = _exec_router(
        code, {name: (lambda *a, **k: 1.0) for name in _native_names(code)}
    )
    schema = TestClient(_service_app(monkeypatch, tmp_path, namespace)).get(
        "/openapi.json"
    ).json()

    assert "/_unexposed" in schema["paths"]
    # Tagged both with the service (from the APIRouter) and "introspection",
    # so generated docs group it away from the numerical endpoints.
    assert schema["paths"]["/_unexposed"]["get"]["tags"] == ["petro", "introspection"]


# --- known gaps, recorded rather than papered over -------------------------


def test_the_lock_is_per_interpreter_and_does_not_span_workers():
    """GAP 1. Characterised, not fixed.

    Two independent imports of the generated router — which is what two
    gunicorn workers, two pods or two replicas are — hold two different locks.
    Each also has its own copy of the Fortran COMMON storage, because COMMON is
    process-global and not machine-global, so within a worker the lock is
    sufficient and across workers there is nothing to serialise. The hazard
    that DOES survive is a caller whose configure and compute requests are
    load-balanced to different workers; no lock nativegate can emit fixes that.
    """
    code = python_pkg_gen.generate_router_py(PVTSOLVE_IR, "petro")

    first = _exec_router(code, {"PVTSOLVE": lambda api: api})
    second = _exec_router(code, {"PVTSOLVE": lambda api: api})

    assert first["_NATIVE_LOCK"] is not second["_NATIVE_LOCK"]
    # Holding one leaves the other completely free.
    with first["_NATIVE_LOCK"]:
        assert second["_NATIVE_LOCK"].acquire(blocking=False)
        second["_NATIVE_LOCK"].release()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP 2: MAX_ARRAY_ITEMS is a memory-exhaustion cap, not the routine's "
        "declared extent. The IR records that a parameter IS an array, never "
        "how long the routine expects it to be, so a 100-element body for a "
        "routine declared REAL*8 PROPS(8) is accepted and the mismatch is only "
        "caught by the separate n-vs-len(props) pairing when a size argument "
        "happens to exist. Recovering real extents needs fparser2 (ROADMAP "
        "1.4); this turns green on its own when it lands."
    ),
)
def test_the_array_cap_reflects_the_routines_real_declared_extent():
    code = python_pkg_gen.generate_router_py(ARRAY_IR, "petro")

    # What a caller would expect from a bound that claimed to protect the
    # native buffer: the extent, not one global number for every array in the
    # service.
    assert "Field(max_length=MAX_ARRAY_ITEMS)" not in code


def test_an_array_without_a_size_argument_gets_no_extent_check_at_all():
    """The visible consequence of GAP 2, pinned so the hole is not a surprise.

    `NORMALIZE(VALUES, NVALUES)` gets the name-paired length guard. An array
    parameter with no size argument beside it gets only the 65 536-element cap,
    which will not stop a 10-element body reaching a routine that reads 64.
    """
    mod = fortran_module(
        functions=[
            FunctionDef(
                name="SMOOTH",
                parameters=[Parameter(name="window", type="float", is_array=True)],
                is_subroutine=True,
            )
        ]
    )

    code = python_pkg_gen.generate_router_py(mod, "petro")

    assert "Field(max_length=MAX_ARRAY_ITEMS)" in code
    assert "!= len(" not in code
    # And the guard from section 1 still holds for it.
    assert _unlocked_native_calls(code) == []
