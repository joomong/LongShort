# MAP_engine — data map of `cpp-engine/` (working tree, 2026-10-14)

Agent: map_engine. Repo read-only. Scratch: `refactor_audit/map_engine/` (text outputs only; build dirs deleted).
Paths below are relative to `/home/jgo/longshort_platform/cpp-engine/` unless absolute. `L` = line count (`wc -l`).
No judgment in this file; severity tags are not used. Everything is a count, a path:line, or a command output.

Scratch evidence files (kept): `warn_build.log` (full compiler log), `warnings_normalized.txt`, `coverage_full.tsv/json`,
`coverage_pertest.json`, `coverage_touch.json`, `ctest.log`, `ctest_LastTest_full.log`, `symbol_refs.tsv`, `compat_grep.txt`,
`gcov_agg.py`, `warn_launcher.sh`, `cov_launcher.sh`.

---

## 1. Inventory

### 1.1 Files and line counts

`include/` (4 files, 210 L)

| file | L |
|---|---|
| include/module.h | 162 |
| include/ModulePrelude.h | 19 |
| include/signal_vector.h | 21 |
| include/star_vector.h | 8 |

`src/` (56 files, 8041 L; grand total src+include 8251 L)

| file | L | | file | L |
|---|---|---|---|---|
| src/main.cpp | 1300 | | src/metrics/metrics_engine.cpp | 50 |
| src/data/parquet_reader.cpp | 923 | | src/metrics/metrics_engine.h | 36 |
| src/engine/simulator.cpp | 577 | | src/metrics/summary_metrics.cpp | 243 |
| src/engine/simulator.h | 99 | | src/metrics/summary_metrics.h | 49 |
| src/api/json_serializer.cpp | 458 | | src/metrics/timeseries_output.cpp | 71 |
| src/api/json_serializer.h | 43 | | src/metrics/timeseries_output.h | 27 |
| src/api/alpha_storage.cpp | 304 | | src/metrics/yearly_metrics.cpp | 136 |
| src/api/alpha_storage.h | 93 | | src/metrics/yearly_metrics.h | 29 |
| src/api/combined_signals.h | 117 | | src/modules/Chain.h | 30 |
| src/compiler/dag_builder.cpp | 453 | | src/modules/Constant.h | 16 |
| src/compiler/dag_builder.h | 50 | | src/modules/Data.h | 23 |
| src/compiler/expansion.cpp | 276 | | src/modules/Decay.h | 100 |
| src/compiler/expansion.h | 21 | | src/modules/Delay.h | 26 |
| src/compiler/expr_parser.cpp | 219 | | src/modules/Event.h | 44 |
| src/compiler/expr_parser.h | 20 | | src/modules/Neutralize.h | 80 |
| src/compiler/module_attributes.h | 79 | | src/modules/Ops.h | 119 |
| src/compiler/range_parser.h | 52 | | src/modules/Rank.h | 68 |
| src/compiler/xml_parser.cpp | 37 | | src/modules/SignalCache.h | 39 |
| src/compiler/xml_parser.h | 16 | | src/modules/TsMean.h | 55 |
| src/core/signal_vector.cpp | 3 | | src/modules/TsRank.h | 65 |
| src/data/config_loader.cpp | 288 | | src/modules/TsZscore.h | 81 |
| src/data/config_loader.h | 66 | | src/modules/Zscore.h | 61 |
| src/data/data_panel.cpp | 72 | | src/simd/m_decay_avx2.cpp | 119 |
| src/data/data_panel.h | 74 | | src/simd/m_decay_avx2.h | 34 |
| src/data/parquet_reader.h | 15 | | src/simd/simd_utils.cpp | 29 |
| src/checkpoint/checkpoint_manager.cpp | 37 | | src/simd/simd_utils.h | 47 |
| src/checkpoint/checkpoint_manager.h | 31 | | src/ga/ga_engine.cpp | 153 |
| src/ga/ga_engine.h | 82 | | src/ga/parallel_evaluator.cpp | 124 |
| src/ga/parallel_evaluator.h | 44 | | src/ga/search_space.cpp | 213 |
| src/ga/search_space.h | 25 | | | |

`tests/` (C++ 11,131 L in 25 files; Python 829 L in 5 files; golden/fixtures)

| file | L | | file | L |
|---|---|---|---|---|
| tests/test_http_contract.cpp | 1699 | | tests/test_summary_metrics.cpp | 163 |
| tests/test_simulator.cpp | 901 | | tests/test_extreme_alpha_stress.cpp | 148 |
| tests/test_xml_compiler.cpp | 659 | | tests/test_simd_parity.cpp | 141 |
| tests/test_execution_price.cpp | 597 | | tests/test_determinism.cpp | 100 |
| tests/test_modules.cpp | 506 | | tests/test_module_attributes.cpp | 78 |
| tests/test_metrics_oracle.cpp | 469 | | tests/test_data_loader.cpp | 31 |
| tests/test_parquet_loader.cpp | 464 | | tests/test_build_group_panels.py | 301 |
| tests/test_module_oracles.cpp | 457 | | tests/test_generate_modules.py | 209 |
| tests/test_json_serializer.cpp | 448 | | tests/test_run_engine.py | 149 |
| tests/test_golden_crossval.cpp | 400 | | tests/test_module_build.py | 100 |
| tests/test_combined_alpha.cpp | 397 | | tests/test_afl_catalog.py | 70 |
| tests/test_thin_loader.cpp | 378 | | tests/golden/generate_golden.py | 521 |
| tests/test_pipeline_contract.cpp | 330 | | tests/golden/metrics.json | 877 |
| tests/test_checkpoint.cpp | 321 | | tests/golden/modules.json | 665 |
| tests/test_alpha_storage.cpp | 297 | | tests/golden/missing_close_bitwise.json | 13 |
| tests/test_config_loader.cpp | 283 | | tests/fixtures/module_input_legacy_v0.bin | binary |
| tests/test_ga.cpp | 267 | | tests/__pycache__/ (3 .pyc), tests/golden/__pycache__/ (1 .pyc) | — |
| tests/test_thin_moves.cpp | 252 | | tests/test_yearly_metrics.cpp | 219 |

`tools/` (3,427 L source + symlink dir + stale bytecode)

| file | L |
|---|---|
| tools/restart_engine.py | 659 |
| tools/missing_close_oracle.py | 527 |
| tools/generate_modules.py | 455 |
| tools/check_group_neutralize_oracle.py | 355 |
| tools/check_missing_data.py | 335 |
| tools/run_logic_tests.py | 221 |
| tools/check_team_sim_parity.py | 163 |
| tools/bench_2000_breakdown.cpp | 159 |
| tools/run_engine.py | 146 |
| tools/build_afl_catalog.py | 145 |
| tools/check_wide_data_oracle.py | 120 |
| tools/make_http_test_fixture.py | 68 |
| tools/data_load_probe.cpp | 60 |
| tools/restart_engine.sh | 11 |
| tools/data -> ../../../universe_builder/cache (symlink out of repo; holds build_group_panels.py 1635 L, build_cache_v2.py 1500, build_distributions.py 1822, build_events.py 2274, refresh_master.py 682, README_group_panels.md 94, README_events_backfill.md 85) | 8092 |
| tools/__pycache__/ 13 .pyc incl. `run_top500_missing_close.cpython-311.pyc` (no `.py` source anywhere in tools/) | — |
| tools/uploads/ (dir, Sep 14) and cpp-engine/uploads/tmp/ (258 entries, Sep 17) | — |

Other: `/home/jgo/longshort_platform/modules/` contains only `README.md` (build log: `Discovered modules: (none)`); `cmake/{ArrowDependencies,DiscoverModules}.cmake`; `examples/datasets/{us_equity,equities_monthly_afl,compare_v2_all}.yaml` + three `.bak-20261008` copies.

### 1.2 Symbols per header and reference counts

Method: regex extraction of top-level `struct/class/enum/using/constexpr` and free-function declarations from each header
(member functions are NOT extracted except where listed separately below), then `\bNAME\b` word counts over
`src/**/*.{cpp,h}`, `include/*.h`, `tests/*.{cpp,py}`, `tools/*.{cpp,py}`, excluding the declaring header. "ext" = hits
outside the header AND outside its own `.cpp`. Generic names (`list`, `exists`, `remove`, `save`, `trim`, `EvalFunc`) are inflated by
unrelated identifiers; they are marked `*`. Full table: `map_engine/symbol_refs.tsv`.

