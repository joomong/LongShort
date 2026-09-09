"""Reproduce the review using original engine sources and small local fixtures.

This is an observation harness for known defects, not a passing regression suite.
It never starts the server or connects to the team's database.
"""
import argparse
import json
import resource
import subprocess
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def run(command, timeout=180):
    p = subprocess.run([str(x) for x in command], capture_output=True, text=True, timeout=timeout)
    return {'exit_code': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--engine-binary', type=Path, help='Use an already compiled review probe')
    parser.add_argument('--ga-binary', type=Path, help='Use an already compiled UBSan GA probe')
    args = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    here = Path(__file__).resolve().parent
    repo = here.parents[1]
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    engine = args.engine_binary or out / 'engine_probe'
    ga = args.ga_binary or out / 'ga_probe'
    inc = ['-I'+str(repo/'cpp-engine/include'), '-I'+str(repo/'cpp-engine/src')]
    builds = {}
    if not args.engine_binary:
        if not pa.__version__.startswith('18.'):
            raise RuntimeError('The review compatibility header targets PyArrow 18.x; use the matching environment.')
        lib = Path(pa.get_library_dirs()[0])
        sources = [here/'probe_engine.cpp'] + [repo/'cpp-engine/src'/p for p in (
            'data/data_panel.cpp', 'data/parquet_reader.cpp', 'engine/simulator.cpp', 'compiler/expr_parser.cpp')]
        command = ['g++', '-std=c++17', '-O0', *inc, '-I'+pa.get_include(),
                   '-include', repo/'cpp-engine/include/ModulePrelude.h',
                   '-include', here/'arrow18_compat.h', *sources,
                   '-Wl,-rpath,'+str(lib), lib/'libarrow.so.1801', lib/'libparquet.so.1801',
                   '-lboost_serialization', '-o', engine]
        builds['engine'] = run(command)
    if not args.ga_binary:
        sources = [here/'probe_ga.cpp'] + [repo/'cpp-engine/src/ga'/p for p in (
            'ga_engine.cpp', 'search_space.cpp', 'parallel_evaluator.cpp')]
        command = ['g++', '-std=c++17', '-O1', '-g', '-ffunction-sections', '-fdata-sections',
                   '-fsanitize=undefined', '-fno-sanitize-recover=all', *inc, *sources,
                   '-Wl,--gc-sections', '-pthread', '-o', ga]
        builds['ga'] = run(command)
    (out/'build_results.json').write_text(json.dumps(builds, indent=2)+'\n')
    if any(v['exit_code'] for v in builds.values()):
        raise RuntimeError(f'Probe build failed: see {out / "build_results.json"}')

    base = {'time': pa.array([1704067200000]*2, type=pa.int64()), 'symbol': ['A', 'A'],
            'open': [100., 100.], 'high': [100., 100.], 'low': [100., 100.],
            'close': [100., 50.], 'volume': [1., 1.]}
    fixtures = {'duplicate': base}
    fixtures['duplicate_reversed'] = {
        k: list(reversed(v)) if isinstance(v, list) else pa.array(v.to_pylist()[::-1], type=v.type)
        for k, v in base.items()}
    fixtures['int32'] = {**base, 'close': pa.array([100, 50], type=pa.int32())}
    fixtures['timestamp_ns'] = {**base, 'time': pa.array([1704067200000000000]*2, type=pa.timestamp('ns'))}
    results = {'engine': run([engine], timeout=30)}
    for name, data in fixtures.items():
        path = out / f'{name}.parquet'
        pq.write_table(pa.table(data), path)
        results[name] = run([engine, path], timeout=30)
    ga_results = {n: run([ga, n], timeout=30) for n in ['2', '0']}
    (out/'engine_probe_results.json').write_text(json.dumps(results, indent=2)+'\n')
    (out/'ga_probe_results.json').write_text(json.dumps(ga_results, indent=2)+'\n')
    print(json.dumps({'engine': results, 'ga': ga_results}, indent=2))


if __name__ == '__main__':
    main()
