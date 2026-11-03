"""Exercise CMake's C++ registration list with real incremental builds."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

DISCOVERY = Path(__file__).resolve().parents[1] / "cmake/DiscoverModules.cmake"


@unittest.skipUnless(shutil.which("cmake") and shutil.which("g++"), "requires CMake and a C++ compiler")
class ModuleBuildTests(unittest.TestCase):
    def test_incremental_cpp_registration(self):
        for generator in ["Unix Makefiles"] + (["Ninja"] if shutil.which("ninja") else []):
            with self.subTest(generator=generator), tempfile.TemporaryDirectory(prefix="longshort module build ") as tmp:
                root = Path(tmp)
                modules = root / "user modules"
                modules.mkdir()
                (root / "module_registration.h").write_text('#pragma once\nstruct ModuleRegistry { int value = 0; };\n')
                (root / "main.cpp").write_text(
                    '#include "module_registration.h"\n#include <iostream>\n'
                    'void register_discovered_modules(ModuleRegistry&);\n'
                    'int main(){ModuleRegistry r; register_discovered_modules(r); std::cout<<r.value;}\n')
                (root / "CMakeLists.txt").write_text(
                    'cmake_minimum_required(VERSION 3.20)\nproject(discovery_test LANGUAGES CXX)\n'
                    'add_executable(smoke main.cpp)\ntarget_include_directories(smoke PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}")\n'
                    f'include("{DISCOVERY.as_posix()}")\n'
                    'longshort_discover_modules(smoke "${CMAKE_CURRENT_SOURCE_DIR}/user modules")\n')
                build = root / "build"

                def command(*args, success=True):
                    result = subprocess.run(args, capture_output=True, text=True)
                    self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                    return result

                def build_and_read(expected):
                    command("cmake", "--build", str(build), "--parallel", "2")
                    self.assertEqual(command(str(build / "smoke")).stdout, str(expected))

                def header(name, value):
                    (modules / f"{name}.h").write_text(
                        f'inline void register_{name}(ModuleRegistry& r) {{r.value += {value};}}\n')

                rejected = command("cmake", "-S", str(root), "-B", str(modules / "bad-build"), "-G", generator, success=False)
                self.assertIn("must not be inside the module directory", rejected.stdout + rejected.stderr)
                shutil.rmtree(modules / "bad-build")
                command("cmake", "-S", str(root), "-B", str(build), "-G", generator)
                build_and_read(0)
                registry = build / "generated/smoke_discovered_modules.cpp"
                binary = build / "smoke"
                previous = registry.stat().st_mtime_ns, binary.stat().st_mtime_ns
                build_and_read(0)
                self.assertEqual(previous, (registry.stat().st_mtime_ns, binary.stat().st_mtime_ns))
                (modules / ".archive").mkdir()
                (modules / ".archive/Unused.h").write_text("invalid disabled C++")
                (modules / ".Disabled.cpp").write_text("invalid hidden C++")
                build_and_read(0)
                header("First", 1)
                build_and_read(1)
                header("Second", 2)
                build_and_read(3)
                header("First", 4)
                build_and_read(6)
                (modules / "Second.h").unlink()
                build_and_read(4)
                extra = modules / "Helper.cpp"
                extra.write_text("invalid C++")
                command("cmake", "--build", str(build), success=False)
                extra.write_text("int helper_value = 3;\n")
                build_and_read(4)
                extra.unlink()
                (modules / "First.h").write_text("// missing registration function\n")
                command("cmake", "--build", str(build), success=False)
                (modules / "First.h").unlink()
                build_and_read(0)
                header("Same", 1)
                nested = modules / "nested"
                nested.mkdir()
                (nested / "Same.hpp").write_text("// duplicate basename\n")
                command("cmake", "--build", str(build), success=False)
                shutil.rmtree(nested)
                build_and_read(1)


if __name__ == "__main__":
    unittest.main()
