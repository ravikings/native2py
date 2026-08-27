"""The C++ oracle driver generator (nativegate.drivers.cpp, T7).

Mirrors test_fortran_driver.py's (T3) two kinds of test:

* golden-file style: a small synthetic IR + golden document, asserting the
  generated source is byte-stable across repeated generation (spec sec 4,
  determinism) and that its wire-format lines are exactly what the spec
  says they should be — checked by actually compiling and running the
  driver against a self-contained fixture header, not by eyeballing the
  source.
* one row per spec section 2.9 scope entry: free function, static method,
  instance method with recorded constructor arguments, and the
  struct-by-value skip.

No `services/` C++ demo exists in this checkout (verified: `services/`
holds only `petro_api`, a Fortran service), so per the task brief this
module's fixture lives here rather than under `services/`. (`cmake_gen.py`
now sets `CMAKE_EXPORT_COMPILE_COMMANDS ON` — see test_cmake_gen.py for an
end-to-end test that configures a generated CMakeLists.txt for real and
checks the resulting compile_commands.json.)
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from nativegate.drivers.cpp import generate_driver
from nativegate.ir import ClassDef, FunctionDef, Method, ModuleIR, Parameter, StructDef

requires_cxx = pytest.mark.skipif(
    shutil.which("clang++") is None and shutil.which("g++") is None,
    reason="neither clang++ nor g++ is installed",
)


def _cxx() -> str:
    return shutil.which("clang++") or shutil.which("g++")


def _hex(value: float) -> str:
    return struct.pack(">d", value).hex()


# --- synthetic fixture -------------------------------------------------

FIXTURE_HPP = """\
#pragma once

#include <vector>

namespace geo {

class Box {
public:
    explicit Box(double side) : side_(side) {}
    double volume() const { return side_ * side_ * side_; }
    static double scale(double factor) { return factor * 2.0; }

private:
    double side_;
};

}  // namespace geo

inline int add_ints(int a, int b) { return a + b; }

inline void increment(int* value) { *value = *value + 1; }

inline float half(float x) { return x * 0.5f; }

struct Point {
    double x;
    double y;
};

inline double with_struct(Point p) { return p.x + p.y; }

inline double with_array(const double* arr, int n) {
    double total = 0.0;
    for (int i = 0; i < n; ++i) total += arr[i];
    return total;
}

inline void fill_buffer(double* out, int n) {
    for (int i = 0; i < n; ++i) out[i] = i * 1.5;
}

inline std::vector<double> make_range(int n) {
    std::vector<double> v;
    for (int i = 0; i < n; ++i) v.push_back(i * 2.0);
    return v;
}

