cmake_minimum_required(VERSION 3.20)
cmake_policy(SET CMP0167 NEW)
set(CMAKE_POLICY_VERSION_MINIMUM 3.5)
project(cpp-engine LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# ---------------------------------------------------------------------------
# Dependencies from conda (Arrow, Parquet)
# ---------------------------------------------------------------------------
find_package(Arrow REQUIRED)
find_package(Parquet REQUIRED)
find_package(Boost REQUIRED COMPONENTS serialization)

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

set(YAML_CPP_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(YAML_CPP_BUILD_TOOLS OFF CACHE BOOL "" FORCE)

# ---------------------------------------------------------------------------
# Conda prefix (jsoncpp, c-ares for Drogon)
# ---------------------------------------------------------------------------
if(DEFINED ENV{CONDA_PREFIX})
    list(APPEND CMAKE_PREFIX_PATH "$ENV{CONDA_PREFIX}")
endif()

find_package(jsoncpp REQUIRED)

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

FetchContent_MakeAvailable(pugixml yaml-cpp googletest drogon)

# ---------------------------------------------------------------------------
# Engine library
# ---------------------------------------------------------------------------
add_library(engine STATIC
    # Phase 0: Data
    src/data/data_panel.cpp
    src/data/config_loader.cpp
    src/data/parquet_reader.cpp

    # Phase 1: Core
    src/core/signal_vector.cpp

    # Phase 2: Compiler
    src/compiler/xml_parser.cpp
    src/compiler/expansion.cpp
    src/compiler/expr_parser.cpp
    src/compiler/dag_builder.cpp

    # Phase 4: Simulator
    src/engine/simulator.cpp

    # Phase 5: Metrics
    src/metrics/summary_metrics.cpp
    src/metrics/yearly_metrics.cpp
    src/metrics/timeseries_output.cpp
    src/metrics/metrics_engine.cpp

    # Phase 6: Checkpoint + SIMD
    src/checkpoint/checkpoint_manager.cpp
    src/simd/simd_utils.cpp
    src/simd/m_decay_avx2.cpp

    # Phase 7: GA Multithread
    src/ga/search_space.cpp
    src/ga/ga_engine.cpp
    src/ga/parallel_evaluator.cpp

    # Phase 8: API
    src/api/json_serializer.cpp
)

if (CMAKE_SYSTEM_PROCESSOR MATCHES "^(x86_64|amd64|AMD64|i[3-6]86)$")
    if (MSVC)
        set_source_files_properties(
            src/simd/m_decay_avx2.cpp
            PROPERTIES COMPILE_OPTIONS "/arch:AVX2")
    else()
        check_cxx_compiler_flag("-mavx2" CPP_ENGINE_HAS_MAVX2_FLAG)
        if (CPP_ENGINE_HAS_MAVX2_FLAG)
            set_source_files_properties(
                src/simd/m_decay_avx2.cpp
                PROPERTIES COMPILE_OPTIONS "-mavx2")
        endif()
    endif()
endif()

target_include_directories(engine PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}/src
)

target_compile_options(engine PUBLIC
    $<$<CXX_COMPILER_ID:MSVC>:SHELL:/FI "${CMAKE_CURRENT_SOURCE_DIR}/include/ModulePrelude.h">
    $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:SHELL:-include "${CMAKE_CURRENT_SOURCE_DIR}/include/ModulePrelude.h">
)

target_link_libraries(engine PUBLIC
    Arrow::arrow_shared
    Parquet::parquet_shared
    pugixml
    yaml-cpp
    Boost::serialization
    JsonCpp::JsonCpp
)

# ---------------------------------------------------------------------------
# Server executable
# ---------------------------------------------------------------------------
add_executable(longshort-server src/main.cpp)
target_link_libraries(longshort-server PRIVATE engine drogon)

# One-off benchmark utility for large multi-alpha request breakdown
add_executable(bench_2000_breakdown tools/bench_2000_breakdown.cpp)
target_link_libraries(bench_2000_breakdown PRIVATE engine)

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
enable_testing()

function(add_engine_test name source)
    add_executable(${name} ${source})
    target_link_libraries(${name} PRIVATE engine GTest::gtest_main)
    add_test(NAME ${name} COMMAND ${name})
endfunction()

add_engine_test(test_data_loader      tests/test_data_loader.cpp)
add_engine_test(test_modules          tests/test_modules.cpp)
add_engine_test(test_simulator        tests/test_simulator.cpp)
add_engine_test(test_summary_metrics  tests/test_summary_metrics.cpp)
add_engine_test(test_module_oracles   tests/test_module_oracles.cpp)
add_engine_test(test_metrics_oracle   tests/test_metrics_oracle.cpp)
add_engine_test(test_pipeline_contract tests/test_pipeline_contract.cpp)
add_engine_test(test_http_contract    tests/test_http_contract.cpp)
add_engine_test(test_extreme_alpha_stress tests/test_extreme_alpha_stress.cpp)
add_engine_test(test_yearly_metrics   tests/test_yearly_metrics.cpp)
add_engine_test(test_checkpoint       tests/test_checkpoint.cpp)
add_engine_test(test_simd_parity      tests/test_simd_parity.cpp)
add_engine_test(test_ga               tests/test_ga.cpp)
add_engine_test(test_determinism      tests/test_determinism.cpp)
add_engine_test(test_xml_compiler     tests/test_xml_compiler.cpp)
add_engine_test(test_golden_crossval  tests/test_golden_crossval.cpp)
add_engine_test(test_combined_alpha   tests/test_combined_alpha.cpp)
