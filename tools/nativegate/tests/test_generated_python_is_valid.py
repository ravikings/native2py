"""Every generator entry point must emit Python that parses (defect D3).

Before this, the only place that ever compiled generated output was one
assertion in tests/test_golden.py. That is why a Fortran `&` line continuation
reached a committed router.py and shipped as a SyntaxError nobody noticed, and
why `def attenuate(lambda: float)` was possible at all. The point of this
module is that no IR shape reaches a container without parsing first.
"""

import pytest

from nativegate.generators import (
    gateway_gen,
    golden_gen,
    python_pkg_gen,
    test_gen,
)
from nativegate.ir import (
    ClassDef,
    FunctionDef,
    Method,
    ModuleIR,
    Parameter,
    StructDef,
)


def _module(**kwargs) -> ModuleIR:
    base = dict(name="svc", language="cpp", source_file="svc.hpp")
    base.update(kwargs)
    return ModuleIR(**base)


# A deliberately awkward spread: C++ classes and structs, Fortran functions and
# subroutines, arrays, every intent, keyword names, overloads, and nothing at all.
IR_SHAPES = {
    "empty": _module(),
    "cpp_free_function": _module(
        functions=[
            FunctionDef(
                name="circle_area",
                parameters=[Parameter(name="r", type="float")],
                returns="float",
            )
        ]
    ),
    "cpp_class": _module(
        classes=[
            ClassDef(
                name="Simulator",
                methods=[
                    Method(name="advance_to", parameters=[Parameter(name="t", type="float")]),
                    Method(name="pressure", returns="float", is_const=True),
                    Method(name="build", returns="float", is_static=True),
                ],
                has_default_constructor=False,
                constructors=[[Parameter(name="dt", type="float")]],
            )
        ]
    ),
    "cpp_struct": _module(
        structs=[
            StructDef(
                name="Sample",
                fields=[
                    Parameter(name="depth", type="float"),
                    Parameter(name="trace", type="float", is_array=True),
                ],
            )
        ],
        functions=[
            FunctionDef(
                name="take",
                parameters=[Parameter(name="s", type="Sample")],
                returns="Sample",
            )
        ],
    ),
    "cpp_void_and_bool": _module(
        functions=[
            FunctionDef(
                name="reset",
                parameters=[Parameter(name="hard", type="bool")],
                returns="None",
            )
        ]
    ),
    "cpp_overloads": _module(
        classes=[
            ClassDef(
                name="Pvt",
                methods=[
                    Method(name="viscosity", parameters=[Parameter(name="p", type="float")], returns="float"),
                    Method(
                        name="viscosity",
                        parameters=[Parameter(name="p", type="float"), Parameter(name="t", type="float")],
                        returns="float",
                    ),
                    Method(name="viscosity", returns="float"),
                ],
            )
        ]
    ),
    "cpp_python_keywords": _module(
        classes=[
            ClassDef(
                name="Filter",
                methods=[
                    Method(
                        name="pass",
                        parameters=[Parameter(name="lambda", type="float"), Parameter(name="from", type="float")],
                        returns="float",
                    )
                ],
            )
        ],
        structs=[StructDef(name="Beam", fields=[Parameter(name="in", type="float")])],
        functions=[
            FunctionDef(
                name="attenuate",
                parameters=[Parameter(name="lambda", type="float"), Parameter(name="global", type="int")],
                returns="float",
            )
        ],
    ),
    "fortran_function": _module(
        name="physics",
        language="fortran",
        source_file="physics.f90",
        functions=[
            FunctionDef(
                name="calculate_pressure",
                parameters=[Parameter(name="depth", type="float")],
                returns="float",
            )
        ],
    ),
    "fortran_subroutine_intents": _module(
        name="petro",
        language="fortran",
        source_file="petro.f",
        functions=[
            FunctionDef(
                name="TRAVER",
                parameters=[
                    Parameter(name="pwh", type="float", intent="in"),
                    Parameter(name="nseg", type="int", intent="inout"),
                    Parameter(name="pbh", type="float", intent="out"),
                ],
                is_subroutine=True,
            )
        ],
    ),
    "fortran_arrays": _module(
        name="grid",
        language="fortran",
        source_file="grid.f90",
        functions=[
            FunctionDef(
                name="smooth",
                parameters=[
                    Parameter(name="cells", type="float", is_array=True),
                    Parameter(name="out", type="float", is_array=True, intent="out"),
                ],
                is_subroutine=True,
            )
        ],
    ),
    "fortran_module_scoped": _module(
        name="reservoir",
        language="fortran",
        source_file="reservoir.f90",
        functions=[
            FunctionDef(
                name="pressure",
                parameters=[Parameter(name="d", type="float")],
                returns="float",
                fortran_module="reservoir_mod",
            ),
            FunctionDef(name="PVTINI", is_subroutine=True),
        ],
    ),
    "fortran_keyword_routine": _module(
        name="legacy",
        language="fortran",
        source_file="legacy.f",
        functions=[
            FunctionDef(
                name="lambda",
                parameters=[Parameter(name="pass", type="float")],
                returns="float",
            )
        ],
    ),
}


