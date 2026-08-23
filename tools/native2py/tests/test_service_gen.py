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


# --- B4: Python keyword names ---------------------------------------------


def test_keyword_parameter_names_are_escaped_and_the_router_compiles():
    # `double attenuate(double lambda, double from)` is ordinary engineering
    # C++ and generated `def attenuate(lambda: float, from: float)` — a
    # SyntaxError that surfaced when the container started.
    mod = module(
        functions=[
            FunctionDef(
                name="attenuate",
                parameters=[
                    Parameter(name="lambda", type="float"),
                    Parameter(name="from", type="float"),
                ],
                returns="float",
            )
        ]
    )

    code = router(mod)

    compile(code, "router.py", "exec")
    assert "def attenuate_endpoint(lambda_: float, from_: float):" in code
    # Only the Python name moved: the route keeps the native spelling.
    assert '@router.post("/attenuate")' in code
    assert "attenuate(lambda_, from_)" in code


def test_keyword_symbol_name_is_escaped_everywhere_it_is_spelled():
    mod = module(
        functions=[
            FunctionDef(name="lambda", parameters=[Parameter(name="x", type="float")], returns="float")
        ]
    )

    init_py = python_pkg_gen.generate_init_py(mod)
    code = router(mod)

    compile(init_py, "__init__.py", "exec")
    compile(code, "router.py", "exec")
    # A keyword cannot appear in an import statement at all.
    assert 'lambda_ = getattr(_native, "lambda")' in init_py
    assert '__all__ = ["lambda_"]' in init_py
    assert "from . import lambda_" in code
    assert '@router.post("/lambda")' in code  # the wire contract is untouched
    assert "def lambda__endpoint(x: float):" in code


def test_keyword_method_name_is_reached_by_getattr():
    mod = module(
        classes=[ClassDef(name="Filter", methods=[Method(name="pass", returns="float")])]
    )

    code = router(mod)

    compile(code, "router.py", "exec")
    assert '@router.post("/pass")' in code
    assert 'getattr(instance, "pass")()' in code


def test_keyword_struct_field_is_escaped_but_the_native_member_is_not():
    mod = module(
        structs=[StructDef(name="Beam", fields=[Parameter(name="lambda", type="float")])],
        functions=[FunctionDef(name="emit", parameters=[], returns="Beam")],
    )

    code = router(mod)

    compile(code, "router.py", "exec")
    assert "    lambda_: float" in code
    assert 'getattr(native, "lambda")' in code
    assert 'setattr(native, "lambda", model.lambda_)' in code


def test_generated_smoke_test_escapes_keywords_and_compiles():
    mod = module(
        classes=[
            ClassDef(
                name="Filter",
                methods=[Method(name="pass", parameters=[Parameter(name="lambda", type="float")], returns="float")],
            )
        ],
        functions=[FunctionDef(name="from", parameters=[], returns="float")],
    )

    code = test_gen.generate_python_api_test(mod, "pvt")

    compile(code, "test_python_api.py", "exec")
    assert 'getattr(instance, "pass")(1.0)' in code
    assert "from pvt import from_" in code


# --- A5: overloads ---------------------------------------------------------


def test_overloaded_methods_each_get_a_reachable_endpoint():
    # Two `viscosity` overloads used to emit two `def viscosity(...)` and two
    # @router.post("/viscosity") in one module: the second def shadowed the
    # first and FastAPI dispatched the first route, so one overload was
    # unreachable with no warning.
    mod = module(
        classes=[
            ClassDef(
                name="Pvt",
                methods=[
                    Method(name="viscosity", parameters=[Parameter(name="p", type="float")], returns="float"),
                    Method(
                        name="viscosity",
                        parameters=[
                            Parameter(name="p", type="float"),
                            Parameter(name="t", type="float"),
                        ],
                        returns="float",
                    ),
                ],
            )
        ]
    )

    code = router(mod)

    compile(code, "router.py", "exec")
    assert code.count("def viscosity(") == 1
    assert "def viscosity_2(p: float, t: float):" in code
    assert code.count('@router.post("/viscosity")') == 1
    assert '@router.post("/viscosity_2")' in code