| header | symbol (ext refs, files) |
|---|---|
| include/module.h | IntParameter(114,18) FloatParameter(37,11) ModuleInput(9,5) RowView(0) ring_capacity(0, private) Module(159,35) |
| include/signal_vector.h | SignalVector(76,23) |
| include/star_vector.h | StarVector(8,4) |
| src/api/alpha_storage.h | Metadata(6,2) SavedAlpha(0) SaveTransaction(7,2) PositionsWriter(3,2) is_valid_name(7,2) is_valid_level(1,1) prepare_directory(8,2) metadata_yaml(7,2) read_metadata(2,1 tests only) read_file(3,2) list*(62) exists*(55) remove*(29) discard(0, private) |
| src/api/combined_signals.h | OrderedSignalAccumulator(9,2) |
| src/api/json_serializer.h | kSignalHistogramBins(0 ext; 12 in own cpp) summary_to_json(7,3) yearly_to_json(5,3) timeseries_to_json(5,3) signal_plot_to_json(15,3) signal_distribution_to_json(6,2) signal_log_distribution_to_json(12,2) |
| src/checkpoint/checkpoint_manager.h | SimulatorState(9,1 tests only) CheckpointManager(8,1 tests only) save*(64) |
| src/compiler/dag_builder.h | CompileError(19,3) ModuleRegistry(5,2) CompiledAlpha(3,1) ModuleFactory(0 ext) register_discovered_modules(1,1 generated registry) compile_pipeline(0 ext; 3 in own cpp) compile_xml(59,7) |
| src/compiler/expansion.h | Op(0 ext; 9 in own cpp) Pipeline(5,3) MAX_ALPHA_COUNT(5,2) expand_pipelines(3,2) |
| src/compiler/expr_parser.h | parse_expr(4,2) |
| src/compiler/module_attributes.h | trim*(2) invalid_value(0, detail) parse_int(8,2) parse_float(6,2) validate_keys(7,2) |
| src/compiler/range_parser.h | trim_copy(2,2) parse_range_notation(2,2) |
| src/compiler/xml_parser.h | XmlElement(11,3) parse_xml(2,2) |
| src/data/config_loader.h | DatasetFormat(9,3) FieldAlignment(17,4) DatasetConfig(6,3) UniverseConfig(0 ext) BacktestConfig(59,10) config_date_ms(4,1) |
| src/data/data_panel.h | LazyColumn(2,1) DataPanel(146,35) MoveEvent(5,2); members: mask_for(main.cpp:289, test_thin_loader ×4) column_names(main:991, test_parquet_loader, data_load_probe) loaded_field_count/loaded_bytes(main:995-996, test_parquet_loader, data_load_probe) 4-arg load(path,cols,time,symbol)(tools/bench_2000_breakdown.cpp:57, test_parquet_loader.cpp:199,217 only) |
| src/data/parquet_reader.h | load_config(data_panel.cpp:60) load_thin_symbol_labels(main.cpp:875, test_thin_loader ×1) load_parquet(data_panel.cpp:64 only) |
| src/engine/simulator.h | MissingClosePolicy(53,6) ExecutionPrice(29,3) MissingDataDiagnostics(2,2) AccountingRowView(6,4) SimulatorConfig(52,10) SimulatorOutput(12,5) Simulator(87,10) SignalRowCallback(1,1) AccountingRowCallback(3,1) normalize_signal_row(8,4) |
| src/ga/ga_engine.h | GAConfig(10,3) GAResult(5,2 tests only) GACandidate(3,2) GAEvents(4,2) GAEngine(15,3) EvalFunc*(4) ProgressCallback(0 ext) Individual(0, private); members: run_detailed(main.cpp:1153) run(test_ga.cpp:1 only) run_streaming(0 callers) |
| src/ga/parallel_evaluator.h | EvaluationCancelled(8,3) EvaluationResult(3,2) ParallelEvaluator(11,4) EvalFunc*(9) Completion(0 ext) Cancelled(1,1); members: evaluate_results(ga_engine.cpp:130) evaluate_batch(tests only: test_determinism ×3, test_ga ×3) cache_hits(tests only: test_determinism ×3) |
| src/ga/search_space.h | SearchSpace(11,4) Gene(0 ext; 6 in own cpp) extract_search_space(19,3) |
| src/metrics/metrics_engine.h | FullMetricsResult(4,2) compute_full_metrics(12,6) |
| src/metrics/summary_metrics.h | SummaryMetrics(14,6) BARS_PER_YEAR(1,1 tests only) compute_summary(12,3 tests only) compute_summary_range(2,1) |
| src/metrics/timeseries_output.h | TimeseriesOutput(4,4) compute_timeseries(2,2) |
| src/metrics/yearly_metrics.h | YearlyMetrics(16,5) compute_yearly(13,2) |
| src/modules/Chain.h | MChain(2,2: expr_parser.cpp, test_xml_compiler) |
| src/modules/Constant.h | MConstant(8,4) |
| src/modules/Data.h | MData(22,8) |
| src/modules/Decay.h | MDecay(7,4) |
| src/modules/Delay.h | MDelay(15,7) |
| src/modules/Event.h | MEvent(1,1: dag_builder.cpp) |
| src/modules/Neutralize.h | MNeutralize(29,4) |
| src/modules/Ops.h | MBinaryOp(1,1) MAdd(4,2) MSub(2,2) MMul(1,1) MDiv(6,3) MLe(0) MGe(0) |
| src/modules/Rank.h | MRank(15,7) |
| src/modules/SignalCache.h | MSignalSave(4,1) MSignalLoad(1,1) |
| src/modules/TsMean.h | MTsMean(8,5) |
| src/modules/TsRank.h | MTsRank(6,4) |
| src/modules/TsZscore.h | MTsZscore(7,4) |
| src/modules/Zscore.h | MZscore(9,5) |
| src/simd/m_decay_avx2.h | mdecay_runtime_avx2_enabled(2,2) mdecay_process_8_scalar(1,1 tests only) mdecay_process_8_avx2(2,2) |
| src/simd/simd_utils.h | AlignedAllocator(1,1 tests only) value_type(0, trait) AlignedVector(4,1 tests only) has_avx2(3,2) aligned_alloc_32(4,1 tests only) aligned_free_32(4,1 tests only) |

**Symbols with 0 references outside their own file (header + own .cpp)** — verified by `grep -rnw`:

- `MLe`, `MGe` (src/modules/Ops.h:92,106): 0 hits anywhere else; not registered in `ModuleRegistry::default_registry` (dag_builder.cpp:170-262), not built by `expr_parser.cpp`.
- `GAEngine::run_streaming` + `ProgressCallback` (src/ga/ga_engine.h:54,62; ga_engine.cpp:20-24): 0 callers.
- `compile_pipeline` (dag_builder.h:45): called only at dag_builder.cpp:442.
- `parquet_reader::load_parquet` (parquet_reader.h:11): called only from data_panel.cpp:64 (the 4-arg `DataPanel::load`), whose callers are tools/bench_2000_breakdown.cpp:57 and tests/test_parquet_loader.cpp:199,217.
- `kSignalHistogramBins` (json_serializer.h:33), `ModuleFactory` (dag_builder.h:18), `Op` (expansion.h:9), `SearchSpace::Gene`, `ParallelEvaluator::Completion`, `UniverseConfig` type name, `alpha_storage::SavedAlpha` type name: used only inside the declaring header or its own .cpp.
- `ModuleInput::push` lazy-allocation branch (module.h:50-59): every built-in stateful module calls `input.init()` first (Decay.h:9, Delay.h:6, TsMean.h:7, TsZscore.h:9); no built-in pushes before init.

**Symbols referenced only from tests** (no src/tools caller):

| symbol | test sites |
|---|---|
| `CheckpointManager`, `SimulatorState` (checkpoint_manager.h) and every `Module::checkpoint()` override (module.h:157-158; Decay/Delay/Event/Ops/Chain/SignalCache/TsMean/TsRank/TsZscore) | tests/test_checkpoint.cpp only; `CheckpointManager` grep: checkpoint_manager.{h,cpp} + test_checkpoint.cpp |
| `GAEngine::run`, `GAResult` type name | test_ga.cpp:1; test_determinism.cpp:2, test_ga.cpp:3 |
| `ParallelEvaluator::evaluate_batch`, `cache_hits` | test_determinism.cpp, test_ga.cpp |
| `compute_summary` (non-range), `BARS_PER_YEAR` | test_golden_crossval, test_metrics_oracle, test_summary_metrics; test_extreme_alpha_stress |
| `mdecay_process_8_scalar`, `AlignedAllocator`, `AlignedVector`, `aligned_alloc_32`, `aligned_free_32` | test_simd_parity.cpp |
| `alpha_storage::read_metadata` | test_alpha_storage.cpp (also used internally by `list()` in alpha_storage.cpp) |
| `OrderedSignalAccumulator::add` (row-wise; combined_signals.h:29-54) | test_combined_alpha.cpp:246-247; main.cpp uses only `add_block` (main.cpp:492) |
| `MChain` | expr_parser.cpp:7 include + returns derivation (nonthin only, §7); test_xml_compiler.cpp |

### 1.3 Test files → src files exercised

"include" = repo headers the test includes; "cov" = number of src/include files with ≥1 executed line when that test binary runs alone (gcov, §6; `coverage_touch.json`).

| test | includes (repo) | cov |
|---|---|---|
| test_alpha_storage | api/alpha_storage.h | 2 |
| test_checkpoint | checkpoint/checkpoint_manager.h data/data_panel.h engine/simulator.h modules/{Data,Rank,TsMean}.h | 8 |
| test_combined_alpha | api/combined_signals.h data/data_panel.h engine/simulator.h metrics/metrics_engine.h modules/{Data,Rank}.h compiler/dag_builder.h | 11 |
| test_config_loader | data/config_loader.h | 1 |
| test_data_loader | data/data_panel.h data/config_loader.h | 1 |
| test_determinism | ga/{search_space,ga_engine,parallel_evaluator}.h | 6 |
| test_execution_price | compiler/dag_builder.h data/data_panel.h engine/simulator.h modules/Data.h | 10 |
| test_extreme_alpha_stress | compiler/dag_builder.h data/data_panel.h engine/simulator.h metrics/metrics_engine.h | 16 |
| test_ga | ga/{search_space,ga_engine,parallel_evaluator}.h | 7 |
| test_golden_crossval | module.h signal_vector.h data/data_panel.h metrics/summary_metrics.h modules/{Decay,Delay,Neutralize,Rank,TsMean,TsRank,TsZscore,Zscore}.h | 14 |
| test_http_contract | none (HTTP via curl popen, test_http_contract.cpp:52,86-170; spawns `longshort-server` by fork/execl :232-248) | 31 (incl. src/main.cpp) |
| test_json_serializer | api/json_serializer.h | 2 |
| test_metrics_oracle | metrics/metrics_engine.h metrics/summary_metrics.h | 4 |
| test_module_attributes | compiler/module_attributes.h | 1 |
| test_module_oracles | module.h signal_vector.h modules/{Decay,Delay,Rank,TsMean,TsRank,TsZscore,Zscore}.h | 11 |
| test_modules | signal_vector.h module.h data/data_panel.h modules/{Data,Delay,TsMean,TsRank,TsZscore,Decay,Rank,Zscore,Neutralize,Ops,Constant}.h | 16 |
| test_parquet_loader | data/config_loader.h data/data_panel.h | 4 |
| test_pipeline_contract | api/json_serializer.h compiler/dag_builder.h data/data_panel.h engine/simulator.h metrics/metrics_engine.h | 23 |
| test_simd_parity | simd/simd_utils.h simd/m_decay_avx2.h | 3 |
| test_simulator | data/data_panel.h engine/simulator.h modules/{Data,Rank,Delay}.h | 7 |
| test_summary_metrics | metrics/summary_metrics.h | 1 |
| test_thin_loader | module.h modules/Data.h compiler/expr_parser.h data/{config_loader,data_panel,parquet_reader}.h compiler/dag_builder.h engine/simulator.h | 12 |
| test_thin_moves | ../src/data/data_panel.h ../src/engine/simulator.h | 2 |
| test_xml_compiler | module.h compiler/{xml_parser,expansion,dag_builder}.h data/data_panel.h modules/Neutralize.h signal_vector.h | 18 |
| test_yearly_metrics | metrics/yearly_metrics.h | 1 |
| test_generate_modules.py | tools/generate_modules.py (import by path, :9) | — |
| test_run_engine.py | tools/run_engine.py (subprocess, :11) | — |
| test_module_build.py | run_engine.py build path (subprocess, :45) | — |
| test_afl_catalog.py | tools/build_afl_catalog.py | — |
| test_build_group_panels.py | tools/data/build_group_panels.py (symlink target outside repo, :23) | — |

Src files that **no C++ test includes directly** (all are still executed through other paths, see §6): src/main.cpp (only via spawned server), src/compiler/{expansion,xml_parser,expr_parser}.h beyond test_xml_compiler/test_thin_loader, src/compiler/range_parser.h, src/modules/{Chain,Event,SignalCache}.h, src/data/parquet_reader.h (only test_thin_loader), src/api/combined_signals.h (only test_combined_alpha).

---

## 2. Include graph

Adjacency (src/include → repo headers; resolved against the including dir, `src/`, `include/`):

