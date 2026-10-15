# VERIFY_engine — adversarial verification of FINDINGS_E1 (20), E2 (14), E3 (16)

Agent: verify_engine. Working tree of `/home/jgo/longshort_platform` as of 2026-10-14. The repo was not modified: `find . -newer <start marker>` lists only `.git`, which `git status` touched.
Paths are relative to `cpp-engine/` unless they are absolute or start with `frontend/` or `docs/`.
Evidence files are in `refactor_audit/verify_engine/`:
- probe sources: `probe/vprobe.cpp`, `probe/dprobe.cpp`, `probe/typo.yaml`
- outputs: `vprobe_out.txt`, `dprobe_out.txt`, `f4_out.txt`, `f6_out.txt`, `h2_out.txt`, `rlt_out.txt`
- scripts: `scripts/{bigfix.py,f4.sh,f6.sh,http2.sh}`
- strace logs and configure/build logs

## Execution environment

**Scratch build** (Release, `LONGSHORT_BUILD_TESTS=ON`) of the targets `engine`, `longshort-server`, `test_xml_compiler` and `test_ga`. Options:
- `LONGSHORT_PYARROW_PYTHON=/home/jgo/usr/venv-3.11/bin/python`
- `CMAKE_PREFIX_PATH=cpp-engine/examples/datasets/.longshort/deps/metrics-20261013`
- `UUID_INCLUDE_DIRS` and `UUID_LIBRARIES`, copied from the live `~/longshort_workspace/.longshort/build/CMakeCache.txt` (UUID is not found without them)
- `FETCHCONTENT_SOURCE_DIR_{PUGIXML,YAML-CPP,DROGON,GOOGLETEST}=scratchpad/xmlnl_build/_deps/*-src`
- `FETCHCONTENT_FULLY_DISCONNECTED=ON`
- an empty `LONGSHORT_MODULE_DIR`
- built with `nice -n 10 -j8`

**Probes:** g++ against that `libengine.a`.

**Servers.** I started three, each on its own port under my own PID and stopped with SIGTERM to that PID only:
- port 50141, on `bigfix`: a scaled synthetic copy of the `make_http_test_fixture.py` layout, N=1000, T=2192;
- ports 50142 and 50143, on the `make_http_test_fixture.py` fixture.

**Real data.** Read only with pyarrow (`-I`), read-only:
- `universe.parquet` names;
- `events.parquet`;
- `base_data.parquet` `(date, tradingItemId, close)`, filtered to event destinations;
- `manifest.json` keys.

**Disk.** `df -h /`: before `46G 29G 15G 67%`, after `46G 29G 15G 67%`. Peak scratch was 295 MB; after cleanup it is 232 KB (build, rlt_build and fixtures deleted). No process of mine remains (`ps | grep verify_engine` → 0).

---

## 1. Per-finding verification

### FINDINGS_E1 (HTTP/API layer)

**E1-F1 [S] simulate body shaped by 5 booleans — CONFIRMED.**
- main.cpp:466 passes `!options.metrics_only` as the collect flag, so `collect_signals` is never read on the single path. api.ts:261 always sends `collectSignals:true, collectSignalsTopK:5`.
- The four frontend `runSimulate` callers send no multi-alpha `xml`: GADetailLoader.tsx:25, SignalPanelSelector.tsx:42, SignalChart.tsx:143, api.ts:284.
- The null-key quirk is real. Lines 584-586 use non-const `operator[]` on `combined` after `append_signal_payload` returned early (series-only, 393-397).
- Severity honest.
- Contract: math_contract.md:204-208 says "These optional flags do not change the ordinary response contract". Deleting top-K and `collectSignals` changes that documented ordinary response, so the paragraph must change with the code.
- Over-engineering: "four named request kinds" can grow into a dispatch layer. The deletion-only alternative is enough: delete 552-565 (top-K replay), 580-587 (combined signals) and the two keys.

**E1-F2 [P] legacy GA path live — CONFIRMED.**
- Tests 519, 908 and 1039 send `popSize`/`generations` without `streamCandidates`; capture_engine_fixtures.mjs:31 does the same. Test :523 still strips `data:`. useBacktest.ts:82-98 handles `generation_end`/`complete`.
- Contract conflict: math_contract.md:197-199 promises "Legacy requests without this flag retain generation events … `popSize` and `generations` remain request aliases". The deletion must remove that clause too.
- Over-engineering: none.

**E1-F3 [S] inconsistent status mapping — CONFIRMED by reading.**
- Simulate maps every `std::exception` to 400 (1020-1025). Saves map anything else to 500 (628-631).
- A GA launch failure after headers only logs (1202-1205; uncovered in coverage_full.json).
- Contract: no doc clause. tools/check_missing_data.py:256-258 depends on 400 for runtime failures, which E1 lists.
- Over-engineering: one shared catch block is a merge, which is acceptable.

**E1-F4 [I] save batch step uses hardware threads — CONFIRMED by execution.**
- Method: strace `clone3` count on a scratch server (`f4_out.txt`).
  - A 40-row `results` save created 53 threads: 1 alpha worker + 20 row workers + **32** batch workers.
  - A 3-row save created 7 (1 + 3 + 3).
