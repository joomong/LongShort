"""Root launcher's build contracts, without starting engine or frontend services."""
import fcntl
import json
import os
from pathlib import Path
import shutil
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
        (self.deps/'lib/cmake/Arrow').mkdir(parents=True)
        (self.deps/'lib/cmake/Arrow/ArrowConfig.cmake').touch()
        (self.deps/'lib/cmake/Parquet').mkdir(parents=True)
        (self.deps/'lib/cmake/Parquet/ParquetConfig.cmake').touch()
        (self.deps/'include/uuid').mkdir(parents=True)
        (self.deps/'include/uuid/uuid.h').touch()
        self.cmake = self.root/'fake cmake'
        self.cmake.write_text(FAKE_CMAKE)
        self.cmake.chmod(0o755)
        self.log = self.root/'calls.jsonl'
        self.env = dict(os.environ, LONGSHORT_WORKSPACE=str(self.workspace), LONGSHORT_DEPENDENCIES=str(self.deps),
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
        self.assertEqual(json.loads(pool_line.split(':', 1)[1]), str(LAUNCHER.parent.parent/'alpha_pool'))
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

    def test_shared_dependencies_and_custom_directories(self):
        custom = self.root/'custom build'
        self.assertEqual(self.launch('--build-dir', str(custom), '--modules-dir', str(self.root/'custom modules')).returncode, 0)
        result = self.launch('--build-dir', str(custom), LONGSHORT_DEPENDENCIES=str(self.root/'missing deps'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Shared dependencies missing', result.stderr)


class InitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='longshort init ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.platform = self.root/'platform'
        self.platform.mkdir()
        shutil.copy2(LAUNCHER, self.platform/'longshort')
        (self.platform/'frontend/node_modules').mkdir(parents=True)
        (self.platform/'frontend/scripts').mkdir()
        (self.platform/'frontend/scripts/prepare-monaco.mjs').touch()
        self.tools = self.root/'tools'
        self.tools.mkdir()
        for name in ['dirname', 'python3', 'curl', 'ss', 'git', 'apt-get', 'dpkg',
                     'flock', 'head', 'awk', 'mkdir', 'find', 'chmod', 'cat', 'realpath', 'touch']:
            (self.tools/name).symlink_to(shutil.which(name))
        self.tool('cmake', 'echo "cmake version 3.24.0"')
        self.tool('node', 'echo v20.20.2')
        self.tool('c++', 'if [ "$1" = --version ]; then echo "C++ test compiler"; else cat >/dev/null; fi')
        self.npm_log = self.root/'npm.log'
        self.tool('npm', 'echo "$*" >> "$NPM_LOG"')
        self.deps = self.root/'dependencies'
        installer = self.platform/'cpp-engine/tools/install_dependencies.sh'
        installer.parent.mkdir(parents=True)
        installer.write_text('#!/bin/sh\nmkdir -p "$1"\ntouch "$1/.installed"\n')
        installer.chmod(0o755)

    def tool(self, name, body):
        file = self.tools/name
        file.unlink(missing_ok=True)
        file.write_text('#!/bin/sh\n'+body+'\n')
        file.chmod(0o755)

    def init(self):
        return subprocess.run(['/bin/bash', str(self.platform/'longshort'), 'init'],
                              capture_output=True, text=True, env=dict(os.environ,
                              PATH=str(self.tools), LONGSHORT_HOST='127.0.0.1',
                              LONGSHORT_DEPENDENCIES=str(self.deps), NPM_LOG=str(self.npm_log)))

    def rejected(self, expected):
        result = self.init()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(expected, result.stdout+result.stderr)
        self.assertFalse(self.deps.exists())
        self.assertFalse(self.npm_log.exists())

    def test_supported_tools_install_locked_packages_even_with_node_modules(self):
        result = self.init()
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(self.npm_log.read_text().strip(), 'ci --no-audit --no-fund')
        self.assertTrue((self.deps/'.installed').exists())

    def test_missing_tool_reports_shared_tool_before_installing(self):
        (self.tools/'git').unlink()
        self.rejected('Missing shared tools: git. Contact the server administrator.')

    def test_old_cmake_is_rejected(self):
        self.tool('cmake', 'echo "cmake version 3.23.9"')
        self.rejected('CMake >= 3.24 required')

    def test_incompatible_node_versions_are_rejected(self):
        for version in ['20.18.9', '21.7.3', '22.11.0', '23.11.0']:
            self.tool('node', 'echo v'+version)
            self.rejected('Node.js 20.19+, 22.12+, or 24+ required')

    def test_compatible_node_versions_are_accepted(self):
        for version in ['20.19.0', '22.12.0', '24.0.0', '24.22.2']:
            self.tool('node', 'echo v'+version)
            result = self.init()
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_old_python_is_rejected(self):
        self.tool('python3', 'shift\nexec "'+sys.executable+'" -c '
                  "'import sys; sys.version_info = (3, 7, 0); exec(sys.stdin.read())' \"$@\"")
        self.rejected('Python >= 3.8 required')

    def test_compiler_without_cpp17_is_rejected(self):
        self.tool('c++', 'exit 1')
        self.rejected('A C++17 compiler is required')


class FrontendWorkspaceTests(unittest.TestCase):
    def test_two_users_keep_generated_files_private(self):
        with tempfile.TemporaryDirectory(prefix='longshort frontend ') as temporary:
            root = Path(temporary)
            platform = root/'shared platform'
            source = platform/'frontend'
            for entry in ['src', 'node_modules', 'public/monaco/vs']:
                (source/entry).mkdir(parents=True)
            (source/'package.json').write_text('{}')
            (source/'next.config.mjs').write_text('export default {}')
            (source/'tailwind.config.ts').write_text('export default {}')
            (source/'src/page.tsx').write_text('shared source')
            (source/'tsconfig.json').write_text(json.dumps({
                'compilerOptions': {'tsBuildInfoFile': './node_modules/.cache/typescript/cache'}
            }))
            script = LAUNCHER.read_text().split('\ncase "${1:-}" in', 1)[0]
            script = script.replace('PLATFORM="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"', 'PLATFORM="$1"')
            script += '\nPLATFORM="$1"\nSTATE="$2"\nprepare_frontend\n'
            targets = []
            for user in ['user-a', 'user-b']:
                state = root/user/'.longshort'
                result = subprocess.run(['/bin/bash', '-c', script, 'prepare', str(platform), str(state)],
                                        capture_output=True, text=True,
                                        env=dict(os.environ, LONGSHORT_HOST='127.0.0.1'))
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                target = state/'frontend'
                targets.append(target)
                self.assertFalse((target/'src').is_symlink())
                self.assertEqual((target/'src/page.tsx').read_text(), 'shared source')
                self.assertEqual((target/'node_modules').resolve(), source/'node_modules')
                config = json.loads((target/'tsconfig.json').read_text())
                self.assertEqual(config['compilerOptions']['tsBuildInfoFile'], './tsconfig.tsbuildinfo')
            (targets[0]/'next-env.d.ts').write_text('user a only')
            (targets[0]/'tsconfig.json').write_text('{}')
            self.assertFalse((targets[1]/'next-env.d.ts').exists())
            self.assertFalse((source/'next-env.d.ts').exists())
            self.assertNotEqual((targets[0]/'tsconfig.json').read_text(), (targets[1]/'tsconfig.json').read_text())


if __name__ == '__main__':
    unittest.main()
