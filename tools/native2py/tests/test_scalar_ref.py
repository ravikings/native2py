"""extern "C" scalars passed by reference — the Fortran-linkage convention.

`void pvtini(double* api, double* sgg, double* tres, int* icorr)` takes four
SCALARS: Fortran has no pass-by-value, so a C bridge to it spells every
argument as a pointer. Refusing those as "pointer with no length" refused the
single most common C-linkage signature there is.

But the same spelling is how C passes an ARRAY whose extent travels through a
PARAMETER or COMMON block — FLASH2's `double* x` is XLIQ(NCMAX) — and binding
an array as one scalar hands the callee a pointer to a single stack double it
reads past. The type information genuinely does not exist in the prototype, so
this is opt-in per function (`clang.scalar_ref_functions`), never inferred.

Verified against the real petro bridge, compiled and linked against the real
Fortran: pvtrs_(2000) == 355.0764805578969 and pvtbub_(1000) ==
4784.822092740284 through the generated lambdas — the recorded golden values,
exactly.
"""

from pathlib import Path

import pytest

from native2py.config import ExposeConfig, ServiceConfig
from native2py.generators import pybind_gen, python_pkg_gen
from native2py.ir import FunctionDef, ModuleIR, Parameter
from native2py.parsers import cpp_ast
from native2py.parsers.cpp_ast import ClangOptions

pytestmark = pytest.mark.skipif(
    not cpp_ast.is_available(),
    reason=f"libclang unavailable: {cpp_ast.unavailable_reason()}",
)

BRIDGE = """
extern "C" {
void   pvtini_(double* api, double* sgg, double* tres, int* icorr);
double pvtrs_(double* p);
void   krload_(double* sw, double* krw, double* kro, double* pcw, int* n);
}
double outside_linkage(double* p);
"""


def _parse(tmp_path, scalar_refs=()):
    header = tmp_path / "bridge.hpp"
    header.write_text(BRIDGE)
    return cpp_ast.parse_header(
        header,
        ExposeConfig(all=True),
        options=ClangOptions(scalar_ref_functions=tuple(scalar_refs)),
    )


def test_nothing_is_scalar_bound_without_the_opt_in(tmp_path):
    # The default must stay a refusal: the prototype cannot say scalar-or-
    # array, so native2py must not guess — that is the project's first rule.
    module = _parse(tmp_path)

    assert module.functions == []
    reasons = {s.name: s.reason for s in module.skipped}
    # And the refusal teaches the way out, including the hazard.
    assert "scalar_ref_functions" in reasons["pvtini_"]
    assert "corrupts memory" in reasons["pvtini_"]


def test_the_opt_in_is_per_function_not_per_header(tmp_path):
    module = _parse(tmp_path, scalar_refs=["pvtrs_"])

    assert [f.name for f in module.functions] == ["pvtrs_"]
    assert {s.name for s in module.skipped} >= {"pvtini_", "krload_"}


def test_opted_in_scalars_carry_inout_intent(tmp_path):
    # Non-const T* may be an OUTPUT (an error code, a computed value). The
    # binding returns the final values, so intent must say "inout" — an "in"
    # here would let an output die with the lambda's locals, silently.
    module = _parse(tmp_path, scalar_refs=["pvtini_", "pvtrs_"])
    by_name = {f.name: f for f in module.functions}

    for fn in by_name.values():
        for param in fn.parameters:
            assert param.is_scalar_ref is True
            assert param.is_array is False
            assert param.intent == "inout"


def test_a_size_word_in_the_signature_vetoes_the_opt_in(tmp_path):
    # krload_(sw, krw, kro, pcw, n): four arrays sharing one `n`. The pairing
    # is ambiguous, and the presence of `n` says "these are arrays" — so even
    # an explicit opt-in must not turn five pointers into five scalars.
    module = _parse(tmp_path, scalar_refs=["krload_"])

    assert all(f.name != "krload_" for f in module.functions)


def test_cpp_linkage_is_never_scalar_bound(tmp_path):
    # In C++ linkage a scalar in/out is spelled `T&`, which already binds; a
    # bare `T*` there usually IS an array. The convention is a C-linkage fact,
    # so the fallback is fenced to extern "C" even under "*".
    module = _parse(tmp_path, scalar_refs=["*"])

    assert all(f.name != "outside_linkage" for f in module.functions)
    assert any(s.name == "outside_linkage" for s in module.skipped)


# --- emission -------------------------------------------------------------


def _module(returns="None"):
    return ModuleIR(
        name="bridge",
        language="cpp",
        source_file="bridge.hpp",
        functions=[
            FunctionDef(
                name="pvtini_",
                parameters=[
                    Parameter(name="api", type="float", intent="inout", is_scalar_ref=True),
                    Parameter(name="icorr", type="int", intent="inout", is_scalar_ref=True),
                ],
                returns=returns,
            )
        ],
    )


def test_the_lambda_passes_addresses_and_returns_final_values():
    bindings = pybind_gen.generate_bindings(_module(), "bridge.hpp")

    # By value in, by address to the callee, final values back out. This is
    # what stops an output scalar vanishing into a discarded lambda local.
    assert "[](double api, int icorr)" in bindings
    assert "pvtini_(&api, &icorr);" in bindings
    assert "return py::make_tuple(api, icorr);" in bindings
    # Scalars alone need no numpy machinery.
    assert "numpy.h" not in bindings


def test_a_function_result_comes_first_in_the_tuple():
    bindings = pybind_gen.generate_bindings(_module(returns="float"), "bridge.hpp")

    assert "auto result = pvtini_(&api, &icorr);" in bindings
    assert "return py::make_tuple(result, api, icorr);" in bindings


def test_the_endpoint_unpacks_the_tuple_shape():
    router = python_pkg_gen.generate_router_py(_module(returns="float"), "bridge")

    assert "result, api, icorr = pvtini_(api, icorr)" in router
    assert '"result": result, "api": api, "icorr": icorr' in router


def test_the_config_round_trips(tmp_path):
    (tmp_path / "native").mkdir()
    (tmp_path / "native" / "b.hpp").write_text("int f();")
    (tmp_path / "native2py.yaml").write_text(
        "name: svc\nlanguage: cpp\nexpose:\n  all: true\n"
        "clang:\n  scalar_ref_functions:\n    - pvtini_\n    - pvtrs_\n"
    )

    config = ServiceConfig.load(tmp_path)

    assert config.clang.scalar_ref_functions == ["pvtini_", "pvtrs_"]