```
include/ModulePrelude.h -> include/module.h include/signal_vector.h include/star_vector.h src/data/data_panel.h src/simd/m_decay_avx2.h
include/module.h -> include/signal_vector.h include/star_vector.h
src/api/alpha_storage.cpp -> src/api/alpha_storage.h
src/api/combined_signals.h -> src/data/data_panel.h src/engine/simulator.h
src/api/json_serializer.cpp -> src/api/json_serializer.h
src/api/json_serializer.h -> src/metrics/metrics_engine.h
src/checkpoint/checkpoint_manager.cpp -> include/module.h src/checkpoint/checkpoint_manager.h
src/compiler/dag_builder.cpp -> dag_builder.h expansion.h expr_parser.h xml_parser.h src/data/data_panel.h src/modules/{Constant,Data,Decay,Delay,Event,Neutralize,Ops,Rank,SignalCache,TsMean,TsRank,TsZscore,Zscore}.h   (18 edges)
src/compiler/dag_builder.h -> include/module.h src/compiler/expansion.h
src/compiler/expansion.cpp -> expansion.h range_parser.h
src/compiler/expansion.h -> xml_parser.h
src/compiler/expr_parser.cpp -> expr_parser.h src/data/data_panel.h src/modules/{Chain,Constant,Data,Delay,Ops}.h
src/compiler/module_attributes.h -> dag_builder.h
src/compiler/xml_parser.cpp -> xml_parser.h
src/core/signal_vector.cpp -> include/signal_vector.h
src/data/config_loader.cpp -> config_loader.h
src/data/data_panel.cpp -> data_panel.h parquet_reader.h
src/data/parquet_reader.cpp -> parquet_reader.h
src/data/parquet_reader.h -> config_loader.h data_panel.h
src/engine/simulator.cpp -> include/module.h src/data/data_panel.h simulator.h
src/ga/ga_engine.cpp -> ga_engine.h
src/ga/ga_engine.h -> parallel_evaluator.h search_space.h
src/ga/parallel_evaluator.cpp -> parallel_evaluator.h
src/ga/search_space.cpp -> src/compiler/{expansion,range_parser,xml_parser}.h search_space.h
src/main.cpp -> api/{alpha_storage,combined_signals,json_serializer}.h compiler/dag_builder.h data/{config_loader,data_panel,parquet_reader}.h engine/simulator.h ga/{ga_engine,search_space}.h metrics/metrics_engine.h   (11 edges)
src/metrics/metrics_engine.cpp -> metrics_engine.h
src/metrics/metrics_engine.h -> summary_metrics.h timeseries_output.h yearly_metrics.h
src/metrics/{summary_metrics,timeseries_output,yearly_metrics}.cpp -> own .h
src/modules/{Neutralize,Rank,Zscore}.h -> src/data/data_panel.h
src/simd/m_decay_avx2.cpp -> m_decay_avx2.h simd_utils.h
src/simd/simd_utils.cpp -> simd_utils.h
```

Cycles: **none** (DFS over 49 TUs + headers; `UNRESOLVED` list empty).

Headers explicitly included by ≥8 files (counting src, include, tests, tools):

| header | explicit includers |
|---|---|
| src/data/data_panel.h | 26 |
| src/engine/simulator.h | 12 |
| src/compiler/dag_builder.h | 10 |
| src/data/config_loader.h | 9 |
| include/module.h | 9 |
| src/metrics/metrics_engine.h | 8 |
| src/modules/Data.h | 8 |

Force-include: `CMakeLists.txt:153-156` adds `-include include/ModulePrelude.h` as a PUBLIC compile option of target `engine`, so every TU that links `engine` (server, 2 tools, 25 tests, generated registry; 49 TUs) effectively includes `module.h`, `signal_vector.h`, `star_vector.h`, `data/data_panel.h`, `simd/m_decay_avx2.h` plus `<vector> <string> <cmath> <cstring> <limits> <algorithm>` and `using std::isfinite; using std::isnan;` (ModulePrelude.h:6-19). `module.h` itself pulls `boost/archive/binary_{i,o}archive.hpp` and `boost/serialization/vector.hpp` (module.h:6-8) into all 49 TUs. Module headers contain no `#include` of their own except Neutralize/Rank/Zscore (`../data/data_panel.h`).

---

## 3. main.cpp anatomy (1300 L)

### 3.1 Handler registrations

| path | method | registration | implementing lambda / function | lines | L |
|---|---|---|---|---|---|
| (pre-routing advice, CORS preflight) | OPTIONS | 900 | lambda | 901-914 | 14 |
| (post-handling advice, CORS header) | * | 916 | lambda | 917-919 | 3 |
| /api/health | GET | 922-931 | lambda | 924-930 | 7 |
| /api/config | GET | 934-1000 | lambda | 936-999 | 64 |
| /api/simulate | POST | 1003-1035 | lambda 1005-1034 → `simulate_response` | 429-594 | 30 + 166 |
| /api/ga/stream | POST | 1038-1216 | lambda 1040-1215; worker body 1065-1201 | 1040-1215 | 176 |
| /api/alphas | GET, POST | 1219-1230 | lambda 1221-1229 → `list_alphas_response` 656-670 / `save_alpha` 684-846 | | 9 + 15 + 163 |
| /api/alphas/{name} | DELETE | 1233-1249 | lambda 1235-1248 | | 14 |
| /api/alphas/{name}/results | GET | 1252-1259 | lambda → `saved_result_response(name,"index.json")` 672-682 | | 6 + 11 |
| /api/alphas/{name}/results/batch | GET | 1260-1267 | lambda → `saved_result_response(name,"batch.json")` | | 6 |
| /api/alphas/{name}/results/rows/{id} | GET | 1268-1281 | lambda 1270-1280 → `saved_result_response(name,"rows/<id>.json")` | | 11 |

Helpers (static, file-local): `supports_exact_fields` 38-48, `supports_ohlc4/vwap` 50-51, `parse_missing_close_policy` 53-58, `missing_close_policy_name` 60-63, `parse_env_uint` 65-78, `parse_uint_option` 81-92, `iso_date_of` 94-102, `parse_iso_date_ms` 104-122, `class BackgroundWorkers` 128-177, `struct RunOptions` 184-201, `parse_run_options` 203-291, `simulator_config` 294-307, `execute_alpha` 309-317, `full_metrics` 319-328, `missing_data_payload` 331-357, `diagnostics_payload` 359-363, `metrics_payload` 365-374, `copy_top_metrics` 376-379, `append_signal_payload` 381-425, `simulate_response` 429-594, `AlphaRequestError` 596-600, `json_error_response` 602-608, `json_text` 611-617, `run_alpha_request<Work>` 620-638, `metadata_json` 645-654, `list_alphas_response` 656-670, `saved_result_response` 672-682, `save_alpha` 684-846, `main` 848-1300.
Two date formatters coexist: `iso_date_of` (94-102) and the lambda `ms_to_date` inside /api/config (950-957); `json_serializer.cpp:7-14` has a third `ms_to_date`.

### 3.2 Request fields parsed, per handler/function

| field | parse_run_options (203-291) | simulate_response (429-594) | /api/ga/stream (1040-1215) | save_alpha (684-846) |
|---|---|---|---|---|
| executionPrice | 205-213 | via p_r_o | via p_r_o | meta 711 → run_body 704 → p_r_o(714) |
| isStart / osStart | 214-219 | via | via | meta 708-709 → run_body |
| simStart / simEnd | 221-245 | via | via | meta 706-707 → run_body |
| threads | 246 | via | via (1053 `n_threads`) | via (758) |
| collectSignals | 247 | via, 559/580 | — | — |
| metricsOnly | 248 | via, 466/559/569/580 | — | set to true at 774, 827 |
| signalSeriesOnly | 249, 270 | 453 | — | — |
| collectSignalsTopK | 250 | 559 | — | — |
| signalPlotMaxCells | 251-254 | via | via | — |
| signalPlotSymbols | 255-268 | via | via | — |
| combinedMode | 269-273 | 591 | 1179 | — |
| missingClose | 274-277 | via | via | — |
| defaultDelistingReturn, staleQuoteSettleBars, settlementLog, distributionLog | 278-281 (reject) | via | via | — |
| universe | 282-290 | 592 | 1085, 1180 | meta 710 → run_body |
| combinedOnly | — | 432 | — | set true 826 |
| xmls | — | 434-445 | — | built 825-830 |
| xml | — | 447-449 | 1045-1046 | 690-691; rows[].xml 733-741 |
| firstSearch / popSize | — | — | 1049 | — |
| roundSize | — | — | 1050 | — |
| stopRound / generations | — | — | 1051 | — |
| stopAlphas | — | — | 1052 | — |
| streamCandidates | — | — | 1059 | — |
| name | — | — | — | 687-689 |
| level | — | — | — | 692-696 |
| metadata.{simStart,simEnd,isStart,osStart,universe,executionPrice} | — | — | — | 697-711 (`kAlphaMetadataKeys` 642-643) |
| rows[].{id,round,xml} | — | — | — | 722-742 |

`parse_run_options` call sites: 431 (simulate_response ← /api/simulate 1017, save_alpha rows 787, save_alpha batch 834), 714 (save_alpha metadata), 1047 (ga). `compile_xml` call sites in main.cpp: 441, 449, 560, 736, 1095, 1165 (6). `execute_alpha`: 466, 505, 561, 1097, 1167 (5). `full_metrics`: 467, 506, 574, 1099, 1169. `metrics_payload`: 554, 574, 1169. `append_signal_payload`: 562, 570, 582, 1175.

### 3.3 Global state and who touches it

| global (decl line) | type | touched at (main.cpp lines) |
|---|---|---|
| `g_config` (32) | BacktestConfig | write 858; read 42, 205, 269, 298, 327, 862-891, 971-974 (20 sites) |
| `g_symbol_labels` (33) | Json::Value | write 872, 876; read 945-946 |
| `g_data` (34) | DataPanel | write 862; read at 62 further sites (40, 199, 220-290, 296-297, 316, 389-409, 441, 449, 471, 488-503, 549-572, 736, 781-784, 863-877, 942-996, 1082-1183); `const` pointers into it stored in `RunOptions::universe_mask` (199, 289) |
| `g_alphas_dir` (35) | fs::path | write 891; read 659, 676, 744, 747, 844, 893, 981, 1241 |
| `g_missing_close_policy` (36) | enum | write 886; read 274, 979 |
| `g_ga_workers` (178) | BackgroundWorkers | 1065, 1067, 1112, 1296 |
| `g_simulate_workers` (179) | BackgroundWorkers | 1012, 1018, 1297 |
| `g_alpha_workers` (180) | BackgroundWorkers | 624, 751, 1298 |
| `kMaxRowId` (640), `kAlphaMetadataKeys` (642) | constexpr/static const | 728, 1276; 700 |
| `ModuleRegistry::default_registry()` static (dag_builder.cpp:171) | function-local static | compile_pipeline 307 |
| `cold_read_mutex` (parquet_reader.cpp:230) | function-local static mutex | every `read_field` call (eager at startup, lazy on first use at request time) |
| `sequence` (alpha_storage.cpp:45) | static atomic | `unique_suffix` |

