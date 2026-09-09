"""Launcher subprocess contracts: build freshness, argument safety, and failures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


LAUNCHER = Path(__file__).resolve().parents[1] / "tools/run_engine.py"
FAKE_CMAKE = r'''#!PYTHON
import json, os
from pathlib import Path
import sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as out:
    out.write(json.dumps(args) + "\n")
if "--build" in args:
    if os.environ.get("FAKE_FAIL") == "build": sys.exit(9)
    build = Path(args[args.index("--build") + 1])
    if os.environ.get("FAKE_NO_BINARY"): sys.exit(0)
    binary = build / "longshort-server"
    binary.write_text("#!PYTHON\nimport os,sys,json\nfrom pathlib import Path\nPath(os.environ['SERVER_LOG']).write_text(json.dumps({'argv':sys.argv[1:],'cwd':os.getcwd(),'port':os.environ.get('LONGSHORT_PORT')}))\n")
    binary.chmod(0o755)
else:
    if os.environ.get("FAKE_FAIL") == "configure": sys.exit(8)
    build = Path(args[args.index("-B") + 1])
    source = args[args.index("-S") + 1]
    (build / "CMakeCache.txt").write_text("CMAKE_HOME_DIRECTORY:INTERNAL=" + source + "\n")
'''.replace("PYTHON", sys.executable)


@unittest.skipUnless(os.name == "posix", "launcher uses POSIX process and locking behavior")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="longshort launch ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "personal config.yaml"
        self.config.write_text("datasets: []\n")
        self.cmake = self.root / "fake cmake"
        self.cmake.write_text(FAKE_CMAKE)
        self.cmake.chmod(0o755)
        self.build = self.root / ".longshort/build"
        self.log = self.root / "cmake.log"
        self.server_log = self.root / "server.log"
        self.env = dict(os.environ, FAKE_LOG=str(self.log), SERVER_LOG=str(self.server_log))

    def launch(self, *args, **environment):
        return subprocess.run(
            [sys.executable, str(LAUNCHER), "--config", str(self.config), "--cmake", str(self.cmake), *args],
            capture_output=True, text=True, env=dict(self.env, **environment), shell=False,
        )

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_build_then_start_uses_personal_paths_and_preserves_spaces(self):
        result = self.launch("--port", "8317", "--cmake-define", "EXAMPLE:STRING=a b", "--cmake-define", "SOURCE-YAML:BOOL=OFF")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("-DLONGSHORT_MODULE_DIR:PATH=" + str(self.root / "modules"), calls[0])
        self.assertIn("-DEXAMPLE:STRING=a b", calls[0])
        self.assertIn("-DSOURCE-YAML:BOOL=OFF", calls[0])
        self.assertEqual(calls[1][0], "--build")
        started = json.loads(self.server_log.read_text())
        self.assertEqual(started, {"argv": [str(self.config)], "cwd": str(self.root), "port": "8317"})

    def test_explicit_test_build_option_can_override_the_launcher_default(self):
        result = self.launch("--build-only", "--cmake-define", "LONGSHORT_BUILD_TESTS:BOOL=ON")
        self.assertEqual(result.returncode, 0, result.stderr)
        command = self.calls()[0]
        self.assertLess(command.index("-DLONGSHORT_BUILD_TESTS:BOOL=OFF"), command.index("-DLONGSHORT_BUILD_TESTS:BOOL=ON"))

    def test_build_only_never_starts_server(self):
        result = self.launch("--build-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.build / "longshort-server").exists())
        self.assertFalse(self.server_log.exists())

    def test_every_launch_builds_even_with_an_existing_binary(self):
        self.assertEqual(self.launch("--build-only").returncode, 0)
        self.assertEqual(self.launch("--build-only").returncode, 0)
        self.assertEqual(len(self.calls()), 4)

    def test_failure_does_not_run_previous_binary(self):
        for stage in ("configure", "build"):
            with self.subTest(stage=stage):
                self.assertEqual(self.launch("--build-only").returncode, 0)
                result = self.launch(FAKE_FAIL=stage)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("server was not started", result.stderr)
                self.assertFalse(self.server_log.exists())

    def test_success_without_executable_is_an_error(self):
        result = self.launch(FAKE_NO_BINARY="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("without an executable", result.stderr)
        self.assertFalse(self.server_log.exists())

    def test_missing_config_and_non_directory_modules_stop_before_configure(self):
        self.config.unlink()
        self.assertNotEqual(self.launch().returncode, 0)
        self.assertEqual(self.calls(), [])
        self.config.write_text("datasets: []\n")
        (self.root / "modules").write_text("not a directory")
        self.assertNotEqual(self.launch().returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_build_inside_module_tree_is_rejected_before_configure(self):
        modules = self.root / "modules"
        for build in (modules, modules / "build"):
            with self.subTest(build=build):
                result = self.launch("--build-dir", str(build))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must not be inside the module directory", result.stderr)
                self.assertEqual(self.calls(), [])

    def test_cache_from_another_checkout_is_not_reused_or_deleted(self):
        self.build.mkdir(parents=True)
        cache = self.build / "CMakeCache.txt"
        text = "CMAKE_HOME_DIRECTORY:INTERNAL=/a/different/checkout\n"
        cache.write_text(text)
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Choose a new --build-dir", result.stderr)
        self.assertEqual(cache.read_text(), text)
        self.assertEqual(self.calls(), [])

    def test_build_directory_lock_prevents_concurrent_build(self):
        import fcntl
        self.build.mkdir(parents=True)
        with (self.build / ".launch-build.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Another build", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_managed_cmake_options_and_invalid_port_are_rejected(self):
        self.assertNotEqual(self.launch("--cmake-define", "LONGSHORT_MODULE_DIR=/other").returncode, 0)
        self.assertNotEqual(self.launch("--port", "65536").returncode, 0)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
