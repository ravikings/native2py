# =====================================================================
# libraries/petro - legacy reservoir engineering library
#
# The original build was a hand written Makefile per platform (HP-UX,
# VMS, AIX, later NT). This CMake file was bolted on in 2011 purely so
# the library could be linked from newer tooling; the source itself is
# unchanged and still needs -std=legacy on the Fortran side.
# =====================================================================
cmake_minimum_required(VERSION 3.15)
project(petro C CXX Fortran)

set(CMAKE_CXX_STANDARD 98)
set(CMAKE_CXX_STANDARD_REQUIRED OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Fixed-form F77 decks. INCLUDE 'PETRO.INC' resolves via -I.
set(PETRO_F77_SOURCES
    fortran/pvtcor.f
    fortran/relperm.f
    fortran/matsol.f
    fortran/hydrau.f
    fortran/flash.f
    fortran/simcor.f
    fortran/wellib.f
)

# Free-form F90 facade added in 2003.
set(PETRO_F90_SOURCES
    fortran/modern/petro_api.f90
)

set(PETRO_CXX_SOURCES
    cpp/src/Units.cpp
    cpp/src/FluidModel.cpp
    cpp/src/WellModel.cpp
    cpp/src/DeckReader.cpp
    cpp/src/Simulator.cpp
)

set_source_files_properties(${PETRO_F77_SOURCES} PROPERTIES
    Fortran_FORMAT FIXED
    COMPILE_OPTIONS "-std=legacy;-fno-automatic;-finit-local-zero"
)
set_source_files_properties(${PETRO_F90_SOURCES} PROPERTIES
    Fortran_FORMAT FREE
)

add_library(petro STATIC
    ${PETRO_F77_SOURCES}
    ${PETRO_F90_SOURCES}
    ${PETRO_CXX_SOURCES}
)

target_include_directories(petro PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/cpp/include
    ${CMAKE_CURRENT_SOURCE_DIR}/fortran/include
)

# INCLUDE 'PETRO.INC' / 'GRID.INC' lookup path for the fixed-form decks.
target_compile_options(petro PRIVATE
    $<$<COMPILE_LANGUAGE:Fortran>:-I${CMAKE_CURRENT_SOURCE_DIR}/fortran/include>
)