def test_overloaded_free_functions_each_get_a_reachable_endpoint():
    mod = module(
        functions=[
            FunctionDef(name="mix", parameters=[Parameter(name="a", type="float")], returns="float"),
            FunctionDef(
                name="mix",
                parameters=[Parameter(name="a", type="float"), Parameter(name="b", type="float")],
                returns="float",
            ),
        ]
    )

    code = router(mod)

    compile(code, "router.py", "exec")
    assert "def mix_endpoint(a: float):" in code
    assert "def mix_2_endpoint(a: float, b: float):" in code
    assert '@router.post("/mix_2")' in code


# --- code-review findings [6][7]: identifier escaping --------------------

def test_soft_keywords_are_not_escaped():
    """`match`/`case`/`type`/`_` are not reserved — escaping them moves the wire contract.

    The escaped name becomes the FastAPI request-body field, so escaping a
    soft keyword would silently rename a field on a service that already
    worked. True keywords carry no such risk: a service exposing one never
    generated importable Python in the first place.
    """
    from native2py import ir

    exec("def _f(match=1, case=2, type=3): return match, case, type")  # valid Python
    assert [ir.python_identifier(n) for n in ("match", "case", "type", "_")] == [
        "match", "case", "type", "_",
    ]
    assert [ir.python_identifier(n) for n in ("lambda", "from", "class")] == [
        "lambda_", "from_", "class_",
    ]


def test_validate_catches_a_collision_created_by_escaping():
    """Two distinct native names can escape to one Python name.

    Keying the collision check on the raw native spelling saw two different
    keys and missed it, so the generated package defined the name twice and
    one symbol silently shadowed the other.
    """
    from native2py import ir
    from native2py.ir import ClassDef, ModuleIR, StructDef

    module = ModuleIR(
        name="m", language="cpp", source_file="m.hpp",
        structs=[StructDef("lambda_")],
        classes=[ClassDef("lambda")],
    )

    problems = ir.validate(module)

    assert len(problems) == 1
    assert "lambda_" in problems[0].message


# --- COMMON-block safety: serialising native calls ------------------------
#
# Fortran COMMON blocks are process-global. FastAPI runs synchronous `def`
# endpoints in a threadpool, so two requests can be inside the generated
# router at the same time and share one copy of COMMON /FLUID/. Nothing
# crashes — the numbers are just computed from somebody else's fluid.
#
# The generated Fortran router therefore holds a module-level RLock across
# every native call. These tests pin both the shape of that code and the
# behaviour it buys, including what it deliberately does NOT buy.


import threading



def fortran_module(**kwargs) -> ModuleIR:
    base = dict(name="petro", language="fortran", source_file="petro.f")
    base.update(kwargs)
    return ModuleIR(**base)


def _load_router(code: str, native: dict):
    """Exec a generated router with a fake native package and a fake FastAPI.

    The generated file starts `from . import PVTINI, ...`, a relative import
    with no package to resolve against, so the native names are injected
    directly instead. The point of the exercise is the endpoint bodies.
    """

    class _FakeRouter:
        def __init__(self, **kwargs):
            self.routes = {}

        def post(self, path):
            def decorate(fn):
                self.routes[path] = fn
                return fn

            return decorate

        # The router also carries the generated `GET /_unexposed` introspection
        # route, which has to bind somewhere when the file is exec'd.
        def get(self, path, **kwargs):
            def decorate(fn):
                self.routes[path] = fn
                return fn

            return decorate

    namespace = dict(native)
    namespace["APIRouter"] = _FakeRouter
    stripped = "\n".join(
        line
        for line in code.splitlines()
        if not line.startswith("from . import") and not line.startswith("from fastapi")
    )
    exec(compile(stripped, "router.py", "exec"), namespace)  # noqa: S102
    return namespace["router"]


def _with_the_lock_removed(code: str) -> str:
    """The same router as if a maintainer had deleted the lock for throughput.

    Written as a transform of the real generated source rather than a
    hand-copied variant, so the "without the lock" arm cannot drift away from
    what the generator actually emits.
    """
    out = []
    lines = code.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() == "with _NATIVE_LOCK:":
            indent = len(line) - len(line.lstrip())
            i += 1
            while i < len(lines) and (
                not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent
            ):
                out.append(lines[i][4:] if lines[i].strip() else lines[i])
                i += 1
            continue
        out.append(line)
        i += 1
    source = "\n".join(out)
    assert "_NATIVE_LOCK:" not in source
    return source


# --- the generated code shape --------------------------------------------


