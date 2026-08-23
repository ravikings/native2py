"""Generated-C++ correctness for the pybind11 generator.

These tests build IR by hand rather than parsing a header, so they exercise
the generator alone, and — where a toolchain is available — they hand the
emitted C++ to clang++ instead of string-matching it. Every defect below was
originally proved by a compiler, so that is what proves the fix.
"""

from __future__ import annotations

import shutil
import subprocess
import sysconfig
import textwrap

import pytest

from native2py.generators import pybind_gen
from native2py.ir import ClassDef, FunctionDef, Method, ModuleIR, Parameter, StructDef

PROBE_HEADER = textwrap.dedent(
    """
    #pragma once
    #include <cstdint>

    namespace petro {

    enum class Correlation { Standing, VazquezBeggs, Glaso };

    struct Sample {
        const double reference_pressure;
        double value;
    };

    struct Reading {
        double value;
    };

    class PvtModel {
    public:
        PvtModel(std::uint64_t well_id, float api, Correlation corr);
        double viscosity(double pressure) const;
        double viscosity(double pressure, double temperature) const;
        double attenuate(double lambda_, double from) const;
    };

    double bubble_point(double gor, double api);
    double bubble_point(double gor);

    }  // namespace petro

    namespace geo {
    struct Grid { int nx; };
    }  // namespace geo
    """
).lstrip()


def _probe_module() -> ModuleIR:
    # Qualified, because that is what parsers/cpp_ast records — it resolves
    # each type through its declaration and qualifies it there. An
    # unqualified spelling here would be a fixture the parser never emits.
    correlation = Parameter("corr", "int", native_type="petro::Correlation")
    return ModuleIR(
        name="reservoir",
        language="cpp",
        source_file="reservoir.hpp",
        structs=[
            StructDef(
                name="Sample",
                namespace="petro",
                fields=[
                    Parameter("reference_pressure", "float", native_type="double", is_const=True),
                    Parameter("value", "float", native_type="double"),
                ],
                has_default_constructor=False,
            ),
            StructDef(
                name="Reading",
                namespace="petro",
                fields=[Parameter("value", "float", native_type="double")],
            ),
            # A second namespace in the same service: this one used to be
            # qualified with the *first* record's namespace.
            StructDef(
                name="Grid",
                namespace="geo",
                fields=[Parameter("nx", "int", native_type="int")],
            ),
        ],
        classes=[
            ClassDef(
                name="PvtModel",
                namespace="petro",
                has_default_constructor=False,
                constructors=[
                    [
                        Parameter("well_id", "int", native_type="std::uint64_t"),
                        Parameter("api", "float", native_type="float"),
                        correlation,
                    ]
                ],
                methods=[
                    Method(
                        name="viscosity",
                        parameters=[Parameter("pressure", "float", native_type="double")],
                        returns="float",
                        is_const=True,
                        is_overloaded=True,
                    ),
                    Method(
                        name="viscosity",
                        parameters=[
                            Parameter("pressure", "float", native_type="double"),
                            Parameter("temperature", "float", native_type="double"),
                        ],
                        returns="float",
                        is_const=True,
                        is_overloaded=True,
                    ),
                    Method(
                        name="attenuate",
                        parameters=[
                            Parameter("lambda_", "float", native_type="double"),
                            Parameter("from", "float", native_type="double"),
                        ],
                        returns="float",
                        is_const=True,
                    ),
                ],
            )
        ],
        functions=[
            FunctionDef(
                name="bubble_point",
                namespace="petro",
                is_overloaded=True,
                parameters=[
                    Parameter("gor", "float", native_type="double"),
                    Parameter("api", "float", native_type="double"),
                ],
                returns="float",
            ),
            FunctionDef(
                name="bubble_point",
                namespace="petro",
                is_overloaded=True,
                parameters=[Parameter("gor", "float", native_type="double")],
                returns="float",
            ),
        ],
    )


# --- compile harness ----------------------------------------------------


