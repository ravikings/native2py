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