def test_fortran_router_defines_the_lock_and_holds_it_across_native_calls():
    mod = fortran_module(
        functions=[
            FunctionDef(
                name="PVTINI",
                parameters=[Parameter(name="api", type="float")],
                is_subroutine=True,
            ),
            FunctionDef(
                name="PVTRS",
                parameters=[Parameter(name="p", type="float")],
                returns="float",
            ),
        ]
    )

    code = python_pkg_gen.generate_router_py(mod, "petro")

    assert "import threading" in code
    # RLock, not Lock: one generated endpoint calling another must not deadlock
    # the request against itself.
    assert "_NATIVE_LOCK = threading.RLock()" in code
    assert "with _NATIVE_LOCK:\n        PVTINI(api)" in code
    # A bare function call is hoisted out of the return so JSON shaping stays
    # outside the critical section.
    assert "with _NATIVE_LOCK:\n        result = PVTRS(p)" in code
    assert '    return {"result": result}' in code


def test_the_generated_lock_explains_why_it_is_there():
    # A maintainer who deletes this to "fix throughput" reintroduces silently
    # wrong answers, so the reason has to be in the generated file itself.
    mod = fortran_module(
        functions=[FunctionDef(name="PVTRS", returns="float")]
    )

    code = python_pkg_gen.generate_router_py(mod, "petro")

    assert "COMMON" in code
    assert "threadpool" in code
    assert "DO NOT REMOVE" in code


def test_only_the_native_call_is_inside_the_lock():
    # Array marshalling touches no COMMON storage; holding the lock across it
    # would lengthen the critical section for no correctness gain.
    mod = fortran_module(
        functions=[
            FunctionDef(
                name="NORMALIZE",
                parameters=[
                    Parameter(name="values", type="float", is_array=True),
                    Parameter(name="n", type="int"),
                ],
                is_subroutine=True,
            )
        ]
    )

    code = python_pkg_gen.generate_router_py(mod, "petro")

    conversion = code.index("values = np.array(")
    assert code.index("with _NATIVE_LOCK:") > conversion


def test_cpp_routers_get_no_lock():
    # C++ already builds an instance per request, and a free function has no
    # equivalent of COMMON that native2py can see. Locking every C++ service
    # would cost real concurrency to guard a hazard that is not there.
    mod = module(
        classes=[
            ClassDef(
                name="Calculator",
                methods=[
                    Method(
                        name="add",
                        parameters=[Parameter(name="a", type="float")],
                        returns="float",
                    )
                ],
            )
        ],
        functions=[
            FunctionDef(
                name="area",
                parameters=[Parameter(name="r", type="float")],
                returns="float",
            )
        ],
    )

    code = router(mod)

    assert "_NATIVE_LOCK" not in code
    assert "import threading" not in code


# --- the behaviour the lock actually buys ---------------------------------
#
# The fake native module below stands in for COMMON /FLUID/: module-level
# state a routine writes and then reads back inside one call. The barrier
# makes the interleaving deterministic instead of hoping for a scheduler
# preemption, which is the only way a race test is worth running in CI.


def _fake_pvt(barrier):
    common = {"api": None}

    def PVTSOLVE(api):
        """PVTINI-then-PVTRS inside one routine: write COMMON, work, read it."""
        common["api"] = api
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return common["api"]

    return {"PVTSOLVE": PVTSOLVE}


PVTSOLVE_IR = ModuleIR(
    name="petro",
    language="fortran",
    source_file="petro.f",
    functions=[
        FunctionDef(
            name="PVTSOLVE",
            parameters=[Parameter(name="api", type="float")],
            returns="float",
        )
    ],
)