@pytest.mark.parametrize("shape", sorted(IR_SHAPES))
def test_generated_package_files_compile(shape):
    module = IR_SHAPES[shape]

    for filename, source in [
        ("__init__.py", python_pkg_gen.generate_init_py(module)),
        ("router.py", python_pkg_gen.generate_router_py(module, module.name)),
        ("service.py", python_pkg_gen.generate_service_py(module.name)),
        ("test_python_api.py", test_gen.generate_python_api_test(module, module.name)),
        (
            "test_golden.py",
            golden_gen.generate_golden_test(module.name, module.name, "golden.json"),
        ),
    ]:
        try:
            compile(source, filename, "exec")
        except SyntaxError as exc:  # pragma: no cover - the failure we exist for
            pytest.fail(f"{shape}/{filename} line {exc.lineno}: {exc.msg}\n{source}")


def test_generated_gateway_compiles():
    source = gateway_gen.generate_gateway_app("platform", ["pvt", "reservoir"])
    compile(source, "app.py", "exec")


def test_gateway_refuses_a_service_name_that_cannot_be_imported():
    with pytest.raises(ValueError):
        gateway_gen.generate_gateway_app("platform", ["lambda"])


# --- no endpoint may shadow another (A5) ---------------------------------


@pytest.mark.parametrize("shape", sorted(IR_SHAPES))
def test_router_defines_every_endpoint_exactly_once(shape):
    """A duplicate `def` compiles perfectly well and silently loses a route."""
    import ast

    tree = ast.parse(python_pkg_gen.generate_router_py(IR_SHAPES[shape], "svc"))
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert len(names) == len(set(names)), f"{shape}: shadowed endpoint(s) in {names}"

    routes = [
        d.args[0].value
        for n in tree.body
        if isinstance(n, ast.FunctionDef)
        for d in n.decorator_list
        if isinstance(d, ast.Call) and d.args
    ]
    assert len(routes) == len(set(routes)), f"{shape}: duplicate route(s) in {routes}"


# --- determinism ----------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(IR_SHAPES))
def test_generation_is_byte_identical_when_repeated(shape):
    """Two runs over the same IR must produce the same bytes.

    Anything derived from set iteration or dict ordering makes `generate`
    produce a spurious diff on every run, which trains reviewers to ignore the
    diff — and hides a real change when one lands.
    """
    module = IR_SHAPES[shape]
    for generate in (
        lambda m: python_pkg_gen.generate_init_py(m),
        lambda m: python_pkg_gen.generate_router_py(m, m.name),
        lambda m: python_pkg_gen.generate_service_py(m.name),
        lambda m: test_gen.generate_python_api_test(m, m.name),
        # Covered for compilation elsewhere in this file, and both build their
        # output from dict/set-derived orderings — exactly what this test
        # exists to catch — so they belong here too.
        lambda m: golden_gen.generate_golden_test(m.name, m.name, "golden.json"),
    ):
        assert generate(module).encode() == generate(module).encode()


def test_gateway_generation_is_byte_identical_when_repeated():
    """The gateway builds its imports and mounts by iterating service names."""
    services = ["petro_api", "fluid", "wellbore"]
    for generate in (
        lambda: gateway_gen.generate_gateway_app("gw", services),
        lambda: gateway_gen.generate_gateway_pyproject("gw", "gw_pkg", services),
    ):
        assert generate().encode() == generate().encode()