- The cause is that `run_body` has no `threads` key (699-705), so the batch call at 834 → 474-475 falls back to the hardware count.
- Severity: the tag is honest, but "every multi-row save uses all 32 cores" is overstated. The batch uses min(32, rows), so only saves with more than 20 rows exceed the comment's cap. The batch step is also `run_signal_rows` with no book.
- No contract conflict; no over-engineering.

**E1-F5 [S] `threads` three defaults, uncapped — CONFIRMED by execution.**
- `xmls`×200 with `threads:200` created 201 threads; ×64 created 65 (`h2_out.txt`).
- Severity: partly overstated. The UI clamps `nJobs` to 1..32 with default 20 (SettingsBar.tsx:54-55, alphaStore.ts:128), so the unbounded path is reachable only from non-UI clients and from the save batch (E1-F4). The "40-72 threads" case is several bounded requests running concurrently, not the uncapped default.
- Contract: math_contract.md:186-187 says "`threads` (default 20 in HTTP requests)". Simulate's 0→hardware default already contradicts it; the simpler shape restores the documented rule.
- Over-engineering: none.

**E1-F6 [T] single simulate ignores disconnect — CONFIRMED by execution (`f6_out.txt`).**
- Request: bigfix, `<data delay=1/><tsrank days=600/><rank/>`, metricsOnly.
- A full single run took wall 1.04 s and 1.03 s of server CPU.
- Aborted at 0.3 s, the server kept computing: CPU after the abort went 0.50 → **0.71 s**, then flat.
- The same XML ×4 via `xmls`+`combinedOnly`, aborted at 0.3 s, used **0.00 s** of CPU after the abort.
- Severity honest (T, and also I). No contract conflict; no over-engineering, since it passes the existing lambda.

**E1-F7 [S] saved results store raw HTTP body — CONFIRMED** (main.cpp:610-617, 672-682, 787-789).
- This is a decision to make, not a defect.
- The workspace `alphas/` is empty (E1 §4), so the migration cost is zero today.
- Contract: CLAUDE.md fixes the directory layout, not the body shape.
- Over-engineering: "keep both shapes in a tolerant adapter" adds a layer. The deletion-only alternative is to decide before any save exists.

**E1-F8 [I] metrics serialized twice — CONFIRMED.**
- `copy_top_metrics` (376-379) runs at 568 and 588; api.ts:155-207 reads batch or combined first.
- Nuance: for a single alpha, the top-level signal fields are the only copy and the frontend reads them (`single ? resp.signalPlot`, api.ts:191-194). Only the metric keys are duplicated.
- The byte size is an estimate (no saved file exists).

**E1-F9 [I] quantile `distribution` and `signalRange` dead work — PARTLY.**
- `distribution` is a fallback only: SignalDistribution.tsx:26-29 uses `signalDistributionLog` and its nested `linear` first. This part is confirmed.
- `signalRange` is not dead. It is rendered as the "Full value range" tooltip (SignalDistribution.tsx:60) and used as the min/max fallback (:44-45). It is *derivable* from `signalDistributionLog.min/max`, which covers the same finite population from `begin` (json_serializer.cpp:353-366).
- Missed by E1: the nested `linear` call (json_serializer.cpp:456) repeats the log call's full min/max pass (:453).
- The 47 MB figure is an upper bound.

**E1-F10 [S] worker loop written twice — CONFIRMED.**
- The two sites are 473-535 and 756-819.
- All of their failure lines are unexecuted in coverage_full.json: 497, 515-521, 528-533, 802-807, 813-817.
- A file-local helper is a merge, which is acceptable.

**E1-F11 [S] 176-line GA lambda, XML-keyed payload map — CONFIRMED** (1040-1215; map at 1078-1129).
- Over-engineering: "carry the payload in `EvaluationResult`" puts `Json::Value` into `ga/parallel_evaluator.h`, which adds a jsoncpp dependency to the GA library.
- The lighter alternative is to move the worker body to a static function and keep the map.

**E1-F12 [S] metadata keys in two files — CONFIRMED** (main.cpp:642-654, 706-711; alpha_storage.cpp:29-42).

**E1-F13 [P] legacy wire names — CONFIRMED** (json_serializer.cpp:18-35, 91; api.ts:47-65).
- Contract: the mismatch is *documented*, not accidental. docs/data_contract.md (손익 체결과 기간, ¶3: "레거시 JSON은 평균을 `minBet`, 최소를 `minBetMin`으로 보내므로…") and CLAUDE.md ("Legacy JSON keys differ from internal names").
- Renaming is an API change that must update both docs and the frontend fixtures.

**E1-F14 [T] drogon 1 MiB body limit — CONFIRMED by execution (`h2_out.txt`).**
- 15000 `xmls` (975,068 B) → engine JSON 400. 17000 `xmls` (1,105,068 B) → **413 with an empty body**.
- drogon `HttpAppFrameworkImpl.h:738` sets `clientMaxBodySize_{1024 * 1024}`.
- A typical concrete XML is about 55-65 escaped bytes, so 5000 fit in about 0.33 MB. The limit binds only for candidates over 210 escaped bytes; real GA XML lengths were not measured.
- Severity honest.
- Over-engineering: the proposed "MAX_ALPHA_COUNT × max XML size" adds a constant. A plain `setClientMaxBodySize` line suffices.

