cmake_minimum_required(VERSION 3.18)
project(common_cpp LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# STATIC, not SHARED: the library gets linked directly into each service's
# Python extension module, so there's no second .so to ship, install, or
# keep on LD_LIBRARY_PATH inside the container. POSITION_INDEPENDENT_CODE is
# required because the consuming target is a shared module.
add_library(common_cpp STATIC
    src/units.cpp
)

set_target_properties(common_cpp PROPERTIES POSITION_INDEPENDENT_CODE ON)

target_include_directories(common_cpp PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
)
