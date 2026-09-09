"""Exercise the real CMake discovery/build graph independently of the parser."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


DISCOVERY = Path(__file__).resolve().parents[1] / "cmake/DiscoverModules.cmake"
FAKE_GENERATOR = '''import argparse
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--modules-dir',type=Path)
p.add_argument('--output',type=Path)
a=p.parse_args()
value=sum(int(f.read_text()) for f in a.modules_dir.rglob('*.h') if all(not p.startswith('.') for p in f.relative_to(a.modules_dir).parts))
text='int discovered_value() { return '+str(value)+'; }\\n'
a.output.parent.mkdir(parents=True,exist_ok=True)
if not a.output.exists() or a.output.read_text()!=text: a.output.write_text(text)
'''


@unittest.skipUnless(shutil.which("cmake") and shutil.which("g++"), "requires CMake and a C++ compiler")
class ModuleBuildTests(unittest.TestCase):
    def test_build_detects_add_edit_remove_and_leaves_unchanged_outputs_alone(self):
        generators = ["Unix Makefiles"]
        if shutil.which("ninja"):
            generators.append("Ninja")
        for generator in generators:
            with self.subTest(generator=generator), tempfile.TemporaryDirectory(prefix="longshort module build ") as tmp:
                root = Path(tmp)
                modules = root / "user modules"
                modules.mkdir()
                (root / "generate.py").write_text(FAKE_GENERATOR)
                (root / "main.cpp").write_text('#include <iostream>\nint discovered_value();\nint main(){std::cout<<discovered_value();}\n')
                (root / "CMakeLists.txt").write_text(
                    'cmake_minimum_required(VERSION 3.20)\nproject(discovery_test LANGUAGES CXX)\n'
                    'add_executable(smoke main.cpp)\n'
                    f'include("{DISCOVERY.as_posix()}")\n'
                    'longshort_discover_modules(smoke "${CMAKE_CURRENT_SOURCE_DIR}/user modules" "${CMAKE_CURRENT_SOURCE_DIR}/generate.py")\n'
                )
                build = root / "build"

                def command(*args, success=True):
                    result = subprocess.run(args, capture_output=True, text=True)
                    if success:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    else:
                        self.assertNotEqual(result.returncode, 0)
                    return result

                def build_and_read(expected):
                    command("cmake", "--build", str(build), "--parallel", "2")
                    result = command(str(build / "smoke"))
                    self.assertEqual(result.stdout, str(expected))

                rejected = command("cmake", "-S", str(root), "-B", str(modules / "bad-build"), "-G", generator, success=False)
                self.assertIn("must not be inside the module directory", rejected.stdout + rejected.stderr)
                # Failed configure may leave compiler probes; remove only this test-owned directory.
                shutil.rmtree(modules / "bad-build")
                command("cmake", "-S", str(root), "-B", str(build), "-G", generator)
                build_and_read(0)
                registry = build / "generated/smoke_discovered_modules.cpp"
                binary = build / "smoke"
                previous = registry.stat().st_mtime_ns, binary.stat().st_mtime_ns
                build_and_read(0)
                self.assertEqual(previous, (registry.stat().st_mtime_ns, binary.stat().st_mtime_ns))
                hidden = modules / ".archive"
                hidden.mkdir()
                (hidden / "Unused.cpp").write_text("invalid C++ should remain disabled")
                (hidden / "Unused.h").write_text("invalid disabled generator input")
                (modules / ".Disabled.cpp").write_text("invalid hidden C++ should remain disabled")
                build_and_read(0)
                first = modules / "First.h"
                first.write_text("1")
                build_and_read(1)
                second = modules / "Second.h"
                second.write_text("2")
                build_and_read(3)
                first.write_text("4")
                build_and_read(6)
                second.unlink()
                build_and_read(4)
                registry.unlink()
                build_and_read(4)
                extra = modules / "Helper.cpp"
                extra.write_text("invalid C++ here")
                command("cmake", "--build", str(build), success=False)
                extra.write_text("int helper_value = 3;\n")
                build_and_read(4)
                extra.unlink()
                build_and_read(4)
                first.write_text("invalid generator input")
                command("cmake", "--build", str(build), success=False)
                first.unlink()
                build_and_read(0)


if __name__ == "__main__":
    unittest.main()