**E1-F15 [T] untested handler branches — CONFIRMED.**
- Rechecked as unexecuted in coverage_full.json: 1194-1199, 1202-1205, 1210-1215, 1028-1034, 635-636, 161-167, 391-392, 674, 1058.
- The stale comment at test_http_contract.cpp:298-299 ("strict-policy server below") is real: the strict case is a per-request `missingClose` at :1024.

**E1-F16 [P] string fields read three ways — CONFIRMED by reading.**
- `asString` with hidden defaults at 214-215 and 221; `isString` at 233-235; `xml` read at 447 and 1044 vs checked at 690.

**E1-F17 [I] save evaluates rows up to 3× — CONFIRMED by reading** (736; 787→449/466; 834→441/503).
- "Roughly twice" is consistent with E2's timing: at 2700×5200, rank modules cost about 600 ms against about 89 ms for the book (audit_e2/probe_out2.txt).

**E1-F18 [P] three worker groups stop sequentially — PARTLY.**
- Sequential stop is confirmed (1296-1298; `stop_and_join` sets only its own flag).
- "Shutdown takes the sum instead of the maximum" is wrong. All groups run concurrently. The save is only told to stop later, so it keeps replaying rows, or finishes, while the GA and simulate joins run. The added latency is at most one save row after those joins.
- Severity: low.

**E1-F19 [P] dead branches and small duplicates — CONFIRMED.**
- Unexecuted: 960-966 and 888-889.
- `diagnostics_payload` ignores its `RunOptions` argument (359).
- `/api/config` readers: `thinLayout` 0; `combinedMode` 0 readers of the config field; `loadedDataBytes` typed only (grep across frontend, tools, tests).

**E1-F20 [P] launcher fallbacks — PARTLY (two of its deletions refuted).**
- `restart_engine.sh` is the **production entry of the daily Airflow DAG**:
  - `/home/jgo/universe_builder/dag/cache_v2_daily.py:128-131` (BashOperator `bash …/restart_engine.sh`);
  - `dag/run_step.sh:21` (`restart_engine.sh --port 50007 --timeout 1800 "$@"`);
  - `dag/STEP_CONTRACT.md:9,37`.
- `--build`/`--prev` are the STEP_CONTRACT common arguments that run_step.sh forwards ("$@", STEP_CONTRACT.md:9, 15-16). Deleting them makes argparse reject the DAG call. MAP §8's "referenced by nothing" stopped at the repo boundary.
- Confirmed parts:
  - `DS` is unused (restart_engine.py:67, 1 hit).
  - run_engine.py silently does `mkdir` for a missing modules dir.
  - The `asOf` fallback is unused today, because the manifest has `asOfDate`.
- Contract conflict: STEP_CONTRACT.md:9.

### FINDINGS_E2 (compute core)

**E2-F1 [S] close execution ignores the frozen budget for successor stock — CONFIRMED by re-execution, latent on current data.**
- I rebuilt the audit_e2 probe as `vprobe.cpp`, extended to 3 bars with controls (`vprobe_out.txt`). Results on bar 1 unless noted:

  | case | execution / policy | weights | gross | frozen_gross | outcome |
  |---|---|---|---|---|---|
  | A and B quoteless | close / carry | [0 .5 1] | **1.5** | 0 | — |
  | A and B quoteless | close / error | [0 .5 1] | **1.5** | 0 | no throw |
  | A and B quoteless | ohlc4 / carry | [0 .5 .5] | 1.0 | 0.5 | — |
  | A and B quoteless | vwap / carry | [0 .5 .5] | 1.0 | 0.5 | — |
  | A and B quoteless | ohlc4 / error | — | — | — | throws |
  | control: B quoted | close / carry | [0 0 1] | 1.0 | 0 | — |
  | control: B quoted | close / error | [0 0 1] | 1.0 | 0 | — |
  | B still quoteless on bar 2 | close / carry, **bar 2** | [0 .5 .5] | 1.0 | 0.5 | — |

  The overshoot therefore lasts exactly the move bar (last row).
- Contract: this violates math_contract.md:54 ("Carry freezes a position without an executable quote and reserves its budget") and :56-57 ("Error policy rejects a required trade without a fill"). E2's citation of :17 is weak: that clause explicitly allows "carried corporate positions can differ from the target normalization".
- Frequency on real data, `cache_v2_cash_1d`:
  - `events.parquet` has 2,963 source-ending moves with a stock destination, all on calendar bars.
  - **0** of them have a missing destination close on the move bar (pyarrow join on `base_data.parquet` close).
  - The live workspace runs `vwap`, which takes the correct path; only close configs (us_equity.yaml, config default) are exposed.
- Severity: the defect is real and is the only contract-violating accounting path found, but E2's "money bug" Top-1 is overstated for the current cache.
- Over-engineering: none; the fix moves code (compute once, after the transfers).

**E2-F2 [S] GA search space is a second expander and serializer — PARTLY.**
- Reproduced: `5|5|10` gives compiler 3 vs GA 2.
- Escape divergence reproduced: `note="it's"` gives GA `it's` vs `source_xml` `it&apos;s`. It is reachable only through attributes the compiler ignores (E3-F3): no built-in attribute or `expr` accepts `'`. Practical impact ≈0.
- The "choice makes the grid hit 5000" point is **documented behaviour** (math_contract.md:181-182, "Structural choice/shuffle searches use the compiler expansion contract and its existing 5,000-pipeline cap"), not a disagreement.
- Stale comments (search_space.cpp:68, .h:22) and the unreachable :168 are confirmed.
- Contract: math_contract.md:180 promises dedupe only for the GA. Moving dedupe into `expand_value` extends it to /api/simulate multi-alpha counts; no clause protects the duplicates.

