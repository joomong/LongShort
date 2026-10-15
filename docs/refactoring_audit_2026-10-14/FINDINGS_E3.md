The report was not written to `refactor_audit/FINDINGS_E3.md`: the harness refused the Write call ("Subagents should return findings as text"). The full report is below for you to save. Scratch `refactor_audit/audit_e3/` is deleted (peak 3.5 MB; `df -h /` was 67% before and after). The repo was not modified and no engine was started.

**Top 5**
1. **F1:** `equities_monthly_afl.yaml` (`name: Top500`), `compare_v2_all.yaml` (`name: ALL`) and the neutralize oracle's default config fail at startup. Neither name exists in the cache's `universe.parquet`.
2. **F2:** `<choice bind>` and `<shuffle>` silently drop or mispair variants in both simulate and GA. A probe confirmed it, no test covers it, and no saved alpha uses either tag.
3. **F3:** built-in XML attributes accept typos and bad values silently (`dayz`→20, `ddof="1.9"`→1, `pct="flase"`→0). Discovered modules reject them. The reserved-tag list is a hand-kept Python copy of the C++ one.
4. **F7:** unknown config keys are ignored. A misspelt `missing_close_polcy: error` silently stays on carry (probe confirmed).
5. **F5:** each restart takes about 190 s (from the engine log). `base_data.parquet` (51.4M rows) is decoded 8 times, and `returns.parquet` is loaded twice, as a float and a double copy.

---

# FINDINGS_E3 — engine data layer, compiler, modules, tools

Agent: audit_e3. Working tree of `/home/jgo/longshort_platform` as of 2026-10-14. Repo was not modified.
Paths are relative to `cpp-engine/` unless they are absolute or start with `docs/`, `modules/` or `CLAUDE.md`. Line numbers are real file lines.
Probes were small g++ builds of repo sources plus a driver, in `refactor_audit/audit_e3/`. They used pugixml, yaml-cpp and boost from existing `/tmp/longshort_build_final/_deps` and `/usr/include`. Scratch has been deleted (peak 3.5 MB; `df -h /` 67% before and after).
No engine was started. Real data was read only through pyarrow for metadata, universe names and schemas.
The startup time comes from `/home/jgo/longshort_workspace/.longshort/logs/engine_50007.log`.

## 0. Scope read

- `src/data/parquet_reader.cpp (923)`: `ParquetFile` reads the time column, plus the symbol column when the file is long. It sorts a row index, builds the calendar and maps each row to its row group. `read_field` aligns one field onto (T,N) coordinates. Wide files use exact or as-of row lookup and read columns in batches of 64. Long files use an exact (date,id) map. `load_config` builds the full panel: field→Source map, master calendar from close, date window and warmup, universe (interval, wide membership, symbol list or all), transitive thin event destinations, masks, close, `latest` tail trim, eager/lazy fields, group sanity check, thin `adj_factor`, `returns_total` and move events.
- `src/data/parquet_reader.h (15)`: declares `load_config`, `load_thin_symbol_labels` and `load_parquet`.
- `src/data/config_loader.cpp (288)` / `.h (66)`: YAML → `BacktestConfig`. `parse_base` expands `base:` into `base_prices`/`base_groups`. A thin manifest adds `base_returns` and `base_adj_returns` as long files. The loader then merges user datasets with catalog, `fields` and `use_col`, fills the legacy first-dataset view, then reads universe and backtest options. The last steps reject 4 removed keys and a thin cache that still has `returns_total.parquet`.
- `src/data/data_panel.cpp (72)` / `.h (74)`: `LazyColumn` loads once under `call_once`. `row()`/`at()` dispatch between eager and lazy columns. `mask_for` selects a named universe. Thin-only members are `returns_total` and `move_events`.
- `src/compiler/xml_parser.cpp (37)`: pugixml wraps the input in `<alpha>` and produces an `XmlElement` tree.
- `src/compiler/expansion.cpp (276)` / `.h (21)`: per element it takes the cartesian product of attribute values. Values are expanded by range (`a...b...c`) or by pipe. `choice` gives alternatives, `shuffle` gives permutations, and `bind` locks the index of matching `choice` elements. The result is capped at 5000 pipelines.
- `src/compiler/expr_parser.cpp (219)` / `.h (20)`: tokenizer plus recursive descent. Grammar: `+ - * /`, parentheses, unary minus, numeric literals and identifiers (panel fields). `returns` resolves to the panel field on thin, and to `close/Chain(close,Delay 1)-1` otherwise.
- `src/compiler/dag_builder.cpp (453)` / `.h (50)`: `compile_pipeline` needs `data` as the first op and turns `data delay` into an `MDelay`. It handles `signal`, `event` and `input=` itself and sends every other tag to `ModuleRegistry`. The registry has 8 hand-written built-in factories plus the generated `register_discovered_modules`. `compile_xml` re-serializes `source_xml` and injects the run universe by `dynamic_cast`.
- `src/compiler/module_attributes.h (79)`: strict `parse_int`, `parse_float` and `validate_keys`, used only by generated factories.
- `src/compiler/range_parser.h (52)`: `trim_copy` and `parse_range_notation` (uses `stoi`, no validation).
- `include/module.h (162)`, `ModulePrelude.h (19)`, `signal_vector.h (21)`, `star_vector.h (8)`: `Module` ABI is `init` plus in-place `calculate(di, SignalVector&)` and two boost `checkpoint` virtuals. `ModuleInput` is a flat ring buffer with a legacy vector-of-vector wire format. The prelude is force-included in every TU.
- `src/modules/*.h (14 files, 807 L)`:
  - Data copies a panel row. Constant broadcasts a scalar.
  - Delay and Decay use the ring buffer (Decay can use AVX2). TsMean and TsZscore keep O(1) running sums. TsRank counts O(days).
  - Rank, Zscore and Neutralize mask with `universe`, else with `panel->universe_mask`, then compute cross-sectionally.
  - Ops covers Add, Sub, Mul and Div; Le and Ge are unused. They copy the output into the lhs and rhs buffers and evaluate each operand.
  - Chain runs two modules in sequence. Event builds a hold mask from cached rows. SignalCache saves a row and loads it back.
