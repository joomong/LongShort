## audit_e2 summary

I could not save the report to `refactor_audit/FINDINGS_E2.md`: the Write tool refused with "Subagents should return findings as text", so the full report is below this summary. I built in scratch only for the timing question, then deleted the build and the fixture. The scratch dir is 56 KB (logs, probe sources and outputs), and the root fs is unchanged at 29G used, 67%.

**Top 5:**
1. **F1 — money bug on close execution.** With close fills, a successor stock that lands on a name with no quote skips the frozen budget and the Error policy. The probe shows gross 1.5, `frozen_gross` 0, and no throw under Error; OHLC4 correctly gives gross 1.0 and throws. Cause: `frozen_gross` and the Error gate are worked out before the transfer, and only the OHLC4/VWAP branch redoes them afterwards.
2. **F2 — GA search space is a second XML expander and serializer.**
   - It dedupes pipe values and the compiler doesn't: `5|5|10` gives 3 alphas in simulate and 2 in GA.
   - The two serializers escape differently (`'` is escaped only by the compiler's serializer).
   - Adding one `<choice>` makes a grid GA can otherwise handle hit the compiler's 5000 cap and throw.
   - The comments in search_space are stale and line 168 is dead.
3. **F3 — the ×100 unit conversion happens in three places.** The API mixes percentage points and fractions, the frontend compensates field by field, and fitness has to undo /100.
4. **F5 — `threads` has no cap on /api/simulate** (up to 5000 OS threads per request) and three different defaults (hardware, 20, min(hardware, 20)). There is no shared thread pool.
5. **F6 — `run_logic_tests.py` is a stale second build system.** It compiles `test_thin_loader` but leaves out the config and parquet loader sources it calls, so it should fail to link. I inferred this from the source and git dates (harness last changed 09-11, that test added 09-24), not by running it.

**Other findings:**
- **F7:** dead GA cache and entry points.
- **F8:** `bench_2000_breakdown` aborts on every `base:` config.
- **F9:** `execution_intent()` is left over from the removed execution mode.
- **F10:** 3 of the 6 bitwise-golden cases are skipped.
- **F11:** the checkpoint code is dead and no longer matches the simulator state.
- **F12:** combined portfolio P&L is never pinned numerically.
- **F13:** `SimulatorConfig.T/N` and `bars_per_year` are duplicated.
- **F14:** the loop that drives the modules is written twice.

**Timing (Q1):**
- **Synthetic real scale** (2700×5200, in-memory):
  - The portfolio book costs about 85–90 ms per run.
  - Adding the rank module brings it to about 600 ms.
  - Metrics take 0.1 ms and compiling 0.02 ms.
- **Fixture:** a run takes 0.185 ms.
- **Memory:** about 0.5–0.84 MB per GA candidate thread. No bars×symbols buffer is allocated per candidate; the bars×symbols float matrix (52–54 MB) exists only when signals are collected.

---

# FINDINGS_E2 — engine compute core: simulator, metrics, GA

Agent: audit_e2. Working tree of `/home/jgo/longshort_platform/cpp-engine` as of 2026-10-14, read-only. Paths are relative to `cpp-engine/` unless absolute.

Scratch evidence in `refactor_audit/audit_e2/`:
- `probe/probe.cpp`, `probe/threads.cpp` (probe sources)
- `probe_out.txt`, `probe_out2.txt`, `threads_out.txt`, `bench_out.txt`, `stress_out.txt`, `tests_out.txt`
- `configure.log`, `build.log`

The build directory and the fixture were deleted; the scratch dir is now 56 KB. MAP references are written `MAP_engine.md §n`.

**Build used for the timing question:**
- Release, `LONGSHORT_BUILD_TESTS=ON`, with the PREFIX/UUID/PYARROW options from MAP §5/§6.
- `FETCHCONTENT_SOURCE_DIR_*` pointed at the already-fetched `scratchpad/xmlnl_build/_deps/*-src`. The copy at `/tmp/longshort_refactor_deps/drogon-1.9.8` has no LICENSE file, and CPack aborts on that.
- Targets built: `engine`, `test_simulator`, `test_extreme_alpha_stress`, `bench_2000_breakdown`, `test_ga`, `test_determinism`, with `nice -n 10 … -j8`.
- The two probes were compiled with g++ against that `libengine.a`.
- They ran on the HTTP fixture (`tools/make_http_test_fixture.py --output-dir audit_e2/fixture`) and on an in-memory synthetic panel.
- No real workspace data was used and no server was started.

## 0. Scope read

**Source files**
- `src/engine/simulator.h (99)`: declares the config, output, accounting-view and diagnostics structs and `Simulator` with three entry points: `run(pipeline)`, `run_signal_rows`, `run_from_signals`.
- `src/engine/simulator.cpp (577)`: `PortfolioSimulation` holds a fixed-notional book of about 13 N-vectors.
  - Per bar: moves/carry → mask and L1-normalize targets → price gates → holding P&L (close/mark, or carried × returns_total) → successor transfers → execution at close/OHLC4/VWAP with a frozen-budget scale → per-bar outputs.
  - `normalize_signal_row` is the shared target rule.
  - The constructor validates dimensions, columns and thin move events.
- `src/metrics/metrics_engine.{h,cpp} (36+50)`: finds the IS and OS start indices with `lower_bound`, clamps them to `start_index`, then calls timeseries (net and gross), yearly (gross), and summary twice (IS, then OS) on gross returns.
- `src/metrics/summary_metrics.{h,cpp} (49+243)`: 17 statistics over a [start, end) slice. Returns are filtered to finite values and copied, then about 12 separate loops run over the slices. Sharpe is written out three times (full, last 400, last 80).
- `src/metrics/yearly_metrics.{h,cpp} (29+136)`: groups bar indices by UTC year in a `std::map<int, vector<size_t>>`. Per year: additive PnL and drawdown, Sharpe and IR, win rate, turnover, margin, WorldQuant-style fitness, long/short means. Some values are already in percentage points.
- `src/metrics/timeseries_output.{h,cpp} (27+71)`: one pass producing cumulative net and gross PnL, gross drawdown, turnover × 100, max weight × 100, position count, and an optional copy of frozen_gross.
- `src/ga/ga_engine.{h,cpp} (82+153)`: round loop.
  - Enumerates exhaustively when the remainder fits; otherwise random plus tournament/crossover/mutation.
  - Dedupes by XML in a cumulative `scheduled` set.
  - Evaluates through `ParallelEvaluator`, ranks parents in planned order.
  - Stops on exhaustion, stop_alphas, or the round limit.
- `src/ga/parallel_evaluator.{h,cpp} (44+124)`: per call it checks a global XML→result cache, dedupes within the batch, creates `min(n, work)` std::threads that pull the next index, and runs completion callbacks on the calling thread via a deque and condition variable. Cancellation and errors are rethrown.
- `src/ga/search_space.{h,cpp} (25+213)`:
  - If a top-level `<choice>` or `<shuffle>` exists, the whole `expand_pipelines` output becomes one "pipeline" gene.
  - Otherwise each pipe or range attribute becomes a gene; the template gets `__GENE_n__` placeholders, and `decode` substitutes strings.
  - Has its own escaping and serializer.

**Tests**
- `tests/test_simulator.cpp (901)`: hand-calculated timing, fee and missing-close cases; generated-vs-replay equality; bitwise golden dump from HEAD 2e77af1 (3 of 6 cases skipped).
- `tests/test_thin_moves.cpp (252)`: hand-calculated thin cases with close execution only: ordinary bar, split, dividend, spinoff, cash/stock/mixed merger, rights, same-bar chain, resumption, and a settlement bar without a quote.
- `tests/test_execution_price.cpp (597)`: hand-calculated OHLC4 and VWAP legs; missing, corrupt and inconsistent open/high/low; successor transfers with intraday fills; the fixed-notional convention.
- `tests/test_metrics_oracle.cpp (469)`: a C++ mirror oracle of the summary formulas (same formulas, including the 1e-12 epsilon) on 140 bars; IS/OS slicing; gross vs net; hand-calculated drawdown and warmup cases.
- `tests/test_summary_metrics.cpp (163)` and `tests/test_yearly_metrics.cpp (219)`: hand values for the summary formulas and for yearly IR, fitness, PnL, turnover and margin.
- `tests/test_golden_crossval.cpp` (metric part, lines 330-400): `compute_summary` vs numpy `golden/metrics.json` (generate_golden.py:257-400), on the same 140-bar input as the C++ oracle.
- `tests/test_ga.cpp (267)` and `tests/test_determinism.cpp (100)`: search-space extraction and decode format, round sizes and limits, serial vs 20 threads, evaluator scheduling, errors and cache. All use synthetic objectives.
- `tests/test_combined_alpha.cpp (397)`: accumulator order exactness across worker counts, row vs block adds, normalized vs raw weights, cancellation, overflow, and signal-only vs `run` signals. Two tests sum the signals with their own loop.
- `tests/test_extreme_alpha_stress.cpp (148)`: compiles and simulates 2000 delay variants on 2048×8; checks only that results are finite.
- `tests/test_pipeline_contract.cpp (330)`: compile/run/plot smoke tests plus a neutralize group-sum check; most asserts are `>0` or `<=`.
- `tests/test_checkpoint.cpp (321)`: round-trips SimulatorState, a counter module and the ring buffer through Boost archives, including a legacy v0 fixture. "RoundTrip" never resumes anything.

**Tools and docs**
- `tools/bench_2000_breakdown.cpp (159)`: legacy 4-argument `DataPanel::load`, then compile, simulate, metrics and JSON for 2000 alphas with timers.
- `tools/run_logic_tests.py (221)`: a hand-written build of all `src/*.cpp` except 3 and all `tests/test_*.cpp` except 3 into one gtest binary, against separately pinned dependency sources.
- `docs/math_contract.md (229)`:
  - Conventions: full-book gross, additive returns, fees, OHLC4/VWAP legs, carry.
  - Summary, yearly and IS/OS formulas.
  - The GA/stream contract.

## 1. Flow across the owned files

**Per-bar loop** (`PortfolioSimulation::advance`, simulator.cpp:74-134):
1. Read the close row (75); `trading` = di ≥ sim_start (77).
2. Book carry:
   - Thin layout: `apply_moves` (145-192) classifies quotes, retires ending sources and books their returns_total, and queues successor and receipt weights.
   - Wide layout: `prepare_book` (232-237) copies previous weights and classifies quotes.
3. `normalize_intent` (251-262): applies the NaN mask for retired names, then `normalize_signal_row` (449-476): universe mask ∧ (Error policy: valid close) ∧ finite → L1-normalize into `targets_`.
4. If trading:
   - `require_priced_quotes` (266-274) rejects corrupt prices.
   - `require_tradable_quotes` (277-288, Error policy) rejects missing ones.
   - Both check the pre-transfer `carried_`.
5. Holding P&L:
   - `value_holdings_thin` (196-219): carried × returns_total.
   - `value_holdings` (292-318): carried × (close/mark − 1).
   - A missing quote freezes the position and adds |carried| to `frozen_gross`.
6. `apply_successor_transfers` (222-229) adds ending-move weight to `carried_`.
7. `execute` (371-412):
   - For OHLC4/VWAP, `prepare_execution_prices` (323-367) **resets and recomputes** `frozen_gross`, after the transfers.
   - Budget scale = 1 − frozen.
   - Blocked names keep their carried weight.
   - Turnover = Σ|trade|; execution leg = Σ trade × (close/fill − 1).
8. Receipts join after execution (240-247).
9. Outputs: gross = holding + leg; net = gross − turnover × fee (107-111); max/long/short/count (113-125). Warmup bars zero the weights (127).
10. Diagnostics are written every bar (129), then the optional accounting row (414-422).

**Entry paths:**
- `Simulator::run` (525-549) runs the modules and then `advance` each bar. Used by:
  - single simulate;
  - each alpha of a multi-alpha simulate (main.cpp:505);
  - each GA candidate (main.cpp:1097);
  - the GA best (main.cpp:1167);
  - top-k replays (main.cpp:561).
- `run_signal_rows` (551-564) **re-implements the module loop without a book**; used for combinedOnly (main.cpp:503).
- `run_from_signals` (566-577) is the combined replay (main.cpp:573).
- All three share `PortfolioSimulation::advance` for accounting. Only the module-driving loop is duplicated (F14).
- Combined rows go through `OrderedSignalAccumulator::add_block` (combined_signals.h:58-95), which in normalized mode calls `normalize_signal_row` a second time per alpha row. That is by design, but it skips the retired-name masking (§4).

**Metrics:**
- main.cpp:319-328 copies `frozen_gross` (length T) and calls `compute_full_metrics` (metrics_engine.cpp:5-50).
- That calls `compute_timeseries` (net and gross), `compute_yearly` (gross), and `compute_summary_range` (gross) for IS and then OS.
- Sharpe is implemented 4 times and drawdown 3 times (F4); the ×100 unit conversion happens in 3 places (F3).

**GA candidate lifecycle:**
1. `extract_search_space` (search_space.cpp:128-213) builds the genes.
2. `run_impl` (ga_engine.cpp:58-153), per round:
   - target size (82-84);
   - enumerated (70-78, 99-101) or random/genetic (109-124) candidates;
   - dedupe against `scheduled` (89).
3. `evaluate_results` (parallel_evaluator.cpp:18-124) **dedupes again** through its cache and `seen` (24-37, F7) and creates the round's threads (50-56).
4. The objective (main.cpp:1093-1110): `compile_xml` → `execute_alpha(collect=false)` → `full_metrics` → `is_summary.sharpe` (gross IS Sharpe). With streamCandidates, the summary, yearly and missing-data JSON are parked in `payloads` (1101-1107).
5. Completion → `events.candidate` (1114-1137), on the controller thread.
6. Parents are selected in planned order (138-145); stop conditions are at 148-151.
7. Without streaming, the best XML is recompiled and re-run with signal collection (1165-1191).

**Decisions made twice:**
- Frozen budget and Error gate: steps 4, 5 and 7 (F1).
- XML pipe/range expansion and serialization: expansion/dag_builder vs search_space (F2).
- Dedupe: ga_engine vs parallel_evaluator (F7).
- Execution-price string→enum mapping: main.cpp:304-305 and bench:75-76.
- Availability of the open/high/low or VWAP columns: main.cpp:50-51 and simulator.cpp:500-507.
- Quote classification: prepare_book/apply_moves vs normalize_signal_row.
- Thread-count defaults (F5).
- `bars_per_year` default declared 5 times (F13).

## 2. Findings

### F1 [S] With close execution, successor stock landing on a name with no quote ignores the frozen budget: book reaches gross 1.5, and the Error policy does not throw
- **Where:**
  - simulator.cpp:93-104 (order of steps).
  - 196-219 and 292-318: `frozen_gross` is summed from the pre-transfer `carried_`.
  - 222-229: the transfer.
  - 266-288: the Error gate checks the pre-transfer `carried_`.
  - 380-388: the budget scale.
  - 323-367: only the OHLC4/VWAP branch recomputes frozen after the transfer (its own comment says "including successor stock that arrived before execution").
- **Evidence:** `probe.cpp successor_probe`: thin layout, N=3, A→B move ends at bar 1 (ratio 0.5), A and B have NaN close on bar 1, target C=1. Output from `probe_out.txt`:
  ```
  successor mode=close policy=carry weights=[0.000 0.500 1.000] gross=1.500 frozen_gross=0.000 missing_held=0 blocked=1
  successor mode=close policy=error weights=[0.000 0.500 1.000] gross=1.500 frozen_gross=0.000 missing_held=0 blocked=1
  successor mode=ohlc4 policy=carry weights=[0.000 0.500 0.500] gross=1.000 frozen_gross=0.500 missing_held=0 blocked=1
  successor mode=ohlc4 policy=error threw: Cannot execute: missing or inconsistent OHLC4 price at bar 1, asset 1
  ```
  The OHLC4 behaviour is pinned by test_execution_price.cpp:402-416; there is no close-execution analogue.
- **Why it matters:**
  - With close execution (`execution_price: close` in us_equity.yaml, also the config default), the book breaks the gross ≤ 1 promise in math_contract.md:17.
  - The diagnostics under-report frozen exposure.
  - The Error policy silently freezes instead of rejecting.
  - Root cause: frozen and the Error check are computed twice (before and after transfers), and only the intraday branch redoes them.
- **Simpler shape:** compute can-trade, `frozen_gross` and the Error gate once, after `apply_successor_transfers`, for all three prices (for close, fill-available = close observed). Delete the frozen accumulation from both `value_holdings*`.
- **Blast radius:** simulator.cpp; test_thin_moves, test_execution_price, test_simulator (the frozen_gross pins at 396-404, 451-466 and 556-579 must stay unchanged).
- **Test gate:**
  - Existing MissingDataAccounting.* and ExecutionPrice.Missing* tests guard the intraday and wide paths.
  - **Missing:** `ThinMoves.SuccessorIntoQuotelessDestinationReservesBudget` (carry: weights [0, .5, .5]; error: throws).
- **Effort:** M.

### F2 [S] The GA search space is a second XML expander and serializer beside the compiler, and the two disagree
- **Where:**
  - search_space.cpp:74-83 `split` trims and **dedupes**; expansion.cpp:13-20 `split_pipe` trims only.
  - Range parsing: search_space.cpp:87-89/174 vs expansion.cpp:24-33.
  - Escaping: search_space.cpp:96-105 `escape_attribute` handles 4 characters; dag_builder.cpp:119-133 `xml_escape` handles 5, including `'`→`&apos;`.
  - Serialization: search_space.cpp:109-124 `element_to_xml` serializes children; dag_builder.cpp:135-148 `pipeline_to_xml` emits none.
  - search_space.cpp:137-158 delegates choice/shuffle to `expand_pipelines`, including its 5000 cap.
- **Evidence:**
  - `probe_out.txt`: `expansion '5|5|10': compiler pipelines=3 ga_combinations=2`. /api/simulate makes two identical alphas, so the combined portfolio counts one twice; GA evaluates it once.
  - `choice+grid (81x80=6480 pipelines) GA extract threw: Expansion produced more than 5000 variants` vs `no-choice grid 80x80: genes=2 combos=6400 (factored)`. Adding one `<choice>` turns a factored GA space into a capped full expansion.
  - Stale comments: search_space.cpp:68 "(choice/shuffle: not yet supported; treated as literal)" and search_space.h:22 "Each expansion point (pipe, range, choice) becomes a Gene" contradict lines 137-158.
  - Line 168 (`if (elem.tag == "choice" ...) continue;`) is unreachable because 137-158 already returned.
  - `decode` with an empty template returns "" (32-35).
- **Why it matters:**
  - The same template means different alpha sets in /api/simulate and /api/ga/stream.
  - A GA candidate's XML text can differ from the compiler's `source_xml` for the same alpha (any `'`), and the frontend keys rows by XML string.
  - Every grammar change has to be made twice.
- **Simpler shape:** export `expand_value` (expansion.cpp) and `pipeline_to_xml` (dag_builder.cpp); build genes from `expand_value`, serialize candidates with `pipeline_to_xml`, and delete `split`, `try_parse_range`, `escape_attribute` and `element_to_xml` from search_space.cpp. Choose one dedupe rule for both paths.
- **Blast radius:** search_space.cpp, expansion.{h,cpp}, dag_builder.{h,cpp}; test_ga (`SearchSpaceDecodePutsOneModulePerLine` pins the exact text), test_xml_compiler.
- **Test gate:**
  - test_ga.cpp:52-61 and 212-221 pin decode and dedupe.
  - **Missing:** `GA.CandidateXmlEqualsCompiledSourceXml` (template with `'`, `5|5|10`, and a choice) and `XmlCompiler.PipeDuplicatesPolicy`.
- **Effort:** M.

### F3 [P] The ×100 unit conversion happens in three places, and the API mixes percentage points and fractions
- **Where:**
  - Summary: converted in the serializer (json_serializer.cpp:18-35).
  - Yearly: converted inside compute.
    - yearly_metrics.cpp:67-69: `pnl_pct`, `ann_ret` and `max_dd` ×100; line 94: `win_lose` ×100.
    - `daily_tvr` (105), `margin` (109), `long_sm`/`short_sm` (128-129) and `ir` stay fractions.
  - Timeseries: converted inside compute.
    - timeseries_output.cpp:61 `turnover_pct` and :63 `max_weight_pct` ×100.
    - `cum_pnl`, `cum_pnl_gross` and `drawdown` stay fractions.
- **Evidence:**
  - The frontend compensates field by field: frontend/components/visualization/DetailSummaryTable.tsx:50 `(r.dailyTvr * 100).toFixed(2)` vs BacktestMetrics.tsx:68 `isMetrics.turnover.toFixed(1) + '%'`.
  - Fitness has to undo the conversion (yearly_metrics.cpp:113-114 `ann_ret / 100.0`), and math_contract.md:112-113 has to warn "convert it to a fraction exactly once".
  - Summary `mdd` is in percentage points, yearly `maxDD` is in percentage points, timeseries `drawdown` is a fraction.
- **Why it matters:** both sides need a per-field unit lookup. Likely bugs: ×100 applied twice or not at all (fitness already needed a special case).
- **Simpler shape:** `compute_yearly` and `compute_timeseries` return fractions only, and json_serializer.cpp applies every ×100. The API is unchanged.
- **Blast radius:** yearly_metrics.{h,cpp}, timeseries_output.{h,cpp}, json_serializer.cpp; test_yearly_metrics (pnl_pct and ann_ret pins), test_metrics_oracle.cpp:377-378 and 412, test_json_serializer.
- **Test gate:**
  - Existing yearly, oracle and serializer tests catch value changes.
  - **Missing:** `JsonSerializer.UnitsPerField`, pinning percentage points vs fraction for every key.
- **Effort:** S.

### F4 [S] Sharpe is implemented 4 times and running drawdown 3 times
- **Where:**
  - Sharpe:
    - summary_metrics.cpp:55-64 (full);
    - summary_metrics.cpp:178-193 (last 400) and 196-211 (last 80, a line-for-line copy);
    - yearly_metrics.cpp:72-85 (with IR).
  - Drawdown:
    - summary_metrics.cpp:35-43 (seeds `cumulative_pnl = finite_r[0]` and skips i=0);
    - yearly_metrics.cpp:57-65;
    - timeseries_output.cpp:52-56.
- **Evidence:** 7 loop bodies for 2 formulas. The oracles add two more copies (test_metrics_oracle.cpp:59-73, generate_golden.py:287).
- **Why it matters:** the epsilon and degenerate rules (std > 0, then std + 1e-12; n ≥ 2) must be kept identical by hand in 4 places, and readers have to diff them.
- **Simpler shape:** one `sharpe(first, last, bars_per_year)` and one running-drawdown loop in summary_metrics.cpp, called by summary (3 times), yearly and timeseries.
- **Blast radius:** metrics/*.cpp only.
- **Test gate:**
  - test_metrics_oracle, test_golden_crossval (numpy), test_yearly_metrics and test_summary_metrics pin all of these.
  - Gap: the 400-bar window cut-off is never exercised, because both oracles use n=140 (test_metrics_oracle.cpp:248, generate_golden.py:258).
  - **Missing:** `MetricsOracle.TailSharpeWindowsCutAt400And80`.
- **Effort:** S.

### F5 [I] `threads` has three defaults, one cap, and no shared limit
- **Where:**
  - /api/simulate: main.cpp:473-475, 0 → `hardware_concurrency()` (32 on this host); no cap other than the alpha count, which can reach 5000.
  - GA: main.cpp:1053 (0 → 20) and 1057 (≤ 32).
  - Save: main.cpp:756-758 (0 → min(hardware, 20)).
  - ga_engine.cpp:59-60: 0 → hardware; unreachable from main.
  - parallel_evaluator.cpp:50-56 creates and joins `count` std::threads every GA round.
- **Evidence:**
  - `parse_uint_option` accepts up to 4294967295 (main.cpp:81-92). A simulate request with 5000 `xmls` and `threads=5000` spawns 5000 OS threads.
  - Concurrent requests each create their own workers (MAP §4.3: "No persistent thread pool").
  - There is no nested parallelism inside a worker: the 3× `std::async` serialization (main.cpp:400-410) runs only on the request thread after workers have joined (simulate 559-582, GA 1175).
  - Per-worker memory is small. `threads_out.txt`: 8 concurrent real-scale candidates add 6.8 MB of peak memory (VmHWM), 0.84 MB per thread; wall time is 632 ms for one candidate alone and 855 ms for eight at once.
- **Why it matters:** on the shared machine, one request can oversubscribe all cores with no bound, and the same request field means three things depending on the endpoint.
- **Simpler shape:** one rule, `threads = min(requested or 20, 32, work)`, derived once in `parse_run_options` and used at all three sites; delete the GAConfig `n_threads=0` auto default.
- **Blast radius:** main.cpp, ga_engine.{h,cpp}; test_ga (passes n_threads explicitly), test_http_contract (uses `threads` 1 and 20).
- **Test gate:**
  - test_http_contract.cpp:1237-1243.
  - **Missing:** `HttpContract.ThreadsAbove32Rejected` for /api/simulate.
- **Effort:** S.
- **Note:** main.cpp belongs to E1; this is included only because the brief's Q4 asks about it.

### F6 [P] `tools/run_logic_tests.py` is a stale second build system
- **Where:** run_logic_tests.py:119-191 (its own flags, force-include, AVX2 rule, generated registry, source and test lists); dependency versions pinned at 105-117.
- **Evidence:**
  - It compiles every `tests/test_*.cpp` except 3 (line 187), so test_thin_loader.cpp is included.
  - It excludes `config_loader.cpp` and `parquet_reader.cpp` (line 179).
  - test_thin_loader.cpp:156-274 calls `BacktestConfig::from_yaml`, `parquet_reader::load_thin_symbol_labels` and `DataPanel::load(config)` from test bodies the linker keeps, so the link must fail with unresolved symbols.
  - git log: run_logic_tests.py last changed 2026-09-11 (a63f45f); test_thin_loader.cpp was added 2026-09-24 (c0b3c9b).
  - It duplicates CMakeLists.txt:144-147 (the prelude), the FetchContent tags (pugixml v1.14, gtest v1.14.0), and 4 of the 5 Python tests that CTest already runs (MAP §6).
- **Why it matters:** a second build path that silently drifts; CLAUDE.md:48, README.md:84 and docs/build_and_validation.md:83 still advertise it.
- **Simpler shape:** delete run_logic_tests.py and its 3 doc mentions; CTest is the documented full path.
- **Blast radius:** tools/run_logic_tests.py, CLAUDE.md, README.md, docs/build_and_validation.md.
- **Test gate:** none.
- **Effort:** S.
- **Caveat:** the link failure is inferred from source, not executed (builds were restricted to the timing question; see §4).

### F7 [P] GA dedupes twice; the evaluator cache and several GA entry points are dead in production
- **Where:**
  - ga_engine.cpp:63/89: the cumulative `scheduled` set dispatches each XML at most once per run.
  - parallel_evaluator.cpp:20-42 and 119-122: global `cache_`, in-batch `seen`/`duplicates`, and the cached-completion replay.
  - ga_engine.h:54-62: `run`, `run_streaming`, `ProgressCallback`; `GAResult.history` (ga_engine.h:25).
  - parallel_evaluator.h:29 and 36: `evaluate_batch`, `cache_hits`.
- **Evidence:**
  - main.cpp calls only `run_detailed` (line 1153) and never reads `history`.
  - `run_streaming` has 0 callers; `run`, `evaluate_batch` and `cache_hits` are used only by tests (MAP §1.2).
  - `scheduled` already guarantees unique XMLs, so the cache can never hit from GA. If it did, main.cpp:1127 would throw "Missing evaluated candidate payload", because payloads are written only by the objective (1101-1107).
  - The cache keeps every XML plus its result for the whole run, a fourth copy of each XML (MAP §4.2).
- **Why it matters:** readers have to reason about cache replays that cannot happen; tests (test_determinism.cpp:48-100) pin behaviour production never uses.
- **Simpler shape:** delete `cache_`, `seen`, `duplicates`, `evaluate_batch`, `cache_hits`, `run`, `run_streaming`, `ProgressCallback` and `history`; point the tests at `run_detailed` and `evaluate_results`.
- **Blast radius:** ga_engine.{h,cpp}, parallel_evaluator.{h,cpp}, test_ga, test_determinism.
- **Test gate:** test_ga.cpp:175-210 (unique evaluations, serial vs 20 threads) still guards dedupe via `scheduled`.
- **Effort:** S.

### F8 [P] `bench_2000_breakdown` cannot run on any current config and measures a path main never takes
- **Where:**
  - tools/bench_2000_breakdown.cpp:57: `DataPanel::load(cfg.parquet_path, cfg.use_columns, …)`.
  - 71-81: no universe mask, no warmup or sim_start, `is_ms=0`, no frozen series.
  - 75-76: a copy of the execution-price mapping at main.cpp:304-305.
- **Evidence:**
  - `bench_out.txt` on the documented fixture: `terminate called after throwing an instance of 'std::runtime_error' what(): Data configuration must provide close`, exit code 134.
  - With `base:` configs (all 4 real configs and the fixture, MAP §7 #1), `use_columns` is empty (config_loader.cpp:204-216), so `load_parquet` throws at parquet_reader.cpp:697.
- **Why it matters:** it is the only non-test caller of the 4-argument `DataPanel::load`, `parquet_reader::load_parquet` and the "legacy first-dataset view" fields (MAP §1.2, §7 #1), and its numbers would describe a non-production path.
- **Simpler shape:** delete the tool and its CMake target. A production-shaped timing probe is about 60 lines (`audit_e2/probe/`).
- **Blast radius:** tools/bench_2000_breakdown.cpp, CMakeLists.txt:174-179 (removing the legacy loader is E1's call).
- **Test gate:** none.
- **Effort:** S.

### F9 [P] Simulator leftovers from the removed execution mode
- **Where:**
  - simulator.cpp:139-141: `execution_intent()` returns `targets_`; called at 267, 280, 330, 372 and 418.
  - simulator.cpp:497-499: an `enum class` checked against its own three values.
- **Evidence:**
  - `git diff HEAD -- src/engine/simulator.cpp` shows the removed `return config_.mode == 0 ? targets_ : previous_targets_;` and `previous_targets_(config.mode == 1 ? data.N : 0, 0.0)`. The indirection survived with only one target left.
  - `grep -n "mode\|delay"` in simulator.{h,cpp} finds nothing else.
- **Why it matters:** it reads as if a second intent source still exists.
- **Simpler shape:** use `targets_` directly; delete `execution_intent()` and the unreachable enum check.
- **Blast radius:** simulator.cpp.
- **Test gate:** all simulator and execution-price tests.
- **Effort:** S.

### F10 [P] Half the bitwise golden is dead, and the rest is the engine's own past output
- **Where:**
  - test_simulator.cpp:579-582: `Case{variant, mode, sim_start}`, 6 cases.
  - test_simulator.cpp:705-706: `if (plan.mode != 0) continue; // Historical implicit-delay cases are no longer supported`.
  - tests/golden/missing_close_bitwise.json: `"source": "pre-change engine at HEAD 2e77af1"`.
- **Evidence:** 3 of the 6 recorded cases are skipped. The remaining 3 cover a wide panel with close execution only: no thin, OHLC4 or VWAP.
- **Why it matters:** dead fixture data, and a `mode` field for a concept that no longer exists. The golden only guards against drift from itself.
- **Simpler shape:** drop the mode ≠ 0 entries from the JSON and the `mode` field from `Case`. The panel and signal digests don't cover the case list, so the remaining rows stay valid.
- **Blast radius:** test_simulator.cpp, tests/golden/missing_close_bitwise.json.
- **Test gate:** the test itself.
- **Effort:** S.

### F11 [P] The checkpoint subsystem is dead and no longer matches the simulator state
- **Where:**
  - src/checkpoint/checkpoint_manager.h:11-19: `SimulatorState{di, prev_weights, prev_target}`.
  - simulator.cpp:426-445: the real state is marks_, retired_, missing_bars_, next_move_, pending/received, and no prev_target since F9.
  - test_checkpoint.cpp:278-321.
- **Evidence:**
  - There is no `Simulator` resume entry point and no src caller of `CheckpointManager` (MAP §1.2).
  - `Checkpoint.RoundTrip` only asserts that a full run has a nonzero return and that `di` round-trips; nothing is resumed.
  - The tests write fixed `/tmp/test_checkpoint_*.bin` paths (lines 80, 107, 140, 242, 279) on a shared machine.
- **Why it matters:** a feature that looks supported but cannot reproduce a run. Boost serialization headers are pulled into every translation unit via module.h (MAP §2).
- **Simpler shape:** delete CheckpointManager, SimulatorState and their 3 tests. Whether `Module::checkpoint` and the legacy ring stay is E3's call.
- **Blast radius:** src/checkpoint/*, test_checkpoint.cpp, CMakeLists.txt.
- **Test gate:** none.
- **Effort:** S.

### F12 [T] Combined P&L and several contract tests pin nothing numeric
- **Where:**
  - test_combined_alpha.cpp:36-80 (`NaNTolerantSignalSummation`) and 162-227 (`CombinedMetricsAreFinite`) sum signals with a loop written in the test and never call `OrderedSignalAccumulator`.
  - test_pipeline_contract.cpp:131, 193-194 and 264 only assert `>0` or `<=`; line 288 compiles `alphas` that are never used.
  - test_extreme_alpha_stress.cpp:142-144 checks only that results are finite.
- **Evidence:**
  - The only combined pins are ordering exactness (254-336), row-0 weights (230-252, 373-382), and HTTP parity of combinedOnly vs ordinary (test_http_contract.cpp:1228-1255).
  - No test pins a combined normalized or raw P&L value, and none uses a thin panel.
- **Why it matters:** a regression in how combined rows reach `run_from_signals` (masking, retired names, float→double) passes every test as long as both HTTP paths change together.
- **Simpler shape:** replace the two self-summing tests with one hand-calculated `CombinedAlpha.NormalizedAndRawReplayPnl` (2 alphas, 3 bars) through the accumulator and `run_from_signals`.
- **Blast radius:** test_combined_alpha.cpp, test_pipeline_contract.cpp.
- **Effort:** S.

### F13 [S] Fields and defaults that duplicate another source
- **Where:**
  - `SimulatorConfig.T/N` (simulator.h:49-50) must equal `data.T/N`, which the constructor enforces (simulator.cpp:481-482). Every caller copies them: main.cpp:296-297, bench:72-73, and 43 test sites (23 of them positional `SimulatorConfig x{…}`).
  - `bars_per_year = 365` is declared 5 times: config_loader.h:48, config_loader.cpp:259, summary_metrics.h:5/36/48, metrics_engine.h:33, yearly_metrics.h:27.
  - The GA sizes 200/50/40/400 appear in both ga_engine.h:10-13 and main.cpp:1049-1052.
- **Evidence:**
  - `grep -rnE "cfg\.T *=|config\.T *=|SimulatorConfig [a-z_]+\{"` → 43 hits.
  - Production always passes `g_config.bars_per_year` (main.cpp:327).
- **Why it matters:** redundant state that needs validation; defaults that differ from production only affect tests.
- **Simpler shape:** read T and N from the panel and delete the fields; make `bars_per_year` a required argument, with the only default in config_loader.
- **Blast radius:** simulator.{h,cpp}, main.cpp, the metrics headers, about 25 test files (the positional initializers make this mechanical but wide).
- **Test gate:** all simulator tests.
- **Effort:** M (test churn).

### F14 [S] The module-driving loop is written twice
- **Where:** simulator.cpp:532-547 (`run`) vs 555-563 (`run_signal_rows`). Both have the same module init, `skips_nan_init` rule, calculate chain and end bound; the bound is written `output.ret.size()` in one and `std::min(T, sim_end)` in the other.
- **Evidence:** a 13-line near-copy whose equality is guarded only by test_combined_alpha.cpp:350-371.
- **Why it matters:** any change to module stepping (warmup, NaN init) has to be made twice, or combinedOnly diverges.
- **Simpler shape:** make `run_signal_rows` the same as `run` with the `advance` call skipped (one private loop with a `bool book`).
- **Blast radius:** simulator.cpp.
- **Test gate:** CombinedAlpha.SignalOnlyEvaluationPreservesWarmupAndEndBoundary; the HTTP combinedOnly parity test.
- **Effort:** S.

## 3. Looks odd but is fine

- **No full matrix per GA candidate.** GA candidates run with signal collection off (main.cpp:1097), so each holds only about 11 N-vectors (simulator.cpp:55-71) plus 9 T-vectors (32-44). Measured at 0.5-0.84 MB per worker (`threads_out.txt`).
  - The "per-thread position buffer of 2,700 × 5,200 doubles" asked about in the brief does not exist.
  - The T×N float matrix (52-54 MB measured, `probe_out2.txt`) is allocated only when signals are collected: single simulate, top-k replays, and the GA best.
- **Modules dominate run time, not the book.** At synthetic real scale (T=2700, N=5200, in-memory, `probe_out2.txt`):
  - data-only `run` 89 ms ≈ `run_from_signals` 83-91 ms, so the book costs about 6 ns per cell;
  - adding rank: 602 ms; tsmean20 + rank: 624-629 ms;
  - `compile_xml` 0.02 ms; `compute_full_metrics` 0.1 ms.
  - The duplicated quote classification and the per-bar `row("close")` hash lookups (simulator.cpp:75, 149/234, 452) therefore cost at most a few percent. Rank cost is E3's concern.
- **Full metrics for one scalar.** The GA objective computes full metrics (timeseries, yearly, OS) to get one number: 0.1 ms against 600 ms of simulation, so not worth special-casing.
- **Fixture and test timings.**
  - Fixture (T=2192, N=4, 50 reps, `probe_out.txt`): load 4.1 ms, compile 0.003 ms, run 0.185 ms, metrics 0.095 ms, run with signals collected 0.254 ms, replay 0.147 ms; generated and replayed `ret` are identical.
  - test_extreme_alpha_stress: 2000 alphas on 2048×8 simulated and scored in 479 ms (`stress_out.txt`).
  - test_simulator (26 cases) and test_ga (15 cases) pass on this build (`tests_out.txt`).
- **Gross vs net is consistent.** Every performance statistic uses `gross_ret` (metrics_engine.cpp:34-47); net only feeds `cum_pnl` (timeseries_output.cpp:49). Pinned by test_metrics_oracle.cpp:355-378 and 433-457.
- **Book size is consistent:** full book, no /2, everywhere (math_contract.md:17-19, 117-119). The BRAIN half-book difference is documented once, at fitness.
- **Finite filtering is unreachable but justified.** The `isfinite` filtering and `finite_r` copy in summary (summary_metrics.cpp:22-29) and yearly (line 53) never trigger from the simulator, whose `ret` is always finite (P&L only uses validated quotes and `isfinite(rt)`, simulator.cpp:178, 204). It is the metrics library's own documented contract (math_contract.md:66) and cheap.
- **Sharpe vs IR epsilon is documented.** Sharpe checks std > 0 and then divides by std + 1e-12; IR uses no epsilon (math_contract.md:69, 101-103).
- **Float accumulation is consistent within each path.**
  - Summary PnL and mean use the same sequential order.
  - Combined rows are normalized in double, stored as float, and summed in a fixed alpha order; exactness across worker counts is pinned (test_combined_alpha.cpp:254-336).
- **The GA round barrier is intended.** All candidates must finish before the next round is generated (ga_engine.cpp:130-147), which idles workers at the round tail (50 candidates on 20 threads is 2.5 waves). It follows from the documented planned-order parent selection (math_contract.md:189-190).
- **Combined matrix sized to T.** `OrderedSignalAccumulator` allocates T rows even when simEnd < T (combined_signals.h:23). `run_from_signals` requires a full-T matrix (simulator.cpp:567), so the shapes agree; the cost is (T − simEnd) × N floats.
- **Two Error gates for intraday fills.** `require_tradable_quotes` (pre-transfer) and the check inside `prepare_execution_prices` (line 358) are both needed today because of the ordering in F1. They collapse once F1's simpler shape is applied.

## 4. Unverified

- **F6 link failure.** Inferred from source and history. To confirm: `tools/run_logic_tests.py --deps-dir /tmp/longshort_refactor_deps --build-dir <scratch>` (the pinned sources exist there) and expect undefined `BacktestConfig::from_yaml`. Not run, because builds were limited to the timing question.
- **Combined normalized mode on thin panels.** `add_block` normalizes each alpha row without the retired-name NaN mask that `normalize_intent` applies (simulator.cpp:253-258 vs combined_signals.h:66-71). Retired names may keep a share of each alpha's 1.0 before the combined renormalization, which would shift relative alpha weights. To confirm: run a two-alpha thin probe with a retirement and compare per-alpha weights.
- **Frontend XML matching.** Not checked whether the frontend matches GA candidates against /api/simulate `source_xml` and would miss on text differences (F2). To confirm: grep frontend/store for XML-keyed maps.
- **Drawdown chart units.** math_contract.md:74 says "the chart reports `(equity - peak_equity) / initial_capital * 100`", but timeseries `drawdown` is emitted as a fraction (timeseries_output.cpp:56). To confirm: check whether the frontend drawdown chart multiplies by 100.

## 5. Top 5

1. **F1** — wrong-money bug (gross 1.5, Error policy bypassed) on the default close-execution path, caused by the frozen budget being decided twice. Small local fix with a clear missing test.
2. **F2** — two grammars for the same template make /api/simulate and GA disagree on which alphas exist (`5|5`, quote escaping, the choice cap); every future grammar change is paid twice.
3. **F3** — units are converted in three places and the frontend already compensates per field. Moving all ×100 into the serializer is small, keeps the API unchanged, and removes a recurring bug class.
4. **F5** — an uncapped `threads` value on /api/simulate, plus three different defaults, is an operational risk on the shared machine. Fix by deriving one rule.
5. **F6** — a stale second build system that the docs still advertise. With the likely link break, anyone using it gets a false failure or false confidence. Pure deletion.
