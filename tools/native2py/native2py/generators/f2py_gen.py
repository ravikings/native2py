"""f2py-based CMake build generator (design.md sections 8, 14)."""

from __future__ import annotations

from ..ir import ModuleIR


def generate_cmake(
    module: ModuleIR,
    service_name: str,
    native_sources: list[str],
    only_routines: list[str] | None = None,
) -> str:
    # add_custom_command's COMMAND runs with CMAKE_CURRENT_BINARY_DIR as its
    # working directory, not the source dir — relative "native/foo.f90" only
    # resolves by coincidence for in-source builds. scikit-build-core builds
    # out-of-tree (a temp dir), so this must be an absolute path.
    sources = "\n".join(f"    ${{CMAKE_CURRENT_SOURCE_DIR}}/{src}" for src in native_sources)

    # `only: a b :` restricts f2py to the named routines. For legacy F77 this
    # is a correctness requirement, not a size optimization: wrapping a whole
    # file pulls in routines whose PARAMETER-dimensioned arrays f2py emits but
    # never defines, and the C wrapper then fails to compile
    # ("use of undeclared identifier 'mxcell'"). Verified against
    # libraries/petro — matsol.f wraps fine per-routine, fails whole-file.
    only_clause = ""
    if only_routines:
        only_clause = " only: " + " ".join(r.lower() for r in only_routines) + " :"

    # f2py's intermediate build (custom module.c + optional f2pywrappers2.f90,
    # reassembled by hand into python_add_library) only produces the wrapper
    # file when routines actually need one (e.g. array-intent subroutines) —
    # a plain `function` like a scalar-only routine doesn't generate it, so
    # hardcoding it as a build output breaks for exactly that case. `f2py -c`
    # runs f2py's own build end-to-end and sidesteps this entirely.
    return f"""cmake_minimum_required(VERSION 3.18)
project({service_name} LANGUAGES Fortran)

find_package(Python REQUIRED COMPONENTS Interpreter Development.Module NumPy)

execute_process(
    COMMAND "${{Python_EXECUTABLE}}" -c "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))"
    OUTPUT_VARIABLE PY_EXT_SUFFIX
    OUTPUT_STRIP_TRAILING_WHITESPACE
)

set(F2PY_SOURCES
{sources}
)

set(F2PY_OUTPUT "${{CMAKE_CURRENT_BINARY_DIR}}/{module.name}${{PY_EXT_SUFFIX}}")

add_custom_command(
    OUTPUT "${{F2PY_OUTPUT}}"
    COMMAND "${{Python_EXECUTABLE}}" -m numpy.f2py -c -m {module.name} ${{F2PY_SOURCES}}{only_clause}
    WORKING_DIRECTORY "${{CMAKE_CURRENT_BINARY_DIR}}"
    DEPENDS ${{F2PY_SOURCES}}
)

add_custom_target({module.name}_ext ALL DEPENDS "${{F2PY_OUTPUT}}")

install(FILES "${{F2PY_OUTPUT}}" DESTINATION {service_name}/_native)
"""
