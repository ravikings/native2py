"""The shared-native-state contract of THIS service's checked-in source.

Hand-written, not generated: `native2py generate` will not overwrite it.

tools/native2py/tests/test_native_state_contract.py pins what the generator
EMITS. This pins what is actually committed under python/petro_api/, which is
the thing that gets built into the wheel and deployed. The two can drift: the
generated files are checked in, so a hand edit here — the "just this once, for
throughput" change the lock's own comment warns about — would never be seen by
the generator's suite.

Deliberately source-level and import-free. Every other module in this
directory imports `petro_api`, so they only run once the native extension has
been built; these run anywhere, which is the point of a guard against a commit.

libraries/petro is F77 with COMMON /FLUID/: PVTSET writes it and PVTRS, PVTBO
and PVTVIS read it back. Two requests configuring different fluids inside one
worker share that storage. Nothing raises — the answers are just wrong — so
the only thing standing between this service and silently wrong numbers is the
router holding _NATIVE_LOCK across every native call.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "python" / "petro_api"


def _source(name: str) -> str:
    path = PACKAGE / name
    if not path.exists():  # pragma: no cover - a regenerate that dropped a file
        pytest.fail(f"{path} is missing; the service is not generated")
    return path.read_text()


def _native_symbols() -> set[str]:
    """Whatever __init__.py re-exports from the compiled extension."""
    tree = ast.parse(_source("__init__.py"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            assert isinstance(node.value, ast.List)
            return {
                element.value
                for element in node.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
    pytest.fail("__init__.py no longer declares __all__")


def _endpoints(tree: ast.Module) -> list[ast.FunctionDef]:
    def routed(fn: ast.FunctionDef) -> bool:
        for dec in fn.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "router"
            ):
                return True
        return False

    return [n for n in tree.body if isinstance(n, ast.FunctionDef) and routed(n)]


def _holds_lock(node: ast.With) -> bool:
    return any(
        isinstance(item.context_expr, ast.Name)
        and item.context_expr.id == "_NATIVE_LOCK"
        for item in node.items
    )


# --- the lock ------------------------------------------------------------


def test_the_router_declares_the_process_wide_native_lock():
    tree = ast.parse(_source("router.py"))

    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_NATIVE_LOCK" for t in node.targets)
    ]
    assert len(assignments) == 1
    call = assignments[0].value
    # RLock, not Lock: one endpoint calling another must not deadlock the
    # request against itself.
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Attribute)
    assert call.func.attr == "RLock"
    assert isinstance(call.func.value, ast.Name)
    assert call.func.value.id == "threading"


def test_no_endpoint_in_this_service_calls_native_code_outside_the_lock():
    """The regression that must fail CI.

    COMMON /FLUID/ is process-global storage. FastAPI runs synchronous `def`
    endpoints in a threadpool, so dropping the lock on ANY one endpoint lets
    two requests interleave their writes and reads in the same COMMON block.
    """
    source = _source("router.py")
    native = _native_symbols()
    tree = ast.parse(source)
    offenders: list[str] = []

    def visit(node: ast.AST, endpoint: str, locked: bool) -> None:
        if isinstance(node, ast.With):
            locked = locked or _holds_lock(node)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in native
            and not locked
        ):
            offenders.append(f"{endpoint} calls {node.func.id} outside _NATIVE_LOCK")
        for child in ast.iter_child_nodes(node):
            visit(child, endpoint, locked)

    endpoints = _endpoints(tree)
    for fn in endpoints:
        for stmt in fn.body:
            visit(stmt, fn.name, locked=False)

    assert offenders == []
    # Not vacuous: the endpoints exist and really do reach the extension.
    assert len(endpoints) >= len(native)


def test_every_bound_symbol_is_reachable_and_locked():
    # A symbol exported by __init__.py but never called from a locked block
    # would mean an endpoint was regenerated into some other shape.
    source = _source("router.py")
    tree = ast.parse(source)
    native = _native_symbols()

    locked_calls = {
        node.func.id
        for fn in _endpoints(tree)
        for with_stmt in ast.walk(fn)
        if isinstance(with_stmt, ast.With) and _holds_lock(with_stmt)
        for node in ast.walk(with_stmt)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert native <= locked_calls, f"never called under the lock: {native - locked_calls}"


def test_the_lock_comment_survives_regeneration():
    # The reason has to live in the file a maintainer opens, not only in docs.
    source = _source("router.py")

    assert "COMMON" in source
    assert "threadpool" in source
    assert "DO NOT REMOVE THIS TO IMPROVE THROUGHPUT" in source


def test_array_marshalling_stays_outside_the_critical_section():
    # numpy conversion touches no COMMON storage; holding the lock across it
    # would lengthen the critical section for no correctness gain.
    source = _source("router.py")

    assert source.index("props = np.array(props") < source.index("props = pvt_state(")


# --- input bounds --------------------------------------------------------


def test_the_array_cap_is_declared_configurable_and_defaults_to_65536():
    source = _source("router.py")

    assert 'MAX_ARRAY_ITEMS = int(os.environ.get("NATIVE2PY_MAX_ARRAY_ITEMS", "65536"))' in source
    assert "Field(max_length=MAX_ARRAY_ITEMS)" in source
    # Every array parameter, not just the first one found.
    tree = ast.parse(source)
    array_args = [
        arg
        for fn in _endpoints(tree)
        for arg in fn.args.args
        if isinstance(arg.annotation, ast.Subscript)
    ]
    assert array_args, "no array endpoint left; drop this test with it"
    for arg in array_args:
        assert arg.annotation is not None
        assert "MAX_ARRAY_ITEMS" in ast.unparse(arg.annotation)


def test_a_size_argument_is_checked_against_its_array_before_the_lock():
    # n is the extent Fortran uses for props. Letting them disagree is an
    # out-of-bounds read inside the native routine, not a wrong answer, so it
    # must be refused before anything native runs.
    source = _source("router.py")

    guard = source.index("if n != len(props):")
    assert guard < source.index("with _NATIVE_LOCK:\n        props = pvt_state(")
    assert "status_code=422" in source


# --- error, health and introspection contract ----------------------------


def test_the_service_answers_a_failed_request_with_a_correlatable_json_body():
    source = _source("service.py")

    assert '"error": "internal server error"' in source
    assert '"error_id": error_id' in source
    # Off by default: native exception text can carry deck values and build
    # machine paths.
    assert 'os.environ.get("NATIVE2PY_DEBUG_ERRORS", "")' in source
    assert "if _DEBUG_ERRORS:" in source
    # debug=True would defeat the handler entirely (Starlette consults it
    # first), so the generated app must not enable it.
    # Asserted on the parsed call, not the text: the handler's own docstring
    # names debug=True in order to warn about it.
    app_call = next(
        node.value
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "app" for t in node.targets)
    )
    assert isinstance(app_call, ast.Call)
    assert [kw.arg for kw in app_call.keywords] == ["title"]


def test_liveness_and_readiness_are_separate_and_liveness_is_the_cheap_one():
    service = _source("service.py")
    middleware = _source("middleware.py")

    # /healthz is defined in service.py and returns a literal.
    healthz = next(
        node
        for node in ast.parse(service).body
        if isinstance(node, ast.FunctionDef) and node.name == "healthz"
    )
    body = [
        n
        for n in healthz.body
        if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
    ]
    assert len(body) == 1 and isinstance(body[0], ast.Return)
    # No call at all: liveness must not go behind _NATIVE_LOCK, or a slow
    # simulation would look like a dead worker and get the container killed.
    assert not [n for n in ast.walk(body[0]) if isinstance(n, ast.Call)]
    assert "_NATIVE_LOCK" not in service

    # /readyz lives in middleware.py and DOES have a failing condition.
    assert '@app.get("/readyz"' in middleware
    assert "status_code=503" in middleware
    assert "_DRAINING" in middleware
    # Liveness has no such condition — that asymmetry is the whole point.
    assert "503" not in service


def test_unexposed_is_part_of_this_services_contract():
    source = _source("router.py")

    assert '@router.get("/_unexposed"' in source
    assert "_UNEXPOSED: dict[str, str] = {" in source
    # This build refused nothing, so the mapping is empty — which is a 200
    # with {}, distinct from a 404 meaning "this service cannot tell you".
    tree = ast.parse(source)
    unexposed = next(
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_UNEXPOSED"
    )
    assert isinstance(unexposed.value, ast.Dict)
    assert unexposed.value.keys == []


def test_health_and_readiness_never_require_an_api_key():
    # An orchestrator has no credentials. A liveness probe that 401s is a
    # service that looks dead and gets restarted forever.
    middleware = _source("middleware.py")

    assert '_UNAUTHENTICATED_PATHS = frozenset({"/healthz", "/readyz"})' in middleware


# --- known gaps ----------------------------------------------------------
#
# Recorded here so the limits are visible in the suite, not only in prose.
#
# GAP 1 — the lock is per interpreter. It serialises FastAPI's threadpool in
#   ONE worker. Two gunicorn workers, two pods or two replicas hold two locks
#   and two copies of COMMON. Within a worker that is sufficient; a caller
#   whose PVTSET and PVTRS requests are load-balanced to different workers is
#   NOT protected by anything in this repository, and no lock native2py can
#   emit would protect them. It needs session affinity or a combined endpoint.
#   Untestable from a single-process test suite, so it is stated, not asserted.
#
# GAP 2 — the lock covers one native call. `POST /pvt_set_fluid` followed by
#   `POST /solution_gor` releases it in between, so a second caller can
#   reconfigure COMMON in the gap. Pinned as a generator-level test in
#   tools/native2py/tests/test_service_gen.py.
#
# GAP 3 — MAX_ARRAY_ITEMS bounds request size, not the extent the Fortran
#   routine declares; the IR does not record extents (ROADMAP 1.4). For
#   /pvt_state the separate n-vs-len(props) guard covers it because a size
#   argument happens to exist. An array parameter with no size argument beside
#   it would have no extent check at all.
#
# GAP 4 — a segfault, a Fortran STOP or a hang kills the worker before any
#   Python handler runs, so the error contract above does not apply to those.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "GAP 1/2: nothing in the generated service makes a configure-then-read "
        "pair atomic. A per-caller session, or a combined endpoint that does "
        "PVTSET and PVTRS under one acquisition, is the fix; neither exists "
        "today. Delete this xfail when one does."
    ),
)
def test_a_configure_then_compute_pair_is_atomic():
    source = _source("router.py")

    # Either a single endpoint that configures and computes under one lock...
    combined = "with _NATIVE_LOCK:\n        pvt_set_fluid(" in source and (
        source.split("with _NATIVE_LOCK:\n        pvt_set_fluid(")[1]
        .split("\n    return")[0]
        .count("(")
        > 1
    )
    # ...or some notion of a caller-scoped session.
    session = "session" in source.lower()
    assert combined or session
