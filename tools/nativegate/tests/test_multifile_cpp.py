"""Multi-header C++ services and external include directories."""

import subprocess
import sys
from pathlib import Path

from nativegate.generators import cmake_gen, pybind_gen
from nativegate.ir import ClassDef, Method, ModuleIR


def _run_cli(args, cwd, expect_ok=True):
    venv_bin = Path(sys.executable).parent
    result = subprocess.run(
        [str(venv_bin / "nativegate"), *args], cwd=cwd, capture_output=True, text=True
    )
    if expect_ok:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def _service(tmp_path, name="svc", headers=None, extra_yaml=""):
    (tmp_path / "services").mkdir(exist_ok=True)
    _run_cli(["create-service", name, "--language", "cpp", "--force"], cwd=tmp_path)
    native = tmp_path / "services" / name / "native"
    for filename, content in (headers or {}).items():
        (native / filename).write_text(content)
    if extra_yaml:
        cfg = tmp_path / "services" / name / "nativegate.yaml"
        cfg.write_text(cfg.read_text() + extra_yaml)
    return tmp_path / "services" / name


def test_bindings_include_every_contributing_header():
    module = ModuleIR(name="svc", language="cpp", source_file="x")
    module.classes.append(ClassDef(name="A", methods=[Method(name="f", returns="float")]))
    module.classes.append(ClassDef(name="B", methods=[Method(name="g", returns="float")]))

    bindings = pybind_gen.generate_bindings(module, ["a.hpp", "b.hpp"])

    assert '#include "a.hpp"' in bindings
    assert '#include "b.hpp"' in bindings
    # A Python extension carries exactly one module init symbol.
    assert bindings.count("PYBIND11_MODULE") == 1
    assert '"A"' in bindings and '"B"' in bindings


def test_multiple_headers_merge_into_one_extension(tmp_path):
    # Regression: generation used to loop per header, overwriting
    # CMakeLists.txt and __init__.py each time. The last header won and every
    # earlier class was silently dropped from the built package.
    service = _service(
        tmp_path,
        headers={
            "vec3.hpp": "#pragma once\nclass Vec3 {\npublic:\n    double norm(double x);\n};\n",
            "vec3.cpp": '#include "vec3.hpp"\ndouble Vec3::norm(double x) { return x; }\n',
            "engine.hpp": '#pragma once\n#include "vec3.hpp"\nclass Engine {\npublic:\n    double run(double x);\n};\n',
            "engine.cpp": '#include "engine.hpp"\ndouble Engine::run(double x) { Vec3 v; return v.norm(x); }\n',
        },
    )

    _run_cli(["generate", "svc"], cwd=tmp_path)

    init_py = (service / "python" / "svc" / "__init__.py").read_text()
    assert "Engine" in init_py
    assert "Vec3" in init_py

    cmake = (service / "CMakeLists.txt").read_text()
    assert "native/engine.cpp" in cmake
    assert "native/vec3.cpp" in cmake
    # One bindings file for the whole service, not one per header.
    generated = list((service / "bindings" / "generated").glob("*.cpp"))
    assert [p.name for p in generated] == ["svc_bindings.cpp"]


def test_duplicate_symbol_across_headers_is_rejected(tmp_path):
    # Both headers merge into one extension, so a repeated class name would
    # be a redefinition — fail with a clear message instead of at link time.
    _service(
        tmp_path,
        headers={
            "a.hpp": "#pragma once\nclass Dup {\npublic:\n    double f(double x);\n};\n",
            "b.hpp": "#pragma once\nclass Dup {\npublic:\n    double g(double x);\n};\n",
        },
    )

    result = _run_cli(["generate", "svc"], cwd=tmp_path, expect_ok=False)

    assert result.returncode != 0
    assert "defined in both" in (result.stdout + result.stderr)


def test_cmake_adds_external_include_paths():
    module = ModuleIR(name="svc", language="cpp", source_file="x")
    module.classes.append(ClassDef(name="A", methods=[Method(name="f", returns="float")]))

    cmake = cmake_gen.generate_cmake(
        module, "svc", ["native/a.cpp"], include_paths=["libraries/extheaders"]
    )

    # Repo-root-relative, and services/<name>/ is two levels down.
    assert "${CMAKE_CURRENT_SOURCE_DIR}/../../libraries/extheaders" in cmake
    assert "PRIVATE native" in cmake


