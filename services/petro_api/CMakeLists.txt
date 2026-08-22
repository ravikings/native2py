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
)

set(F2PY_OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/petro_api${PY_EXT_SUFFIX}")

add_custom_command(
    OUTPUT "${F2PY_OUTPUT}"
    COMMAND "${Python_EXECUTABLE}" -m numpy.f2py -c -m petro_api ${F2PY_SOURCES} only: pvt_set_fluid solution_gor oil_fvf oil_viscosity gas_z_factor bubble_point pvt_state tubing_bhp vogel_rate last_error :
    WORKING_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}"
    DEPENDS ${F2PY_SOURCES}
)

add_custom_target(petro_api_ext ALL DEPENDS "${F2PY_OUTPUT}")

install(FILES "${F2PY_OUTPUT}" DESTINATION petro_api/_native)