**E2-F3 [P] ×100 in three places — CONFIRMED.**
- Sites: yearly_metrics.cpp:67-69, 94; timeseries_output.cpp:61, 63; json_serializer.cpp:18-35. Fitness undoes it at yearly_metrics.cpp:114; the frontend multiplies again at DetailSummaryTable.tsx:50.
- Tag: arguably S, since one decision is scattered. API unchanged, so no contract conflict.

**E2-F4 [S] Sharpe 4×, drawdown 3× — CONFIRMED.**
- Line ranges match.
- Missed: a 4th drawdown in frontend DetailTimeSeries.tsx:111-126 (see V4).
- Caveat for the merge: summary uses two summation orders (pnl seeded with `finite_r[0]`, mean in a separate loop) that the numpy golden compares against.

**E2-F5 [I] threads — CONFIRMED.** Same root and evidence as E1-F5.

**E2-F6 [P] run_logic_tests.py stale — PARTLY (conclusion confirmed, mechanism wrong).**
- Command: `python3 -B tools/run_logic_tests.py --deps-dir /tmp/longshort_refactor_deps --build-dir <scratch> --modules-dir <empty> --jobs 8`.
- Result: the pinned deps built, then "3 compilation(s) failed":
  - `src/api/alpha_storage.cpp:19`
  - `tests/test_alpha_storage.cpp:4`
  - `tests/test_thin_loader.cpp:12`

  All three: `fatal error: arrow/api.h: No such file or directory`, EXIT=1 (`rlt_out.txt`).
- It never reaches the link failure E2 predicted.
- git: `a63f45f 2026-09-11` (harness) vs `c0b3c9b 2026-09-24` (thin test); `alpha_storage.cpp` is untracked (`??`).
- Contract: the CLAUDE.md "Commands and validation" paragraph documents the harness and must go with it.

**E2-F7 [P] GA dedupes twice, dead cache and entry points — CONFIRMED.**
- One `ParallelEvaluator` per `run_impl` (ga_engine.cpp:61) plus the per-run `scheduled` set (:63, :89) means `cache_` cannot hit.
- Contract: math_contract.md:188-189 ("XML keys … identify duplicates") remains satisfied by `scheduled`.

**E2-F8 [P] bench_2000_breakdown broken — CONFIRMED** from audit_e2/bench_out.txt plus reading (config_loader.cpp:212-216; parquet_reader.cpp:697). Not re-run. Same root as E3-F6.

**E2-F9 [P] `execution_intent()` leftover — CONFIRMED.**
- `git diff HEAD` shows the removed `config_.mode == 0 ? targets_ : previous_targets_`.
- simulator.cpp:499 is unexecuted.

**E2-F10 [P] half the bitwise golden is skipped — CONFIRMED, line refs wrong.**
- `struct Case` is at test_simulator.cpp:675 and the skip at :722, not at 579-582 or 705-706.
- The golden JSON case modes are `[0,1,0,1,0,1]`.

**E2-F11 [P] checkpoint dead — CONFIRMED.**
- grep finds 0 references to `CheckpointManager`/`SimulatorState` in src, tools or include outside `src/checkpoint/`.
- Fixed `/tmp/test_checkpoint_*.bin` paths at test_checkpoint.cpp:80, 107, 140, 242, 279.
- Same root as E3-F13.

**E2-F12 [T] combined P&L not pinned — CONFIRMED with nuance.**
- `run_from_signals` *is* pinned by a hand value (test_combined_alpha.cpp:109, `ret[0] == -0.0005`) and by run≡replay (150-157).
- The unpinned link is accumulator → `run_from_signals` P&L, and any thin panel.

**E2-F13 [S] duplicated T/N and defaults — CONFIRMED.**
- grep count is 42, not the reported 43.
- `bars_per_year` 365 defaults sit at config_loader.h:48, config_loader.cpp:259, summary_metrics.h:5, metrics_engine.h:33 and yearly_metrics.h:27.

**E2-F14 [S] module loop twice — CONFIRMED** (simulator.cpp:532-547 vs 555-563).

### FINDINGS_E3 (data, compiler, modules, tools)

**E3-F1 [P] two example configs fail at startup — CONFIRMED.**
- pyarrow `unique` of `universe.parquet` gives: TOP1000, TOP1000_ADR, TOP1000_CAP, TOP2000, TOP2000_ADR, TOP3000, TOP3000_ADR, TOP4000, TOP4000_BUF80, TOP500, TOP500_ADR, TOP500_CAP.
- The examples ask for `name: Top500` (equities_monthly_afl.yaml:20) and `name: ALL` (compare_v2_all.yaml:26, not :25).
- Matching is exact (parquet_reader.cpp:560); the throw is at :575-576.
- The oracle default is `DEFAULT_CONFIG` at check_group_neutralize_oracle.py:41 (:201 is the argparse use).
- Contract: data_contract.md already disclaims examples ("예제 파일명이 현재 발행 build의 이름을 보장하지 않는다"), but README:15 tells users to write `Top500 또는 ALL`.
- Over-engineering: the deletion-only shape (two YAMLs plus one README line) is enough; `test_example_configs` depends on /home/base and is optional.