def _pybind11_include() -> str | None:
    try:
        import pybind11
    except ImportError:
        return None
    return pybind11.get_include()


def compile_bindings(tmp_path, source: str, header: str = PROBE_HEADER) -> None:
    """Syntax-check generated bindings, or fail with clang's diagnostics.

    Skips when there is no clang++. Without pybind11 headers the full module
    cannot be compiled, so we fall back to compiling just the address-of and
    overload_cast expressions — which is what proved these bugs in the first
    place.
    """
    clang = shutil.which("clang++")
    if clang is None:
        pytest.skip("clang++ not available")

    (tmp_path / "reservoir.hpp").write_text(header)
    include = _pybind11_include()

    if include is None:
        source = _expression_probe(source)
        includes = []
    else:
        includes = ["-I", include, "-I", sysconfig.get_paths()["include"]]

    gen = tmp_path / "gen.cpp"
    gen.write_text(source)
    result = subprocess.run(
        [clang, "-std=c++17", "-fsyntax-only", "-I", str(tmp_path), *includes, str(gen)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _expression_probe(source: str) -> str:
    """Reduce generated bindings to the &-expressions, for a pybind11-less box."""
    exprs = []
    for raw in source.splitlines():
        line = raw.strip()
        for marker in ('.def("', 'def_readwrite("', 'def_readonly("', 'm.def("'):
            if marker in line:
                expr = line.split(",", 1)[1].rsplit(")", 1)[0].strip().rstrip(";")
                if expr.startswith("&"):
                    exprs.append(expr)
                break
    body = "\n".join(f"    auto p{i} = {e};" for i, e in enumerate(exprs))
    return f'#include "reservoir.hpp"\n#include <cstdint>\nvoid probe() {{\n{body}\n}}\n'


# --- the whole thing compiles ------------------------------------------


def test_probe_bindings_compile(tmp_path):
    module = _probe_module()
    source = pybind_gen.generate_bindings(module, "reservoir.hpp")
    compile_bindings(tmp_path, source)


# --- B1: overloaded methods --------------------------------------------


def test_overloaded_method_uses_overload_cast():
    module = _probe_module()
    source = pybind_gen.generate_bindings(module, "reservoir.hpp")

    assert (
        '.def("viscosity", py::overload_cast<double>('
        "&petro::PvtModel::viscosity, py::const_))" in source
    )
    assert (
        '.def("viscosity", py::overload_cast<double, double>('
        "&petro::PvtModel::viscosity, py::const_))" in source
    )
    # A non-overloaded method keeps the simple, cheaper form.
    assert '.def("attenuate", &petro::PvtModel::attenuate)' in source


def test_non_const_overload_omits_py_const():
    module = ModuleIR(
        name="m",
        language="cpp",
        source_file="m.hpp",
        classes=[
            ClassDef(
                name="Widget",
                namespace="ui",
                methods=[
                    Method(
                        name="size",
                        parameters=[Parameter("n", "int", native_type="int")],
                        is_overloaded=True,
                    )
                ],
            )
        ],
    )
    source = pybind_gen.generate_bindings(module, "m.hpp")
    assert '.def("size", py::overload_cast<int>(&ui::Widget::size))' in source


# --- B2: namespaced free functions -------------------------------------


def test_free_function_is_namespace_qualified():
    module = ModuleIR(
        name="m",
        language="cpp",
        source_file="m.hpp",
        functions=[
            FunctionDef(
                name="bubble_point",
                namespace="petro",
                parameters=[Parameter("gor", "float", native_type="double")],
            )
        ],
    )
    source = pybind_gen.generate_bindings(module, "m.hpp")
    assert 'm.def("bubble_point", &petro::bubble_point);' in source


def test_global_free_function_stays_unqualified():
    module = ModuleIR(
        name="m",
        language="cpp",
        source_file="m.hpp",
        functions=[FunctionDef(name="rate")],
    )
    assert 'm.def("rate", &rate);' in pybind_gen.generate_bindings(module, "m.hpp")


def test_overloaded_free_function_uses_overload_cast():
    source = pybind_gen.generate_bindings(_probe_module(), "reservoir.hpp")
    assert (
        'm.def("bubble_point", py::overload_cast<double, double>(&petro::bubble_point));'
        in source
    )
    assert (
        'm.def("bubble_point", py::overload_cast<double>(&petro::bubble_point));' in source
    )


# --- B3: const fields ---------------------------------------------------


def test_const_field_is_readonly():
    source = pybind_gen.generate_bindings(_probe_module(), "reservoir.hpp")
    assert (
        '.def_readonly("reference_pressure", &petro::Sample::reference_pressure)' in source
    )
    assert '.def_readwrite("value", &petro::Sample::value)' in source


# --- B5: struct default constructor ------------------------------------


def test_struct_without_default_constructor_gets_no_init():
    source = pybind_gen.generate_bindings(_probe_module(), "reservoir.hpp")
    sample = source.split('py::class_<petro::Sample>')[1].split("py::class_")[0]
    assert "py::init<>()" not in sample
    reading = source.split('py::class_<petro::Reading>')[1].split("py::class_")[0]
    assert "py::init<>()" in reading


# --- B6: per-record namespaces -----------------------------------------


def test_each_record_uses_its_own_namespace():
    source = pybind_gen.generate_bindings(_probe_module(), "reservoir.hpp")
    assert 'py::class_<geo::Grid>(m, "Grid")' in source
    assert '.def_readwrite("nx", &geo::Grid::nx)' in source
    assert "petro::Grid" not in source


def test_struct_only_module_is_still_qualified():
    module = ModuleIR(
        name="m",
        language="cpp",
        source_file="m.hpp",
        structs=[
            StructDef(
                name="Grid",
                namespace="geo",
                fields=[Parameter("nx", "int", native_type="int")],
            )
        ],
    )
    source = pybind_gen.generate_bindings(module, "m.hpp")
    assert 'py::class_<geo::Grid>(m, "Grid")' in source


# --- A3/B7: init<> argument types --------------------------------------


def test_init_uses_native_types():
    source = pybind_gen.generate_bindings(_probe_module(), "reservoir.hpp")
    assert ".def(py::init<std::uint64_t, float, petro::Correlation>())" in source
    assert "py::init<int, double, int>()" not in source


def test_init_falls_back_when_native_type_missing():
    """IR from an older native2py has no native_type; behave as before."""
    module = ModuleIR(
        name="m",
        language="cpp",
        source_file="m.hpp",
        classes=[
            ClassDef(
                name="Widget",
                namespace="ui",
                has_default_constructor=False,
                constructors=[
                    [Parameter("n", "int"), Parameter("x", "float"), Parameter("s", "str")]
                ],
            )
        ],
    )
    source = pybind_gen.generate_bindings(module, "m.hpp")
    assert ".def(py::init<int, double, std::string>())" in source


def test_native_type_from_another_namespace_is_not_re_qualified():
    """A parameter type outside the bound record's namespace must survive verbatim.

    The generator knows only the namespace of the record it is binding, so
    prefixing every unqualified native spelling with it turned a global
    `Color` into `ns::Color` inside `ns::Widget` — which fails to compile with
    "no type named 'Color' in namespace 'ns'". The parser already qualified
    whatever needed qualifying, so the spelling is emitted as recorded.
    """
    module = ModuleIR(
        name="m",
        language="cpp",
        source_file="m.hpp",
        classes=[
            ClassDef(
                name="Widget",
                namespace="ui",
                has_default_constructor=False,
                constructors=[[
                    Parameter("c", "int", native_type="Color"),          # global namespace
                    Parameter("p", "int", native_type="paint::Shade"),   # a different one
                ]],
            )
        ],
    )
    source = pybind_gen.generate_bindings(module, "m.hpp")

    assert ".def(py::init<Color, paint::Shade>())" in source
    assert "ui::Color" not in source
    assert "ui::paint::Shade" not in source