- `tools/generate_modules.py (455)`: masks comments, finds one public `Module`-derived class per header and collects public `IntParameter`/`FloatParameter` fields. It rejects 14 `RESERVED_TAGS` and writes factories that call `validate_keys` and `parse_int`/`parse_float`. A `panel` member is bound when present.
- `tools/build_afl_catalog.py (145)`: maps the AFL dictionary rows (CSV/XLSX) to `afl_<id>` plus readable-code field names. It checks a 6-name `RESERVED` set.
- `tools/run_logic_tests.py (221)`: compiles every `src/*.cpp` except main, parquet_reader and config_loader, plus every test except 3, against pinned gtest, jsoncpp, pugixml and boost. It then runs the combined binary.
- `tools/missing_close_oracle.py (527)`: independent Python book for carry/error missing-close accounting. About half of it is settlement, successor, default-delisting and stale-quote rules.
- `tools/check_missing_data.py (335)`: runs the scenarios through the oracle (`--self-check`) and through isolated servers on synthetic wide data.
- `tools/check_team_sim_parity.py (163)`: execs the team `StatsHandler.run` body from its source file and compares PnL, turnover and counts with an isolated server on 5-bar synthetic data.
- `tools/check_group_neutralize_oracle.py (355)`: compares `<neutralize>` with the team `cs_group_neut` on a real-data server.
- `tools/check_wide_data_oracle.py (120)`: compares `data_load_probe` JSON with a NumPy as-of alignment on the v1 wide cache.
- `examples/datasets/*.yaml` (3 files plus 3 untracked `.bak-20261008`): thin `base:`, a lazy as-of AFL catalog dataset, an interval universe and `end_date: latest`.
- `examples/datasets/README.md (104)`: Korean guide to the loader.
- `modules/README.md (26)`: user module SDK guide.
- `docs/data_contract.md (49)`: current data contract in prose. It has no numbered sections.
- Tests read for branch pinning: `tests/test_config_loader.cpp`, `test_parquet_loader.cpp`, `test_thin_loader.cpp`, `test_thin_moves.cpp`, `test_xml_compiler.cpp`, `test_module_attributes.cpp` (case lists below).

## 1. Flow across the owned files

**Startup:** `BacktestConfig::from_yaml` (config_loader.cpp:144)
1. `parse_base` (:60) reads `manifest.json` (:73-77) and pushes the base datasets (:84-112).
2. User datasets go through `merge_fields` (:26).
3. `DataPanel::load` (data_panel.cpp:59) calls `parquet_reader::load_config` (:681):
   1. It builds the `sources` map (:684-695).
   2. It opens the master `ParquetFile` (close) (:719) and computes the window (:721-733).
   3. It reads the universe with `interval_membership` (:531), then `read_interval_universe` (:546) or `read_universe` (:468).
   4. It expands thin destinations transitively (:767-778), builds the masks (:781-796), and calls `read_field(close)` (:800). The `latest` trim follows (:801-818).
   5. Eager fields go through `read_field`. Lazy fields get a `LazyColumn` keyed by path, columns and alignment (:820-836).
   6. Last come the group check (:841-861), thin `adj_factor`, `read_long_doubles(returns)` and move events (:862-908).

**Compile:** `compile_xml` (dag_builder.cpp:434) → `parse_xml` (xml_parser.cpp:18) → `expand_pipelines` (expansion.cpp:142) → per pipeline `compile_pipeline` (:268) → `parse_expr` (expr_parser.cpp:202) → `ModuleRegistry::default_registry()` (:170). The registry is a lazy static, so its first use is the first request. Then `pipeline_to_xml` (:136) runs and the universe pointer is injected (:444-449).

**User module:** `run_engine.py:94` (`<config dir>/modules`) → CMake `-DLONGSHORT_MODULE_DIR` (:117) → `cmake/DiscoverModules.cmake:38-46` → `generate_modules.py` → `build/generated/engine_discovered_modules.cpp`. That file is called at dag_builder.cpp:258 after the built-ins. Its `.cpp` sources are added to the `engine` target.

**GA:** `extract_search_space` (src/ga/search_space.cpp:128). If `choice` or `shuffle` is present it calls `expand_pipelines` and serializes with its own `element_to_xml`. Otherwise it parses pipes and ranges itself into genes. Each decoded XML goes back through `compile_xml`.

**Decisions made twice or more:**
- execution_price validity: config_loader.cpp:261-262, parquet_reader.cpp:682-683, and main `parse_run_options`.
- `returns` reserved: config_loader.cpp:33-34 and :189-190, parquet_reader.cpp:687-688.
- field-name collisions: user datasets only at config_loader.cpp:196/206; all datasets at parquet_reader.cpp:692.
- `end_date` latest default: config_loader.cpp:247-249 and parquet_reader.cpp:722.
- data delay: dag_builder.cpp:291-305, again at :313-337, plus the `<delay>` factory at :208-213.
- universe mask: the injection at dag_builder.cpp:446-448, then the fallback in each of Rank.h:12, Zscore.h:9 and Neutralize.h:18.
- pipe/range expansion: expansion.cpp:13-33 and search_space.cpp:74-94.
- XML serialization: dag_builder.cpp:120-148 and search_space.cpp:96-124.
- reserved module tags: generate_modules.py:49-51, plus dag_builder.cpp:174-231 and the intercepted tags at :313/340/356.
- reserved field names: build_afl_catalog.py:18 and config_loader `base_dataset` names.
- timestamp unit switch: parquet_reader.cpp:49-53 and :416-420.
- missing_close_policy: config_loader.cpp:282 and main.cpp:886-890.

### 1.1 parquet_reader branch map (brief Q1, Q6, Q7)

Source abbreviations: `ws` = /home/jgo/longshort_workspace/config.yaml, `us` = us_equity.yaml, `mo` = equities_monthly_afl.yaml, `cv` = compare_v2_all.yaml.

