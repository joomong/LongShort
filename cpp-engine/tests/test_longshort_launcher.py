"""Root launcher's build contracts, without starting engine or frontend services."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

LAUNCHER = Path(__file__).resolve().parents[2] / "longshort"
FAKE_CMAKE = r'''#!PYTHON
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['FAKE_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\n')
if '--build' in args:
    if os.environ.get('FAKE_FAIL') == 'build': sys.exit(9)
    if not os.environ.get('FAKE_NO_BINARY'):
        binary = Path(args[args.index('--build')+1])/'longshort-server'
        binary.write_text('#!/bin/sh\nexit 99\n')
        binary.chmod(0o755)
else:
    if os.environ.get('FAKE_FAIL') == 'configure': sys.exit(8)
    build = Path(args[args.index('-B')+1])
    (build/'CMakeCache.txt').write_text('CMAKE_HOME_DIRECTORY:INTERNAL='+args[args.index('-S')+1]+'\n')
'''.replace('PYTHON', sys.executable)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="longshort launch ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root/'personal workspace'
        self.workspace.mkdir()
        (self.workspace/'config.yaml').write_text('base: {cache: cache}\nbacktest: {}\n')
        self.build = self.workspace/'.longshort/build'
        self.deps = self.root/'deps'
        (self.deps/'venv/bin').mkdir(parents=True)
        python = self.deps/'venv/bin/python'
        python.write_text('#!/bin/sh\nexit 0\n')
        python.chmod(0o755)
        (self.deps/'prefix/include/uuid').mkdir(parents=True)
        (self.deps/'prefix/include/uuid/uuid.h').touch()
        (self.deps/'prefix/lib').mkdir()
        self.cmake = self.root/'fake cmake'
        self.cmake.write_text(FAKE_CMAKE)
        self.cmake.chmod(0o755)
        self.log = self.root/'calls.jsonl'
        self.env = dict(os.environ, LONGSHORT_WORKSPACE=str(self.workspace), LONGSHORT_DEPS=str(self.deps),
                        LONGSHORT_CMAKE=str(self.cmake), FAKE_LOG=str(self.log))

    def launch(self, *args, **env):
        return subprocess.run(['bash', str(LAUNCHER), 'start', '--build-only', *args],
                              capture_output=True, text=True, env=dict(self.env, **env))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_build_only_and_argument_boundaries(self):
        result = self.launch('--jobs', '2', '--build-type', 'Debug', '--cmake-define', 'EXAMPLE:STRING=a b;literal')
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        configure, build = self.calls()
        self.assertIn('-DLONGSHORT_MODULE_DIR:PATH='+str(self.workspace/'modules'), configure)
        self.assertIn('-DEXAMPLE:STRING=a b;literal', configure)
        self.assertIn('-DCMAKE_BUILD_TYPE:STRING=Debug', configure)
        self.assertEqual(build[-2:], ['--parallel', '2'])
        self.assertFalse((self.workspace/'.longshort/engine.pid').exists())
        self.assertFalse((self.workspace/'.longshort/ui.pid').exists())
        self.assertEqual(self.launch().returncode, 0)
        self.assertEqual(len(self.calls()), 4)

    def test_new_workspace_uses_the_platform_alpha_pool(self):
        config = self.workspace/'config.yaml'
        config.unlink()
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        pool_line = next(line for line in config.read_text().splitlines() if line.startswith('alpha_pool:'))
        self.assertEqual(json.loads(pool_line.split(':', 1)[1]), str(LAUNCHER.parent/'alpha_pool'))
        original = config.read_text()
        self.assertEqual(self.launch().returncode, 0)
        self.assertEqual(config.read_text(), original)

    def test_failures_do_not_start_old_binary(self):
        for stage in ['configure', 'build']:
            self.assertEqual(self.launch().returncode, 0)
            result = self.launch(FAKE_FAIL=stage)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('server was not started', result.stderr)

    def test_success_without_binary_is_error(self):
        result = self.launch(FAKE_NO_BINARY='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('without an executable', result.stderr)

    def test_lock_and_source_cache_validation(self):
        self.build.mkdir(parents=True)
        with (self.build/'.launch-build.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn('Another build', self.launch().stderr)
        cache = self.build/'CMakeCache.txt'
        original = 'CMAKE_HOME_DIRECTORY:INTERNAL=/another/checkout\n'
        cache.write_text(original)
        self.assertIn('another source directory', self.launch().stderr)
        self.assertEqual(cache.read_text(), original)
        self.assertEqual(self.calls(), [])

    def test_module_path_and_managed_options(self):
        for args in [('--build-dir', str(self.workspace/'modules/build')),
                     ('--cmake-define', 'LONGSHORT_MODULE_DIR=/other'),
                     ('--cmake-define', 'CMAKE_BUILD_TYPE:STRING=Debug'),
                     ('--jobs', '0'), ('--jobs', '2;echo bad'), ('--build-type', 'invalid')]:
            self.assertNotEqual(self.launch(*args).returncode, 0)
        self.assertEqual(self.calls(), [])
        (self.workspace/'modules').rmdir()
        (self.workspace/'modules').write_text('not a directory')
        self.assertIn('not a directory', self.launch().stderr)

    def test_cached_dependencies_and_custom_directories(self):
        custom = self.root/'custom build'
        self.assertEqual(self.launch('--build-dir', str(custom), '--modules-dir', str(self.root/'custom modules')).returncode, 0)
        result = self.launch('--build-dir', str(custom), LONGSHORT_DEPS=str(self.root/'missing deps'))
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertFalse(any('CMAKE_PREFIX_PATH' in arg for arg in self.calls()[-2]))


if __name__ == '__main__':
    unittest.main()
