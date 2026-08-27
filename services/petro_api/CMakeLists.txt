cmake_minimum_required(VERSION 3.18)
project(petro_api LANGUAGES Fortran)

find_package(Python REQUIRED COMPONENTS Interpreter Development.Module NumPy)

execute_process(
    COMMAND "${Python_EXECUTABLE}" -c "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))"
    OUTPUT_VARIABLE PY_EXT_SUFFIX
    OUTPUT_STRIP_TRAILING_WHITESPACE
)

set(F2PY_SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/native/_expanded/petro_api.f90
    ${CMAKE_CURRENT_SOURCE_DIR}/native/_expanded/flash.f
    ${CMAKE_CURRENT_SOURCE_DIR}/native/_expanded/hydrau.f
    ${CMAKE_CURRENT_SOURCE_DIR}/native/_expanded/matsol.f
    ${CMAKE_CURRENT_SOURCE_DIR}/native/_expanded/pvtcor.f
    ${CMAKE_CURRENT_SOURCE_DIR}/native/_expanded/relperm.f
    ${CMAKE_CURRENT_SOURCE_DIR}/native/_expanded/simcor.f
    ${CMAKE_CURRENT_SOURCE_DIR}/native/_expanded/wellib.f
)

set(F2PY_OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/petro_api${PY_EXT_SUFFIX}")

# f2py's meson backend picks tempfile.mkdtemp() for its build directory
# whenever it isn't told otherwise, and nothing removes that directory or
# records where it went — orphaning the compile_commands.json and .o files
# meson always writes there. Pin an explicit, discoverable build directory
# under the service's own `.nativegate/` tree (the same convention used for
# `.nativegate/ir.json`) so `ngate build` leaves a real, locatable build
# tree behind: services/<name>/.nativegate/build/bbdir/compile_commands.json.
set(F2PY_BUILD_DIR "${CMAKE_CURRENT_SOURCE_DIR}/.nativegate/build")

add_custom_command(
    OUTPUT "${F2PY_OUTPUT}"
    COMMAND "${Python_EXECUTABLE}" -m numpy.f2py -c --backend meson --build-dir "${F2PY_BUILD_DIR}" -m petro_api ${F2PY_SOURCES} only: pvt_set_fluid solution_gor oil_fvf oil_viscosity gas_z_factor bubble_point pvt_state tubing_bhp vogel_rate last_error :
    WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}"
    DEPENDS ${F2PY_SOURCES}
)

add_custom_target(petro_api_ext ALL DEPENDS "${F2PY_OUTPUT}")

install(FILES "${F2PY_OUTPUT}" DESTINATION petro_api/_native)