| branch (file:line) | exercised by a config | pinned by tests |
|---|---|---|
| thin detection plus base long `base_data`, `returns` and `adj_returns` (config_loader.cpp:84-100; reader long branch :255-279) | ws, us, mo, cv | test_thin_loader (11 cases) |
| nonthin wide `BaseData/*.parquet` (config_loader.cpp:101-105; reader wide exact :235-254) | none | test_parquet_loader:311-457, HTTP fixture (make_http_test_fixture.py) |
| wide as-of (aligned_rows :190) | all 4 (AFL, base_groups) | test_parquet_loader:93, 393 |
| generic `datasets:` without `base:`, long or wide (config_loader.cpp:150-211) | none | test_config_loader:36-89, test_parquet_loader:68-305 |
| lazy field, with aliases sharing one `LazyColumn` (:827-834) | all 4 (1,192 catalog names → 600 files) | test_parquet_loader:93 |
| **eager alias → separate full T×N copy per alias (:829-830)** | none | **none** |
| execution fields forced eager when the dataset is lazy (:829, `execution_fields`) | none (base is eager anyway) | test_parquet_loader:119, 146 |
| `selected_fields` restriction (:700-703, 824-825, 865) | **no production caller** (main.cpp:862 and data_load_probe.cpp:22 pass none) | test_parquet_loader:128-180 |
| interval universe, `name` / `names` (:546-582, 791-796) | ws, us (`names`); mo, cv (`name`, **fail**, see F1) | test_thin_loader:238, 272, 321 |
| wide membership universe (:468-504) | none | test_parquet_loader:72-86, 238-249; check_team_sim_parity / check_missing_data (unrun) |
| `universe.symbols` without a path, or no universe at all (:755-758) | none | test_parquet_loader:175, 289; HTTP fixture `["all"]` |
| `universe.name`/`names` on a non-interval file → error (:740-743) | none | **none** (coverage: 741, 743 not executed) |
| transitive thin destinations (:767-778) | all 4 | test_thin_loader:217 |
| thin event before the panel → retire at bar 0 (:875-884) | all 4 | test_thin_loader:302 |
| **thin event date inside the panel but not a bar → refuse (:889-892)** | none | **none** (coverage: not executed) |
| `latest` tail trim (:801-818) / explicit end date | all 4 / none | test_parquet_loader:231-305 |
| group-panel sanity check (:841-861) | all 4 | test_parquet_loader:345-457 |
| `returns` reserved guard (:687-688) | unreachable via YAML (rejected earlier) | **none** (coverage: 688 not executed) |
| `load_parquet` / 4-arg `DataPanel::load` (:912-922) | none (bench_2000_breakdown is broken; see F6) | test_parquet_loader:199, 217 |
| `symbol_labels_path` (config_loader.cpp:265-269) | none | **none** (coverage: 266-269 not executed) |
| Long dataset with `asof` → reject (config_loader.cpp:178-179) | none | **none** (coverage: 179 not executed) |

Request-time re-alignment does not exist. Panels are built once at startup, and each lazy field loads once on first touch under `cold_read_mutex` (:230-231).

Full-panel copies: one T×N float per field (:233, needed); the duplicated returns buffers (F5); the eager alias copies above; 10 T×N uint8 masks for `names` (:781-796, needed for selectable universes).

## 2. Findings

### F1 [P] Two of the three example configs, and the neutralize oracle's default config, fail at startup on the current cache
- Where: examples/datasets/equities_monthly_afl.yaml:20 (`name: Top500`), compare_v2_all.yaml:25 (`name: ALL`), examples/datasets/README.md:15 ("`universe.name: Top500` 또는 `ALL`"), tools/check_group_neutralize_oracle.py:204 (`DEFAULT_CONFIG` = equities_monthly_afl.yaml). The failure is raised at parquet_reader.cpp:575-576.
- Evidence:
  - The universe names in `/home/base/cache/cache_v2_cash_1d/universe.parquet` (pyarrow `unique`) are `TOP1000, TOP1000_ADR, TOP1000_CAP, TOP2000, TOP2000_ADR, TOP3000, TOP3000_ADR, TOP4000, TOP4000_BUF80, TOP500, TOP500_ADR, TOP500_CAP`. Neither `Top500` nor `ALL` is among them.
  - The comparison is an exact string match (`std::find(wanted…, names[row])`, :560).
  - The config probe (`from_yaml`) accepts both files (`OK … name=Top500`, `OK … name=ALL`). The error therefore only appears after the master close file is decoded (:719, 51,428,272 rows).
  - `us_equity.yaml` and the workspace config use `names: [TOP3000, …]` and load.
- Why it is a problem: the README tells users to copy these YAMLs. Two of them die at startup with "Universe 'Top500' has no rows in …". The oracle cannot run with its defaults.
- Simpler shape: delete `equities_monthly_afl.yaml` and `compare_v2_all.yaml` (us_equity.yaml already covers the thin layout) and drop README:15's name examples.
- Blast radius: 2 YAMLs, README.md:15,17,96, check_group_neutralize_oracle.py:204.
- Test gate: no test loads `examples/`. Missing: `test_example_configs`, which runs `from_yaml` on every `examples/datasets/*.yaml` and checks `universe.name`/`names` against the referenced `universe.parquet` (skip when the cache is absent).
- Effort: S

### F2 [S][T] `<choice bind>` and `<shuffle>` produce wrong variant sets silently; zero tests; the GA inherits them
- Where: expansion.cpp:241-246 (stride), :196-252 (bind), :107-127 (shuffle); search_space.cpp:137-158 (GA delegates here).
- Evidence (probe: expansion.cpp, xml_parser.cpp and search_space.cpp built with a driver):
  - **Three bound choices**, `bind="same"` on `rank|zscore`, `tsmean 5|10` and `decay 1|2`. Output: `<rank/><tsmean days=5/><decay days=1/>` and `<zscore/><tsmean days=10/><decay days=1/>`. The third member is stuck at alt 0, because the stride counts the earlier secondary member, which never multiplied the pipelines.
  - **Bound choices of unequal size (3 vs 2).** Output: 3 variants, with the third pairing `decay 3` with `tsmean 5` (`% N` wraps). There is no size check, although the comment at :182-184 says the sizes "must" match.
  - **`<shuffle><rank pct="true|false"/><tsmean days="5|10"/></shuffle>`.** Output: 2 variants. Only `child_alts[0][0]` (:114) is used, so `pct=false` and `days=10` vanish.
  - **`<shuffle>` over `tsmean 5`, `tsmean 10` and `rank`.** Output: 3 of 6 orders. The comparator compares only the tag (:117-125), so equal tags count as identical.
  - The GA output was identical in every case.
  - The existing tests contain no `shuffle` or `bind=` (grep: 0 matches in tests/). The bind and shuffle lines are not executed in MAP coverage (expansion.cpp uncovered 110-127, 201-254; file at 72.0%).
  - No saved alpha or research file uses `<choice` or `<shuffle` (grep over workspace alphas/ and research/: 0).