### 3.4 Startup sequence (`main`, 848-1300)

1. 851: `config_path = argv[1]` else `"../../config.yaml"`.
2. 858: `g_config = BacktestConfig::from_yaml(config_path)`; 860: `cfg_dir`.
3. 862: `g_data = DataPanel::load(g_config)` → `parquet_reader::load_config` (full panel load, all eager fields, §4 hop 2); prints `T`,`N` (863).
4. 864-879: symbol labels — `symbol_labels_path` JSON if configured, else if `base_thin` → `parquet_reader::load_thin_symbol_labels`.
5. 886-890: `parse_missing_close_policy(g_config.missing_close_policy)` (second validation of a value already validated at config_loader.cpp:282-283).
6. 891-897: `g_alphas_dir = g_config.alphas_dir; alpha_storage::prepare_directory` (RENAME_NOREPLACE probe, stale staging cleanup).
7. 900-919: CORS pre-routing and post-handling advices.
8. 922-1281: 10 `registerHandler` calls (§3.1).
9. 1283: `LONGSHORT_PORT` env (default 8000, range 1-65535).
10. 1287-1294: drogon app: `setUploadPath(cfg_dir/.longshort/uploads)`, log level kWarn, `addListener("0.0.0.0", port)`, `setThreadNum(4)`, `setIdleConnectionTimeout(LONGSHORT_IDLE_TIMEOUT env, default 3600)`, `run()` (blocks).
11. 1296-1298: after `run()` returns: `stop_and_join()` on the three worker groups.

---

## 4. Data flow (data only)

### 4.1 Hops, with key types and functions

1. **HTTP request → worker thread.** drogon handler lambda `main.cpp:1005-1034` wraps the callback in a `shared_ptr` (1009) and calls `g_simulate_workers.launch(...)` (1012): one `std::thread` per request (`BackgroundWorkers::launch` 147-169). Body: `req->getJsonObject()` (1015) → `simulate_response(body, cancelled)` (1017).
2. **Config (startup only).** `BacktestConfig::from_yaml` `config_loader.cpp:144-288`: `parse_base` 60-113 expands `base:` into `DatasetConfig` entries (`base_prices`, thin: `base_returns`, `base_adj_returns`; always `base_groups` as-of); user `datasets:` 161-211 (catalog merge 180-183, `use_col` 185-200); first enabled dataset copied into the "legacy first-dataset view" fields 212-216; universe 221-242; backtest block 244-283; rejection of removed keys 284-286. Types: `BacktestConfig`, `DatasetConfig`, `UniverseConfig` (config_loader.h).
3. **DataPanel (startup only).** `DataPanel::load` `data_panel.cpp:59` → `parquet_reader::load_config` `parquet_reader.cpp:681-911`: `sources` map field→`Source` 684-695; master `ParquetFile` on close 719-720 (`ParquetFile` ctor 114-177 reads coordinate columns, sorts an `order` index, builds `calendar`, row-group offsets); date window + warmup 721-733; universe: interval file → `read_interval_universe` 546 / wide membership → `read_universe` 468 / symbol list / all 734-758; thin event destinations added transitively 765-778; masks 781-796; close via `read_field` 800 (`read_field` 227-281 under `cold_read_mutex` 230, T×N float buffer 233); `latest` tail trim 801-818; eager vs `LazyColumn` fields 820-836 (aliases share one `LazyColumn` keyed by path/cols/alignment 827-834; execution fields forced eager 829); group sanity 841-861; thin: `adj_factor` 865-867, `returns_total` (T×N double, `read_long_doubles` 604) 868-869, `move_events` 870-908. Result `DataPanel` (data_panel.h:27-74): `arrays` (unordered_map<string, vector<float>>), `lazy_columns`, `universe_mask` (T×N uint8) + `universe_masks` per name, `returns_total`, `move_events`.
4. **Request options.** `parse_run_options` `main.cpp:203-291` → `RunOptions` (184-201); `universe_mask` = pointer into `g_data` via `DataPanel::mask_for` (data_panel.cpp:67-72).
5. **Compiler.** `compile_xml(xml, &g_data, universe)` `dag_builder.cpp:434-453`: `parse_xml` `xml_parser.cpp:18-37` (pugixml → `XmlElement` tree, wraps in `<alpha>`) → `expand_pipelines` `expansion.cpp:142-276` (per-element `AltList` via `expand_element` 95-135: `choice`, `shuffle` permutations, cartesian attribute product 40-93; bind groups; cap `MAX_ALPHA_COUNT`=5000 expansion.h:13) → per `Pipeline` (vector<Op=pair<tag, map<attr,val>>>) `compile_pipeline` 268-428: first op must be `data` (277); `parse_expr` `expr_parser.cpp:202-219` (tokeniser 26, recursive-descent `Parser` building `MData`/`MConstant`/`MAdd|MSub|MMul|MDiv`/`MChain`; `returns` special case 183-185); `delay` → `MDelay` 291-305; `signal id` → `MSignalSave` 340-354 + `cache_registry`; `event` → `MEvent` 356-404; `input=` → `MSignalLoad` 406-418; everything else → `ModuleRegistry::create` 420 (`default_registry` 170-262: 8 built-in factories + `register_discovered_modules` 258, generated into `build/generated/engine_discovered_modules.cpp` by `tools/generate_modules.py` via `cmake/DiscoverModules.cmake`). Output `CompiledAlpha{modules (owning), pipeline (Module*), output, source_xml}` (dag_builder.h:37-42); `source_xml` re-serialized by `pipeline_to_xml` 136-148; universe pointer injected by `dynamic_cast` into `MRank/MZscore/MNeutralize` 444-449.
6. **Modules.** ABI `Module` (include/module.h:147-162): `init()`, `calculate(di, SignalVector&)` in place on one N-float buffer; `ModuleInput` ring buffer (module.h:34-141) for lookback; `SignalVector` non-owning view (signal_vector.h:9-21). Cross-sectional modules read the mask directly from `panel->universe_mask` or injected `universe` (Rank.h:12-14, Zscore.h:9-11, Neutralize.h:18-20).
7. **Simulator.** `execute_alpha` `main.cpp:309-317` → `simulator_config` 294-307 (`SimulatorConfig`, simulator.h:45-61) → `Simulator::run(pipeline)` `simulator.cpp:525-549`: `PortfolioSimulation` 51-446 holds the book; per bar: `fill_nan` unless `skips_nan_init` (537-538), `module->calculate` chain (539-540), optional copy into `output.signals[di]` (543), `signal_row_callback` (545), `advance(di, signal)` 74-134: `apply_moves` (thin, 145-192) or `prepare_book` (232-237) → `normalize_intent` 251-262 → `normalize_signal_row` 449-476 (mask ∧ finite ∧ quote policy, L1-normalize into `targets`) → `require_priced_quotes`/`require_tradable_quotes` 266-288 → `value_holdings_thin` 196-219 (`carried × returns_total`) or `value_holdings` 292-318 (`close/mark − 1`) → `apply_successor_transfers` 222-229 → `execute` 371-412 (OHLC4/VWAP fill via `prepare_execution_prices` 323-367; frozen budget scaling 380-388; turnover) → `apply_received_moves` 240-247 → per-bar outputs 107-125 → `emit_row` 414-422 (accounting callback for position saves). Output `SimulatorOutput` (simulator.h:63-75): 8 T-length series + `signals` (T×N when collected) + `missing_data` (T diagnostics structs).
   Combined/multi-alpha path (`simulate_response` 469-537): worker threads run `execute_alpha(..., observe)` (505) or `run_signal_rows` (503, simulator.cpp:551-564, no book); rows are batched 32 at a time (487-500) into `OrderedSignalAccumulator::add_block` (combined_signals.h:58-95: normalize per row if `normalized`, wait for alpha order on a condition variable, sum into `signals_` T×N) → `take_signals()` (105, move) → `Simulator::run_from_signals` (simulator.cpp:566-577) for the combined book.
8. **Metrics.** `full_metrics` `main.cpp:319-328` (builds `frozen_gross` T-vector 321-323) → `compute_full_metrics` `metrics_engine.cpp:5-50` → `compute_timeseries` `timeseries_output.cpp:6-71` (8 vectors of n=T−start), `compute_yearly` `yearly_metrics.cpp:18`, `compute_summary_range` `summary_metrics.cpp:6-229` twice (IS 40-43, OS 45-48; each copies the finite return slice into `finite_r` 22-29).
9. **Serializer.** `metrics_payload` `main.cpp:365-374` → `summary_to_json` `json_serializer.cpp:16-37`, `yearly_to_json` 39-58, `timeseries_to_json` 60-104 (8 Json arrays of n); `diagnostics_payload`/`missing_data_payload` 331-363; signals → `append_signal_payload` 381-425 → `signal_plot_to_json` 131-222 (sampled rows×columns, membership strings), `signal_distribution_to_json` 224-335 (quantile-clipped linear histogram), `signal_log_distribution_to_json` 447-458 → `full_signal_distribution_to_json` 337-444 twice (log + nested `linear`); response assembled 547-593; `drogon::HttpResponse::newHttpJsonResponse` 1019.
   GA path: `/api/ga/stream` 1040-1215 → `extract_search_space` `search_space.cpp:128-213` (genes = pipe/range attribute values, template with `__GENE_N__` placeholders; or one "pipeline" gene from `expand_pipelines` when `choice/shuffle` present 137-158) → `GAEngine::run_detailed` `ga_engine.cpp:17` → `run_impl` 58-153 (rounds; `scheduled` set of XML strings; `ParallelEvaluator::evaluate_results` 130) → `parallel_evaluator.cpp:18-124` (cache lookup under `cache_mutex_`, `count` worker threads per call, completion callbacks on the calling thread via `finished` deque) → `evaluate` lambda `main.cpp:1093-1110` (compile_xml → execute_alpha → full_metrics → `is_summary.sharpe`; when `streamCandidates`, summary/yearly/missingData JSON parked in `payloads` under `payload_mutex`) → ndjson events via `write` 1069-1075; final best alpha re-compiled and re-run with signals 1165-1191.
   Save path: `POST /api/alphas` → `save_alpha` 684-846: validates rows by compiling each XML (736), `SaveTransaction` (alpha_storage.cpp) staging dir, per-row `simulate_response(metricsOnly)` with `PositionsWriter` accounting callback (778-790, parquet positions), `batch.json` via `simulate_response(combinedOnly)` 834, `index.json` 835, `commit()` 837.

### 4.2 Whole-panel copies / per-request / per-candidate allocations (grep + read; sizes in elements)

