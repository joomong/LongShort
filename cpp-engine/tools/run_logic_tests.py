#!/usr/bin/env python3
"""Build the non-HTTP engine tests without Arrow/Parquet or the server build.

This is a temporary-environment validation path, not a replacement for CMake/CI.
It uses Boost 1.85 headers already on the machine and builds matching static
serialization, GoogleTest, JsonCpp, and pugixml libraries from pinned sources.
See docs/refactor_2026-09-09_validation.md for dependency preparation and scope.
"""

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys


def run(command, *, cwd=None):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    return {
        "command": [str(arg) for arg in command],
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def checked(command, *, cwd=None):
    result = run(command, cwd=cwd)
    if result["exit_code"]:
        print(result["stdout"] + result["stderr"], file=sys.stderr)
        raise RuntimeError(f"Command failed: {command[0]}")
    return result


def build_objects(sources, out_dir, flags, jobs):
    out_dir.mkdir(parents=True, exist_ok=True)

    def compile_one(entry):
        index, source = entry
        output = out_dir / f"{index}_{source.stem}.o"
        extra = []
        if source.name == "m_decay_avx2.cpp" and platform.machine().lower() in {
            "x86_64", "amd64", "i386", "i686"
        }:
            extra = ["-mavx2"]
        result = run([*flags, *extra, "-c", str(source), "-o", str(output)])
        result.update(source=str(source), object=str(output))
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(compile_one, enumerate(sources)))
    (out_dir / "compile_results.json").write_text(json.dumps(results, indent=2))
    failures = [result for result in results if result["exit_code"]]
    for result in failures:
        print(f"{result['source']}:\n{result['stderr']}", file=sys.stderr)
    if failures:
        raise RuntimeError(f"{len(failures)} compilation(s) failed; see {out_dir}")
    return [result["object"] for result in results]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deps-dir", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--boost-include", type=Path, default=Path("/usr/local/include"))
    parser.add_argument("--cxx", default=os.environ.get("CXX", "g++"))
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--drogon-source", type=Path)
    parser.add_argument("--drogon-build", type=Path)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    if bool(args.drogon_source) != bool(args.drogon_build):
        parser.error("--drogon-source and --drogon-build must be supplied together")

    engine = Path(__file__).resolve().parents[1]
    deps = args.deps_dir.resolve()
    build = args.build_dir.resolve()
    build.mkdir(parents=True, exist_ok=True)
    boost_include = args.boost_include.resolve()
    boost_version = (boost_include / "boost/version.hpp").read_text()
    if not re.search(r"^#define BOOST_VERSION 108500$", boost_version, re.MULTILINE):
        raise RuntimeError("This harness requires Boost 1.85.0 headers to match its library sources")

    serialization = deps / "serialization-boost-1.85.0"
    gtest = deps / "googletest-1.14.0/googletest"
    jsoncpp = deps / "jsoncpp-1.9.5"
    pugixml = deps / "pugixml-1.14/src"
    for required in (
        serialization / "CMakeLists.txt", gtest / "src/gtest-all.cc",
        jsoncpp / "src/lib_json/json_reader.cpp", pugixml / "pugixml.cpp",
    ):
        if not required.is_file():
            raise RuntimeError(f"Missing pinned dependency source: {required}")

    common = [
        args.cxx, "-std=c++17", "-O1", "-g", "-pthread",
        "-ffunction-sections", "-fdata-sections",
        "-DBOOST_SERIALIZATION_NO_LIB", "-DBOOST_SERIALIZATION_STATIC_LINK",
        "-I" + str(serialization / "include"), "-I" + str(boost_include),
    ]
    dep_flags = common + [
        "-I" + str(gtest / "include"), "-I" + str(gtest),
        "-I" + str(jsoncpp / "include"), "-I" + str(pugixml),
    ]
    # The upstream source list also includes the narrow text/XML archives.
    # Keeping that list avoids maintaining a partial hand-written Boost build.
    cmake = (serialization / "CMakeLists.txt").read_text()
    narrow_library = cmake.split("add_library(boost_serialization", 1)[1].split(")", 1)[0]
    boost_sources = [serialization / path for path in re.findall(r"src/\S+\.cpp", narrow_library)]
    dep_sources = boost_sources + [
        gtest / "src/gtest-all.cc", gtest / "src/gtest_main.cc",
        jsoncpp / "src/lib_json/json_reader.cpp",
        jsoncpp / "src/lib_json/json_value.cpp",
        jsoncpp / "src/lib_json/json_writer.cpp",
        pugixml / "pugixml.cpp",
    ]
    dep_archive = build / "liblogic_test_dependencies.a"
    manifest = build / "dependency_manifest.json"
    expected_manifest = {
        "flags": dep_flags,
        "compiler": checked([args.cxx, "--version"])["stdout"],
        "sources": [str(source) for source in dep_sources],
    }
    if not dep_archive.exists() or not manifest.exists() or json.loads(manifest.read_text()) != expected_manifest:
        print("Building pinned validation dependencies...", flush=True)
        objects = build_objects(dep_sources, build / "dependency_objects", dep_flags, args.jobs)
        # Recreate the archive so a changed upstream source list cannot leave old members.
        dep_archive.unlink(missing_ok=True)
        checked(["ar", "rcs", str(dep_archive), *objects])
        manifest.write_text(json.dumps(expected_manifest, indent=2))
    if args.prepare_only:
        print(f"Prepared {dep_archive}")
        return 0

    engine_flags = dep_flags + [
        "-I" + str(engine / "include"), "-I" + str(engine / "src"),
        "-include", str(engine / "include/ModulePrelude.h"),
    ]
    # DataPanel::load is unreferenced by these in-memory tests and is discarded
    # with --gc-sections. File loading, YAML and HTTP integration are excluded.
    excluded = {"main.cpp", "parquet_reader.cpp", "config_loader.cpp"}
    sources = sorted(path for path in (engine / "src").rglob("*.cpp") if path.name not in excluded)
    sources += sorted(path for path in (engine / "tests").glob("test_*.cpp") if path.name != "test_http_contract.cpp")
    print(f"Building {len(sources)} engine/test translation units...", flush=True)
    objects = build_objects(sources, build / "engine_objects", engine_flags, args.jobs)
    binary = build / "logic_tests"
    checked([args.cxx, *objects, str(dep_archive), "-pthread", "-Wl,--gc-sections", "-o", str(binary)])

    if args.drogon_source:
        drogon = args.drogon_source.resolve()
        generated = args.drogon_build.resolve()
        include_paths = [
            drogon / "lib/inc", drogon / "orm_lib/inc", drogon / "nosql_lib/redis/inc",
            drogon / "trantor", generated, generated / "exports", generated / "trantor/exports",
        ]
        syntax = run([
            *engine_flags, *["-I" + str(path) for path in include_paths],
            "-fsyntax-only", str(engine / "src/main.cpp"),
        ])
        (build / "server_syntax_check.json").write_text(json.dumps(syntax, indent=2))
        if syntax["exit_code"]:
            print(syntax["stderr"], file=sys.stderr)
            return syntax["exit_code"]
        print("Server main.cpp passed syntax check with official Drogon/Trantor headers.", flush=True)

    result = run([str(binary), "--gtest_color=no", "--gtest_output=json:" + str(build / "test_results.json")], cwd=engine)
    (build / "test_output.log").write_text(result["stdout"] + result["stderr"])
    print(result["stdout"] + result["stderr"], end="")
    return result["exit_code"]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