- Why it is a problem: users get fewer, or mispaired, alphas with no error. The GA treats the truncated set as the whole space.
- Simpler shape: delete `shuffle` and `bind` (plain `choice` stays). If they are kept, they need tests first.
- Blast radius: expansion.cpp, expansion.h comment, search_space.cpp:162 condition, math_contract.md:181, docs/progress.md:26.
- Test gate: test_xml_compiler `ChoiceExpansion` covers only plain choice. Missing: `XmlCompiler.BoundChoiceThreeMembersStayAligned` and `XmlCompiler.ShuffleKeepsChildVariants`, or deletion tests that assert the tags are rejected.
- Effort: S (delete) / M (fix and pin)

### F3 [P][T] Built-in module factories silently accept typos and malformed values; discovered modules reject them. The reserved-tag list is a hand copy
- Where: dag_builder.cpp:28-33 (`get_int` = bare `stoi`), :105-118 (`get_double`, `get_bool`), :174-231 (factories read only known keys); generate_modules.py:399-409 (`validate_keys` plus strict `parse_int`/`parse_float`); generate_modules.py:49-51 `RESERVED_TAGS` vs dag_builder.cpp:174-231 and :313/:340/:356.
- Evidence (probe linking dag_builder, expansion, expr_parser, xml_parser, data_panel and simd against a 50×2 panel):
  - `<tsmean dayz="5"/>` → OK, days=20 (the default).
  - `<tszscore days="5" ddof="1.9"/>` → OK, ddof=1.
  - `<rank pct="flase"/>` → OK, pct=0.
  - `<tszscore ddof="x"/>` → `std::invalid_argument: stoi` (that is the whole HTTP 400 message, main.cpp:1020-1025).
  - Discovered modules throw `Unknown attribute` / `Invalid value` (module_attributes.h:30-78; test_module_attributes 7 cases).
  - `default_registry()` is a lazy static whose only caller is `compile_pipeline` (:307). A drift between `RESERVED_TAGS` and the C++ registry therefore shows up as "Duplicate module tag" on every request after a successful startup. Drift for `data`, `signal` or `event` would instead leave a module that can never be reached.
  - No test compares the two lists (grep `RESERVED` in tests/test_generate_modules.py: only the assertion at :150-152 that a reserved tag is rejected).
- Why it is a problem: a typo in a GA template (`dayz="5|10|20"`) yields three identical default-20 alphas and no error. The editor shows the typo'd XML as the saved `source_xml`.
- Simpler shape: built-in factories call `module_attributes::validate_keys`/`parse_int` like generated ones (delete `get_int`, `get_double` and `get_bool`), and derive `RESERVED_TAGS` from the registry.
- Blast radius: dag_builder.cpp, generate_modules.py, test_xml_compiler.cpp, test_generate_modules.py.
- Test gate: test_xml_compiler `DaysValidation*` covers only `days`. Missing: `XmlCompiler.BuiltinRejectsUnknownAttribute` and `test_generate_modules.ReservedTagsMatchEngineRegistry`.
- Effort: M

### F4 [S] The run-selected universe reaches only three concrete module types via `dynamic_cast`; discovered modules always see the default universe
- Where: dag_builder.cpp:444-449; Rank.h:12-19, Zscore.h:9-16, Neutralize.h:18-25 (the same 8-line preamble 3×); generate_modules.py:401 (binds `panel` only); modules/README.md:17.
- Evidence:
  - main.cpp:289 resolves `mask_for(options.universe)` and passes it to every `compile_xml` (main.cpp:441, 449, 560, 736, 1095).
  - The only consumers are `MRank`, `MZscore` and `MNeutralize` (:446-448).
  - A user module that declares `const DataPanel* panel` and reads `panel->universe_mask` gets `TOP3000` (the default) when the request selects `TOP500`.
  - The workspace `modules/` directory is currently empty (`ls`: 0 files), so there is no live victim yet.
- Why it is a problem: the selectable-universe feature is wrong for any cross-sectional user module, and the SDK README does not warn about it. Each new built-in cross-sectional module must also be added to the cast chain.
- Simpler shape: pass the run mask to factories next to `panel` (one pointer field), and delete the cast loop and the per-module `universe ? universe : panel->universe_mask` fallback.
- Blast radius: dag_builder.{h,cpp}, Rank.h, Zscore.h, Neutralize.h, generate_modules.py `render`, modules/README.md.
- Test gate: test_thin_loader `SelectableUniverseNamesKeepOneMaskPerName` covers the panel only. Missing: `XmlCompiler.DiscoveredModuleSeesSelectedUniverse` (a generated module in the test_module_build fixture).
- Effort: M

### F5 [I] Thin startup re-decodes the same long files once per field; `returns.parquet` is loaded twice into float and double panels
- Where: config_loader.cpp:85-90 (7 fields from `base_data.parquet`, eager); parquet_reader.cpp:234 (a new `ParquetFile` per `read_field`), :719 (master close), :800 (close again), :830 (each eager field), :868 (`read_long_doubles` on `returns.parquet`), :146-158 (`std::to_string` plus `std::map` emplace per row).
- Evidence:
  - `base_data.parquet` has 51,428,272 rows in 50 row groups. `returns.parquet` has 51,414,052 and `adj_returns.parquet` 50,001,916 (pyarrow metadata).
  - Coordinate decode plus sort runs **8×** for base_data (master + close + open, high, low, volume, vwap, cap), **2×** for returns (field `returns` and `returns_total`), and **1×** for adj_returns.
  - Each decode turns every row's int64 id into a string, emplaces it in a map, and sorts the 51M-entry `order` vector.
  - One pyarrow decode of the two coordinate columns takes 0.4 s to read plus 1.5 s for `lexsort`. The C++ path adds the per-row string and map work.
  - Observed restarts: 14:20:03 → 14:23:15 and 15:19:19 → 15:22:29 (≈190 s each, engine_50007.log:169-175, 184-190), for T=2942 and N=5959.
  - The float `returns` panel is 2942×5959×4 ≈ 70 MB. `returns_total` is the same data at ≈140 MB.