| site | what is allocated or copied | per |
|---|---|---|
| simulator.cpp:32-44 `make_output` | 9 vectors of length `end`(≈T): ret, gross_ret, execution_ret, turnover, max_weight, long_weight, short_weight, position_count, missing_data (struct of 9 fields) + `timestamps.assign` copy of T int64 | Simulator::run / run_from_signals (per alpha, per GA candidate, per saved row) |
| simulator.cpp:53-72 `PortfolioSimulation` ctor | 13 N-length vectors (previous_weights_, targets_, carried_, weights_, marks_, retired_, quote_state_, execution_prices_, execution_available_, missing_bars_, pending_transfer_, received_(thin), masked_signal_ grows later) + 2 more when accounting callback | per run |
| simulator.cpp:527, 553 | `signal_buffer` N floats | per run |
| simulator.cpp:530, 543 | `output.signals.resize(T)` then `signals[di].assign(N)` → T separate N-float vectors (T×N copy) | per run with `collect_signals` (main.cpp:466 single alpha unless metricsOnly; 561 top-k replay; 1167 GA best) |
| simulator.cpp:84 | `marks_before_.assign(marks_)` N doubles | per bar, when accounting callback (position saves) |
| simulator.cpp:254 | `masked_signal_.assign(signal, N)` | per bar once any thin source retired (`any_retired_`, never reset) |
| simulator.cpp:468 | `targets.assign(N, 0.0)` (refill, capacity reused) | per bar |
| simulator.cpp:566-571 | `std::any_of` row-size validation over T rows | per run_from_signals |
| combined_signals.h:23-24 | `signals_` = T vectors × N floats (NaN) + `next_alpha_` T | per combined request |
| main.cpp:487-500 | per worker: `signal_block.reserve(32×N)`; every row `insert`ed (N floats) → whole T×N copied once per alpha in 32-row blocks; `add_block` normalizes in place into `scratch` (N doubles) and sums into `signals_` | per alpha in multi-alpha request |
| main.cpp:512 | `alphas[i] = CompiledAlpha{}` (frees modules) | per alpha |
| main.cpp:536, 581 | `take_signals()` move; `resize` to sim_end | per combined request |
| main.cpp:559-562 | top-k: `compile_xml` + `execute_alpha(collect=true)` again (full second simulation + T×N signal copy) | per top-k entry (1..5) |
| main.cpp:391 | `prefix_mask.assign` T'×N uint8 copy of the universe mask | per signal payload when `simEnd` < panel end |
| main.cpp:321-323 | `frozen_gross` T doubles | per `full_metrics` (per alpha) |
| main.cpp:554-558 | `metrics_payload` (full timeseries JSON, 8 arrays × n) per batch entry | per alpha in multi-alpha response (all `batches[]` carry full timeseries) |
| main.cpp:1101-1107 | summary+yearly+missingData JSON parked in `payloads` map | per GA candidate (streamCandidates) |
| json_serializer.cpp:264-268 | `sample` vector copy of all finite nonzero values (≤T×N floats) for `nth_element` quantiles | per `signal_distribution_to_json` |
| json_serializer.cpp:337-444 | two full passes over T×N; called twice (log 453, linear 456) → 4 passes | per `signal_log_distribution_to_json` |
| json_serializer.cpp:164 | `ever` N uint8 + mask pass over T×N | per signal plot with mask |
| metrics: timeseries_output.cpp:28-35 | 8 vectors of n | per full_metrics |
| metrics: summary_metrics.cpp:22-29 | `finite_r` copy of the IS or OS slice | ×2 per full_metrics |
| metrics: yearly_metrics.cpp:39,51 | `result` per year + `yr_ret` per year | per full_metrics |
| ga_engine.cpp:85-129 | per round: `genes`, `candidates` (xml string copies), `xmls` (second copy 129), `scheduled` set (third copy 89), `parents.push_back({genes[i],…})` (142) | per GA round |
| parallel_evaluator.cpp:20-23, 81 | `results`, `work`, `cached`, `duplicates`, `seen`; `cache_.emplace(xml, value)` (fourth XML copy) | per evaluate_results / per candidate |
| parallel_evaluator.cpp:51-56 | `count` `std::thread`s created and joined | per evaluate_results (per GA round) |
| main.cpp:1093-1110 | per candidate: `compile_xml` (new module objects), `execute_alpha` (make_output + PortfolioSimulation), `full_metrics` | per GA candidate |
| parquet_reader.cpp:233 | `output` T×N float per field | once per field (startup, or first lazy touch) |
| parquet_reader.cpp:250, 276 | `numbers()` decodes one Arrow column into a `vector<float>` per symbol per batch | load time |
| parquet_reader.cpp:782 | `build_mask` T×N uint8 per universe name (11 names in workspace config → 11 masks) | startup |
| parquet_reader.cpp:607 | `returns_total` T×N doubles (thin) | startup |
| expansion.cpp:40-93, 95-135 | per variant: attribute `map` copies, `AltList` vectors of `Pipeline` | per compile_xml |
| dag_builder.cpp:441-450 | `dynamic_cast` loop over all modules of every alpha when a universe is selected | per compile_xml |
| include/module.h:49-70 `ModuleInput::push` | N floats copied into the ring | per bar per stateful module |
| include/module.h:119-137 | `legacy_ring` vector<vector<float>> built on save/load | checkpoint only |
| src/modules/Ops.h:31-40 `eval_operands` | copies `output` into `lhs_buf` and `rhs_buf` (2×N) | per bar per binary op |
| src/modules/TsRank.h:10 | `buf` = N vectors × days floats (N allocations) | init |
| src/modules/Rank.h:8,24 | `finite_values` N pairs (reserve once; `emplace_back` per bar) | per bar |
| src/modules/Neutralize.h:12-14, 52-53 | `member_codes`, `group_sum`, `group_count` (reserve once; `assign` per bar) | per bar |
| alpha_storage.cpp:277-288 `PositionsWriter::add_bar` | nonzero weights appended per trading bar | per saved row |

Return-by-value in hot paths: `Simulator::run` returns `SimulatorOutput` by `std::move` (548); `compile_xml` returns `vector<CompiledAlpha>` (move); `OrderedSignalAccumulator::take_signals` move; `expand_pipelines` returns `vector<Pipeline>` by value; `parse_run_options` returns `RunOptions` by value (small); `read_field` returns `vector<float>` by value (NRVO).

### 4.3 Mutexes, atomics, condition variables, threads

| primitive | file:line | protects / purpose |
|---|---|---|
| `BackgroundWorkers::mutex_`, `stopping_` (atomic), per-task `done` (shared_ptr<atomic<bool>>) | main.cpp:172-176, 133-169 | `tasks_` vector of `std::thread`; one thread per request, reaped on next `launch`, joined at shutdown; 3 instances (178-180) |
| ad-hoc worker threads + `next_index`/`failed` atomics + `error_mutex` (first_error) | main.cpp:473-535 | multi-alpha simulate: `thread_count = min(threads|hardware, alphas)` |
| same pattern | main.cpp:756-818 | save_alpha rows: `min(threads|min(hardware,20), rows)` |
| `std::async` ×3 (`async` when cells ≥ 1e6 else `deferred`) | main.cpp:394-410 | plot / linear histogram / log histogram serialization |
| `payload_mutex` | main.cpp:1078, 1106, 1125 | `payloads` unordered_map<xml, Json> for streamed GA candidates |
| `OrderedSignalAccumulator::mutex_` + `ready_` (condition_variable) + `cancelled_` | combined_signals.h:114-116 | `signals_`, `next_alpha_` (alpha-order gate per row) |
| `ParallelEvaluator::cache_mutex_`, `cache_hits_` atomic | parallel_evaluator.h:42-43 | `cache_` unordered_map<xml, EvaluationResult> |
| per-call `next`, `stopping` atomics, `mutex` + `ready` cv, `finished` deque, `workers_done`, `worker_error`; `count` threads | parallel_evaluator.cpp:43-100 | one worker set per `evaluate_results` call |
| `LazyColumn::once` (once_flag) + `ready` atomic | data_panel.h:21-24, data_panel.cpp:7-15 | single load of a lazy field |
| `cold_read_mutex` (function-local static) | parquet_reader.cpp:230-231 | serializes every `read_field` (all Parquet decoding, including lazy first-touch during requests) |
| `sequence` static atomic | alpha_storage.cpp:45 | unique staging suffix; plus OS `flock` on the staging dir (alpha_storage.h:47-50) |
| drogon IO loops `setThreadNum(4)` | main.cpp:1291 | HTTP |
| `hardware_concurrency()` reads | main.cpp:473, 756; ga_engine.cpp:60 | thread-count defaults (GA default forced to 20 at main.cpp:1053, max 32 at 1057) |

No persistent thread pool exists; threads are created per request (BackgroundWorkers), per multi-alpha request (worker vector), per GA round (ParallelEvaluator), and per signal payload (std::async).

---

## 5. Compiler warnings

Build: `cmake -S cpp-engine -B map_engine/build_warn` with the brief's options, `-DCMAKE_CXX_FLAGS=` empty and
`-DCMAKE_CXX_COMPILER_LAUNCHER=warn_launcher.sh` (appends `-Wall -Wextra -Wshadow -Wunused -Wnon-virtual-dtor -Wold-style-cast`
only to TUs whose source path is under the repo; reason: drogon/trantor add `-Werror` to their own targets
(`_deps/drogon-src/CMakeLists.txt:95`, `trantor/CMakeLists.txt:79`) and a global `-Wold-style-cast` broke `AsyncFileLogger.cc:67` —
first attempt log kept in `warn_build.log` history). `nice -n 10 cmake --build … -j4` (two builds ran concurrently at 4 jobs each). EXIT=0.
Full log: `map_engine/warn_build.log` (1,105,603 bytes, 1547 `warning:` lines incl. duplicates across TUs and 1462 from drogon headers
included by main.cpp/test_http_contract — e.g. `drogon/orm/SqlBinder.h` 432, `drogon/utils/Utilities.h` 172, `HttpViewData.h` 152).

Repo-only, deduplicated by (file, line, kind): **60 warnings** (`warnings_normalized.txt`).

Per kind:

| kind | count |
|---|---|
| -Wold-style-cast | 39 |
| -Wrange-loop-construct | 10 |
| -Wshadow | 6 |
| -Wdangling-else | 4 |
| -Wunused-parameter | 1 |
| -Wnon-virtual-dtor, -Wunused-* other | 0 |

Per file (src+include: 35; tests: 25):

