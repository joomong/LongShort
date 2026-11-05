cmake_minimum_required(VERSION 3.24)
if(POLICY CMP0167)
    cmake_policy(SET CMP0167 NEW)
endif()
set(CMAKE_POLICY_VERSION_MINIMUM 3.5)
project(cpp-engine LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# ---------------------------------------------------------------------------
# Shared C++ dependencies
# ---------------------------------------------------------------------------
find_package(Arrow 18.1.0 EXACT CONFIG REQUIRED)
find_package(Parquet 18.1.0 EXACT CONFIG REQUIRED)

set(LONGSHORT_MODULE_DIR "" CACHE PATH
    "Directory containing additional user module headers and sources (empty: built-in modules only)")
option(LONGSHORT_BUILD_TESTS "Build engine tests" ON)

# ---------------------------------------------------------------------------
# FetchContent for pugixml, yaml-cpp, googletest
# ---------------------------------------------------------------------------
include(FetchContent)
include(CheckCXXCompilerFlag)

FetchContent_Declare(pugixml
    GIT_REPOSITORY https://github.com/zeux/pugixml.git
    GIT_TAG        v1.14
    GIT_SHALLOW    TRUE
)

FetchContent_Declare(yaml-cpp
    GIT_REPOSITORY https://github.com/jbeder/yaml-cpp.git
    GIT_TAG        0.8.0
    GIT_SHALLOW    TRUE
)

FetchContent_Declare(googletest
    GIT_REPOSITORY https://github.com/google/googletest.git
    GIT_TAG        v1.14.0
    GIT_SHALLOW    TRUE
)

# Eigen 3.4.0 (header-only linear algebra for <meta> combination weights).
FetchContent_Declare(eigen
    URL      https://gitlab.com/libeigen/eigen/-/archive/3.4.0/eigen-3.4.0.tar.gz
    URL_HASH SHA256=8586084f71f9bde545ee7fa6d00288b264a2b7ac3607b974e54d13e7162c1c72
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE
    SOURCE_SUBDIR  headers-only   # no such directory: download only, Eigen's CMake project is not configured
)

set(YAML_CPP_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(YAML_CPP_BUILD_TOOLS OFF CACHE BOOL "" FORCE)

# ---------------------------------------------------------------------------
# Conda prefix (jsoncpp, c-ares for Drogon)
# ---------------------------------------------------------------------------
find_package(jsoncpp CONFIG REQUIRED)

# Drogon options (disable unused features)
set(BUILD_TESTING OFF CACHE BOOL "" FORCE)
set(BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(BUILD_CTL OFF CACHE BOOL "" FORCE)
set(BUILD_ORM OFF CACHE BOOL "" FORCE)
set(BUILD_YAML_CONFIG OFF CACHE BOOL "" FORCE)
set(BUILD_BROTLI OFF CACHE BOOL "" FORCE)
set(COZ_PROFILING OFF CACHE BOOL "" FORCE)

FetchContent_Declare(drogon
    GIT_REPOSITORY https://github.com/drogonframework/drogon.git
    GIT_TAG        v1.9.8
    GIT_SHALLOW    TRUE
)

FetchContent_MakeAvailable(pugixml yaml-cpp drogon eigen)
if(LONGSHORT_BUILD_TESTS)
    FetchContent_MakeAvailable(googletest)
endif()

# ---------------------------------------------------------------------------
# Engine library
# ---------------------------------------------------------------------------
add_library(engine STATIC
    src/loader.cpp
    src/parallel_executor.cpp
    src/xml_compiler.cpp
    src/simulator_engine.cpp
    src/alpha_search.cpp
    src/meta.cpp
    src/alpha_storage.cpp
    src/pool_correlation.cpp
    src/modules/decay_kernel.cpp
)

include(cmake/DiscoverModules.cmake)
if(LONGSHORT_MODULE_DIR STREQUAL "")
    set(LONGSHORT_MODULE_DIR "${CMAKE_BINARY_DIR}/no_user_modules")  # empty: built-in modules only
endif()
longshort_discover_modules(engine "${LONGSHORT_MODULE_DIR}")

option(LONGSHORT_ENABLE_AVX2 "Build for AVX2-capable CPUs (OFF: scalar kernels)" ON)
if(LONGSHORT_ENABLE_AVX2)
    if(NOT CMAKE_SYSTEM_PROCESSOR MATCHES "^(x86_64|amd64|AMD64|i[3-6]86)$")
        message(FATAL_ERROR "AVX2 needs x86; configure with -DLONGSHORT_ENABLE_AVX2=OFF")
    endif()
    if(MSVC)
        target_compile_options(engine PUBLIC /arch:AVX2)
    else()
        check_cxx_compiler_flag("-mavx2" LONGSHORT_HAS_MAVX2_FLAG)
        if(NOT LONGSHORT_HAS_MAVX2_FLAG)
            message(FATAL_ERROR "Compiler does not support -mavx2; set LONGSHORT_ENABLE_AVX2=OFF")
        endif()
        target_compile_options(engine PUBLIC -mavx2)
    endif()
endif()

target_include_directories(engine PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)
target_include_directories(engine PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)

target_include_directories(engine SYSTEM PUBLIC ${eigen_SOURCE_DIR})

target_compile_options(engine PUBLIC
    "$<$<CXX_COMPILER_ID:MSVC>:SHELL:/FI \"${CMAKE_CURRENT_SOURCE_DIR}/include/ModulePrelude.h\">"
    "$<$<NOT:$<CXX_COMPILER_ID:MSVC>>:SHELL:-include \"${CMAKE_CURRENT_SOURCE_DIR}/include/ModulePrelude.h\">"
)

find_package(Threads REQUIRED)
target_link_libraries(engine PUBLIC
    Threads::Threads
    Arrow::arrow_shared
    Parquet::parquet_shared
    pugixml
    yaml-cpp
    JsonCpp::JsonCpp
)

# ---------------------------------------------------------------------------
# Server executable
# ---------------------------------------------------------------------------
add_library(http_server STATIC src/http_server.cpp)
target_link_libraries(http_server PUBLIC engine drogon)
target_include_directories(http_server PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
add_executable(longshort-server src/main.cpp)
target_link_libraries(longshort-server PRIVATE http_server)
# Stable executable location for the launcher and HTTP tests, also with multi-config generators.
set_target_properties(longshort-server PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "$<1:${CMAKE_BINARY_DIR}>")

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
if(LONGSHORT_BUILD_TESTS)
find_package(Python3 3.8 REQUIRED COMPONENTS Interpreter)
enable_testing()

function(add_engine_test name source)
    add_executable(${name} ${source})
    target_link_libraries(${name} PRIVATE engine GTest::gtest_main)
    target_include_directories(${name} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
    add_test(NAME ${name} COMMAND ${name})
    if(NOT name STREQUAL "test_http_contract")
        set_tests_properties(${name} PROPERTIES WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
    endif()
endfunction()

add_engine_test(test_data_loader      tests/test_data_loader.cpp)
add_engine_test(test_config_loader    tests/test_config_loader.cpp)
add_engine_test(test_parquet_loader   tests/test_parquet_loader.cpp)
add_engine_test(test_modules          tests/test_modules.cpp)
add_engine_test(test_module_parallelism tests/test_module_parallelism.cpp)
set_tests_properties(test_module_parallelism PROPERTIES TIMEOUT 30)
add_engine_test(test_simulator        tests/test_simulator.cpp)
add_engine_test(test_thin_loader      tests/test_thin_loader.cpp)
add_engine_test(test_thin_moves       tests/test_thin_moves.cpp)
add_engine_test(test_summary_metrics  tests/test_summary_metrics.cpp)
add_engine_test(test_module_oracles   tests/test_module_oracles.cpp)
add_engine_test(test_metrics_oracle   tests/test_metrics_oracle.cpp)
add_engine_test(test_pipeline_contract tests/test_pipeline_contract.cpp)
target_link_libraries(test_pipeline_contract PRIVATE http_server)
add_engine_test(test_http_contract    tests/test_http_contract.cpp)
add_dependencies(test_http_contract longshort-server)
add_engine_test(test_extreme_alpha_stress tests/test_extreme_alpha_stress.cpp)
add_engine_test(test_yearly_metrics   tests/test_yearly_metrics.cpp)
add_engine_test(test_simd_parity      tests/test_simd_parity.cpp)
add_engine_test(test_ga               tests/test_ga.cpp)
add_engine_test(test_determinism      tests/test_determinism.cpp)
add_engine_test(test_xml_compiler     tests/test_xml_compiler.cpp)
add_engine_test(test_golden_crossval  tests/test_golden_crossval.cpp)
add_engine_test(test_combined_alpha   tests/test_combined_alpha.cpp)
add_engine_test(test_json_serializer tests/test_json_serializer.cpp)
target_link_libraries(test_json_serializer PRIVATE http_server)
add_engine_test(test_alpha_storage    tests/test_alpha_storage.cpp)
add_engine_test(test_meta_dir         tests/test_meta_dir.cpp)
add_engine_test(test_alpha_pool       tests/test_alpha_pool.cpp)
add_engine_test(test_meta_sets        tests/test_meta_sets.cpp)
add_engine_test(test_meta_weights     tests/test_meta_weights.cpp)
add_engine_test(test_unary_modules    tests/test_unary_modules.cpp)
add_engine_test(test_expr_functions   tests/test_expr_functions.cpp)
add_engine_test(test_lookback         tests/test_lookback.cpp)
add_engine_test(test_compute_range    tests/test_compute_range.cpp)
add_engine_test(test_ts_modules       tests/test_ts_modules.cpp)
add_engine_test(test_cs_modules       tests/test_cs_modules.cpp)
add_engine_test(test_ffd_pcaresid     tests/test_ffd_pcaresid.cpp)

# Test-only CLI for the Python oracle of the combination weights.
add_executable(meta_weights_probe tests/meta_weights_probe.cpp)
target_link_libraries(meta_weights_probe PRIVATE engine)
target_include_directories(meta_weights_probe PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)

add_engine_test(test_module_attributes tests/test_module_attributes.cpp)
add_engine_test(test_execution_price  tests/test_execution_price.cpp)
add_engine_test(test_discovered_modules tests/test_discovered_modules.cpp)
longshort_discover_modules(test_discovered_modules
    "${CMAKE_CURRENT_SOURCE_DIR}/tests/fixtures/discovered_modules")
foreach(tool_test test_longshort_launcher test_module_build)
    add_test(NAME ${tool_test}
        COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/${tool_test}.py")
endforeach()

# The weights oracle needs numpy/scipy/scikit-learn, which ${Python3_EXECUTABLE} need not have.
# The test exits 77 when its Python dependencies are unavailable.
add_test(NAME test_meta_weights_oracle
    COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_meta_weights_oracle.py"
            "$<TARGET_FILE:meta_weights_probe>")
set_tests_properties(test_meta_weights_oracle PROPERTIES SKIP_RETURN_CODE 77)
endif()