- Why it is a problem: the user chose restart-based refresh, so every cache publish pays this. The returns duplication is roughly 70 MB of resident memory per engine.
- Simpler shape: decode each long file's coordinates once and read all its requested columns in one `ReadRowGroups`; build the float `returns` field from `returns_total`.
- Blast radius: parquet_reader.cpp (read_field, read_long_doubles, load_config), config_loader.cpp:84-100.
- Test gate: test_thin_loader `DetectsLayoutAndLoadsRawPricesAndFactors` and `ReturnsTotalIsDoubleWithNaNForMissingRows` pin values. Missing: a per-file decode-count assertion. Measurement gate: `data_load_probe CONFIG --summary` `eagerLoadMs` before and after.
- Effort: M

### F6 [P] Test-only loader surface: `load_parquet`, the 4-arg `DataPanel::load`, the legacy first-dataset views, `use_columns` bookkeeping and `selected_fields`; their one non-test user is broken
- Where: parquet_reader.h:11 and .cpp:912-922; data_panel.h:70-73 and .cpp:62-65; config_loader.h:34-38 ("Legacy first-dataset views retained for existing callers/configs"); config_loader.cpp:185-216; parquet_reader.cpp:700-703, 824-825, 865 (`selected_fields`); tools/bench_2000_breakdown.cpp:57.
- Evidence:
  - grep: `parquet_path`, `use_columns`, `time_col` and `symbol_col` are read outside config_loader only by bench_2000_breakdown.cpp:57 and test_config_loader (6 hits).
  - `selected_fields` is passed only by test_parquet_loader (:128-180). main.cpp:862 and data_load_probe.cpp:22 pass none.
  - Config probe on us_equity.yaml: `parquet_path=/home/base/cache/cache_v2_cash_1d/base_data.parquet use_columns=1192`. The path comes from `base_prices` and the columns are the AFL catalog names. `load_parquet` then has no `close` and throws "Data configuration must provide close" (parquet_reader.cpp:697). The bench therefore cannot run on any current config.
  - The `returns` guard at :687-688 is reachable only through `load_parquet` (YAML rejects the name earlier) and is not executed in coverage.
- Why it is a problem: about 60 lines plus a public overload exist for one dead benchmark and for tests that could build a `BacktestConfig` directly. `use_col` makes the duplicate-field loop (:202-209) re-scan `use_col` once per field.
- Simpler shape: delete `load_parquet`, the 4-arg `DataPanel::load`, the four legacy view fields, `use_columns` and the `returns` guard at :687. Make tests build a one-dataset config. Delete bench_2000_breakdown or have it call `DataPanel::load(cfg)`.
- Blast radius: parquet_reader.{h,cpp}, data_panel.{h,cpp}, config_loader.{h,cpp}, bench_2000_breakdown.cpp, test_parquet_loader:194-217, test_config_loader:36-48, 202-225.
- Test gate: test_parquet_loader `LegacyLongFileSupportsReorderingSelectionAndAdditionalNumericTypes` and `Ohlc4RequiresExactOhlEvenWithRestrictedLazySelection` must be rewritten on `DataPanel::load(config)`.
- Effort: S–M

### F7 [P] Unknown config keys are silently ignored while four removed keys get dedicated rejection; defaults and validations are written twice
- Where: config_loader.cpp:284-286 (`events`, `corporate_actions`, `default_delisting_return`, `stale_quote_settle_bars`) and :78-83 (thin `returns_total.parquet`). The duplicated defaults are listed under Evidence.
- Evidence:
  - The probe accepted `backtest: {missing_close_polcy: error, executon_price: vwap, mode: 1}` → `OK … policy=carry`.
  - The three `.bak-20261008` files carry `backtest.mode: 1` and load OK.
  - Rejection-only code is 9 of 288 lines. No example, workspace config or `.bak` file contains any rejected key (grep: 0), so those rejections protect only unseen personal configs.
  - Defaults are duplicated between header initializers and `from_yaml` literals: bars_per_year 365 (.h:48, .cpp:259); combined_mode (.h:49, .cpp:264); execution_price (.h:59, .cpp:260); missing_close_policy (.h:50, .cpp:281); fee 0.00015 (.h:42) vs 1.5 bp (.cpp:255).
  - Re-validation: execution_price at parquet_reader.cpp:682-683, which `from_yaml` makes unreachable (coverage: 683 not executed).
  - Reserved field names live in 3 places: config_loader (`returns`), parquet_reader:692 (duplicates against base), and build_afl_catalog.py:18. The catalog's list `{open, high, low, close, volume, returns}` lacks `vwap, cap, sector, industry, adj_returns, adj_factor`. A catalog alias `cap` would pass the tool and fail engine startup.
- Why it is a problem: a misspelt `missing_close_policy` silently changes the accounting policy, and the explicit legacy list catches only the four keys someone remembered.
- Simpler shape: one allowed-key list per YAML block (root, datasets[], universe, backtest, base); delete the 4-key legacy check; use `cfg`'s own member as the fallback (as `initial_capital` already does at :254).
- Blast radius: config_loader.cpp, test_config_loader.cpp `RemovedSettlementAndCorporateActionSettingsAreRejected` (message changes), build_afl_catalog.py:18.
- Test gate: test_config_loader (11 cases). Missing: `ConfigLoader.RejectsUnknownKeys`.
- Effort: S