| file | count | kinds |
|---|---|---|
| tests/test_parquet_loader.cpp | 8 | range-loop-construct (const char* bound to const string&: 315, 329, 349, 369, 372, 397, 423, 446) |
| src/modules/Decay.h | 7 | old-style-cast 17, 38, 68, 74, 75, 79, 80 |
| src/modules/TsZscore.h | 6 | old-style-cast 18, 28, 29, 35, 36, 60 |
| src/compiler/expr_parser.cpp | 5 | old-style-cast 30, 38, 40, 45, 47 (`(unsigned char)`) |
| src/modules/TsMean.h | 4 | old-style-cast 15, 23, 29, 36 |
| tests/test_simulator.cpp | 4 | old-style-cast 24, 30, 31, 32 |
| src/modules/TsRank.h | 3 | old-style-cast 9, 16, 47 |
| src/compiler/dag_builder.cpp | 2 | shadow 447, 448 (`m` in the dynamic_cast chain) |
| src/metrics/summary_metrics.cpp | 2 | shadow 180, 198 (`size_t start` shadows parameter) |
| src/modules/Zscore.h | 2 | old-style-cast 40, 50 |
| tests/test_http_contract.cpp | 2 | dangling-else 1097; range-loop-construct 1660 |
| tests/test_json_serializer.cpp | 2 | dangling-else 128, 359 |
| tests/test_modules.cpp | 2 | old-style-cast 29, 38 |
| tests/test_xml_compiler.cpp | 2 | old-style-cast 21, 29 |
| src/data/parquet_reader.cpp | 1 | shadow 806 (`begin`) |
| src/main.cpp | 1 | shadow 1013 (`callback` shadows parameter) |
| src/modules/Constant.h | 1 | unused-parameter `di` 11 |
| src/modules/Event.h | 1 | old-style-cast 20 |
| tests/test_checkpoint.cpp, test_config_loader.cpp, test_determinism.cpp, test_ga.cpp, test_pipeline_contract.cpp | 1 each | |