**E3-F2 [S][T] bind/shuffle produce wrong variant sets — CONFIRMED by probe (`vprobe_out.txt`).**

  | input | output |
  |---|---|
  | `bind` over 3 members | 2 pipelines; the third member is stuck at `decay days=1` in both |
  | `bind` 3 vs 2 alternatives | 3 pipelines; the third pairs `decay 3` with `tsmean 5` |
  | `<shuffle>` with pipe children | 2 variants; `pct=false` and `days=10` are lost |
  | `<shuffle>` of tszscore5, tszscore10, rank | 3 of 6 orders |
  | plain `<choice>` | correct, 4 pipelines |

- GA counts are identical in every case.
- I used tszscore deliberately: E3's own example (two tsmeans) loses only orders that commute, because both are linear filters.
- Usage: 0 hits for `<shuffle`/`bind=` in tests, the frontend, workspace `alphas/` and `research/`.
- Contract: math_contract.md:181 names shuffle and must change with a deletion.

**E3-F3 [P][T] built-in attributes accept typos — CONFIRMED, and severity is understated.**
- Probe results:
  - `tsmean dayz=5` → days=20.
  - `tszscore ddof=1.9` → ddof=1.
  - `rank pct=flase` and `pct=FALSE` → pct=0.
  - `ddof=x` → the error text is just "stoi".
  - `days=5x` → rejected (`parse_bounded_int`).
- Addendum (V3): the `data` op reads only `expr` and `delay`, so a misspelt `delay` silently compiles with **no delay**. That is a same-bar look-ahead against CLAUDE.md ("Delay is explicit in alpha XML only. New editor XML contains delay="1""). This makes the finding the highest-risk compiler issue, not P.
- `RESERVED_TAGS` (generate_modules.py:49-51) matches the registry today.
- Over-engineering: "derive `RESERVED_TAGS` from the registry" needs a cross-language extraction step. The deletion-only alternative is the `ReservedTagsMatchEngineRegistry` test alone.
- Contract: strict parsing can reject previously saved XML (CLAUDE.md "Preserve saved XML…"). `alphas/` is empty, so there is no live victim.

**E3-F4 [S] run universe reaches only three modules — CONFIRMED, latent.**
- Evidence: dag_builder.cpp:444-449; generate_modules.py:270-278 binds `panel` only; `modules/` is empty.
- The simpler shape is an SDK change (a new optional binding), so modules/README.md must document it.

**E3-F5 [I] repeated long-file decode — CONFIRMED in mechanism, share unmeasured.**
- Mechanism:
  - `read_field` constructs a new `ParquetFile` per field (parquet_reader.cpp:234).
  - 7 base fields map to `base_data.parquet` (config_loader.cpp:85-88), plus the master file.
  - `read_long_doubles` decodes `returns.parquet` again (:868).
- Restart windows in engine_50007.log were 192 s and 190 s, but they include restart_engine's verification polling.
- Tag honest.

**E3-F6 [P] test-only loader surface — CONFIRMED.** Same root as E2-F8.

**E3-F7 [P] unknown config keys ignored — CONFIRMED by probe.**
- `typo.yaml` with `missing_close_polcy: error`, `executon_price: vwap`, `transaction_fees: 50` and `mode: 1` gives `config OK policy=carry execution_price=close fee=0.000150`.
- The workspace config and us_equity.yaml use only keys the loader reads (checked with yaml), so an allowed-key list would not break them.
- Contract: CLAUDE.md names the rejected legacy keys; rejection survives under a generic message.
- Over-engineering: a per-block key table, not a layer.

**E3-F8 [S] GA re-implements pipe/range/serialize — CONFIRMED.** Same root as E2-F2: dedupe and escape are reproduced; escape is reachable only via ignored attributes.

**E3-F9 [P] run_logic_tests.py cannot build — CONFIRMED by execution.** Exact mechanism as E3 stated: the alpha_storage Arrow include (see E2-F6).

**E3-F10 [P] five unreferenced oracle tools — CONFIRMED.**
- 0 references in the repo or in universe_builder/dag.
- `DEFAULT_GROUPS` (`REPO/data/groups/cache_v1_cash_1d`) is missing.
- `/home/base/cache/cache_v1_cash_1d` still exists, so check_wide_data_oracle's defaults are stale rather than dangling.

**E3-F11 [P] dangling refs after the tools/data symlink — CONFIRMED.**
- `tools/data -> ../../../universe_builder/cache`.
- test_build_group_panels.py:23 loads through the symlink without a missing-file skip.
- parquet_reader.cpp:633 and :858 name files under it.

**E3-F12 [P] stale example labels and artifacts — PARTLY.**
- Confirmed: the stale text, 3 untracked `.bak` files and 256 `uploads/tmp` entries.
- Refuted: deleting `examples/datasets/.longshort/`. It holds `deps/metrics-20261013` (jsoncpp 1.9.5 and uuid headers), which the **live** build uses (workspace CMakeCache `CMAKE_PREFIX_PATH`, `UUID_INCLUDE_DIRS`) and docs/build_and_validation.md:58-60 documents. My scratch configure failed ("Could not find UUID") until pointed there.
- Contract conflict: build_and_validation.md:58-60.