### F8 [S] The GA re-implements pipe/range parsing and XML serialization with different semantics from the compiler
- Where: search_space.cpp:74-94 (`split` with dedupe, `try_parse_range`, `is_pipe`), :96-124 (`escape_attribute`, `element_to_xml`) vs expansion.cpp:13-33 (`split_pipe` without dedupe, `expand_value`) and dag_builder.cpp:120-148 (`xml_escape`, `pipeline_to_xml`).
- Evidence (probe):
  - `<tsmean days="5|5|10"/>`: the compiler gives 3 variants, two of them identical, so `/api/simulate` runs both. The GA gives 2 genes.
  - `note="it's"`: the compiler escapes `'` as `&apos;` (dag_builder.cpp:129). The GA does not (search_space.cpp:99-104), so a GA candidate string differs from the compiled `source_xml` for the same alpha.
  - Attribute order (`std::map`, sorted) and `\n` joining match.
  - math_contract.md:180 says "Parameter lists are trimmed and deduplicated", which is true only for the GA path.
  - A non-numeric range (`days="a...b"`) yields `std::invalid_argument: stoi` in both paths (range_parser.h:26-33).
- Why it is a problem: there are two behaviours for one notation, and the same XML gets a different alpha count in simulate vs GA.
- Simpler shape: search_space calls `expand_value` (dedupe there) and `pipeline_to_xml` (made non-static); delete `split`, `try_parse_range`, `is_pipe`, `escape_attribute` and `element_to_xml`.
- Blast radius: search_space.cpp, expansion.{h,cpp}, dag_builder.{h,cpp}, test_ga.cpp, test_xml_compiler `PipeExpansion`.
- Test gate: test_xml_compiler `PipeExpansion` and `SourceXmlEscapesAttributes`, plus test_ga. Missing: `XmlCompiler.PipeValuesAreDeduplicated`.
- Effort: S

### F9 [P] `run_logic_tests.py` is documented as a working harness but cannot build the current tree
- Where: tools/run_logic_tests.py:171-191; documented at CLAUDE.md:48, README.md:84 and docs/build_and_validation.md:83.
- Evidence:
  - The exclusions are only `main.cpp`, `parquet_reader.cpp` and `config_loader.cpp` (:179) and three tests (:187).
  - The compiled set includes `src/api/alpha_storage.cpp`, which includes `<arrow/api.h>`, `<parquet/arrow/writer.h>` and `<yaml-cpp/yaml.h>` (:19-22). It also includes `tests/test_alpha_storage.cpp` (Arrow, :4-6) and `tests/test_thin_loader.cpp` (Arrow, :12-14; calls `BacktestConfig::from_yaml` at :204 while config_loader.cpp is excluded).
  - The flags (:119-128, 171-175) carry no Arrow or yaml-cpp include or library. The final link (:191) links only `liblogic_test_dependencies.a`.
  - The last commit touching the file is a63f45f, older than alpha_storage (untracked) and the thin tests.
- Why it is a problem: three docs point people at a validation path that fails at compile or link, and CLAUDE.md tells agents how to report its results.
- Simpler shape: delete run_logic_tests.py and its three doc mentions; CTest is the validation path.
- Blast radius: tools/run_logic_tests.py, CLAUDE.md:48, README.md:84, docs/build_and_validation.md:83.
- Test gate: none. Run confirmation is in §4.
- Effort: S

### F10 [P] Five oracle tools (≈1.5k L) are referenced by nothing; the missing-close oracle still models the removed settlement ledger; defaults point at v1 or missing data
- Where:
  - tools/check_missing_data.py (335) with missing_close_oracle.py (527).
  - check_group_neutralize_oracle.py (355), check_team_sim_parity.py (163), check_wide_data_oracle.py (120).
  - MAP §8 "Referenced by nothing".
- Evidence:
  - `LedgerConfig.default_delisting_return` and `stale_quote_settle_bars` (missing_close_oracle.py:46-47), the settlement, successor and cash-leg logic (:201-273, 311-411), and "settlementLog contract" (:126-127) model keys and request fields that the engine rejects (config_loader.cpp:284-286; main.cpp:278-281). The only caller passes no settlement arrays (check_missing_data.py:61), so those branches never run.
  - check_group_neutralize_oracle defaults: `DEFAULT_GROUPS = REPO/data/groups/cache_v1_cash_1d` (:205, `ls`: missing), `DEFAULT_CACHE` v1 (:206, compared against a thin-v2 engine), and a `DEFAULT_CONFIG` that fails (F1).
  - check_wide_data_oracle runs only against v1 `BaseData`, `TopUniverseV1` and `AflData_M` (:44, 54-55, 85).
  - check_team_sim_parity still matches the API (`nonzeroCount` json_serializer.cpp:91; "invalid close" simulator.cpp:285), and its oracle source `/home/base/sim/utils` exists.
  - On the brief's duplication question: these are deliberately independent re-implementations (correct in principle), but no CTest, doc or CI runs them.