def test_cmake_without_include_paths_is_unchanged():
    module = ModuleIR(name="svc", language="cpp", source_file="x")
    module.classes.append(ClassDef(name="A", methods=[Method(name="f", returns="float")]))

    cmake = cmake_gen.generate_cmake(module, "svc", ["native/a.cpp"])

    assert "target_include_directories(svc_cpp PRIVATE native)" in cmake


def test_missing_include_path_is_rejected(tmp_path):
    _service(
        tmp_path,
        headers={"a.hpp": "#pragma once\nclass A {\npublic:\n    double f(double x);\n};\n"},
        extra_yaml="include_paths:\n  - does/not/exist\n",
    )

    result = _run_cli(["generate", "svc"], cwd=tmp_path, expect_ok=False)

    assert result.returncode != 0
    assert "is not a directory" in (result.stdout + result.stderr)


def test_forward_declared_type_is_skipped_not_bound(tmp_path):
    # `class FluidModel;` names an INCOMPLETE type. Binding a method that
    # takes one compiles into a wall of pybind11 template errors
    # ("incomplete type 'FluidModel' used in type trait expression"), so it
    # must be skipped with a message naming the actual problem.
    _service(
        tmp_path,
        headers={
            "sim.hpp": (
                "#pragma once\n"
                "class FluidModel;\n"
                "class Sim {\n"
                "public:\n"
                "    void set_fluid(FluidModel* f);\n"
                "    double pressure();\n"
                "};\n"
            ),
            "sim.cpp": '#include "sim.hpp"\ndouble Sim::pressure() { return 1.0; }\n',
        },
    )

    result = _run_cli(["generate", "svc"], cwd=tmp_path)
    output = result.stdout + result.stderr

    assert "forward-declared" in output
    bindings = (tmp_path / "services" / "svc" / "bindings" / "generated" / "svc_bindings.cpp").read_text()
    assert "set_fluid" not in bindings
    assert "pressure" in bindings  # the bindable method still comes through


def test_type_defined_in_sibling_header_is_not_skipped(tmp_path):
    # Forward-declared in one header, defined in another. All of a service's
    # headers compile into one extension, so it IS complete — skipping it
    # would drop a perfectly bindable method.
    _service(
        tmp_path,
        headers={
            "fluid.hpp": "#pragma once\nclass Fluid {\npublic:\n    double density();\n};\n",
            "fluid.cpp": '#include "fluid.hpp"\ndouble Fluid::density() { return 1.0; }\n',
            "sim.hpp": (
                "#pragma once\n"
                "class Fluid;\n"
                "class Sim {\n"
                "public:\n"
                "    void set_fluid(Fluid* f);\n"
                "};\n"
            ),
            "sim.cpp": '#include "sim.hpp"\n#include "fluid.hpp"\nvoid Sim::set_fluid(Fluid* f) { (void)f; }\n',
        },
    )

    result = _run_cli(["generate", "svc"], cwd=tmp_path)

    assert "forward-declared" not in (result.stdout + result.stderr)
    bindings = (tmp_path / "services" / "svc" / "bindings" / "generated" / "svc_bindings.cpp").read_text()
    assert "set_fluid" in bindings


def test_warns_when_no_implementation_file(tmp_path):
    # Header-only declarations link "successfully" on macOS and fail at
    # dlopen. Warn at generate time instead.
    _service(
        tmp_path,
        headers={"a.hpp": "#pragma once\nclass A {\npublic:\n    double f(double x);\n};\n"},
    )

    result = _run_cli(["generate", "svc"], cwd=tmp_path)

    assert "no .cpp/.cc/.cxx implementation files" in (result.stdout + result.stderr)


def test_no_warning_when_implementation_present(tmp_path):
    _service(
        tmp_path,
        headers={
            "a.hpp": "#pragma once\nclass A {\npublic:\n    double f(double x);\n};\n",
            "a.cpp": '#include "a.hpp"\ndouble A::f(double x) { return x; }\n',
        },
    )

    result = _run_cli(["generate", "svc"], cwd=tmp_path)

    assert "no .cpp/.cc/.cxx implementation files" not in (result.stdout + result.stderr)