def _run_two_fluids(code: str, barrier):
    router_obj = _load_router(code, _fake_pvt(barrier))
    endpoint = router_obj.routes["/PVTSOLVE"]
    seen = {}

    def call(api):
        seen[api] = endpoint(api)["result"]

    threads = [threading.Thread(target=call, args=(api,)) for api in (30.0, 45.0)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return seen


def test_without_the_lock_two_fluids_contaminate_each_other():
    """The failure the lock exists to prevent — proved, not assumed.

    Deleting the lock from the real generated source is exactly the "fix
    throughput" change a future maintainer is tempted to make.
    """
    code = _with_the_lock_removed(
        python_pkg_gen.generate_router_py(PVTSOLVE_IR, "petro")
    )

    seen = _run_two_fluids(code, threading.Barrier(2, timeout=10))

    # Both threads wrote COMMON before either read it, so both read the same
    # value and at least one caller got the other caller's fluid.
    assert seen[30.0] == seen[45.0]
    contaminated = [api for api, answer in seen.items() if answer != api]
    assert contaminated, f"expected cross-contamination, got {seen}"


def test_with_the_lock_each_caller_sees_its_own_fluid():
    code = python_pkg_gen.generate_router_py(PVTSOLVE_IR, "petro")

    # The barrier can never be satisfied while the lock is held — only one
    # thread is ever inside the native call — so it times out and each call
    # completes on its own fluid.
    seen = _run_two_fluids(code, threading.Barrier(2, timeout=0.5))

    assert seen == {30.0: 30.0, 45.0: 45.0}


def test_the_lock_is_reentrant_so_one_endpoint_may_call_another():
    """A plain Lock here would deadlock a request against itself."""
    code = python_pkg_gen.generate_router_py(PVTSOLVE_IR, "petro")
    endpoint = _load_router(
        code, _fake_pvt(threading.Barrier(2, timeout=0.1))
    ).routes["/PVTSOLVE"]

    # Re-enter the same lock around the endpoint, the way a hand-written
    # composite endpoint calling two generated ones would.
    module_globals = endpoint.__globals__
    with module_globals["_NATIVE_LOCK"]:
        assert endpoint(30.0)["result"] == 30.0


def test_the_locked_router_still_compiles_and_is_byte_deterministic():
    first = python_pkg_gen.generate_router_py(PVTSOLVE_IR, "petro")
    second = python_pkg_gen.generate_router_py(PVTSOLVE_IR, "petro")

    compile(first, "router.py", "exec")
    assert first.encode() == second.encode()


def test_the_lock_does_not_make_a_configure_then_compute_pair_atomic():
    """The residual gap, pinned deliberately.

    The lock is held for one native call. A caller that POSTs /PVTINI and then
    POSTs /PVTRS releases it in between, so a second caller can reconfigure
    COMMON in the gap. Closing that needs session affinity or a combined
    endpoint — both out of scope for the lock, both recorded in
    docs/production-readiness.md. This test exists so nobody reads the lock as
    a complete fix.
    """
    common = {"api": None}

    def PVTINI(api):
        common["api"] = api

    def PVTRS(p):
        return common["api"]

    mod = fortran_module(
        functions=[
            FunctionDef(
                name="PVTINI",
                parameters=[Parameter(name="api", type="float")],
                is_subroutine=True,
            ),
            FunctionDef(
                name="PVTRS",
                parameters=[Parameter(name="p", type="float")],
                returns="float",
            ),
        ]
    )
    routes = _load_router(
        python_pkg_gen.generate_router_py(mod, "petro"),
        {"PVTINI": PVTINI, "PVTRS": PVTRS},
    ).routes

    routes["/PVTINI"](30.0)
    routes["/PVTINI"](45.0)  # a second request lands between configure and read
    assert routes["/PVTRS"](2000.0)["result"] == 45.0


# --- input bounds (ROADMAP 2.3) and /_unexposed (ROADMAP 2.4) ------------
#
# Both of these are about what a generated endpoint accepts from an untrusted
# caller. `props: list[float]` accepted a body of any length — a
# memory-exhaustion vector in every array endpoint — and a size argument that
# disagreed with its array went straight into Fortran, which then indexed past
# the end of the buffer. Neither failed loudly; the first exhausted the worker,
# the second read or wrote memory it did not own.

from fastapi import FastAPI
from fastapi.testclient import TestClient

from native2py.ir import SkippedSymbol


def _live_client(code: str, native: dict) -> TestClient:
    """A real FastAPI app serving a generated router, with fake native symbols.

    The generated file opens `from . import PVTSTATE`, a relative import with
    no package to resolve against, so the native names are injected into the
    module namespace instead. Everything else — the real APIRouter, the real
    Pydantic validation, the real 422s — is exercised as deployed.
    """
    stripped = "\n".join(
        line for line in code.splitlines() if not line.startswith("from . import")
    )
    namespace = dict(native)
    exec(compile(stripped, "router.py", "exec"), namespace)  # noqa: S102
    app = FastAPI()
    app.include_router(namespace["router"])
    return TestClient(app)


def _array_fortran_ir(**kwargs) -> ModuleIR:
    return fortran_module(
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
        ],
        **kwargs,
    )


