"""The generated FastAPI layer, for signatures real numerical APIs actually have.

Every case here previously produced a service that *imported* wrongly rather
than failing a build: a TypeError at module scope, a 500 from the JSON
encoder, or a FastAPI error while registering a route. A generated service
that cannot start is worse than one that refuses a symbol, because nothing
says which symbol caused it.
"""

from native2py.generators import python_pkg_gen, test_gen
from native2py.ir import ClassDef, FunctionDef, Method, ModuleIR, Parameter, StructDef


def module(**kwargs) -> ModuleIR:
    base = dict(name="pvt", language="cpp", source_file="pvt.hpp")
    base.update(kwargs)
    return ModuleIR(**base)


def router(mod: ModuleIR) -> str:
    return python_pkg_gen.generate_router_py(mod, "pvt")


# --- constructors --------------------------------------------------------


def test_class_without_a_default_constructor_gets_its_arguments_from_the_request():
    # `pvtmodel = PvtModel()` at module scope was a TypeError at import: the
    # service never started, and nothing named the class responsible.
    mod = module(
        classes=[
            ClassDef(
                name="PvtModel",
                methods=[
                    Method(
                        name="oil_fvf",
                        parameters=[Parameter(name="pressure", type="float")],
                        returns="float",
                    )
                ],
                has_default_constructor=False,
                constructors=[
                    [Parameter(name="api", type="float"), Parameter(name="corr", type="int")]
                ],
            )
        ]
    )

    code = router(mod)

    assert "def oil_fvf(api: float, corr: int, pressure: float):" in code
    assert "    instance = PvtModel(api, corr)" in code
    assert "PvtModel()" not in code


def test_instances_are_built_per_request_not_per_process():
    # One module-scope instance is shared mutable state across every
    # concurrent request; a simulation object is stateful by definition.
    mod = module(
        classes=[
            ClassDef(name="Simulator", methods=[Method(name="advance", returns="float")])
        ]
    )

    code = router(mod)

    assert "    instance = Simulator()" in code
    assert "\nsimulator = Simulator()" not in code


def test_class_with_no_callable_constructor_is_skipped_with_a_reason():
    mod = module(
        classes=[
            ClassDef(
                name="Correlation",
                methods=[
                    Method(
                        name="apply",
                        parameters=[Parameter(name="x", type="float")],
                        returns="float",
                    )
                ],
                has_default_constructor=False,
                constructors=[],
            )
        ]
    )

    code = router(mod)

    assert "Correlation()" not in code
    assert '@router.post("/apply")' not in code
    assert "no public constructor" in code


def test_static_methods_need_no_instance():
    mod = module(
        classes=[
            ClassDef(
                name="Units",
                methods=[
                    Method(
                        name="psi_to_bar",
                        parameters=[Parameter(name="psi", type="float")],
                        returns="float",
                        is_static=True,
                    )
                ],
                has_default_constructor=False,
                constructors=[],
            )
        ]
    )

    code = router(mod)

    assert "    return {\"result\": Units.psi_to_bar(psi)}" in code


def test_constructor_argument_colliding_with_a_method_argument_is_renamed():
    mod = module(
        classes=[
            ClassDef(
                name="Grid",
                methods=[
                    Method(
                        name="at",
                        parameters=[Parameter(name="n", type="int")],
                        returns="float",
                    )
                ],
                has_default_constructor=False,
                constructors=[[Parameter(name="n", type="int")]],
            )
        ]
    )

    code = router(mod)

    assert "def at(n_init: int, n: int):" in code
    assert "    instance = Grid(n_init)" in code


# --- structs over HTTP ---------------------------------------------------


STATE = StructDef(
    name="PvtState",
    fields=[Parameter(name="bo", type="float"), Parameter(name="rs", type="float")],
)


def test_struct_return_is_serialised_through_a_pydantic_model():
    # Returning the pybind11 object gave a 500 from the JSON encoder at
    # request time, with the struct bound perfectly well underneath.
    mod = module(
        structs=[STATE],
        classes=[
            ClassDef(
                name="PvtModel",
                methods=[
                    Method(
                        name="properties_at",
                        parameters=[Parameter(name="p", type="float")],
                        returns="PvtState",
                    )
                ],
            )
        ],
    )

    code = router(mod)

    assert "class PvtStateModel(BaseModel):" in code
    assert "    bo: float" in code
    assert 'return {"bo": native.bo, "rs": native.rs}' in code
    assert '{"result": _from_PvtState(instance.properties_at(p))}' in code


def test_struct_parameter_is_parsed_from_the_request_body():
    mod = module(
        structs=[STATE],
        classes=[
            ClassDef(
                name="Report",
                methods=[
                    Method(
                        name="summarise",
                        parameters=[Parameter(name="state", type="PvtState")],
                        returns="float",
                    )
                ],
            )
        ],
    )

    code = router(mod)

    assert "def summarise(state: PvtStateModel):" in code
    assert "instance.summarise(_to_PvtState(state))" in code
    assert "def _to_PvtState(model: PvtStateModel):" in code
    assert "    native.bo = model.bo" in code