**E3-F13 [P] checkpoint boilerplate in 9 modules — CONFIRMED.**
- 9 headers × 2 overrides.
- `Module::checkpoint` is a default no-op (module.h:157-158), and modules/README.md never mentions checkpoint, so deletion keeps the SDK contract.

**E3-F14 [P] duplicated Arrow plumbing — CONFIRMED.**
- `read_long_doubles` (604-631) is the `read_field` long branch (255-279) with double values.
- It also bypasses `cold_read_mutex`, which is harmless at single-threaded startup.

**E3-F15 [P] data op handled twice — CONFIRMED** (278-305 vs 313-337). Neither copy validates unknown attributes (V3).

**E3-F16 [T] loader branches untested — CONFIRMED.** Unexecuted per coverage_full.json: parquet_reader 741, 743, 889-892; config_loader 179, 266-269.

---

## 2. Cross-report overlap (same root)

1. **E1-F5 = E2-F5, with E1-F4.** Root: `threads` is parsed unbounded in `parse_run_options` (main.cpp:246), and each site picks its own default (474, 757, 1053).
2. **E2-F2 = E3-F8, with E3-F2 inherited by the GA.** Root: search_space.cpp keeps its own pipe/escape/serialize grammar and delegates choice/shuffle to the buggy `expand_pipelines`.
3. **E2-F6 = E3-F9.** Root: run_logic_tests.py drifted from CMake.
4. **E2-F8 = E3-F6.** Root: the legacy 4-arg loader is kept alive by one broken bench.
5. **E2-F11 = E3-F13.** Root: checkpointing has no runtime caller.
6. **E1-F19 ⊂ E3-F7.** E1-F19 is the policy re-check at main.cpp:886-890 and `combinedMode` validated twice. Root: defaults and validations are written in both config_loader and main/parquet_reader.
7. **E1-F11 ↔ E2-F7.** Root: `EvalFunc` returns only a double, which forces the XML-keyed JSON side map; a cache replay would miss in it.
8. **E1-F3 ↔ E3-F3.** Root: bare `stoi` parsing surfaces as a 400 whose whole message is "stoi".
9. **E3-F15 ↔ E3-F3 ↔ V3.** Root: built-in ops (including `data`) never validate their attribute keys.
10. **E2-F4 ↔ V4.** Root: the drawdown rule is computed in the engine three times and again in the frontend.

---

## 3. Seams the reports missed

### V1 [S] Cross-repo coupling: a platform tool is the DAG's production entry, and platform tests import universe_builder; neither is recorded in the repo
- Where:
  - Callers: /home/jgo/universe_builder/dag/cache_v2_daily.py:128-131, dag/run_step.sh:21, dag/STEP_CONTRACT.md:9, 37.
  - Called: tools/restart_engine.sh, restart_engine.py:622-623.
  - Reverse direction: tools/data symlink, tests/test_build_group_panels.py:23, CMakeLists.txt:224-229.
- Evidence:
  - run_step.sh:21 is `exec bash /home/jgo/longshort_platform/cpp-engine/tools/restart_engine.sh --port 50007 --timeout 1800 "$@"`.
  - MAP §8 and E1-F20 list restart_engine.sh as "referenced by nothing".
- Why it is a problem:
  - Two audit outputs proposed deleting a live production entry point and its required CLI arguments.
  - A rename on either side breaks the other side silently.
- Simpler shape: one line in restart_engine.sh's header naming the DAG caller and STEP_CONTRACT args; move test_build_group_panels.py to universe_builder (E3-F11).
- Blast radius: tools/restart_engine.{sh,py} headers, CMakeLists.txt test registration, universe_builder/dag.
- Test gate: none. Missing: `test_restart_engine_accepts_step_contract_args` (`--asof/--build/--prev` parse without error).
- Effort: S

### V2 [P] The live build's dependency prefix lives in a gitignored examples folder
- Where: docs/build_and_validation.md:58-60; `~/longshort_workspace/.longshort/build/CMakeCache.txt` (`CMAKE_PREFIX_PATH`, `UUID_INCLUDE_DIRS` → `cpp-engine/examples/datasets/.longshort/deps/metrics-20261013`); .gitignore:238 `.longshort/`.
- Evidence:
  - Scratch configure failed with "Could not find UUID" and then "uuid.h: No such file" until given those paths (`configure.log`, `configure2.log`).
  - The directory (70 MB) also holds a uuid-dev `.deb` and a stale `build/`.
- Why it is a problem: E3-F12's cleanup, or a fresh clone, removes jsoncpp and the uuid headers needed by every engine build. The examples README does not mention it.
- Simpler shape: move `deps/metrics-20261013` next to the workspace build (`~/longshort_workspace/.longshort/deps`) and update the 3 doc lines.
- Blast radius: docs/build_and_validation.md:58-60; the live CMakeCache on its next reconfigure.
- Test gate: none (configure is the gate).
- Effort: S

### V3 [T] Misspelt `delay` on `<data>` compiles to a same-bar look-ahead
- Where: dag_builder.cpp:278-305 and 313-337 (only `expr` and `delay` are read).
- Evidence (`dprobe_out.txt`):
  - `<data expr="close" delay="1"/><rank/>` → pipeline=3, MDelay=1.
  - `dealy="1"` → pipeline=2, MDelay=0, OK.
  - `Delay="1"` → pipeline=2, MDelay=0, OK.