inline double sum_mystery(const double* arr) {
    // No length is derivable from the signature alone — the length_param-
    // less skip case.
    return arr[0];
}
"""


def _synthetic_module() -> ModuleIR:
    return ModuleIR(
        name="synth",
        language="cpp",
        source_file="fixture.hpp",
        structs=[
            StructDef(
                name="Point",
                fields=[
                    Parameter(name="x", type="float"),
                    Parameter(name="y", type="float"),
                ],
            )
        ],
        classes=[
            ClassDef(
                name="Box",
                namespace="geo",
                has_default_constructor=False,
                constructors=[[Parameter(name="side", type="float", intent="in")]],
                methods=[
                    Method(name="volume", parameters=[], returns="float"),
                    Method(
                        name="scale",
                        parameters=[Parameter(name="factor", type="float", intent="in")],
                        returns="float",
                        is_static=True,
                    ),
                ],
            )
        ],
        functions=[
            FunctionDef(
                name="add_ints",
                parameters=[
                    Parameter(name="a", type="int", intent="in"),
                    Parameter(name="b", type="int", intent="in"),
                ],
                returns="int",
            ),
            FunctionDef(
                name="increment",
                parameters=[
                    Parameter(
                        name="value",
                        type="int",
                        intent="inout",
                        native_type="int*",
                        is_scalar_ref=True,
                    )
                ],
                returns="void",
            ),
            FunctionDef(
                name="half",
                parameters=[Parameter(name="x", type="float", intent="in", native_type="float")],
                returns="float",
            ),
            FunctionDef(
                name="with_struct",
                parameters=[Parameter(name="p", type="Point", intent="in")],
                returns="float",
            ),
            FunctionDef(
                name="with_array",
                parameters=[
                    Parameter(
                        name="arr",
                        type="float",
                        is_array=True,
                        intent="in",
                        length_param="n",
                    ),
                    Parameter(name="n", type="int", intent="in"),
                ],
                returns="float",
            ),
            FunctionDef(
                name="fill_buffer",
                parameters=[
                    Parameter(
                        name="out",
                        type="float",
                        is_mutable_buffer=True,
                        intent="inout",
                        native_type="double*",
                        length_param="n",
                    ),
                    Parameter(name="n", type="int", intent="in"),
                ],
                returns="void",
            ),
            FunctionDef(
                name="make_range",
                parameters=[Parameter(name="n", type="int", intent="in")],
                returns="float",
                returns_array=True,
            ),
            FunctionDef(
                name="sum_mystery",
                parameters=[
                    Parameter(name="arr", type="float", is_array=True, intent="in")
                ],
                returns="float",
            ),
        ],
    )


def _synthetic_document() -> dict:
    return {
        "format": 2,
        "service": "synth",
        "language": "cpp",
        "entries": {
            "add_ints": {
                "kind": "function",
                "name": "add_ints",
                "arguments": [2, 3],
                "result": 5,
            },
            "Box.volume": {
                "kind": "method",
                "class": "Box",
                "name": "volume",
                "constructor_arguments": [2.0],
                "arguments": [],
                "result": 8.0,
            },
            "Box.scale": {
                "kind": "static",
                "class": "Box",
                "name": "scale",
                "constructor_arguments": [],
                "arguments": [3.0],
                "result": 6.0,
            },
            "increment": {
                "kind": "function",
                "name": "increment",
                "arguments": [5],
                "result": None,
                "argument_effects": {"0": 6},
            },
            "half": {
                "kind": "function",
                "name": "half",
                "arguments": [3.0],
                "result": 1.5,
            },
            "with_struct": {
                "kind": "function",
                "name": "with_struct",
                "arguments": [{"__struct__": "Point", "fields": {"x": 1.0, "y": 2.0}}],
                "result": 3.0,
            },
            "with_array": {
                "kind": "function",
                "name": "with_array",
                "arguments": [[1.0, 2.0, 3.0], 3],
                "result": 6.0,
            },
            "fill_buffer": {
                "kind": "function",
                "name": "fill_buffer",
                # The mutable buffer has no recorded initial content (golden.py
                # does not populate one for an output-only buffer today) — the
                # driver must size it from length_param "n"'s recorded value.
                "arguments": [None, 3],
                "result": None,
                "argument_effects": {"0": [0.0, 1.5, 3.0]},
            },
            "make_range": {
                "kind": "function",
                "name": "make_range",
                "arguments": [3],
                "result": [0.0, 2.0, 4.0],
            },
            "sum_mystery": {
                "kind": "function",
                "name": "sum_mystery",
                # No recorded list value and no length_param — genuinely
                # unsizeable; must be skipped, not guessed.
                "arguments": [None],
                "result": 1.0,
            },
        },
        "skipped": {"Existing.symbol": "recorded by golden, not by the driver"},
    }


RETURN_KINDS = {"half": "float"}


def test_generated_driver_source_is_byte_stable():
    module = _synthetic_module()
    document = _synthetic_document()

    first = generate_driver(document, module, "fixture.hpp", return_kinds=RETURN_KINDS)
    second = generate_driver(document, module, "fixture.hpp", return_kinds=RETURN_KINDS)

    assert first.source == second.source
    assert first.driver_sha256 == second.driver_sha256
    assert first.driver_sha256 == hashlib.sha256(first.source.encode("utf-8")).hexdigest()


def test_generated_driver_source_is_stable_across_a_fresh_process(tmp_path):
    module = _synthetic_module()
    document = _synthetic_document()
    expected = generate_driver(document, module, "fixture.hpp", return_kinds=RETURN_KINDS).source

    # Build the module/document inline rather than importing this test file
    # (pytest test modules are not meant to be imported as a library) — the
    # subprocess reconstructs the same fixture from scratch.
    script = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n"
        "from nativegate.drivers.cpp import generate_driver\n"
        "from nativegate.ir import ClassDef, FunctionDef, Method, ModuleIR, Parameter, StructDef\n"
        "import tests.test_cpp_driver as t\n"
        "module = t._synthetic_module()\n"
        "document = t._synthetic_document()\n"
        'r = generate_driver(document, module, "fixture.hpp", return_kinds={"half": "float"})\n'
        "sys.stdout.write(r.source)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == expected


def test_generated_driver_contains_no_environmental_leakage():
    module = _synthetic_module()
    document = _synthetic_document()
    source = generate_driver(document, module, "fixture.hpp", return_kinds=RETURN_KINDS).source

    assert "/Users/" not in source
    assert "/home/" not in source
    assert str(Path(__file__).resolve().parent) not in source


def test_free_function_row():
    """§2.9 scope row: a C++ free function."""
    module = _synthetic_module()
    document = _synthetic_document()
    result = generate_driver(document, module, "fixture.hpp", return_kinds=RETURN_KINDS)

    assert "add_ints" not in result.skipped
    assert "add_ints(2, 3)" in result.source


def test_static_method_row():
    """§2.9 scope row: a C++ static method."""
    module = _synthetic_module()
    document = _synthetic_document()
    result = generate_driver(document, module, "fixture.hpp", return_kinds=RETURN_KINDS)

    assert "Box.scale" not in result.skipped
    assert "geo::Box::scale(3.0)" in result.source
    # a static call must not construct an instance
    assert "n2p_2_instance" not in result.source


def test_instance_method_row_constructs_from_recorded_constructor_arguments():
    """§2.9 scope row: an instance method, constructed from constructor_arguments."""
    module = _synthetic_module()
    document = _synthetic_document()
    result = generate_driver(document, module, "fixture.hpp", return_kinds=RETURN_KINDS)

    assert "Box.volume" not in result.skipped
    assert "geo::Box n2p_1_instance(2.0);" in result.source
    assert "n2p_1_instance.volume()" in result.source


def test_struct_by_value_row_is_skipped_with_a_reason():
    """§2.9 scope row: structs by value are skipped in v1, never invented."""
    module = _synthetic_module()
    document = _synthetic_document()
    result = generate_driver(document, module, "fixture.hpp", return_kinds=RETURN_KINDS)

    assert "with_struct" in result.skipped
    assert "struct" in result.skipped["with_struct"]
    assert "with_struct(" not in result.source


def test_array_parameter_is_supported():
    """An `is_array` parameter with a known length is fully emitted, not skipped."""
    module = _synthetic_module()
    document = _synthetic_document()
    result = generate_driver(document, module, "fixture.hpp", return_kinds=RETURN_KINDS)

    assert "with_array" not in result.skipped
    assert "double n2p_6_arg0[3] = {1.0, 2.0, 3.0};" in result.source
    assert "with_array(n2p_6_arg0, 3)" in result.source


def test_mutable_buffer_parameter_sized_by_length_param_is_supported():
    """An `is_mutable_buffer` parameter with no recorded content is sized from
    its `length_param`'s recorded value, and its elements are emitted as
    `arg:<i>[<n>]` wire slots — mirroring drivers/fortran.py's intent(out)
    array handling."""
    module = _synthetic_module()
    document = _synthetic_document()
    result = generate_driver(document, module, "fixture.hpp", return_kinds=RETURN_KINDS)

    assert "fill_buffer" not in result.skipped
    assert "double n2p_7_arg0[3] = {};" in result.source
    assert "fill_buffer(n2p_7_arg0, 3)" in result.source
    assert '"fill_buffer", "arg:0[0]"' in result.source
    assert '"fill_buffer", "arg:0[2]"' in result.source


def test_vector_return_is_supported():
    """A `returns_array` function's `std::vector<T>` return is captured and
    each element printed as `return[<n>]`."""
    module = _synthetic_module()
    document = _synthetic_document()
    result = generate_driver(document, module, "fixture.hpp", return_kinds=RETURN_KINDS)

    assert "make_range" not in result.skipped
    assert "std::vector<double> n2p_8_result = make_range(3);" in result.source
    assert '"make_range", "return[0]"' in result.source
    assert '"make_range", "return[2]"' in result.source


def test_array_parameter_with_no_length_param_is_skipped_with_a_reason():
    """The one remaining hard skip: no recorded list value AND no
    length_param — genuinely unsizeable, mirroring T3's own fallback."""
    module = _synthetic_module()
    document = _synthetic_document()
    result = generate_driver(document, module, "fixture.hpp", return_kinds=RETURN_KINDS)

    assert "sum_mystery" in result.skipped
    assert "length_param" in result.skipped["sum_mystery"]
    assert "sum_mystery(" not in result.source


