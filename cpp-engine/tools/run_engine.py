#!/usr/bin/env python3
"""Discover user modules, incrementally build the engine, then start the server."""

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


class LaunchError(RuntimeError):
    pass


def positive_int(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def cmake_definition(value):
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*)(?::[A-Za-z_]+)?=(.*)", value, re.DOTALL)
    if not match:
        raise argparse.ArgumentTypeError("expected NAME=VALUE or NAME:TYPE=VALUE")
    if match[1] in {"LONGSHORT_MODULE_DIR", "Python3_EXECUTABLE", "CMAKE_BUILD_TYPE"}:
        raise argparse.ArgumentTypeError(f"{match[1]} is managed by the launcher")
    return value


@contextmanager
def build_lock(build_dir):
    # flock releases on process exit, including interrupted/failed builds.
    if os.name != "posix":
        raise LaunchError("run_engine.py currently requires a POSIX build environment")
    import fcntl
    lock_path = build_dir / ".launch-build.lock"
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LaunchError(f"Another build is using {build_dir}; retry after it finishes") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def check_cache_source(build_dir, engine_dir):
    cache = build_dir / "CMakeCache.txt"
    if not cache.exists():
        return
    for line in cache.read_text().splitlines():
        if line.startswith("CMAKE_HOME_DIRECTORY:INTERNAL="):
            cached_source = Path(line.split("=", 1)[1]).resolve()
            if cached_source != engine_dir:
                raise LaunchError(
                    f"Build cache belongs to {cached_source}, but this engine is {engine_dir}. "
                    "Choose a new --build-dir; the existing cache was not changed."
                )
            return
    raise LaunchError(f"Cannot identify the source directory in {cache}; choose a new --build-dir")


def run_checked(command, stage, *, cwd):
    print(f"[{stage}] {shlex.join(str(arg) for arg in command)}", flush=True)
    completed = subprocess.run([str(arg) for arg in command], cwd=cwd, shell=False)
    if completed.returncode:
        raise LaunchError(f"{stage} failed with exit code {completed.returncode}; server was not started")


def main(argv=None):
    engine_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=engine_dir.parent / "config.yaml")
    parser.add_argument("--modules-dir", type=Path, help="default: modules/ beside config")
    parser.add_argument("--build-dir", type=Path, help="default: .longshort/build/ beside config")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--jobs", type=positive_int, default=min(os.cpu_count() or 1, 8))
    parser.add_argument("--build-type", choices=("Debug", "Release", "RelWithDebInfo", "MinSizeRel"), default="Release")
    parser.add_argument("--cmake", default="cmake", help="CMake executable")
    parser.add_argument("--cmake-define", action="append", default=[], type=cmake_definition, metavar="NAME=VALUE")
    parser.add_argument("--port", type=positive_int, help="server port; otherwise LONGSHORT_PORT/default")
    args = parser.parse_args(argv)
    if args.port is not None and args.port > 65535:
        parser.error("--port must be between 1 and 65535")

    config = args.config.expanduser().resolve()
    if not config.is_file():
        raise LaunchError(f"Config file does not exist: {config}")
    modules = (args.modules_dir or config.parent / "modules").expanduser().resolve()
    if modules.exists() and not modules.is_dir():
        raise LaunchError(f"Module path is not a directory: {modules}")
    if any(character in str(modules) for character in ";\n\r"):
        raise LaunchError("Module directory cannot contain semicolons or newlines")
    modules.mkdir(parents=True, exist_ok=True)
    build = (args.build_dir or config.parent / ".longshort/build").expanduser().resolve()
    if build == modules or modules in build.parents:
        raise LaunchError("Build directory must not be inside the module directory")
    if build == engine_dir or build in engine_dir.parents:
        raise LaunchError("Build directory must be separate from the engine source directory")
    if build.exists() and not build.is_dir():
        raise LaunchError(f"Build path is not a directory: {build}")
    build.mkdir(parents=True, exist_ok=True)

    with build_lock(build):
        check_cache_source(build, engine_dir)
        configure = [
            args.cmake, "-S", str(engine_dir), "-B", str(build),
            "-DLONGSHORT_BUILD_TESTS:BOOL=OFF",
            *["-D" + value for value in args.cmake_define],
            "-DCMAKE_BUILD_TYPE:STRING=" + args.build_type,
            "-DPython3_EXECUTABLE:FILEPATH=" + sys.executable,
            "-DLONGSHORT_MODULE_DIR:PATH=" + str(modules),
        ]
        run_checked(configure, "configure", cwd=config.parent)
        run_checked([
            args.cmake, "--build", str(build), "--config", args.build_type,
            "--target", "longshort-server", "--parallel", str(args.jobs),
        ], "build", cwd=config.parent)
        binary = build / "longshort-server"
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise LaunchError(f"Build completed without an executable server at {binary}")

    if args.build_only:
        print(f"Engine ready: {binary}", flush=True)
        return 0
    environment = os.environ.copy()
    if args.port is not None:
        environment["LONGSHORT_PORT"] = str(args.port)
    print(f"[start] {shlex.join([str(binary), str(config)])}", flush=True)
    result = subprocess.run([str(binary), str(config)], cwd=config.parent, env=environment, shell=False)
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, LaunchError) as error:
        print(f"Engine startup failed: {error}", file=sys.stderr)
        sys.exit(1)