Files with 0 warnings: all other src/include files (incl. simulator.cpp, json_serializer.cpp, alpha_storage.cpp, ga/*, metrics/* except summary_metrics.cpp, data_panel.cpp, config_loader.cpp, expansion.cpp, xml_parser.cpp, modules/{Chain,Data,Delay,Neutralize,Ops,Rank,SignalCache}.h, simd/*).

---

## 6. Coverage

Build: `map_engine/build_cov`, Release, `-DCMAKE_CXX_COMPILER_LAUNCHER=cov_launcher.sh` (appends `-O0 -g --coverage` to repo TUs; deps unchanged
except that drogon objects also received gcda files), `-DCMAKE_EXE_LINKER_FLAGS=--coverage`. Fixture: `tools/make_http_test_fixture.py --output-dir map_engine/fixture`
(4 symbols, 2020-01-01..2025-12-31, wide BaseData + groups, JSON config). Env: `LONGSHORT_TEST_PORT=50125 LONGSHORT_TEST_SERVER=<build>/longshort-server
LONGSHORT_TEST_CONFIG=<fixture>/config.yaml TMPDIR=<scratch>/tmp`. Server spawning: `test_http_contract.cpp:220-296 ServerProcess` forks/execs the
binary with `LONGSHORT_PORT` and stops it with `SIGTERM` on its own pid (260); the strict-policy server uses port+1 (298-299). I started no process myself.

CTest totals, verbatim (`ctest.log`):

```
100% tests passed, 0 tests failed out of 30

Total Test time (real) =  20.92 sec
```

Per binary (from `ctest_LastTest_full.log`): test_data_loader 2, test_config_loader 11, test_parquet_loader 19, test_modules 23, test_simulator 26,
test_thin_loader 11, test_thin_moves 14, test_summary_metrics 8, test_module_oracles 8, test_metrics_oracle 7, test_pipeline_contract 8,
**test_http_contract 36 (27 HttpContractTest cases ran, 0 `SKIPPED` lines in the log)**, test_extreme_alpha_stress 2, test_yearly_metrics 11,
test_checkpoint 6, test_simd_parity 8, test_ga 15, test_determinism 3, test_xml_compiler 41, test_golden_crossval 9, test_combined_alpha 12,
test_json_serializer 24, test_alpha_storage 13, test_module_attributes 7, test_execution_price 29 → 353 gtest cases; Python: test_generate_modules 19,
test_run_engine 11, test_module_build 1, test_afl_catalog 5, test_build_group_panels 15 (ran with the venv interpreter, not skipped).

Method: `gcov --json-format --stdout` on every `.gcda` under `build_cov/CMakeFiles` (95 files incl. `longshort-server.dir/src/main.cpp.gcda`, written when the spawned
server exited on SIGTERM), union of executed lines per source file (`gcov_agg.py full`). "tests" = count of test binaries that execute ≥1 line of the file when
run alone (gcda cleared between binaries, `gcov_agg.py pertest`; Python tool tests excluded — they do not run the instrumented binaries).

| file | lines (instrumented) | % covered | tests touching |
|---|---|---|---|
| include/module.h | 75 | 97.3 | 12 |
| include/signal_vector.h | 6 | 100.0 | 12 |
| src/api/alpha_storage.cpp | 216 | 93.1 | 2 (test_alpha_storage, test_http_contract) |
| src/api/alpha_storage.h | 2 | 100.0 | 2 |
| src/api/combined_signals.h | 69 | 94.2 | 2 (test_combined_alpha, test_http_contract) |
| src/api/json_serializer.cpp | 343 | 97.4 | 3 (test_http_contract, test_json_serializer, test_pipeline_contract) |
| src/checkpoint/checkpoint_manager.cpp | 20 | 90.0 | 1 (test_checkpoint) |
| src/checkpoint/checkpoint_manager.h | 3 | 100.0 | 1 |
| src/compiler/dag_builder.cpp | 295 | 92.2 | 6 |
| src/compiler/expansion.cpp | 132 | **72.0** | 7 |
| src/compiler/expr_parser.cpp | 144 | 94.4 | 6 |
| src/compiler/module_attributes.h | 44 | 100.0 | 1 (test_module_attributes) |
| src/compiler/range_parser.h | 28 | 92.9 | 6 |
| src/compiler/xml_parser.cpp | 25 | 88.0 | 8 |
| src/data/config_loader.cpp | 242 | 94.2 | 4 |
| src/data/data_panel.cpp | 55 | 96.4 | 14 |
| src/data/data_panel.h | 2 | 100.0 | 1 |
| src/data/parquet_reader.cpp | 740 | 87.0 | 3 (test_http_contract, test_parquet_loader, test_thin_loader) |
| src/engine/simulator.cpp | 398 | 96.2 | 9 |
| src/ga/ga_engine.cpp | 131 | 90.8 | 3 (test_determinism, test_ga, test_http_contract) |
| src/ga/parallel_evaluator.cpp | 100 | 91.0 | 3 |
| src/ga/parallel_evaluator.h | 2 | 100.0 | 2 |
| src/ga/search_space.cpp | 119 | 92.4 | 3 |
| src/main.cpp | 992 | 88.0 | 1 (test_http_contract only) |
| src/metrics/metrics_engine.cpp | 21 | 95.2 | 5 |
| src/metrics/summary_metrics.cpp | 133 | 99.2 | 7 |
| src/metrics/timeseries_output.cpp | 40 | 95.0 | 6 |
| src/metrics/yearly_metrics.cpp | 73 | 100.0 | 6 |
| src/modules/Chain.h | 18 | **55.6** | 3 |
| src/modules/Constant.h | 8 | 100.0 | 4 |
| src/modules/Data.h | 8 | 100.0 | 10 |
| src/modules/Decay.h | 53 | 79.2 | 3 (test_golden_crossval, test_module_oracles, test_modules) |
| src/modules/Delay.h | 15 | **60.0** | 9 |
| src/modules/Event.h | 23 | **69.6** | 1 (test_xml_compiler) |
| src/modules/Neutralize.h | 42 | 95.2 | 5 |
| src/modules/Ops.h | 54 | 85.2 | 4 |
| src/modules/Rank.h | 39 | 100.0 | 11 |
| src/modules/SignalCache.h | 18 | **66.7** | 1 (test_xml_compiler) |
| src/modules/TsMean.h | 33 | 78.8 | 5 |
| src/modules/TsRank.h | 39 | 82.1 | 3 |
| src/modules/TsZscore.h | 50 | 86.0 | 3 |
| src/modules/Zscore.h | 32 | 100.0 | 5 |
| src/simd/m_decay_avx2.cpp | 51 | 96.1 | 4 |
| src/simd/simd_utils.cpp | 13 | 92.3 | 4 |
| src/simd/simd_utils.h | 10 | 90.0 | 1 (test_simd_parity) |

Not instrumented / no lines reported: `include/ModulePrelude.h`, `include/star_vector.h` (alias only), `src/core/signal_vector.cpp` (3 L, no code),
`src/*/*.h` headers whose functions are declarations only (config_loader.h, parquet_reader.h, simulator.h, ga_engine.h, search_space.h, metrics headers,
json_serializer.h, dag_builder.h, expansion.h, expr_parser.h, xml_parser.h, m_decay_avx2.h). The uncovered lines in module headers (Chain 8, Delay 6,
Event 7, SignalCache 6, Decay 11, TsMean 7, TsRank 7, TsZscore 7, Ops 8) are predominantly the `checkpoint(iarchive/oarchive)` overrides, which only
test_checkpoint exercises for MData/MRank/MTsMean. Weighted src line coverage: 6,087 of 6,655 instrumented lines executed = 91.5%.

---

## 7. Compat / legacy inventory

Grep: `legacy|compat|older|deprecated|fallback|backward|no longer|\bstale\b|\bv1\b|thin|nonthin|rc2|returns_total|delisting|\bmode\b` (case-insensitive)
over src/ include/ tests/ tools/ docs/ cpp-engine/docs/ README.md CLAUDE.md examples/datasets/README.md modules/README.md → 392 lines
(`compat_grep.txt`). Counts in src/tests/tools only: legacy 42, compat 1, older 7, deprecated 0, fallback 5, backward 1, "no longer" 7, stale 22, v1 23,
thin 198 (test_thin_loader 42, parquet_reader.cpp 41, test_thin_moves 36, restart_engine.py 17, test_execution_price 14, simulator.cpp 13, config_loader.cpp 9,
main.cpp 6, data_panel.h 5, others ≤3), nonthin 0, rc2 0, returns_total 28, delisting 20 (all in tools/missing_close_oracle.py), mode 23 (all `combined_mode`/`combinedMode`
except combined_signals.h:19-25). Docs hits per file: examples/datasets/README.md 14, CLAUDE.md 11, docs/data_contract.md 6, cpp-engine/docs/math_contract.md 6,
remediation_backlog_2026-03-05.md 4, plan.md 4, README.md 3, docs/README.md 1.

Configs read (read-only): `examples/datasets/us_equity.yaml`, `equities_monthly_afl.yaml`, `compare_v2_all.yaml`, `/home/jgo/longshort_workspace/config.yaml`.
All four: `base.cache: /home/base/cache/cache_v2_cash_1d` whose `manifest.json` declares `"layout": "thin-v1"` (build_id 2026-09-15, source_build_id v21_20261008_062532Z);
cache dir listing has `returns.parquet`, `adj_returns.parquet`, `adj_factor.parquet`, `events.parquet`, `master.parquet`, `universe.parquet`, `groups/`, no `returns_total.parquet`.
All four add one `AFL` wide lazy as-of dataset with a catalog; `universe.path` = interval file; `end_date: latest`; `missing_close_policy: carry`; `combined_mode: normalized`;
no `symbol_labels_path`; no `events:`/`corporate_actions`. Differences: `universe.names` (us_equity, workspace: 10 names) vs `universe.name: Top500` (monthly) / `ALL` (compare_v2_all);
`execution_price`: close (us_equity), ohlc4 (monthly, compare_v2_all), vwap (workspace); `start_date` 2016 vs 2010. The `.bak-20261008` copies differ only by
`path: /home/base/cache/cache_v1_cash_1d/AflData_M` and an extra `backtest.mode: 1` line (no `mode` key is read by config_loader.cpp — grep: 0 hits).

| # | site | stated reason / text | exercised by a config? | exercised by tests/tools? |
|---|---|---|---|---|
| 1 | config_loader.h:34-38 `parquet_path, time_col, symbol_col, use_columns` | "Legacy first-dataset views retained for existing callers/configs" (set at config_loader.cpp:198, 208, 214) | n/a (set for every config; read by no src file outside config_loader.cpp) | tools/bench_2000_breakdown.cpp:57; tests/test_config_loader.cpp (6 hits) |
| 2 | config_loader.cpp:78-83 reject thin cache with `returns_total.parquet` | "predates 2026-09-28 naming … rebuild it with thin_build.py" | no (cache has no returns_total.parquet) | test_thin_loader.cpp:285-288 |
| 3 | config_loader.cpp:284-286 reject `events:`, `corporate_actions`, `default_delisting_return`, `stale_quote_settle_bars` | "Legacy settlement and corporate-action config is no longer supported; use a thin-v1 cache" | no (none present) | test_config_loader.cpp:151-158 |
| 4 | config_loader.cpp:101-105 nonthin `base:` → wide `BaseData/*.parquet` | CLAUDE.md:21 "Generic nonthin … wide base.cache remain loadable" | **no** (all configs thin-v1) | HTTP fixture (make_http_test_fixture.py writes BaseData wide), test_parquet_loader, test_config_loader:201-232 |
| 5 | config_loader.cpp:150-155, 161-211 `datasets:`-only config (no `base:`) | "`base:` alone is a complete data configuration; without it `datasets:` stays required" | no (all have base) | test_config_loader.cpp:22-36 ("legacy" YAML string), test_parquet_loader.cpp:194 "LegacyLongFile…" |
| 6 | config_loader.cpp:33-34, 189-190; parquet_reader.cpp:687-688 `returns` reserved for close-derived | nonthin rule with thin exception `base_returns` | thin exception path (configs) | tests |
| 7 | expr_parser.cpp:183-185 `returns` → panel field if `thin_layout` or column, else `make_returns()` (close/Delay(close,1)−1 via MChain/MDiv/MSub) | "A thin-v1 panel carries `returns` … other layouts derive it from close" | thin branch only | nonthin branch: test_xml_compiler (MChain), HTTP fixture |
| 8 | simulator.cpp:71, 88-100, 508-522 `thin_layout` branches (`apply_moves`/`value_holdings_thin` vs `prepare_book`/`value_holdings`) | docs/data_contract.md §6 | thin branch | nonthin: test_simulator (26), test_execution_price (both via `thin` flag), test_http_contract |
| 9 | data_panel.h:44-58 thin-only members on the generic panel (`thin_layout`, `returns_total`, `move_events`) | comment "Thin cache (docs/data_contract.md§6)" | yes | yes |
| 10 | parquet_reader.cpp:145 int64 symbol ids → decimal strings; :702, 865 `adj_factor` handled outside the `sources` map | "int64 ids (thin-v1 tradingItemId)" | yes | test_thin_loader |
| 11 | main.cpp:278-281 reject request keys `defaultDelistingReturn`, `staleQuoteSettleBars`, `settlementLog`, `distributionLog` | "is no longer supported; use a thin-v1 cache" | n/a (request-level) | test_http_contract.cpp:1081 |
| 12 | main.cpp:1049, 1051 alias request keys `popSize`→`firstSearch`, `generations`→`stopRound` | none stated | n/a | test_http_contract.cpp (popSize 3, "generations" 3) |
| 13 | main.cpp:1144-1147 event type `generation_end` + `generation`=round−1 + `totalGenerations` when `streamCandidates` is false | math_contract.md:197 "Legacy requests without this flag retain generation events and the best-alpha complete" | n/a | test_http_contract (GA without flag: 519; unverified which cases) |
| 14 | json_serializer.cpp:23, 31 keys `minBet` (= avg position count), `minBetMin` (= min position count) | "legacy key" ×2; CLAUDE.md:28 | always emitted | test_json_serializer.cpp:35, 49 pin them |
| 15 | json_serializer.cpp:454-456 nested `linear` histogram inside `signalDistributionLog` | "keep the historical quantile-clipped linear API intact for older clients" | always computed | test_json_serializer |
| 16 | json_serializer.h:37 / main.cpp:412-413: separate `distribution` (quantile-clipped, `signal_distribution_to_json`) still emitted next to `signalDistributionLog` | "Unlike the legacy linear histogram…" | always (3 histograms per signal payload) | test_json_serializer, test_http_contract |
| 17 | include/module.h:82 `ModuleInput::init` "Legacy init for explicit allocation (non-STAR code paths)" vs lazy allocation in `push` (50-59) | comment | `init` is the path all built-ins use; lazy branch has no built-in user | — |
| 18 | include/module.h:113-140 `legacy_ring` vector<vector<float>> wire format in `serialize` | remediation_backlog_2026-03-05.md:207 "ModuleInput legacy wire format … 호환 테스트" | no runtime caller of CheckpointManager in src | test_checkpoint.cpp:210-212 with tests/fixtures/module_input_legacy_v0.bin |
| 19 | combined_signals.h:22-26 / main.cpp:269-273 `combinedMode` `raw` vs `normalized` | CLAUDE.md:23 | configs: normalized; raw only by request | test_combined_alpha.cpp:245 names the raw accumulator `legacy` |
| 20 | combined_signals.h:29-54 `add` (row-wise) next to `add_block` (58-95) | "fewer wakeups" | main uses `add_block` only (492) | `add`: test_combined_alpha only |
| 21 | ga_engine.h:59-62 `run`, `run_streaming`, `ProgressCallback`; parallel_evaluator.h:29, 36 `evaluate_batch`, `cache_hits` | "Core loop shared by run() and run_streaming()" | main uses `run_detailed`/`evaluate_results` only | run/evaluate_batch/cache_hits: tests only; run_streaming: nobody |
| 22 | main.cpp:864-879 `symbol_labels_path` JSON vs thin master labels | — | configs: none set the path → thin master branch | test_thin_loader (load_thin_symbol_labels ×1) |
| 23 | main.cpp:851 default config path `"../../config.yaml"`; main.cpp:1283 default port 8000 | — | launchers pass explicit paths (test_http_contract.cpp:248; run_engine/restart_engine: unverified) | — |
| 24 | parquet_reader.cpp:468 `read_universe` (wide membership file) vs :546 `read_interval_universe` vs config_loader.cpp:114-127 symbol list / `all` | — | configs: interval file; fixture: `universe: ["all"]` | wide membership: test_parquet_loader (2 `universe` hits, unverified form) |
| 25 | parquet_reader.cpp:801-818 `end_date: latest` tail trim vs explicit range | CLAUDE.md:32 | all configs `latest` | fixture explicit 2025-12-31 |
| 26 | main.cpp:50-51, 210-213, 977-978 / simulator.cpp:323-367 / parquet_reader.cpp:705-717 execution price close/ohlc4/vwap | CLAUDE.md:19 | close (us_equity), ohlc4 (monthly, compare_v2_all), vwap (workspace) | test_execution_price 29 cases |
| 27 | simulator.cpp:277-288, 358 `MissingClosePolicy::Error` vs `Carry` | CLAUDE.md:21 | all configs carry | test_http_contract strict server (port+1), test_simulator |
| 28 | tools/missing_close_oracle.py:46, 70-71, 319-325, 340, 363 `default_delisting_return`, stale settle rule | docstring "Independent missing-close / delisting accounting ledger … from the accounting contract and the implementation brief" | models keys the loader rejects (site 3) | used only by tools/check_missing_data.py |
| 29 | tools/__pycache__/run_top500_missing_close.cpython-311.pyc | bytecode without source | — | — |
| 30 | examples/datasets/*.yaml.bak-20261008 (`backtest.mode: 1`, `cache_v1_cash_1d/AflData_M`) | — | not loaded by anything (grep: no reference) | — |
| 31 | tests/test_simulator.cpp:595, 705-706, 716, 722 golden `missing_close_bitwise.json` from engine commit 2e77af1; `plan.mode != 0` skipped "Historical implicit-delay cases are no longer supported" | comments | — | test_simulator |
| 32 | cpp-engine/docs/plan.md, progress.md, remediation_backlog_2026-03-05.md | CLAUDE.md:14 "Reports dated 2026-09-08 describe an earlier state" | — | — |
| 33 | math_contract.md:104 "Older API responses without `ir`" | yearly `ir` field (json_serializer.cpp:49) always emitted | — | — |

---

## 8. tools/

| script | L | purpose (from docstring / header) | referenced by |
|---|---|---|---|
| run_engine.py | 146 | "Discover user modules, incrementally build the engine, then start the server." | README.md, CLAUDE.md, modules/README.md, docs/build_and_validation.md, examples/datasets/README.md, CMakeLists.txt (comment), tests/test_run_engine.py, tools/run_logic_tests.py |
| restart_engine.py | 659 | restart the longshort-server on one port after a cache publish and verify /api/config; roll back on failure | CLAUDE.md, README.md, docs/build_and_validation.md, restart_engine.sh |
| restart_engine.sh | 11 | bash wrapper `exec $ENGINE_PY restart_engine.py "$@"` | nothing (only its own header comment) |
| generate_modules.py | 455 | discover Module headers, generate XML factories (`register_discovered_modules`) | CMakeLists.txt / cmake/DiscoverModules.cmake, tests/test_generate_modules.py, tools/run_logic_tests.py |
| make_http_test_fixture.py | 68 | deterministic synthetic base cache + JSON config for HTTP contract tests | CLAUDE.md, docs/build_and_validation.md, tests/test_http_contract.cpp (comment) |
| run_logic_tests.py | 221 | in-memory core test harness without Arrow/server ("temporary-environment validation path") | CLAUDE.md, README.md, docs/build_and_validation.md |
| build_afl_catalog.py | 145 | build AFL name/file catalog JSON from a dictionary (CSV/xlsx) | examples/datasets/README.md:70, tests/test_afl_catalog.py |
| check_group_neutralize_oracle.py | 355 | compare `<neutralize>` vs team `cs_group_neut` via its own server on a free port | **nothing** |
| check_missing_data.py | 335 | missing-close P&L vs independent ledger; `--server` spins isolated servers | **nothing**; imports missing_close_oracle |
| check_team_sim_parity.py | 163 | close-to-close PnL parity vs team StatsHandler.run with synthetic wide files + isolated server | **nothing** |
| check_wide_data_oracle.py | 120 | verify C++ wide loading against Arrow/NumPy reference | **nothing** |
| missing_close_oracle.py | 527 | independent missing-close/delisting ledger (models default_delisting_return & stale settle, see §7 #28) | tools/check_missing_data.py only |
| bench_2000_breakdown.cpp | 159 | CMake target `bench_2000_breakdown`: "One-off benchmark utility for large multi-alpha request breakdown" (CMakeLists.txt:174-179); uses the 4-arg `DataPanel::load` (57) | CMakeLists.txt only |
| data_load_probe.cpp | 60 | CMake target `data_load_probe`: load a config, print field count/bytes/timing (`usage: data_load_probe CONFIG [--summary] [FIELD ...]`) | CMakeLists.txt only |
| data/ (symlink → /home/jgo/universe_builder/cache) | 8092 | out-of-repo builders: build_group_panels.py (test target), build_cache_v2.py, build_distributions.py, build_events.py, refresh_master.py, 2 READMEs | tests/test_build_group_panels.py:23, parquet_reader.cpp:858 (error message), examples/datasets/README.md:45 (README_group_panels.md) |
| tests/golden/generate_golden.py | 521 | writes tests/golden/{modules,metrics}.json | **nothing** (test_golden_crossval.cpp:44,139-140 reads the JSON files; the script name appears nowhere) |

Referenced by nothing: `check_group_neutralize_oracle.py`, `check_missing_data.py`, `check_team_sim_parity.py`, `check_wide_data_oracle.py`, `restart_engine.sh`,
`tests/golden/generate_golden.py`; plus the orphan bytecode `tools/__pycache__/run_top500_missing_close.cpython-311.pyc`.

---

## 9. Hotspot table (data only)

refs-in = explicit `#include` count of the file's header across src/include/tests/tools (0 for .cpp without a header role; `*` = also force-included into all 49 TUs via ModulePrelude);
refs-out = repo headers directly included by the file; warnings = §5; cov = §6; tests = test binaries executing the file (§6). Sorted by lines.

| file | lines | refs-in | refs-out | warnings | cov % | tests |
|---|---|---|---|---|---|---|
| src/main.cpp | 1300 | — | 11 | 1 | 88.0 | 1 |
| src/data/parquet_reader.cpp | 923 | 4 (parquet_reader.h) | 1 | 1 | 87.0 | 3 |
| src/engine/simulator.cpp | 577 | 12 (simulator.h) | 3 | 0 | 96.2 | 9 |
| src/api/json_serializer.cpp | 458 | 5 (json_serializer.h) | 1 | 0 | 97.4 | 3 |
| src/compiler/dag_builder.cpp | 453 | 10 (dag_builder.h) | 18 | 2 | 92.2 | 6 |
| src/api/alpha_storage.cpp | 304 | 3 (alpha_storage.h) | 1 | 0 | 93.1 | 2 |
| src/data/config_loader.cpp | 288 | 9 (config_loader.h) | 1 | 0 | 94.2 | 4 |
| src/compiler/expansion.cpp | 276 | 5 (expansion.h) | 2 | 0 | 72.0 | 7 |
| src/metrics/summary_metrics.cpp | 243 | 5 (summary_metrics.h) | 1 | 2 | 99.2 | 7 |
| src/compiler/expr_parser.cpp | 219 | 3 (expr_parser.h) | 7 | 5 | 94.4 | 6 |
| src/ga/search_space.cpp | 213 | 5 (search_space.h) | 4 | 0 | 92.4 | 3 |
| include/module.h | 162 | 9* | 2 | 0 | 97.3 | 12 |
| src/ga/ga_engine.cpp | 153 | 4 (ga_engine.h) | 1 | 0 | 90.8 | 3 |
| src/metrics/yearly_metrics.cpp | 136 | 3 (yearly_metrics.h) | 1 | 0 | 100.0 | 6 |
| src/ga/parallel_evaluator.cpp | 124 | 4 (parallel_evaluator.h) | 1 | 0 | 91.0 | 3 |
| src/simd/m_decay_avx2.cpp | 119 | 3* (m_decay_avx2.h) | 2 | 0 | 96.1 | 4 |
| src/modules/Ops.h | 119 | 3 | 0 | 0 | 85.2 | 4 |
| src/api/combined_signals.h | 117 | 2 | 2 | 0 | 94.2 | 2 |
| src/modules/Decay.h | 100 | 4 | 0 | 7 | 79.2 | 3 |
| src/engine/simulator.h | 99 | 12 | 0 | 0 | — | — |
| src/api/alpha_storage.h | 93 | 3 | 0 | 0 | 100.0 | 2 |
| src/ga/ga_engine.h | 82 | 4 | 2 | 0 | — | — |
| src/modules/TsZscore.h | 81 | 4 | 0 | 6 | 86.0 | 3 |
| src/modules/Neutralize.h | 80 | 4 | 1 | 0 | 95.2 | 5 |
| src/compiler/module_attributes.h | 79 | 1 | 1 | 0 | 100.0 | 1 |
| src/data/data_panel.h | 74 | 26* | 0 | 0 | 100.0 | 1 |
| src/data/data_panel.cpp | 72 | — | 2 | 0 | 96.4 | 14 |
| src/metrics/timeseries_output.cpp | 71 | 2 (timeseries_output.h) | 1 | 0 | 95.0 | 6 |
| src/modules/Rank.h | 68 | 7 | 1 | 0 | 100.0 | 11 |
| src/data/config_loader.h | 66 | 9 | 0 | 0 | — | — |
| src/modules/TsRank.h | 65 | 4 | 0 | 3 | 82.1 | 3 |
| src/modules/Zscore.h | 61 | 4 | 1 | 2 | 100.0 | 5 |
| src/modules/TsMean.h | 55 | 5 | 0 | 4 | 78.8 | 5 |
| src/compiler/range_parser.h | 52 | 2 | 0 | 0 | 92.9 | 6 |
| src/compiler/dag_builder.h | 50 | 10 | 2 | 0 | — | — |
| src/metrics/metrics_engine.cpp | 50 | 8 (metrics_engine.h) | 1 | 0 | 95.2 | 5 |
| src/metrics/summary_metrics.h | 49 | 5 | 0 | 0 | — | — |
| src/simd/simd_utils.h | 47 | 3 | 0 | 0 | 90.0 | 1 |
| src/modules/Event.h | 44 | 1 | 0 | 1 | 69.6 | 1 |
| src/ga/parallel_evaluator.h | 44 | 4 | 0 | 0 | 100.0 | 2 |
| src/api/json_serializer.h | 43 | 5 | 1 | 0 | — | — |
| src/modules/SignalCache.h | 39 | 1 | 0 | 0 | 66.7 | 1 |
| src/checkpoint/checkpoint_manager.cpp | 37 | 2 (checkpoint_manager.h) | 2 | 0 | 90.0 | 1 |
| src/compiler/xml_parser.cpp | 37 | 5 (xml_parser.h) | 1 | 0 | 88.0 | 8 |
| src/metrics/metrics_engine.h | 36 | 8 | 3 | 0 | — | — |
| src/simd/m_decay_avx2.h | 34 | 3* | 0 | 0 | — | — |
| src/checkpoint/checkpoint_manager.h | 31 | 2 | 0 | 0 | 100.0 | 1 |
| src/modules/Chain.h | 30 | 1 | 0 | 0 | 55.6 | 3 |
| src/simd/simd_utils.cpp | 29 | — | 1 | 0 | 92.3 | 4 |
| src/metrics/yearly_metrics.h | 29 | 3 | 0 | 0 | — | — |
| src/metrics/timeseries_output.h | 27 | 2 | 0 | 0 | — | — |
| src/modules/Delay.h | 26 | 6 | 0 | 0 | 60.0 | 9 |
| src/ga/search_space.h | 25 | 5 | 0 | 0 | — | — |
| src/modules/Data.h | 23 | 8 | 0 | 0 | 100.0 | 10 |
| include/signal_vector.h | 21 | 7* | 0 | 0 | 100.0 | 12 |
| src/compiler/expansion.h | 21 | 5 | 1 | 0 | — | — |
| src/compiler/expr_parser.h | 20 | 3 | 0 | 0 | — | — |
| include/ModulePrelude.h | 19 | 0 (force-included in 49 TUs) | 5 | 0 | — | — |
| src/modules/Constant.h | 16 | 3 | 0 | 1 | 100.0 | 4 |
| src/compiler/xml_parser.h | 16 | 5 | 0 | 0 | — | — |
| src/data/parquet_reader.h | 15 | 4 | 2 | 0 | — | — |
| include/star_vector.h | 8 | 2* | 0 | 0 | — | — |
| src/core/signal_vector.cpp | 3 | — | 1 | 0 | — | — |

---

## Resource accounting

- `df -h /` before: `46G 29G 15G 67%` (17:15). During (both builds present): `30G used 69%`. After cleanup: `/dev/nvme0n1p3 46G 29G 15G 67%`.
- `du -sh map_engine/` after cleanup: **1.5M** (build_warn 91M and build_cov 948M deleted; fixture and tmp deleted; `xmlnl_build` untouched).
- Processes: I started none directly; the HTTP test spawned and SIGTERMed its own `longshort-server` children on 50125/50126.
  `ss -ltnp | grep -E "5012[56]"` → no listener. `ps` shows only the pre-existing live engine `PID 827723 /home/jgo/longshort_workspace/.longshort/build/longshort-server … config.yaml` (port 50007), not touched.

## Unverified

- Frontend usage of alias request keys (`popSize`, `generations`), of `minBet`/`minBetMin`, and of the three histogram payloads: not checked (out of scope; a grep under `frontend/src` returned nothing, path may differ).
- Whether `run_engine.py`/`restart_engine.py` ever rely on the `"../../config.yaml"` default (main.cpp:851) or `LONGSHORT_PORT` default 8000: scripts not read.
- Which `universe` form tests/test_parquet_loader.cpp exercises for `read_universe` (wide membership file) — only grep counts (2) were taken.
- Whether the generated `engine_discovered_modules.cpp` includes `compiler/module_attributes.h` (it would add one refs-in to that header); the generated file was deleted with the build dir.
- Which HttpContractTest cases send `/api/ga/stream` without `streamCandidates` (legacy `generation_end` event path, §7 #13).