def test_golden_skips_are_reproduced_verbatim():
    module = _synthetic_module()
    document = _synthetic_document()
    result = generate_driver(document, module, "fixture.hpp", return_kinds=RETURN_KINDS)

    assert result.skipped["Existing.symbol"] == "recorded by golden, not by the driver"


def test_float_return_widens_before_memcpy_never_transfers_float_directly():
    module = _synthetic_module()
    document = _synthetic_document()
    source = generate_driver(document, module, "fixture.hpp", return_kinds=RETURN_KINDS).source

    idx = source.index("n2p_4_result = half(3.0)")
    assert "float n2p_4_result = half(3.0);" in source
    following = source[idx : idx + 400]
    assert "double n2p_dbl = static_cast<double>(n2p_4_result);" in following
    assert "std::memcpy(&n2p_bits, &n2p_dbl" in following


def test_never_uses_a_union_or_pointer_cast():
    module = _synthetic_module()
    document = _synthetic_document()
    source = generate_driver(document, module, "fixture.hpp", return_kinds=RETURN_KINDS).source

    assert "union" not in source.lower()
    assert "reinterpret_cast" not in source
    assert "std::memcpy" in source


@requires_cxx
def test_synthetic_driver_compiles_and_prints_the_documented_slots(tmp_path):
    module = _synthetic_module()
    document = _synthetic_document()
    result = generate_driver(document, module, "fixture.hpp", return_kinds=RETURN_KINDS)

    (tmp_path / "fixture.hpp").write_text(FIXTURE_HPP)
    driver = tmp_path / "driver.cpp"
    driver.write_text(result.source)

    compiler = _cxx()
    subprocess.run(
        [compiler, "-std=c++17", "-O2", "-o", str(tmp_path / "driver_test"), str(driver)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    completed = subprocess.run(
        [str(tmp_path / "driver_test")], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    lines = [line.split("\t") for line in completed.stdout.splitlines() if line.strip()]
    by_key_slot = {(k, s): v for k, s, v in lines}

    assert by_key_slot[("add_ints", "return")] == "5"
    assert by_key_slot[("Box.volume", "return")] == _hex(8.0)
    assert by_key_slot[("Box.scale", "return")] == _hex(6.0)
    assert by_key_slot[("increment", "arg:0")] == "6"
    assert by_key_slot[("half", "return")] == _hex(1.5)
    assert by_key_slot[("with_array", "return")] == _hex(6.0)
    assert by_key_slot[("fill_buffer", "arg:0[0]")] == _hex(0.0)
    assert by_key_slot[("fill_buffer", "arg:0[1]")] == _hex(1.5)
    assert by_key_slot[("fill_buffer", "arg:0[2]")] == _hex(3.0)
    assert by_key_slot[("make_range", "return[0]")] == _hex(0.0)
    assert by_key_slot[("make_range", "return[1]")] == _hex(2.0)
    assert by_key_slot[("make_range", "return[2]")] == _hex(4.0)

    # slot names emitted match wire.slots_for_entry's expectations exactly.
    from nativegate import wire

    with_array_fn = next(f for f in module.functions if f.name == "with_array")
    expected = [str(s) for s in wire.slots_for_entry(document["entries"]["with_array"], with_array_fn)]
    assert expected == ["return"]

    fill_buffer_fn = next(f for f in module.functions if f.name == "fill_buffer")
    expected = [str(s) for s in wire.slots_for_entry(document["entries"]["fill_buffer"], fill_buffer_fn)]
    assert expected == ["arg:0[0]", "arg:0[1]", "arg:0[2]"]

    make_range_fn = next(f for f in module.functions if f.name == "make_range")
    expected = [str(s) for s in wire.slots_for_entry(document["entries"]["make_range"], make_range_fn)]
    assert expected == ["return[0]", "return[1]", "return[2]"]

    # file order preserved among the keys that did print (with_struct and
    # sum_mystery are skipped and print nothing).
    keys_in_order = []
    for key, _slot, _value in lines:
        if key not in keys_in_order:
            keys_in_order.append(key)
    doc_keys = [k for k in document["entries"] if k not in result.skipped]
    positions = [doc_keys.index(k) for k in keys_in_order]
    assert positions == sorted(positions)
