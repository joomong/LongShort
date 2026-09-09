# Prefer normal Arrow/Parquet CMake packages. A PyArrow wheel can explicitly
# supply its matching bundled headers and libraries on Linux/macOS.
set(LONGSHORT_PYARROW_PYTHON "" CACHE FILEPATH "Python executable whose PyArrow supplies Arrow/Parquet")
if(LONGSHORT_PYARROW_PYTHON)
    execute_process(
        COMMAND "${LONGSHORT_PYARROW_PYTHON}" "-c" [=[
import json
from pathlib import Path
import pyarrow

def library(name):
    for directory in pyarrow.get_library_dirs():
        root = Path(directory)
        for pattern in (f"lib{name}.so", f"lib{name}.so.*", f"lib{name}.dylib", f"lib{name}.*.dylib"):
            matches = sorted(root.glob(pattern))
            if matches:
                return str(matches[0].resolve())
    raise RuntimeError(f"PyArrow does not provide a shared {name} library; use Arrow CMake packages")

print(json.dumps({"include": pyarrow.get_include(), "arrow": library("arrow"), "parquet": library("parquet"), "version": pyarrow.__version__}))
]=]
        RESULT_VARIABLE pyarrow_status
        OUTPUT_VARIABLE pyarrow_json
        ERROR_VARIABLE pyarrow_error
        OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(NOT pyarrow_status EQUAL 0)
        message(FATAL_ERROR "Cannot use LONGSHORT_PYARROW_PYTHON=${LONGSHORT_PYARROW_PYTHON}: ${pyarrow_error}")
    endif()
    string(JSON pyarrow_include GET "${pyarrow_json}" include)
    string(JSON pyarrow_arrow GET "${pyarrow_json}" arrow)
    string(JSON pyarrow_parquet GET "${pyarrow_json}" parquet)
    string(JSON pyarrow_version GET "${pyarrow_json}" version)
    add_library(Arrow::arrow_shared SHARED IMPORTED GLOBAL)
    set_target_properties(Arrow::arrow_shared PROPERTIES
        IMPORTED_LOCATION "${pyarrow_arrow}"
        INTERFACE_INCLUDE_DIRECTORIES "${pyarrow_include}")
    add_library(Parquet::parquet_shared SHARED IMPORTED GLOBAL)
    set_target_properties(Parquet::parquet_shared PROPERTIES
        IMPORTED_LOCATION "${pyarrow_parquet}"
        INTERFACE_INCLUDE_DIRECTORIES "${pyarrow_include}"
        INTERFACE_LINK_LIBRARIES Arrow::arrow_shared)
    message(STATUS "Arrow/Parquet from PyArrow ${pyarrow_version}: ${pyarrow_arrow}")
else()
    find_package(Arrow CONFIG REQUIRED)
    find_package(Parquet CONFIG REQUIRED)
endif()