- Why it is a problem: CLAUDE.md says "Delay is explicit in alpha XML only" and the execution contract executes at the same bar. A typo turns a delay-1 alpha into one trading on the close it observes, with inflated metrics and no error.
- Simpler shape: reject unknown attribute keys on `data` (the same change as E3-F3's `validate_keys`).
- Blast radius: dag_builder.cpp, test_xml_compiler.cpp.
- Test gate: missing `XmlCompiler.DataRejectsUnknownAttribute`.
- Effort: S

### V4 [I] Timeseries round trip: the engine cumulates, the frontend de-cumulates and recomputes drawdown, and three engine outputs have no production reader
- Where:
  - Engine: timeseries_output.cpp:49-56, json_serializer.cpp:93-104 (`drawdown`, `frozenGross`), main.cpp:331-363 (`missingData`).
  - Frontend: lib/api.ts:93-136 (`cumPnlToDailyReturns`), DetailTimeSeries.tsx:56-126. It re-cumulates, computes its own drawdown ×100, and keeps a net+turnover×fee gross fallback for older engines.
- Evidence:
  - Non-test frontend grep finds 0 readers of `.drawdown`, `frozenGross` and `missingData`; MAP_frontend §6.2 agrees.
  - `missingData` is read only by tools/check_missing_data.py (unreferenced, E3-F10) and test_http_contract.
- Why it is a problem:
  - The drawdown rule lives in 4 places.
  - 2 of the 8 timeseries arrays, plus `missingData`, ship unread on every detail body, GA candidate event and saved row.
  - This also settles E2 §4's "drawdown chart units" question: the chart uses the frontend's own ×100, and the engine's fraction is never read.
- Simpler shape: one owner. Either delete engine `drawdown`/`frozenGross` serialization and keep the frontend computation, or delete the frontend computation and read the engine series.
- Blast radius: json_serializer.cpp, api.ts adapter, DetailTimeSeries.tsx, test_json_serializer; saved bodies (E1-F7).
- Test gate: frontend apiClient contract test. Missing: `DrawdownChartEqualsEngineDrawdown`.
- Effort: M

### Unverified (seams)
- **Combined normalized mode on thin panels.** `add_block` normalizes each alpha row without the retired-name mask (combined_signals.h:66-71), and `normalize_signal_row` writes 0, not NaN, for ineligible names. A retired name with a finite signal (for example a constant) could keep a per-alpha share before the replay renormalizes. To confirm: a 2-alpha thin probe with a constant signal through a retirement, comparing per-alpha weights against single runs.
- **E1-F8 / E1-F14 / E3-F5 sizes and shares**, as the reports say: no saved file exists, real GA XML lengths were not measured, and per-field decode time was not measured.

---

## 4. Final table

| finding | verdict | severity honest? | contract conflict? | over-engineering? | note (≤15 words) |
|---|---|---|---|---|---|
| E1-F1 | CONFIRMED | yes | math_contract.md:204-208 ordinary response | risk (request-kind layer) | delete top-K, combined signals, two keys instead |
| E1-F2 | CONFIRMED | yes | math_contract.md:197-199 promises legacy path | no | delete clause with code |
| E1-F3 | CONFIRMED | yes | no (check_missing_data.py expects 400) | no | GA launch failure is 200, empty body |
| E1-F4 | CONFIRMED (exec) | overstated | no | no | 40 rows → 1+20+32 threads; batch = min(32, rows) |
| E1-F5 | CONFIRMED (exec) | partly overstated | fixes math_contract.md:186-187 drift | no | 200 threads spawned; UI clamps 1..32 |
| E1-F6 | CONFIRMED (exec) | yes | no | no | aborted single kept 0.71 s CPU; multi stopped at 0 |
| E1-F7 | CONFIRMED | yes (decision item) | no | risk (dual-shape adapter) | alphas/ empty; decide now |
| E1-F8 | CONFIRMED | yes (size estimated) | no | no | single signals are top-level only; metrics are duplicated |
| E1-F9 | PARTLY | partly | no | no | signalRange is displayed (tooltip), derivable, not dead |
| E1-F10 | CONFIRMED | yes | no | no | all failure branches unexecuted |
| E1-F11 | CONFIRMED | yes | no | yes: jsoncpp into GA lib | move body to static function, keep map |
| E1-F12 | CONFIRMED | yes | no | no | — |
| E1-F13 | CONFIRMED | yes | data_contract.md, CLAUDE.md document legacy keys | no | mismatch is documented, not accidental |
| E1-F14 | CONFIRMED (exec) | yes | no | mild (new constant) | 1.1 MB → 413 empty body; 0.98 MB → JSON 400 |
| E1-F15 | CONFIRMED | yes | no | no | coverage lines rechecked |
| E1-F16 | CONFIRMED | yes | no | no | — |
| E1-F17 | CONFIRMED | yes | no | no | ≈2× consistent with E2 module timing |
| E1-F18 | PARTLY | overstated | no | no | groups run concurrently; not a sum of work |
| E1-F19 | CONFIRMED | yes | no | no | — |
| E1-F20 | PARTLY | partly | STEP_CONTRACT.md:9 (--build/--prev) | no | restart_engine.sh is the Airflow DAG entry |
| E2-F1 | CONFIRMED (exec) | kind yes, impact overstated | violates math_contract.md:54, 56-57 | no | 0/2963 real moves hit; one-bar overshoot |
| E2-F2 | PARTLY | partly | math_contract.md:180-182 (cap documented) | no | dedupe real; escape unreachable; cap documented |
| E2-F3 | CONFIRMED | yes (arguably S) | no | no | — |
| E2-F4 | CONFIRMED | yes | no | no | 4th drawdown in frontend |
| E2-F5 | CONFIRMED (exec) | partly overstated | as E1-F5 | no | same root as E1-F5 |
| E2-F6 | PARTLY (exec) | yes | CLAUDE.md validation paragraph | no | fails at compile (arrow/api.h), not link |
| E2-F7 | CONFIRMED | yes | no (math_contract.md:188-189 kept) | no | one evaluator per run; cache cannot hit |
| E2-F8 | CONFIRMED | yes | no | no | same root as E3-F6; not re-run |
| E2-F9 | CONFIRMED | yes | no | no | git diff confirms removed mode |
| E2-F10 | CONFIRMED | yes | no | no | line refs are 675/722 |
| E2-F11 | CONFIRMED | yes | no | no | same root as E3-F13 |
| E2-F12 | CONFIRMED | yes | no | no | `run_from_signals` hand-pinned; accumulator path not |
| E2-F13 | CONFIRMED | yes | no | no | 42 sites, not 43 |
| E2-F14 | CONFIRMED | yes | no | no | — |
| E3-F1 | CONFIRMED (pyarrow) | yes | README:15 vs data_contract disclaimer | test optional | names are uppercase TOPn only |
| E3-F2 | CONFIRMED (probe) | yes | math_contract.md:181 names shuffle | no | bind stride and shuffle drop variants; 0 users |
| E3-F3 | CONFIRMED (probe) | understated | saved XML may stop compiling | yes: derived RESERVED_TAGS | typo'd data delay → silent look-ahead (V3) |
| E3-F4 | CONFIRMED | yes (latent) | modules/README.md must change | small SDK change | no user modules today |
| E3-F5 | CONFIRMED | yes (share unmeasured) | no | no | 190 s includes verification polling |
| E3-F6 | CONFIRMED | yes | no | no | same root as E2-F8 |
| E3-F7 | CONFIRMED (probe) | yes | CLAUDE.md legacy-key message changes | no | live configs use only known keys |
| E3-F8 | CONFIRMED (probe) | yes | math_contract.md:180 GA-only dedupe | no | same root as E2-F2 |
| E3-F9 | CONFIRMED (exec) | yes | CLAUDE.md validation paragraph | no | 3 TUs fail on arrow/api.h |
| E3-F10 | CONFIRMED | yes | no | no | v1 cache still exists |
| E3-F11 | CONFIRMED | yes | no | no | also reverse coupling (V1) |
| E3-F12 | PARTLY | partly | build_and_validation.md:58-60 | no | .longshort/deps is the live build prefix |
| E3-F13 | CONFIRMED | yes | no (SDK silent on checkpoint) | no | — |
| E3-F14 | CONFIRMED | yes | no | no | `read_long_doubles` also bypasses the mutex |
| E3-F15 | CONFIRMED | yes | no | no | neither copy validates attributes |
| E3-F16 | CONFIRMED | yes | no | no | coverage rechecked |
| V1 (new) | ADDED | S | STEP_CONTRACT.md | no | DAG ↔ platform coupling invisible in repo |
| V2 (new) | ADDED | P | build_and_validation.md:58-60 | no | build deps in gitignored examples dir |
| V3 (new) | ADDED | T (high) | CLAUDE.md delay rule | no | `dealy`/`Delay` compile without delay |
| V4 (new) | ADDED | I | no | no | drawdown 4×; drawdown/frozenGross/missingData unread |

## 5. Summary

- **Verdicts (50):** 44 CONFIRMED, 6 PARTLY (E1-F9, E1-F18, E1-F20, E2-F2, E2-F6, E3-F12), 0 REFUTED, 0 UNVERIFIABLE. Re-executed: E1-F4, E1-F5, E1-F6, E1-F14, E2-F1, E2-F2/E3-F8, E2-F6/E3-F9, E3-F1, E3-F2, E3-F3, E3-F7.
- **Refuted in part:**
  - E1-F20 / MAP §8: restart_engine.sh and `--build/--prev` are the live Airflow DAG entry.
  - E3-F12: `examples/.longshort/deps` is the live build's dependency prefix.
  - E1-F9: `signalRange` is displayed.
  - E2-F6: fails at compile, not link.
  - E1-F18: not a sum.
  - E2-F2: the 5000-cap point is documented.
- **Downgraded:**
  - E2-F1 is real and violates math_contract.md:54/56, but 0 of 2,963 real successor moves hit it, and the overshoot lasts one bar.
  - E1-F4: min(32, rows).
  - E1-F5/E2-F5: the UI clamps to 1..32.
- **Upgraded:** E3-F3. A misspelt `delay` on `<data>` silently compiles to a same-bar look-ahead.
- **Seams added:** V1 (cross-repo DAG coupling), V2 (build deps in examples), V3 (data-attribute look-ahead), V4 (drawdown 4×; three serialized outputs unread).