def test_an_array_endpoint_declares_a_cap_on_its_length():
    code = python_pkg_gen.generate_router_py(_array_fortran_ir(), "petro")

    # A named, commented constant — not a magic number buried in the signature.
    assert "MAX_ARRAY_ITEMS" in code
    assert "PLACEHOLDER" in code
    assert "Field(max_length=MAX_ARRAY_ITEMS)" in code
    # Still configurable per deployment without regenerating.
    assert 'os.environ.get("NATIVE2PY_MAX_ARRAY_ITEMS"' in code
    assert "props: list[float]" not in code


def test_an_oversized_array_body_is_rejected_with_a_422():
    code = python_pkg_gen.generate_router_py(_array_fortran_ir(), "petro")
    # Shrink the cap rather than posting 65537 floats: the mechanism is what is
    # under test, and the constant is deliberately readable at module scope.
    code = code.replace(
        'int(os.environ.get("NATIVE2PY_MAX_ARRAY_ITEMS", "65536"))', "4"
    )
    calls = []

    def PVTSTATE(pressure, props, n):
        calls.append(n)
        return 1.0

    client = _live_client(code, {"PVTSTATE": PVTSTATE})

    ok = client.post("/PVTSTATE?pressure=2000&n=4", json=[1.0, 2.0, 3.0, 4.0])
    assert ok.status_code == 200

    too_big = client.post("/PVTSTATE?pressure=2000&n=5", json=[1.0] * 5)
    assert too_big.status_code == 422
    # Rejected during request validation — the native routine never ran, and
    # the oversized list never reached numpy.
    assert calls == [4]


def test_a_size_argument_that_disagrees_with_its_array_is_rejected():
    code = python_pkg_gen.generate_router_py(_array_fortran_ir(), "petro")
    calls = []

    def PVTSTATE(pressure, props, n):
        calls.append((len(props), n))
        return 1.0

    client = _live_client(code, {"PVTSTATE": PVTSTATE})

    mismatch = client.post("/PVTSTATE?pressure=2000&n=9", json=[1.0, 2.0, 3.0])
    assert mismatch.status_code == 422
    assert "len(props)" in mismatch.json()["detail"]
    # This is the whole point: n=9 against a 3-element buffer is an
    # out-of-bounds read inside Fortran, not a wrong answer.
    assert calls == []

    assert client.post("/PVTSTATE?pressure=2000&n=3", json=[1.0, 2.0, 3.0]).status_code == 200
    assert calls == [(3, 3)]


def test_a_size_argument_is_paired_by_name_when_there_are_several_arrays():
    mod = fortran_module(
        functions=[
            FunctionDef(
                name="MIXPROPS",
                parameters=[
                    Parameter(name="props", type="float", is_array=True),
                    Parameter(name="temps", type="float", is_array=True),
                    Parameter(name="nprops", type="int"),
                    Parameter(name="len_temps", type="int"),
                ],
                returns="float",
            )
        ]
    )

    code = python_pkg_gen.generate_router_py(mod, "petro")

    assert "if nprops != len(props):" in code
    assert "if len_temps != len(temps):" in code


def test_an_ambiguous_size_argument_is_not_guessed():
    """A wrong pairing rejects valid calls, so no pairing is emitted at all."""
    mod = fortran_module(
        functions=[
            FunctionDef(
                name="BLEND",
                parameters=[
                    Parameter(name="a", type="float", is_array=True),
                    Parameter(name="b", type="float", is_array=True),
                    Parameter(name="mode", type="int"),
                    Parameter(name="steps", type="int"),
                ],
                returns="float",
            )
        ]
    )

    code = python_pkg_gen.generate_router_py(mod, "petro")

    assert "!= len(" not in code
    assert "HTTPException" not in code
    # The length cap still applies — it needs no pairing to be safe.
    assert code.count("Field(max_length=MAX_ARRAY_ITEMS)") == 2


def test_an_integer_that_is_not_a_length_is_not_treated_as_one():
    """One array and one integer is not on its own a size argument.

    `PRZFAC(IPHASE, X)` in libraries/petro takes a *phase selector* and an
    array. Pairing them would reject every valid call to that routine, which is
    worse than leaving it as unguarded as it is today — so the structural
    fallback also requires the integer to be named like a length.
    """
    mod = fortran_module(
        functions=[
            FunctionDef(
                name="PRZFAC",
                parameters=[
                    Parameter(name="IPHASE", type="int"),
                    Parameter(name="X", type="float", is_array=True),
                ],
                returns="float",
            )
        ]
    )

    code = python_pkg_gen.generate_router_py(mod, "petro")

    assert "!= len(" not in code
    assert "Field(max_length=MAX_ARRAY_ITEMS)" in code