def test_nested_struct_fields_convert_recursively():
    inner = StructDef(name="Inner", fields=[Parameter(name="v", type="float")])
    outer = StructDef(name="Outer", fields=[Parameter(name="inner", type="Inner")])
    mod = module(
        structs=[inner, outer],
        functions=[FunctionDef(name="build", returns="Outer")],
    )

    code = router(mod)

    assert '"inner": _from_Inner(native.inner)' in code
    assert "    native.inner = _to_Inner(model.inner)" in code


def test_free_function_returning_a_struct_is_converted():
    mod = module(
        structs=[STATE],
        functions=[
            FunctionDef(
                name="flash",
                parameters=[Parameter(name="p", type="float")],
                returns="PvtState",
            )
        ],
    )

    code = router(mod)

    assert '{"result": _from_PvtState(flash(p))}' in code


# --- signatures that cannot be served ------------------------------------


def test_bound_class_parameter_gets_no_endpoint_and_says_why():
    # FastAPI raises while *registering* a route whose annotation it cannot
    # model, so this took down the whole service, not one endpoint.
    mod = module(
        classes=[
            ClassDef(name="PvtModel", methods=[Method(name="v", returns="float")]),
            ClassDef(
                name="Registry",
                methods=[
                    Method(
                        name="adopt",
                        parameters=[Parameter(name="model", type="PvtModel")],
                        returns="None",
                    ),
                    Method(name="count", returns="int"),
                ],
            ),
        ]
    )

    code = router(mod)

    assert '@router.post("/adopt")' not in code
    assert "Registry.adopt: no endpoint" in code
    assert "bound class (PvtModel)" in code
    assert '@router.post("/count")' in code  # the rest of the class survives


def test_bound_class_return_gets_no_endpoint():
    mod = module(
        classes=[
            ClassDef(name="PvtModel", methods=[Method(name="v", returns="float")]),
            ClassDef(
                name="Factory", methods=[Method(name="make", returns="PvtModel")]
            ),
        ]
    )

    code = router(mod)

    assert '@router.post("/make")' not in code
    assert "returns a bound class (PvtModel)" in code


def test_void_method_returns_ok_not_null_result():
    mod = module(
        classes=[
            ClassDef(
                name="Sim",
                methods=[
                    Method(
                        name="set_dt",
                        parameters=[Parameter(name="dt", type="float")],
                        returns="None",
                    )
                ],
            )
        ]
    )

    code = router(mod)

    assert "    instance.set_dt(dt)" in code
    assert '    return {"ok": True}' in code


def test_fortran_endpoints_are_unchanged():
    # The Fortran path has its own intent-aware generator; this change must
    # not have reached into it.
    mod = module(
        language="fortran",
        functions=[
            FunctionDef(
                name="blend",
                parameters=[
                    Parameter(name="a", type="float"),
                    Parameter(name="mixed", type="float", intent="out"),
                ],
                is_subroutine=True,
            )
        ],
    )

    code = python_pkg_gen.generate_router_py(mod, "mix")

    assert "def blend_endpoint(a: float):" in code
    assert '    return {"mixed": mixed}' in code


# --- the generated smoke tests -------------------------------------------


def test_generated_test_constructs_with_arguments():
    mod = module(
        classes=[
            ClassDef(
                name="PvtModel",
                methods=[Method(name="v", returns="float")],
                has_default_constructor=False,
                constructors=[
                    [Parameter(name="api", type="float"), Parameter(name="corr", type="int")]
                ],
            )
        ]
    )

    code = test_gen.generate_python_api_test(mod, "pvt")

    assert "    instance = PvtModel(1.0, 1)" in code


def test_generated_test_does_not_instantiate_an_abstract_class():
    mod = module(
        classes=[
            ClassDef(
                name="Correlation",
                methods=[
                    Method(name="version", returns="float", is_static=True),
                    Method(name="apply", returns="float"),
                ],
                has_default_constructor=False,
                constructors=[],
            )
        ]
    )

    code = test_gen.generate_python_api_test(mod, "pvt")

    assert "Correlation()" not in code
    assert "    Correlation.version()" in code


def test_generated_test_imports_every_type_it_names():
    mod = module(
        classes=[
            ClassDef(
                name="PvtModel",
                methods=[Method(name="v", returns="float")],
                has_default_constructor=False,
                constructors=[[Parameter(name="api", type="float")]],
            ),
            ClassDef(
                name="Registry",
                methods=[
                    Method(
                        name="adopt",
                        parameters=[Parameter(name="model", type="PvtModel")],
                        returns="None",
                    )
                ],
            ),
        ]
    )

    code = test_gen.generate_python_api_test(mod, "pvt")

    assert "    from pvt import Registry, PvtModel" in code
    assert "    instance.adopt(PvtModel(1.0))" in code


def test_generated_test_skips_calls_it_cannot_build_arguments_for():
    mod = module(
        classes=[
            ClassDef(
                name="Abstract",
                methods=[Method(name="apply", returns="float")],
                has_default_constructor=False,
                constructors=[],
            ),
            ClassDef(
                name="Holder",
                methods=[
                    Method(
                        name="take",
                        parameters=[Parameter(name="a", type="Abstract")],
                        returns="None",
                    )
                ],
            ),
        ]
    )

    code = test_gen.generate_python_api_test(mod, "pvt")

    assert "instance.take(" not in code
    assert "cannot construct" in code
