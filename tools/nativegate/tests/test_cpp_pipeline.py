from pathlib import Path

from nativegate.config import ExposeConfig
from nativegate.generators import cmake_gen, pybind_gen, python_pkg_gen
from nativegate.parsers import cpp as cpp_parser



def test_parses_calculator_class(calculator_header):
    module = cpp_parser.parse_header(calculator_header, ExposeConfig(classes=["Calculator"]))

    assert module.name == "calculator"
    assert len(module.classes) == 1

    cls = module.classes[0]
    assert cls.name == "Calculator"
    assert cls.namespace == "math"
    assert [m.name for m in cls.methods] == ["add", "multiply"]
    assert cls.methods[0].returns == "float"
    assert [p.name for p in cls.methods[0].parameters] == ["a", "b"]


def test_config_gates_exposure(calculator_header):
    module = cpp_parser.parse_header(calculator_header, ExposeConfig(classes=["SomethingElse"]))

    assert module.is_empty()


def test_generates_pybind_module(calculator_header):
    module = cpp_parser.parse_header(calculator_header, ExposeConfig(classes=["Calculator"]))
    bindings = pybind_gen.generate_bindings(module, "calculator.hpp")

    assert "PYBIND11_MODULE(calculator_cpp, m)" in bindings
    assert 'py::class_<math::Calculator>(m, "Calculator")' in bindings
    assert '.def("add", &math::Calculator::add)' in bindings


def test_generates_cmake(calculator_header):
    module = cpp_parser.parse_header(calculator_header, ExposeConfig(classes=["Calculator"]))
    cmake = cmake_gen.generate_cmake(module, "calculator", ["native/calculator.cpp"])

    assert "pybind11_add_module(calculator_cpp" in cmake
    assert "native/calculator.cpp" in cmake


def test_generates_init_py(calculator_header):
    module = cpp_parser.parse_header(calculator_header, ExposeConfig(classes=["Calculator"]))
    init_py = python_pkg_gen.generate_init_py(module)

    assert "from ._native.calculator_cpp import Calculator" in init_py
    assert '__all__ = ["Calculator"]' in init_py