def test_one_shared_length_across_several_arrays_is_not_guessed_either():
    """`THOMAS(A, B, C, D, N)` — N is almost certainly all four extents.

    "Almost certainly" is not good enough: nothing in the IR says the arrays
    are the same length, and a wrong guess rejects valid calls. Recorded as a
    known false negative rather than a silent one; it becomes a real
    `Field(max_length=...)` when the IR carries extents (ROADMAP 1.4).
    """
    mod = fortran_module(
        functions=[
            FunctionDef(
                name="THOMAS",
                parameters=[
                    Parameter(name=letter, type="float", is_array=True)
                    for letter in "ABCD"
                ]
                + [Parameter(name="N", type="int")],
                is_subroutine=True,
            )
        ]
    )

    assert "!= len(" not in python_pkg_gen.generate_router_py(mod, "petro")


def test_cpp_array_parameters_are_capped_too():
    mod = module(
        functions=[
            FunctionDef(
                name="mean",
                parameters=[Parameter(name="values", type="float", is_array=True)],
                returns="float",
            )
        ]
    )

    code = python_pkg_gen.generate_router_py(mod, "pvt")

    assert "values: Annotated[list[float], Field(max_length=MAX_ARRAY_ITEMS)]" in code


def test_a_service_with_no_arrays_gains_no_bounds_machinery():
    code = python_pkg_gen.generate_router_py(PVTSOLVE_IR, "petro")

    assert "MAX_ARRAY_ITEMS" not in code
    assert "HTTPException" not in code
    assert "from typing import Annotated" not in code


# --- GET /_unexposed -----------------------------------------------------


def test_unexposed_serves_what_the_parser_refused_and_why():
    mod = _array_fortran_ir(
        skipped=[
            SkippedSymbol(name="GETNAME", reason="non-const char* output buffer"),
            SkippedSymbol(name="PVTPACK", reason="derived-type Fortran result"),
        ]
    )

    client = _live_client(
        python_pkg_gen.generate_router_py(mod, "petro"), {"PVTSTATE": lambda *a: 1.0}
    )

    response = client.get("/_unexposed")

    assert response.status_code == 200
    assert response.json() == {
        "GETNAME": "non-const char* output buffer",
        "PVTPACK": "derived-type Fortran result",
    }


def test_unexposed_exists_and_is_empty_when_nothing_was_skipped():
    # "nothing was skipped" and "this service predates the feature" must be
    # distinguishable: {} versus a 404.
    client = _live_client(
        python_pkg_gen.generate_router_py(_array_fortran_ir(), "petro"),
        {"PVTSTATE": lambda *a: 1.0},
    )

    response = client.get("/_unexposed")

    assert response.status_code == 200
    assert response.json() == {}


def test_unexposed_is_generated_for_cpp_services_as_well():
    mod = module(
        functions=[
            FunctionDef(
                name="circle_area",
                parameters=[Parameter(name="r", type="float")],
                returns="float",
            )
        ],
        skipped=[SkippedSymbol(name="raw_buffer", reason="returns a raw pointer")],
    )

    client = _live_client(
        python_pkg_gen.generate_router_py(mod, "pvt"), {"circle_area": lambda r: r}
    )

    assert client.get("/_unexposed").json() == {"raw_buffer": "returns a raw pointer"}


def test_a_native_symbol_named_unexposed_does_not_shadow_the_route():
    mod = module(
        functions=[FunctionDef(name="_unexposed", returns="float")],
        skipped=[SkippedSymbol(name="x", reason="why")],
    )

    client = _live_client(
        python_pkg_gen.generate_router_py(mod, "pvt"), {"_unexposed": lambda: 7.0}
    )

    assert client.get("/_unexposed").json() == {"x": "why"}
    assert client.post("/_unexposed_2").json() == {"result": 7.0}


def test_the_bounded_router_still_compiles_and_is_byte_deterministic():
    mod = _array_fortran_ir(
        skipped=[SkippedSymbol(name="GETNAME", reason="non-const char* output buffer")]
    )

    first = python_pkg_gen.generate_router_py(mod, "petro")
    second = python_pkg_gen.generate_router_py(mod, "petro")

    compile(first, "router.py", "exec")
    assert first.encode() == second.encode()
