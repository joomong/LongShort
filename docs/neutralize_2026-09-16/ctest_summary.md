# Full CTest with the HTTP fixture — 2026-09-16

```bash
cmake -S cpp-engine -B /tmp/longshort_build_final \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=/tmp/longshort_module_deps/prefix \
  -DLONGSHORT_PYARROW_PYTHON=/home/jgo/usr/venv-3.11/bin/python \
  -DLONGSHORT_BUILD_TESTS=ON -DLONGSHORT_MODULE_DIR=/tmp/longshort_logic_data_modules \
  -DUUID_INCLUDE_DIRS=/home/jgo/61/jgo/miniconda3/include/uuid \
  -DUUID_LIBRARIES=/usr/lib/x86_64-linux-gnu/libuuid.so.1
cmake --build /tmp/longshort_build_final --parallel 8
/home/jgo/usr/venv-3.11/bin/python cpp-engine/tools/make_http_test_fixture.py \
  --output-dir <scratch>/http_fixture
LONGSHORT_TEST_SERVER=/tmp/longshort_build_final/longshort-server \
LONGSHORT_TEST_CONFIG=<scratch>/http_fixture/config.yaml \
ctest --test-dir /tmp/longshort_build_final --output-on-failure -j 4
```

Result: **100% tests passed, 0 failed out of 26** (6.03 s). Raw log: `ctest.log`.
With the fixture supplied, `test_http_contract` runs for real — no case is skipped anywhere.

| # | test executable | cases run | passed | failed | skipped | note |
|---|---|---|---|---|---|---|
| 1 | test_data_loader | 2 | 2 | 0 | 0 | |
| 2 | test_config_loader | 11 | 11 | 0 | 0 | includes WP-B's 3 `base:` cases |
| 3 | test_parquet_loader | 33 | 33 | 0 | 0 | 28 legacy + WP-B 3 + WP-D 2 group-panel guard cases |
| 4 | test_modules | 23 | 23 | 0 | 0 | includes WP-C's 9 `MNeutralize` cases |
| 5 | test_simulator | 50 | 50 | 0 | 0 | |
| 6 | test_summary_metrics | 8 | 8 | 0 | 0 | |
| 7 | test_module_oracles | 8 | 8 | 0 | 0 | |
| 8 | test_metrics_oracle | 7 | 7 | 0 | 0 | |
| 9 | test_pipeline_contract | 8 | 8 | 0 | 0 | includes WP-C's neutralize pipeline case |
| 10 | test_http_contract | 21 | 21 | 0 | 0 | 18 legacy + WP-D 3 neutralize/`base:` cases; needs the fixture env vars |
| 11 | test_extreme_alpha_stress | 2 | 2 | 0 | 0 | |
| 12 | test_yearly_metrics | 6 | 6 | 0 | 0 | |
| 13 | test_checkpoint | 6 | 6 | 0 | 0 | needs `WORKING_DIRECTORY=cpp-engine` (CMake sets it) |
| 14 | test_simd_parity | 8 | 8 | 0 | 0 | |
| 15 | test_ga | 7 | 7 | 0 | 0 | |
| 16 | test_determinism | 3 | 3 | 0 | 0 | |
| 17 | test_xml_compiler | 41 | 41 | 0 | 0 | includes WP-C's 7 neutralize compile cases |
| 18 | test_golden_crossval | 9 | 9 | 0 | 0 | needs `WORKING_DIRECTORY=cpp-engine`; 1 case covers the 2 neutralize golden entries |
| 19 | test_combined_alpha | 9 | 9 | 0 | 0 | |
| 20 | test_json_serializer | 14 | 14 | 0 | 0 | |
| 21 | test_module_attributes | 7 | 7 | 0 | 0 | |
| 22 | test_generate_modules (py) | 19 | 19 | 0 | 0 | `/usr/bin/python3`, stdlib only |
| 23 | test_run_engine (py) | 11 | 11 | 0 | 0 | `/usr/bin/python3` |
| 24 | test_module_build (py) | 1 | 1 | 0 | 0 | `/usr/bin/python3` |
| 25 | test_afl_catalog (py) | 5 | 5 | 0 | 0 | `/usr/bin/python3` |
| 26 | test_build_group_panels (py) | 15 | 15 | 0 | 0 | WP-A; registered with `/home/jgo/usr/venv-3.11/bin/python` and `SKIP_RETURN_CODE 77` |

Totals: **283 C++ gtest cases in 21 executables, 51 Python unittest cases in 5 tool tests — 334 cases,
0 failed, 0 skipped.**

Notes

- `test_build_group_panels` was still being revised by the WP-A review-fix agent while this run was
  made; at the time of the run it was registered with the venv interpreter and reported 15 cases, all
  passing. The count may move if that agent adds cases.
- Running `./test_checkpoint` or `./test_golden_crossval` straight from the build directory fails or
  skips because their JSON fixtures are resolved relative to `cpp-engine/`. CTest sets the working
  directory, so only a manual invocation needs `cd cpp-engine` first. Both pass under CTest.
- `test_http_contract` still honours the documented skip convention: without
  `LONGSHORT_TEST_SERVER`/`LONGSHORT_TEST_CONFIG` (and no `longshort-server` next to the binary) all
  21 cases report `GTEST_SKIP` and CTest reports the target as passed.