# --- the CLI gate itself (D3a) --------------------------------------------


def test_the_cli_refuses_to_write_python_that_will_not_parse(tmp_path):
    import click

    from nativegate import cli

    target = tmp_path / "router.py"
    broken = 'def viscosity(x: float):\n    return {"result": viscosity(x &\n'
    module = _module(
        classes=[ClassDef(name="Pvt", methods=[Method(name="viscosity", returns="float")])]
    )

    with pytest.raises(click.ClickException) as caught:
        cli._write_python(target, broken, module)

    message = str(caught.value)
    assert "router.py" in message
    assert "line 2" in message
    assert "viscosity" in message  # the implicated symbol
    # A file that will not parse must never reach disk, let alone a container.
    assert not target.exists()


def test_the_cli_writes_python_that_does_parse(tmp_path):
    from nativegate import cli

    target = tmp_path / "ok.py"
    cli._write_python(target, "x = 1\n")
    assert target.read_text() == "x = 1\n"


def test_the_cli_rejects_a_name_that_cannot_be_spelled_in_python():
    import click

    from nativegate import cli

    module = _module(functions=[FunctionDef(name="operator+", returns="float")])

    with pytest.raises(click.ClickException) as caught:
        cli._validate_module(module)

    assert "operator+" in str(caught.value)


# --- A6: free-form INCLUDE ------------------------------------------------


def _fortran_service(tmp_path):
    from nativegate.config import ServiceConfig

    service_dir = tmp_path / "services" / "grid"
    (service_dir / "native").mkdir(parents=True)
    (service_dir / "python" / "grid").mkdir(parents=True)
    (service_dir / "tests").mkdir(parents=True)
    config = ServiceConfig(name="grid", language="fortran")
    config.expose.functions = ["smooth"]
    return service_dir, config


def test_free_form_include_is_expanded_into_a_generated_copy(tmp_path):
    # f2py silently mis-wraps a routine containing an in-body INCLUDE: the
    # extension builds and imports, but arguments never arrive and calls return
    # uninitialized memory. That protection was applied to fixed-form decks
    # only, so a .f90 with `include 'GRID.INC'` got none of it.
    from nativegate import cli

    service_dir, config = _fortran_service(tmp_path)
    native = service_dir / "native"
    (native / "GRID.INC").write_text("      real(8) :: cell_size\n      common /grid/ cell_size\n")
    source = native / "grid.f90"
    original = (
        "subroutine smooth(cells)\n"
        "  real(8), intent(inout) :: cells\n"
        "  include 'GRID.INC'\n"
        "  cells = cells * cell_size\n"
        "end subroutine smooth\n"
    )
    source.write_text(original)

    cli._generate_fortran_service(service_dir, config)

    expanded = (service_dir / "native" / "_expanded" / "grid.f90").read_text()
    assert "common /grid/ cell_size" in expanded, "the INCLUDE body was not inlined"
    assert "include 'GRID.INC'" not in expanded
    # The marker must be a FREE-form comment: a "C" in column 1 is a comment in
    # fixed form only, and a syntax error in a .f90.
    assert "--- nativegate: expanded INCLUDE" in expanded
    for line in expanded.splitlines():
        if "nativegate:" in line:
            assert line.lstrip().startswith("!"), line

    # The original source is never touched.
    assert source.read_text() == original

    cmake = (service_dir / "CMakeLists.txt").read_text()
    assert "native/_expanded/grid.f90" in cmake
    assert "native/grid.f90" not in cmake


def test_free_form_source_without_an_include_is_not_copied(tmp_path):
    from nativegate import cli

    service_dir, config = _fortran_service(tmp_path)
    (service_dir / "native" / "grid.f90").write_text(
        "subroutine smooth(cells)\n"
        "  real(8), intent(inout) :: cells\n"
        "  cells = cells * 2.0\n"
        "end subroutine smooth\n"
    )

    cli._generate_fortran_service(service_dir, config)

    assert not (service_dir / "native" / "_expanded").exists()
    assert "native/grid.f90" in (service_dir / "CMakeLists.txt").read_text()
