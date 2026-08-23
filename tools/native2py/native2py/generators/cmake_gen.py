"""CMake build generator (design.md section 14)."""

from __future__ import annotations

from ..ir import ModuleIR

# CMake target names can't contain hyphens the way directory names can, and
# libraries/common-cpp/ declares `project(common_cpp)` / `add_library(common_cpp)`.
def _library_target(library_dir_name: str) -> str:
    return library_dir_name.replace("-", "_")


def uses_numpy_buffers(module: ModuleIR) -> bool:
    """True when any binding goes through `pybind11/numpy.h`.

    A bare `T*` paired with a length argument is bound as a numpy buffer, which
    needs numpy's HEADERS at build time and numpy at runtime. A C++ service
    that binds no such pointer should not acquire either, so this is asked
    rather than assumed — see pybind_gen._buffer_lambda.
    """
    return any(
        getattr(p, "length_param", None)
        for fn in module.functions
        for p in fn.parameters
    )


def generate_cmake(
    module: ModuleIR,
    service_name: str,
    native_sources: list[str],
    libraries: list[str] | None = None,
    include_paths: list[str] | None = None,
) -> str:
    module_symbol = f"{module.name}_cpp"
    sources = "\n".join(f"    {src}" for src in native_sources)
    libraries = libraries or []

    # `include_paths:` entries are repo-root-relative, and the service's
    # CMakeLists.txt lives two levels down at services/<name>/.
    extra_includes = "".join(
        f"\n    ${{CMAKE_CURRENT_SOURCE_DIR}}/../../{p}" for p in (include_paths or [])
    )
    if extra_includes:
        extra_includes = extra_includes + "\n"

    library_block = ""
    link_block = ""
    if libraries:
        # add_subdirectory needs a distinct binary dir per library because the
        # source lives outside this project's tree (../../libraries/<name>).
        subdirs = "\n".join(
            f"add_subdirectory(${{CMAKE_CURRENT_SOURCE_DIR}}/../../libraries/{lib} "
            f"${{CMAKE_CURRENT_BINARY_DIR}}/_libraries/{lib})"
            for lib in libraries
        )
        targets = " ".join(_library_target(lib) for lib in libraries)
        library_block = (
            "\n# Shared native libraries (design.md section 4). Each declares its own\n"
            "# CMake target; include directories come through PUBLIC usage requirements.\n"
            f"{subdirs}\n"
        )
        link_block = f"\ntarget_link_libraries({module_symbol} PRIVATE {targets})\n"

    # `pybind11/numpy.h` includes numpy's own headers, which live inside the
    # installed numpy package rather than anywhere CMake looks by default.
    numpy_block = ""
    numpy_link = ""
    if uses_numpy_buffers(module):
        numpy_block = (
            "\n# A raw `T*` argument is bound as a numpy buffer, so pybind11/numpy.h\n"
            "# needs numpy's headers. Asked of the interpreter that will import the\n"
            "# extension, so the headers always match the numpy it runs against.\n"
            "find_package(Python REQUIRED COMPONENTS Interpreter Development.Module NumPy)\n"
        )
        numpy_link = (
            f"\ntarget_link_libraries({module_symbol} PRIVATE Python::NumPy)\n"
        )

    return f"""cmake_minimum_required(VERSION 3.18)
project({service_name} LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(pybind11 CONFIG REQUIRED)
{numpy_block}{library_block}
pybind11_add_module({module_symbol}
{sources}
)

target_include_directories({module_symbol} PRIVATE native{extra_includes})
{numpy_link}{link_block}
install(TARGETS {module_symbol} LIBRARY DESTINATION {service_name}/_native)
"""