- Why it is a problem: they rot unseen, and readers cannot tell which oracle is current.
- Simpler shape: register the synthetic ones (`check_missing_data --self-check --server`, `check_team_sim_parity`) as CTest with a skip code, delete the rest, and strip the settlement branches from the oracle.
- Blast radius: tools/*.py above, CMakeLists.txt test section.
- Test gate: none today.
- Effort: S

### F11 [P] Dangling references after the `tools/data` deletion and symlink, and after the docs purge
- Where:
  - `tools/data`: 89 tracked files deleted and replaced by the untracked symlink `tools/data -> ../../../universe_builder/cache`.
  - CMakeLists.txt:228-231 with tests/test_build_group_panels.py:23.
  - parquet_reader.cpp:633 (`tools/data/cache_v21/loader.py`) and :858 (`tools/data/build_group_panels.py`).
  - examples/datasets/README.md:45 (README_group_panels.md link) and :102 (`../../../../longshort/notebooks/data_and_pnl_contract.ipynb`).
  - CLAUDE.md:31 and docs/data_contract.md:19 (`build_events.py`); config_loader.cpp:83 (`thin_build.py`).
  - compare_v2_all.yaml:2,8 and equities_monthly_afl.yaml:3 ("archived in the cache plan").
  - Section citations "docs/data_contract.md 4.3 / §6 / 4.4 f": config_loader.h:53, config_loader.cpp:45, data_panel.h:44, test_config_loader.cpp:165, test_parquet_loader.cpp:310, 392, test_http_contract.cpp:1103.
- Evidence:
  - `git ls-files | grep -c tools/data/` = 89, all ` D`.
  - The symlink target lies outside the repo, so on any other checkout CTest `test_build_group_panels` hits `FileNotFoundError` from `spec_from_file_location`, which is not the skip code 77.
  - The README:102 target is MISSING (`[ -e ]`), while data_contract.md:3 links the right path in `longshort_workspace/research/notebooks/`.
  - `thin_build.py` exists only at `/home/jgo/universe_builder/cache/cache_v21/thin_build.py`.
  - The deleted docs include `backtest_cache_simplification_plan_2026-09-23.md` and `cache_v2_plan_2026-09-14.md`.
  - `docs/data_contract.md` (49 L) has no numbered sections (headings: Thin 캐시 / 일반 long과 wide 데이터 / 유니버스와 그룹 / 손익 체결과 기간).
- Why it is a problem: a fresh clone has a failing CTest, dead links, and error messages naming files that are not there.
- Simpler shape: move `test_build_group_panels.py` to universe_builder with its tool, cite absolute universe_builder paths, and drop section numbers from the comments.
- Blast radius: CMakeLists.txt, tests/test_build_group_panels.py, parquet_reader.cpp (2 strings), config_loader.cpp:83, README.md, CLAUDE.md, data_contract.md, 2 YAMLs, 7 comment lines.
- Test gate: CTest `test_build_group_panels` (currently passes only through the local symlink).
- Effort: S

### F12 [P] Stale labels and build artifacts in `examples/datasets`
- Where: compare_v2_all.yaml:1,7; equities_monthly_afl.yaml:1 and file name; README.md:17, 23, 27-28, 50-55, 71-72, 96; `.bak-20261008` ×3; `.longshort/` and `uploads/` inside the examples directory.
- Evidence:
  - "thin-v1 rc2f3 cache" while the manifest is `build_id 2026-09-15`, `source_build_id v21_20261008_062532Z`.
  - "returns_total on that date" names a file that config_loader now rejects.
  - "monthly AFL / `AflData_M`" while the path is `cache_v2_cash_1d/AflData` (601 files; manifest `afl_build … last_date=2026-09-15`).
  - The README base example points at the v1 cache and `../../../data/groups/cache_v1_cash_1d` (missing).
  - The catalog command uses v1 `AflData_M`. "기본 9개 필드" vs 12 thin fields. The memory table ("Top500 1,056×4,449") does not match the running panel (T=2942, N=5959).
  - The `.bak` files are untracked and referenced by nothing.
  - `examples/datasets/.longshort/` is 70 MB (gitignored) and `uploads/tmp/` holds 256 empty directories (not ignored).
- Why it is a problem: the examples are the onboarding path, and every stale line costs the next reader an investigation.
- Simpler shape: keep one example YAML (us_equity) with comments derived from its manifest; delete the `.bak` files and artifacts; cut README sections that describe v1.
- Blast radius: examples/datasets/*.
- Test gate: `test_example_configs` (from F1).
- Effort: S

### F13 [P] Checkpoint boilerplate repeated in 9 modules with no runtime caller; boost serialization reaches every TU
- Where: Chain.h:17-26, Decay.h:41-48, Delay.h:15-22, Event.h:28-35, Ops.h:15-24, SignalCache.h:15-22, TsMean.h:41-48, TsRank.h:50-57, TsZscore.h:63-70; module.h:6-8, 113-140, 157-158.
- Evidence: the block below (fields vary) appears in 9 of 14 module headers (`grep -c "void checkpoint(boost"` = 2 each):
  ```
  void checkpoint(boost::archive::binary_iarchive &ar) { ar & input & running_sum & running_count & bar_count; }
  void checkpoint(boost::archive::binary_oarchive &ar) { ar & input & running_sum & running_count & bar_count; }
  ```
  - `CheckpointManager` has no src caller (MAP §1.2), so these overrides are also absent from coverage (MAP §6).
  - My compile probe of dag_builder needed `-lboost_serialization` to link.
  - ModulePrelude.h:3 says "Module .h files must NOT contain any #include", but Rank.h:2, Zscore.h:2 and Neutralize.h:2 include `../data/data_panel.h` (harmless, since it is force-included).
- Why it is a problem: about 75 lines across 9 headers, a boost dependency, and a legacy wire format, all for a test-only feature. New modules copy the pattern.
- Simpler shape: delete the `checkpoint` overrides, `Module::checkpoint` and `ModuleInput::serialize`. CheckpointManager and test_checkpoint go with them, which is E2's decision.
- Blast radius: 9 module headers, module.h, checkpoint_manager.*, test_checkpoint.cpp, tests/fixtures/module_input_legacy_v0.bin, CMake boost dependency.
- Test gate: test_checkpoint (6 cases) is the only user.
- Effort: M

### F14 [P] Duplicated Arrow plumbing inside parquet_reader.cpp
- Where:
  - Parquet open, build and schema boilerplate 3×: :115-124, :511-520, :532-539.
  - Timestamp unit switch 2×: :49-53 and :416-420.
  - Five readers with the same null and type switch: `read_event_ids` :300, `read_optional_ids` :325, `read_flags` :352, `read_doubles` :377, `read_strings` :447.
  - `read_long_doubles` :604-631 duplicates the long branch of `read_field` :255-279, differing only in value type.
  - `trimmed` :294 is a 4th trim helper (also range_parser.h:9, dag_builder.cpp:47, module_attributes.h:13). `iso_date` :286 is a 4th date formatter (MAP §3.1 lists 3).
- Evidence: line ranges above; `interval_membership` (:531) opens the universe file only to read its schema, then `read_interval_universe` (:551) opens it again.
- Why it is a problem: about 200 of 923 lines are near-copies, and any type-support change has to be made five times.
- Simpler shape: one `open_reader(path)`, one `time_value_of(chunk, i)`, and `read_field` templated on float or double (delete `read_long_doubles`).
- Blast radius: parquet_reader.cpp only.
- Test gate: test_parquet_loader `ConvertsTimestampUnitsExactly…` and `RejectsDuplicateCoordinates…`, plus test_thin_loader.
- Effort: M

### F15 [P] dag_builder copies the first `data` op handling for the chain-restart `data` op
- Where: dag_builder.cpp:278-305 (first op: `expr` default plus `delay` → `MDelay`) and :313-337 (identical code inside the loop).
- Evidence: the two blocks match line for line except `data_chain_count`, including `parse_bounded_int("data","delay",…)` and the `d != 0` check. The `<delay>` factory (:208-213) is a third MDelay construction path with its own default (days=1).
- Why it is a problem: the delay rule ("explicit in XML only") is implemented three times, and only one place would get fixed.
- Simpler shape: keep the `ops[0] == "data"` check and handle every `data` op in the loop.
- Blast radius: dag_builder.cpp.
- Test gate: test_xml_compiler `DataDelayValidation*`, `MultiDataCompiles`, `EventWithoutSignalRejectedAfterDataRestart`.
- Effort: S

### F16 [T] Loader branches with no test and no config
- Where: parquet_reader.cpp:889-892 (thin event date inside the panel but not a bar → refuse), :740-743 (`universe.name`/`names` on a wide file), :829-830 (eager alias copies); config_loader.cpp:265-269 (`symbol_labels_path`), :178-179 (long `asof` reject).
- Evidence: none of these lines is executed in MAP coverage (`coverage_full.json`: parquet_reader uncovered 741, 743, 889-892; config_loader uncovered 179, 266-269). No example or workspace config sets `symbol_labels_path`.
- Why it is a problem: :889-892 is the guard that keeps a dropped corporate-action move from freezing a position silently, and it is the only one of these that matters for P&L.
- Simpler shape: pin :889-892 with a test. Delete `symbol_labels_path` (and its main.cpp branch), since thin master labels cover the only live layout.
- Blast radius: test_thin_loader.cpp, config_loader.cpp, main.cpp:864-879 (E1).
- Test gate: missing `ThinLoader.EventDateInsidePanelButNotABarIsRefused`.
- Effort: S

## 3. Looks odd but is fine

- **Rejection code in config_loader** is only 9 lines (:78-83, :284-286). It is cheap; F7 covers the real gap, which is unknown keys.
- **`cold_read_mutex` around every `read_field`** (:230): it bounds transient decode memory, since `set_use_threads(false)` is used and one `ParquetFile` of base_data holds about 1.6 GB of coordinate arrays during the ctor. Lazy loads during requests serialize, but each field loads once.
- **Lazy alias sharing key** (:827-828) includes path, time_col, symbol_col, long column and alignment. Two aliases with different alignment correctly get different buffers.
- **as-of only for wide datasets** (config_loader.cpp:178): the long `read_field` branch ignores alignment by construction, so the config rejection is the guard, not a duplicate.
- **`latest` trim** resizes `universe_mask` and every named mask (:816-817), and lazy coordinates are created after the trim (:820), so no lazy field can be longer than the panel.
- **Thin destinations loaded with the pre-trim window** (:773 uses `panel.timestamps` before :801): this loads a few extra symbols at worst, and move events use the post-trim range (:885-893).
- **Group sanity check after lazy registration** (:841-861): it checks only eager `sector`/`industry`, which base always provides eagerly.
- **The `neutralize` reserved tag** is not a special case. It is one of the 8 built-in registry tags and sits in `RESERVED_TAGS` like the others; it adds only stricter `group` validation (dag_builder.cpp:231-256).
- **`expr_parser` `returns` check** `thin_layout || has_column("returns")` (:183-185): the second disjunct is redundant today, but harmless.
- **The independent Python oracles re-implement C++ logic**: this is intended (a second implementation), and F10 is about liveness, not duplication.
- **`build_afl_catalog.py`**: its output matches the shipped catalog. The engine catalog and the cache copy are identical (1,192 names → 600 files; `fields` equal). README:57's "600개 팩터의 1,192개 이름" is correct.
- **Module discovery CMake** (`DiscoverModules.cmake`): it regenerates only when the content changes and mirrors the generator's hidden-path rule (:25-35). This is fine.

## 4. Unverified

- **run_logic_tests.py failure mode** (F9): inferred from includes and flags, not run. It needs the pinned `--deps-dir`; running it with those would confirm.
- **Share of the 190 s restart caused by the repeated decode** (F5): not measured per field. `data_load_probe CONFIG --summary` on a copy of the workspace config with base fields only, vs a single-decode prototype, would confirm. Running it reads about 2 GB of shared data, so I did not.
- **GA candidate XML vs `source_xml` mismatch with `'`** (F8): whether `payloads` (main.cpp:1101-1125) or the frontend key on the GA string is not traced (E1/F scope).
- **README memory table for ALL (22,871×6,713)**: not checked against the current cache.
- **check_wide_data_oracle.py still passing on the v1 cache**: needs a `data_load_probe` build to run.

## 5. Top 5

1. **F1**: two of three shipped examples (and an oracle default) die at startup today. The fix is a deletion plus one README line: maximum impact per minute.
2. **F2**: `bind` and `shuffle` silently drop or mispair alphas in both simulate and GA with zero tests. Deleting them is S effort and removes a wrong-answer path.
3. **F3**: typos in built-in XML attributes (`dayz`, `ddof="1.9"`, `pct="flase"`) silently run defaults. This hits every GA template, and reusing the strict parser that already exists for discovered modules is a moderate change.
4. **F7**: misspelt config keys (e.g. `missing_close_polcy`) silently change accounting. One allowed-key list per block also deletes the ad-hoc legacy rejection; S effort.
5. **F5**: about 190 s per restart, with 8× re-decode of a 51M-row file and a duplicated returns panel. Restart is the user's chosen refresh path, so every cache publish pays it. It is M effort with good existing value tests.

Next in line: F4 (selected universe invisible to discovered modules; latent until someone writes one), then F6/F11 (deletions).