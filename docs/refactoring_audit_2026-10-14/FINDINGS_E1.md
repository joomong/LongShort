FINDINGS_E1 could not be saved to `refactor_audit/FINDINGS_E1.md`: the harness blocks subagents from writing report files. The full report is below; the lead can save it there unchanged.

Top 5 (I built nothing; coverage numbers are re-queried from `map_engine/coverage_full.json`):
1. **F4: every multi-row save uses all 32 cores for `batch.json`.** The comment at main.cpp:754-755 says saves stay at 20 threads. But `run_body` carries no `threads` (642, 699-705), so the batch call falls back to `hardware_concurrency()` (474-475, 824-834). One-line fix.
2. **F6: single-alpha `/api/simulate` cannot be cancelled.** main.cpp:466 passes an empty row callback. All three per-alpha frontend calls (detail, signals, series) abort often, but the server finishes the simulation anyway, on an uncapped thread. No test cancels anything (497 never ran).
3. **F2: the legacy GA path (`streamCandidates:false`) is still alive** (1164-1191, `generation_end`, `popSize`/`generations` aliases). The frontend always sends `true` (api.ts:309); only 3 of 4 GA tests and `capture_engine_fixtures.mjs` use the old path.
4. **F1 (depends on F7): the simulate response is shaped by 5 booleans plus single/multi.** The frontend uses 4 shapes. Top-K replay and combined-signal payloads are only reached by tests. Saved rows store the raw HTTP body verbatim, so any shape change becomes a data migration — decide F7 first.
5. **F9: the quantile-clipped `distribution` histogram and the `signalRange` scan are dead work.** The frontend uses them only when `signalDistributionLog` is missing (SignalDistribution.tsx:25-31), and this engine always sends it. Each signal payload still makes 3 extra passes over T×N and a full-size float copy (about 47 MB at T=3,900, N=3,000, estimated). Saved files contain no signal payloads, so this is not blocked by F7.

Next in line: F3 (the same failure gives 400 on live simulate, 500 on save, and an empty 200 on GA launch failure) and F5 (`threads` is capped at 32 only for GA; simulate accepts any value; 40-72 threads on 32 cores in normal use).

Scratch: `audit_e1/` was empty and has been deleted; `df` reads 67% before and after.

---

# FINDINGS_E1.md — engine HTTP/API layer (audit_e1, working tree 2026-10-14)

Paths are relative to `/home/jgo/longshort_platform/cpp-engine/` unless they start with `frontend/`. Coverage comes from MAP_engine §6 (`map_engine/coverage_full.json`, re-queried for the owned files; I built nothing). Drogon facts are from the v1.9.8 sources (`CMakeLists.txt:67-69`), read in the existing `scratchpad/xmlnl_build/_deps/drogon-src`.

## 0. Scope read

- **`src/main.cpp` (1300)**
  - Loads globals once and parses each request into `RunOptions`; holds every HTTP handler.
  - `simulate_response` compiles one XML or an `xmls` list. It runs one alpha directly, or N alphas on ad-hoc threads that feed an ordered combined accumulator, then builds the body from booleans.
  - GA runs on a worker thread that streams NDJSON events.
  - Saves validate the rows, re-run each one through `simulate_response`, and publish a staged directory.
- **`src/api/json_serializer.{h,cpp}` (43+458)**
  - Writes summary, yearly and timeseries to JSON under the wire names.
  - Signal plot is sampled under a cell budget, with an optional mask or symbol subset.
  - Emits three histograms: a quantile-clipped linear one, and a full-population log one with a full-range linear one nested inside.
- **`src/api/alpha_storage.{h,cpp}` (93+304)**
  - Validates names and levels; writes and reads metadata YAML through yaml-cpp; lists and removes alphas.
  - Saves through a staging directory: flock, then a RENAME_NOREPLACE commit. Startup sweeps unlocked stale staging directories.
  - Also contains the Arrow writer for positions parquet.
- **`src/api/combined_signals.h` (117)**
  - Holds a T×N combined-signal buffer.
  - Workers add rows, one at a time or in 32-row blocks, normalized if requested.
  - A condition variable makes alphas add to each row in order, so the float sums are deterministic. It can be cancelled.
- **`tests/test_http_contract.cpp` (1699)**
  - Forks the real server on a fixture config and drives it with curl.
  - 27 fixture-backed `TEST_F` tests and 1 startup test.
  - Covers execution price, simEnd/simStart, multi-alpha batches and top-K, combined modes, GA, missing-data fields and removed options.
  - Also covers neutralize, the metricsOnly/signalSeriesOnly flags, `xmls` aggregates, and saved-alpha CRUD, results, positions, CORS and row ids.
- **`tests/test_json_serializer.cpp` (448)**: 24 unit tests.
  - Legacy keys; plot offset, budget, subset and membership.
  - Quantile histogram edge cases; log, symlog and linear full histograms.
- **`tests/test_alpha_storage.cpp` (297)**: 13 unit tests.
  - Names; exact YAML bytes; discard, commit and no-replace; list and remove.
  - Startup sweep with live and stale locks; positions parquet; staging removed in the middle of a save.
- **`tools/run_engine.py` (146)**
  - Takes a flock on the build directory, then runs cmake configure and build of `longshort-server` using the module directory next to the config.
  - Runs the server in the foreground; writes no pid file.
- **`tools/restart_engine.py` (659)**, for port P:
  - Preflight: manifest asOf and the yaml's universe names.
  - Finds old servers through socket owners, the pid file and a /proc scan; sends SIGTERM.
  - Starts a detached server and checks `/api/config` simEnd and universes. On failure it relaunches the old argv/cwd/environ.
  - Does not build anything.
- **`tools/make_http_test_fixture.py` (68)**
  - 4 symbols, daily 2020–2025, wide `BaseData/*` and `groups/*` parquet.
  - JSON config with `universe: ["all"]`: no `universe.names`, no thin layout, no symbol labels.
- **`docs/math_contract.md` (229)**, API section "GA search and streamed results" (174-229)
  - Documents the `streamCandidates` events.
  - Says "Legacy requests without this flag retain generation events and the best-alpha complete".
  - Describes `metricsOnly`, `signalSeriesOnly` and `xmls`+`combinedOnly`, and says serialization runs in parallel.

## 1. Flow across the owned files

- **Simulate**
  - Handler 1005-1034 → `g_simulate_workers.launch`, one new thread per request (147-169).
  - `simulate_response` 429-594 → `parse_run_options` 203-291 → `compile_xml` (441, 449).
  - One alpha: `execute_alpha` 466. Several alphas: worker threads 473-535 → `OrderedSignalAccumulator::add_block` 492 → `run_from_signals` 573.
  - Body: `metrics_payload` 365 + `diagnostics_payload` 359 + `append_signal_payload` 381-425. The last runs 3 `std::async` serializers when the payload has ≥1e6 cells.
  - `copy_top_metrics` 376 copies the metric fields to the top level.
- **GA**
  - Handler 1040-1216 validates → `newAsyncStreamResponse` → `g_ga_workers.launch` 1065.
  - For each candidate, `evaluate` 1093-1110 parks a JSON payload in a map keyed by XML; `events.candidate` 1121-1141 takes it back out.
  - With `streamCandidates` → `search_complete` 1154-1162. Without it → legacy re-run 1164-1191 → `complete`.
- **Save**
  - `/api/alphas` POST → `run_alpha_request` 620-638 → `save_alpha` 684-846.
  - Metadata is validated via `parse_run_options` 714; every row is compiled at 736.
  - Row workers 756-819 call `simulate_response` once per row (787). A final combined `simulate_response` at 834 → `SaveTransaction` (alpha_storage.cpp:186-261).
- **Saved reads**: `saved_result_response` 672-682 serves the stored file bytes as they are.
- **The same decision is made in two or more places:**
  - `threads` default: hardware count at 474-475, 20 at 1053, min(hw, 20) at 757-758.
  - `combinedMode` validation: main.cpp:272, again at combined_signals.h:25-26.
  - Execution price: validated as a string at 206, mapped to the enum by string compares at 304-305.
  - Name validation: main.cpp:673, 687, 1239 and alpha_storage.cpp:174, 188.
  - Metadata keys: main.cpp:642, 645-654, 706-711 and alpha_storage.cpp:30-42.
  - Missing-close policy: config_loader, then again at main.cpp:886.
  - Response metadata block: 548-550 + 591-592, 1080-1087, 1178-1183.
  - Date formatting: main.cpp:94, main.cpp:950, json_serializer.cpp:7.
  - List of signal keys copied to the top level: 583-586 and 1185-1188.

## 2. Findings

### F1 [S] `/api/simulate` body depends on 5 booleans and the alpha count; the frontend uses 4 shapes
- **Where:**
  - 432 (`combinedOnly`); 434-449 (`xml` vs `xmls`); 247-250 (`collectSignals`, `metricsOnly`, `signalSeriesOnly`, `collectSignalsTopK`).
  - 452-454, 465-468; 552-565 (top-K replay).
  - 567-590 (single vs combined branches, top-level copies).
- **Evidence:**
  - frontend/lib/api.ts:261 always sends `collectSignals:true, collectSignalsTopK:5`.
  - The four frontend request kinds:
    - (a) detail: `metricsOnly` (GADetailLoader.tsx:25-26)
    - (b) signals: one concrete XML, nothing else (SignalPanelSelector.tsx:42; only shown when `alphas.length === 1`, :11-12)
    - (c) series: `signalSeriesOnly` + 1 symbol (SignalChart.tsx:143)
    - (d) aggregate: `xmls` + `combinedOnly` + `metricsOnly` (api.ts:284-285)
  - The frontend never triggers:
    - template expansion with per-batch metrics plus combined (469-537 when not `combinedOnly`);
    - the top-K replay (559-563: compile and fully re-simulate up to 5 alphas);
    - combined signal payloads (580-587).
  - `collectSignals` and `collectSignalsTopK` change nothing for any frontend request:
    - the single-alpha path collects when `!metrics_only` (466) and never reads `collect_signals`;
    - (d) always sets `metricsOnly`.
  - Two silent quirks:
    - `signalSeriesOnly` + `metricsOnly` returns no plot (569).
    - `combinedOnly` + `signalSeriesOnly` + `collectSignals` writes null `signalRange`, `distribution` and `signalDistributionLog` into both the response and `combined`. Lines 584-586 read missing keys with non-const `operator[]`, which creates them.
  - Clients that send a multi-alpha template:
    - only tests and `frontend/scripts/capture_engine_fixtures.mjs:16,27`;
    - every `cpp-engine/tools/check_*.py` sends a single alpha (check_group_neutralize_oracle.py:120, check_missing_data.py:245, check_team_sim_parity.py:99).
- **Why it is a problem:**
  - Every branch combines with every flag.
  - Signals end up in three different places: top level for one alpha, in the batch for top-K, in `combined` and also the top for combined.
  - The frontend adapter (api.ts:155-215) carries `?? resp.x` fallbacks to cope.
- **Simpler shape:** four named request kinds (detail, signals, series, aggregate). Delete the top-K replay, the combined-signal block, and the `collectSignals`/`collectSignalsTopK` keys.
- **Blast radius:**
  - main.cpp 247-250, 552-565, 580-587.
  - Tests MultiAlphaCollectSignalsTopKControlsPayloadCount (602), CombinedModesAndTopSignalPayloadsAreExplicit (867), SimEndAlsoCapsCombinedSignalsAndGa (504).
  - ConcreteCandidateAggregateMatchesOrdinaryCombinedPortfolio (1228) uses the multi-template body as its reference for (d) and needs another reference, e.g. N single runs.
  - Frontend api.ts:261 and the adapter fallbacks.
- **Test gate:** test_http_contract and frontend apiClient.contract.test.ts. Missing: `SimulateRequestKindsHaveDisjointKeySets`.
- **Effort:** M

### F2 [P] The GA "compatible complete" path (`streamCandidates:false`) is still live on the server
- **Where:**
  - 1049, 1051: the `popSize` and `generations` aliases.
  - 1088, 1100, 1114, 1122: four `if (stream_candidates)` branches.
  - 1144-1147: the `generation_end` event type, plus `generation` and `totalGenerations` keys that are also emitted on `round_end`.
  - 1164-1191: the legacy complete path, which re-compiles and re-runs the best alpha with signals.
  - Frontend handlers in useBacktest.ts:82-98, commented "Compatible complete events already contain daily performance".
- **Evidence:**
  - The frontend always sends `streamCandidates: true` (api.ts:309).
  - Requests without the flag come only from tests 519, 908 and 1039 (3 of the 4 GA tests) and `capture_engine_fixtures.mjs:31` (with `popSize`/`generations`), which feeds `__tests__/fixtures/cpp_ga_events.ndjson`.
  - 1164-1191 re-implements the single-alpha branch of `simulate_response`, hand-copies the same 4 signal keys as 583-586, and serializes `signalPlot` twice.
  - It checks `alphas.empty()` (1166), while the candidate path checks `size() != 1` (1096).
  - Test line 523 still strips an SSE `data:` prefix the server no longer sends.
- **Why it is a problem:** one endpoint speaks two event formats. The format the frontend actually uses (`search_complete`) has 1 test; the legacy one has 3.
- **Simpler shape:** delete the `streamCandidates:false` branch and the two aliases.
- **Blast radius:**
  - About 45 lines in main.cpp.
  - Tests 504, 899 and 1003 switch to `streamCandidates:true` and read `search_complete`.
  - useBacktest.ts:82-98 and the types `StarGACompleteEvent`/`StarGAGenerationEvent`.
  - The fixture script and its ndjson file; math_contract.md:197-198.
- **Test gate:** GaStreamsEveryUniqueCandidateBetweenRoundBoundaries (1161). Missing: `GaStreamCarriesSimEndAndMissingCloseInSearchStart`, to keep the simEnd and missingClose checks the legacy tests make today.
- **Effort:** M

### F3 [S] The same kind of failure gets a different HTTP outcome in each handler
- **Where:**
  - Simulate: 1020-1025 (any `std::exception` → 400), 1028-1034 (launch → 503).
  - GA: 1209-1214 (before streaming → 400), 1194-1199 (after headers → `error` event with HTTP 200), 1202-1205 (launch failure → log line only).
  - Alphas: 628-631 (`AlphaRequestError` → its own code, anything else → 500), 635-636 (launch → 503).
- **Evidence:**

  | failure | /api/simulate | /api/ga/stream | POST /api/alphas |
  |---|---|---|---|
  | malformed JSON | 400 (1016) | 400 (1044) | 400 (1226) |
  | bad option | 400 | 400 | 400, prefixed `metadata:` (716) |
  | XML compile error | 400 | candidate `failed`, or `error` event for the template (1077) | 400 (739) |
  | simulator runtime error (e.g. `missingClose:error`) | 400 | candidate `failed` | **500** (631, via 787) |
  | internal (`bad_alloc`, accumulator `overflow_error` at combined_signals.h:87) | 400 | `error` event, HTTP 200 | 500 |
  | server stopping / thread creation fails | 503 | **200 with an empty body** (1202-1205) | 503 |
  | cancelled | 400 (client already gone) | connection closed silently | 500 (`EvaluationCancelled` is a `runtime_error`, parallel_evaluator.h:11) |

  - check_missing_data.py:256-258 depends on simulate returning 400 for a runtime failure.
  - On GA launch failure the frontend can only say "GA connection ended before completion." (api.ts:351).
- **Why it is a problem:**
  - A client cannot tell a user error from a server fault: `bad_alloc` comes back as 400.
  - The same data error is 400 on a live run and 500 on save.
  - A stopping server answers GA with 200 and no events.
- **Simpler shape:** one catch-to-status mapping shared by all three handlers (merge 1020-1025 and 628-631 around the existing `AlphaRequestError`):
  - `invalid_argument` and `CompileError` → 400;
  - everything else → 500;
  - launch failure → 503.
- **Blast radius:** main.cpp handlers; check_missing_data.py and check_team_sim_parity.py:103-108; test line 1032.
- **Test gate:** InvalidMissingClosePolicyIsRejected, RemovedSettlementRequestOptionsAreRejected. Missing: `SaveRowRuntimeFailureStatusMatchesSimulate`, `GaInvalidBudgetsReturn400` (lines 1210-1215 never ran).
- **Effort:** S

### F4 [I] A save's `batch.json` step uses all cores, against the comment that caps it at 20
- **Where:**
  - 754-759: the comment and the 20-thread cap for the row phase.
  - 699-705: `run_body` copies only `kAlphaMetadataKeys` (642-643), which has no `threads`.
  - 824-834: `batch_request = run_body`.
  - 474-475: `threads == 0` means use the hardware count.
- **Evidence:**
  - `nproc` is 32, so the combined batch step starts min(32, rows) threads.
  - Comment at 754-755: "does not take every core".
- **Why it is a problem:** this happens on every save with ≥2 rows. Aggregate loads (20 threads) and a running GA (20) are often active at the same time.
- **Simpler shape:** set `batch_request["threads"]` to the `thread_count` already computed at 757-759.
- **Blast radius:** main.cpp:824-827.
- **Test gate:** ResultFilesEqualLiveSimulateBodies (1445). The combined result does not depend on thread count; MultiAlphaCombinedDeterministicAcrossRepeatedThreadedCalls checks that.
- **Effort:** S

### F5 [S] `threads` has three defaults and one upper bound; total compute threads are uncapped
- **Where:**
  - 246: parsed once, accepts 0 to UINT_MAX.
  - 474-476 simulate: 0 → hardware count, no upper bound.
  - 1053-1057 GA: 0 → 20, at most 32.
  - 757-759 save: 0 → min(hw, 20).
  - `BackgroundWorkers` 128-177: no size limit, no queue. Drogon IO threads: 4 (1291).
- **Evidence:**

  | thread group | size | bound |
  |---|---|---|
  | drogon IO loops | 4 | fixed |
  | `g_simulate_workers` | 1 per request | none |
  | ↳ multi-alpha workers | `threads` or 32, at most the alpha count | **none**: 5000 `xmls` with `threads` 5000 starts 5000 threads |
  | ↳ `std::async` serializers | 2 extra per signal payload when ≥1e6 cells (394-410) | 3 |
  | `g_ga_workers` | 1 controller per GA | none |
  | ↳ ParallelEvaluator | 20 by default, new threads every round | 32 |
  | `g_alpha_workers` | 1 per `/api/alphas*` request, including cheap GETs | none |
  | ↳ save rows / save batch | 20 / 32 (F4) | 20 / row count |

  - In normal use these overlap:
    - GACombinedLoader.tsx:14-15, 25 loads a round's aggregate (20 threads) as soon as that round stops running, while the GA continues with 20.
    - A concurrent save adds 20 and then 32.
    - That is 40-72 compute threads on 32 cores.
- **Why it is a problem:** the shared machine gets oversubscribed, and one request key means three different things.
- **Simpler shape:** bound and default `threads` once in `parse_run_options` (1..32, default 20) and use that everywhere.
- **Blast radius:** main.cpp:246, 474-476, 757-759, 1053-1058; math_contract.md:191.
- **Test gate:** InvalidMissingClosePolicyIsRejected (threads=5e9 → 400). Missing: `SimulateRejectsThreadsAbove32`.
- **Effort:** S

### F6 [T] Single-alpha `/api/simulate` ignores client disconnect, and it is the most frequent request
- **Where:**
  - 466: `execute_alpha(..., {}, accounting)` passes no row callback.
  - 1018: the `cancelled` check is used only by the multi-alpha path (496).
  - 561 and 573: top-K and combined replays have no check either.
  - 770 and 820: a save checks only between rows.
- **Evidence:**
  - Frontend kinds (a), (b) and (c) are single-alpha. Each aborts on change: GADetailLoader.tsx:37, SignalPanelSelector.tsx:54, SignalChart.tsx:143.
  - The server still compiles, simulates, computes metrics and serializes up to 4M cells after the abort.
  - Line 497 (the multi-path cancel) never ran in coverage, so nothing is tested for cancellation.
  - GA propagates disconnects at 1067, 1094, 1098, 1112 and 1168.
- **Why it is a problem:** fast row clicking queues full simulations on uncapped threads (F5), and shutdown waits for them.
- **Simpler shape:** pass the existing `cancelled` lambda as the row callback at 466, as GA already does at 1097-1098.
- **Blast radius:** main.cpp:466 (and 561/573 if those paths are kept).
- **Test gate:** none. Missing: `SimulateStopsWhenClientDisconnects`.
- **Effort:** S

### F7 [S] Saved results store the raw HTTP body, so today's response shape is frozen on disk
- **Where:**
  - 610-617: `json_text`, written "so their bytes can be served as is".
  - 787-789: each row file is a full `simulate_response`; 834: `batch.json`.
  - 672-682: files are served verbatim.
  - api.ts:388-401: `getSavedRow` and `getSavedBatch` go through the same adapter as live responses.
- **Evidence:**
  - Saved row files carry the duplicated top-level copies (F8) and the legacy keys (F13).
  - Tests 1492 and 1512 assert stored equals live.
- **Why it is a problem:** F1, F8 and F13 each become a data migration. The frontend must accept both shapes, or every saved alpha must be re-saved.
- **Simpler shape:** decide once. Either keep the verbatim body and make the frontend adapter the one tolerant reader, or store only `{summary, yearly, timeseries, missingData}` per row (partly in `index.json` already, 791-798).
- **Blast radius:** main.cpp save path, saved alphas under every `alphas_dir`, frontend adapters.
- **Test gate:** ResultFilesEqualLiveSimulateBodies. Missing (frontend): `SavedRowFromPreviousFormatStillLoads`.
- **Effort:** M (the decision itself is S)

### F8 [I] Metrics are serialized twice in single-alpha, aggregate and legacy-GA bodies
- **Where:**
  - 568 and 588 call `copy_top_metrics` (376-379), which copies `yearly`, `timeseries`, `isStartIndex`, `osStartIndex` and `missingData` to the top level.
  - 1184-1190: legacy GA complete puts metrics and signals both at the top and in `batches[0]`.
  - 583-586: the same top-level copy for combined signals.
- **Evidence:**
  - The frontend reads the batch or combined copy first: api.ts:155-158, 182-185 (`batch.timeseries ?? resp.timeseries`).
  - The top-level `yearly` set at api.ts:210 is never read (MAP_frontend §6.2).
  - api.ts:207 says so in a comment: "JSON contains duplicate wire objects".
  - Size estimate: 8 arrays × n bars × about 18 bytes, roughly 0.5 MB extra per body at n ≈ 3,700. This applies to kinds (a), (b), (d) and every saved row and batch file.
- **Why it is a problem:** twice the bytes on every detail load and saved row, and two places to read the same field from.
- **Simpler shape:** delete `copy_top_metrics`; metrics live only in `batches[i]` or `combined`.
- **Blast radius:**
  - main.cpp 376-379, 568, 588, 1184.
  - About 11 HTTP tests that read top-level metrics.
  - Frontend adapter fallbacks; saved files (F7).
- **Test gate:** test_http_contract.
- **Effort:** S, once F7 is decided

### F9 [I] The quantile `distribution` histogram and the `signalRange` scan run on every signal payload, but the frontend only uses them as a fallback
- **Where:**
  - main.cpp:405-407 and 412 (`distribution`), 414-424 (the `signalRange` scan over T×N).
  - json_serializer.cpp:224-335: 3 passes; 264-268 copy every finite nonzero cell into `sample`; 270-271 run two `nth_element`.
- **Evidence:**
  - SignalDistribution.tsx:25-31 draws the log histogram and its nested `linear` one.
  - It falls back to the quantile `distribution` only when `signalDistributionLog` is null. This engine always sends both (412-413).
  - `signalRange` covers the same values as `signalDistributionLog.min/max` (finite values from `begin`, json_serializer.cpp:353-366).
  - `sample` can hold T×N floats, about 47 MB at T=3,900, N=3,000 (estimate).
  - Parallelism:
    - the 3-way `std::async` (394) triggers at ≥1e6 cells, counted from row 0 including warmup;
    - it overlaps the plot task with the two histogram tasks;
    - it does not cover the single-threaded jsoncpp write in `newHttpJsonResponse` (1019);
    - the plausible gain is at most the histogram time; not measured.
- **Why it is a problem:** extra work and a large temporary allocation for something nobody displays.
- **Simpler shape:** delete the `distribution` task and the `signalRange` loop; take `signalRange` from the log histogram's min/max.
- **Blast radius:**
  - main.cpp 401-424; json_serializer.cpp 224-335.
  - 8 unit tests (98-187, 265-277); HTTP test lines 597, 630, 851-858, 922; api.ts:71-79.
  - Saved files contain no signal payloads, because rows are saved with `metricsOnly` (774, 827), so F7 does not block this.
- **Test gate:** SingleSignalPlotHonorsCellBudgetAndRejectsInvalidStart (827) and the paired-linear unit tests.
- **Effort:** S

### F10 [S] The parallel worker loop with first-error capture is written twice
- **Where:** 473-535 (multi-alpha simulate) and 756-819 (save rows).
- **Evidence:**
  - Both have the same skeleton: `next_index`/`failed` atomics, `first_error` + `error_mutex`, `while (!failed)` with fetch-add, `catch(...)` saving the first error, an emplace loop whose catch joins, then join and rethrow.
  - None of the failure branches has ever run: 515-521, 528-533, 802-807, 813-817.
- **Why it is a problem:** thread-creation failure and cancellation have to be right in two places, and neither is tested.
- **Simpler shape:** merge both into one file-local `run_parallel(count, threads, fn)` used by both call sites.
- **Blast radius:** main.cpp only.
- **Test gate:** MultiAlphaCombinedDeterministicAcrossRepeatedThreadedCalls, ResultFilesEqualLiveSimulateBodies. Missing: `CombinedReportsFirstFailingAlpha`.
- **Effort:** S

### F11 [S] The GA handler is a 176-line inline lambda, and candidate payloads go through a side map keyed by XML
- **Where:**
  - 1040-1215; the worker body is 1065-1201 and holds 7 nested lambdas.
  - `payloads` is declared at 1078-1079, filled at 1106-1107, and drained at 1125-1129, which throws "Missing evaluated candidate payload" on a miss.
- **Evidence:**
  - The evaluator returns only a `double` (`EvalFunc`, parallel_evaluator.h:23), so the JSON has to be matched back by XML string.
  - A miss ends the whole GA with an `error` event (1194-1199, a path that never ran in tests).
- **Why it is a problem:** the evaluation contract, the event format and the transport are tangled in one lambda, and one lookup miss is fatal.
- **Simpler shape:** move the worker body into a static function next to `simulate_response`, and carry the payload in `EvaluationResult`.
- **Blast radius:** main.cpp 1040-1215; `EvaluationResult` (E2's code); test_ga and test_determinism.
- **Test gate:** GaStreamsEveryUniqueCandidateBetweenRoundBoundaries. Missing: `GaStreamEmitsErrorEventAfterHeaders`.
- **Effort:** M

### F12 [S] Saved-alpha metadata keys and file paths are defined in both main.cpp and alpha_storage
- **Where:**
  - main.cpp:642-643 `kAlphaMetadataKeys`; 645-654 `metadata_json`; 706-711 JSON-to-struct copy.
  - alpha_storage.cpp:30-31 `kMetadataKeys`; 33-42 `metadata_field`.
  - File paths in main.cpp: 676, 789, 790, 834, 835, 1257, 1265, 1278. alpha_storage.h:9 documents the same layout.
- **Evidence:**
  - Adding one metadata key takes 5 edits across 2 files.
  - Row files are named at 789 (`results/rows/<id>.json`) and read back with a separately built string at 1278.
  - YAML: there is no hand-written YAML; both directions use yaml-cpp (alpha_storage.cpp:114-118, 124-129), and test 1361-1363 pins the emitted bytes.
- **Why it is a problem:** the storage module documents a layout it does not own, and the key set exists three times.
- **Simpler shape:** main.cpp iterates alpha_storage's key table through `metadata_field` in both JSON directions, and the `results/…` and `positions/…` path strings move next to the layout comment in alpha_storage.
- **Blast radius:** main.cpp 640-711, 789-835, 1252-1281; alpha_storage.{h,cpp}.
- **Test gate:** XmlLevelStoresEditorBytesAndToolbarMetadataOnly (1338), MetadataYamlHasExactlyTheSevenKeysInOrderAndRoundTrips (unit).
- **Effort:** S

### F13 [P] Legacy wire names are renamed on both ends, and `minBet` means "average" on the wire but "minimum" in the UI
- **Where:**
  - json_serializer.cpp:18 (`return`), 23 (`minBet` = average position count), 29 (`nonzeroPosition`), 31 (`minBetMin` = minimum position count), 91 (`nonzeroCount`).
  - api.ts:47-65 renames every one of them back.
  - AlphaRankingTable.tsx:52, 61: the column headed `minBet` shows `minPositionCount`.
- **Evidence:**
  - The internal names never appear on the wire.
  - Unit tests 35-59 pin the legacy names.
  - math_contract.md:77-90 and CLAUDE.md each need a sentence to explain the mismatch.
  - `winRate` (json_serializer.cpp:25) is sent but the adapter drops it.
- **Why it is a problem:** the same token means two different things across the boundary.
- **Simpler shape:** send the internal names and delete the adapter renames. This needs F7 decided first, because saved files use the old names.
- **Blast radius:** json_serializer.cpp and its tests; frontend types/index.ts:15-25, the adapter and fixtures; saved files.
- **Test gate:** SummaryPreservesLegacyKeysForPositionCounts; frontend contract test.
- **Effort:** M

### F14 [T] Drogon's default 1 MiB body limit is below what the engine's 5000-entry limits allow
- **Where:**
  - main.cpp:1287-1294 never calls `setClientMaxBodySize`.
  - Drogon v1.9.8 defaults to 1 MiB (`HttpAppFrameworkImpl.h:738`) and answers 413 (HttpRequestParser.cc:271, 376).
  - The engine accepts up to 5000 `xmls` (436-437) and 5000 save rows (723-724).
- **Evidence:**
  - 1,048,576 / 5000 = 210 bytes per entry, after JSON escaping of every `"` in the XML.
  - The frontend posts every successful candidate for the Stats aggregate (GACombinedLoader.tsx:30), and `stopAlphas` allows 5000 (1057).
  - Drogon's 413 body is not the engine's `{"error":…}` JSON.
  - No test sends a large body.
- **Why it is a problem:** two inconsistent limits for one thing, and the one that actually binds is invisible in the code.
- **Simpler shape:** set the drogon body limit from `MAX_ALPHA_COUNT` × a stated maximum XML size, or cap `stopAlphas` so the aggregate fits.
- **Blast radius:** main.cpp:1287-1294.
- **Test gate:** none. Missing: `AggregateOf5000ConcreteXmlsIsAcceptedOrRejectedAsJson`.
- **Effort:** S

### F15 [T] Handler branches that no test executes
- **Evidence:** main.cpp lines that never ran in coverage:
  - GA: 400 before streaming (1210-1215), budget check (1058), `error` event (1194-1199), send failure (1074), launch failure (1202-1205).
  - All 503 paths: 1028-1034, 635-636, and thread-creation failure 165-167.
  - Worker exception logging: 161-162.
  - Multi-alpha failure (515-521) and multi-alpha cancel (497).
  - Save row failure (802-807) and the commit race (838).
  - Invalid name in `saved_result_response` (674): the `..%2F` test at 1440 gets its 4xx from routing, not from this check.
  - Prefix-mask copy for a named universe (391-392).
  - `symbolLabels` (946).
  - `signalPlotSymbols` errors (258, 263, 267).
  - Non-string `universe` (284).
  - Two requests overlapping in one worker group (154): never happens.
- **Also untested (by reading):** malformed JSON, oversize bodies, client disconnect, the idle timeout (1293).
- **Fixture limits:**
  - The HTTP fixture has no `universe.names`, no thin layout and no labels (make_http_test_fixture.py:206-208).
  - So universe masks and `membership` strings are tested only in the serializer unit tests (224-263), never over HTTP.
- **Stale and risky test code:**
  - Test lines 298-299 mention a "strict-policy server below" that does not exist; MAP §7 #27 relied on that comment.
  - AlphaStorageStartup (1626) writes `.longshort_http_test_<pid>.yaml` next to whatever config it finds, including the `../../config.yaml` fallbacks (207-209).
- **Why it is a problem:** the failure, cancellation and concurrency halves of the handlers are not pinned, and those halves are where F3, F5, F6 and F10 sit.
- **Simpler shape:** add 2 `universe.names` to the fixture, then add the tests below; no new harness is needed.
- **Missing tests:** `MalformedJsonIs400OnEveryPost`, `GaInvalidBudgetsReturn400`, `GaStreamEmitsErrorEventAfterHeaders`, `SimulateStopsWhenClientDisconnects`, `NamedUniverseSignalPlotHasMembership`, `TwoConcurrentSimulatesBothAnswer`.
- **Effort:** M

### F16 [P] String request fields are read three different ways
- **Where:**
  - `xml` via `asString()` at 447 and 1045, but with an `isString()` check at 690 (save).
  - `isStart`, `osStart` and `simStart` via `asString()` at 214-215 and 221, with hidden defaults "2021-01-01" and "2024-01-01".
  - `simEnd` requires `isString` (233-235).
  - `missingClose` via `asString()` at 276.
- **Evidence:**
  - `asString()` turns numbers into strings (`xml: 42` becomes `"42"`).
  - For objects and arrays it throws jsoncpp's own "Type is not convertible to string", the kind of internal message the comment at 80 says to avoid.
  - Save requires every metadata date (701-703), while live simulate silently fills in defaults.
  - The `xml` check at 447-448 and 1045-1046 is duplicated with the same message.
- **Why it is a problem:** the same field is validated differently per handler, and error text depends on the JSON type.
- **Simpler shape:** one `string_option` helper next to `parse_uint_option`, and read `xml` once inside `parse_run_options`.
- **Blast radius:** main.cpp 203-291, 447, 690, 1045.
- **Test gate:** ExecutionPriceIsValidatedAndScopedToEachRequest, SimEndIsInclusiveAndCapsAllOutputsWithoutChangingSharedData. Missing: `NonStringDatesAndXmlReturnReadable400`.
- **Effort:** S

### F17 [I] A save evaluates each row's pipeline up to three times
- **Where:**
  - 736: compile, to validate the row.
  - 787 → 449/466: compile and simulate the row.
  - 834 → 441/503: compile and `run_signal_rows` again for every row to build `batch.json`.
- **Evidence:** the row runs are `metricsOnly` (774) and throw their signals away; the batch call (826) then rebuilds the same signals.
- **Why it is a problem:** a save with ≥2 rows costs roughly twice the row pipeline time.
- **Simpler shape:** feed each row's signal rows into one `OrderedSignalAccumulator` during the row pass, then call `run_from_signals` once.
- **Blast radius:** save path; stored bytes must stay identical.
- **Test gate:** ResultFilesEqualLiveSimulateBodies.
- **Effort:** M

### F18 [P] Three `BackgroundWorkers` groups differ only by log name, and they stop one after another
- **Where:** 178-180 and 1296-1298; the cancellation checks at 751, 1018, 1067 and 1112 each read their own group's flag.
- **Evidence:**
  - Each group's stopping flag is set only after the previous group has been joined.
  - So a save keeps replaying rows while the GA and simulate threads are being joined.
  - restart_engine.py gives up after 60 s (`STOP_GRACE_S`, :72).
  - The class is not a pool; it just keeps track of one thread per request.
- **Why it is a problem:** shutdown takes the sum of the in-flight work instead of the maximum.
- **Simpler shape:** one instance, or set all three stopping flags before the first join.
- **Blast radius:** main.cpp only.
- **Test gate:** none. Missing: `SigtermDuringSaveExitsWithinGrace`.
- **Effort:** S

### F19 [P] Dead branches and small duplicates
- **Where / Evidence:**
  - 959-966: the `/api/config` "no timestamps" branch cannot run. The loader guarantees T ≥ 1 and trade_start < T (parquet_reader.cpp:726, 811-812); the branch never ran in coverage.
  - 886-890: re-checks the missing-close policy that config_loader already rejected (its own comment at 885 says so); 888-889 never ran.
  - 970: `supportsSimEnd` is always `true`, yet the frontend branches on it 3 times (useBacktest.ts:92, 107; SettingsBar.tsx:32).
  - 359-363: `diagnostics_payload` wraps a single key and ignores its `RunOptions` argument; it is merged by 3 identical loops (555-556, 577-578, 1171-1172).
  - Three date formatters (94-102, 950-957, json_serializer.cpp:7-14).
  - The response metadata block is built in three places.
  - `combinedMode` is validated twice.
  - `OrderedSignalAccumulator::add` (combined_signals.h:29-54) is only called by tests; production uses `add_block` (492).
  - `/api/config` fields nobody reads: `thinLayout`, `loadedDataBytes` and `combinedMode` have 0 readers repo-wide.
    - `symbols` is declared in the frontend types but never read.
    - Readers outside the frontend: `T` and `N` (restart_engine.py:375, 606), `barsPerYear` (check_team_sim_parity.py:112), `loadedFields` (check_wide_data_oracle.py:108), `columns` (check_group_neutralize_oracle.py:230 and a test).
- **Simpler shape:** delete the listed dead branches, unused keys and the test-only method; keep one date formatter and one metadata-block helper.
- **Blast radius:** main.cpp, combined_signals.h, test_combined_alpha.cpp (switch to `add_block` with 1-row blocks), the frontend `supportsSimEnd` branches and types.
- **Test gate:** the full HTTP suite and test_combined_alpha.
- **Effort:** S

### F20 [P] Launcher tools have silent fallbacks, and the 659-line restart script has no tests
- **Where:**
  - run_engine.py:94-99 creates a missing modules directory without complaint.
  - restart_engine.py:212-227: the asOf fallback chain `--asof` → manifest `asOfDate` → manifest `asOf` → max date in `base_data.parquet`, with only a warning.
  - restart_engine.py:622-623: `--build`/`--prev` are accepted and ignored.
  - restart_engine.py:67: `DS` is defined and never used.
  - restart_engine.sh is referenced nowhere and hard-codes the venv Python path.
- **Evidence:**
  - A mistyped `--modules-dir` builds without the user's modules; test_run_engine.py:103-110 only covers the "path is a file" case.
  - restart_engine.py has 0 tests.
  - The flock and pid logic is not duplicated between the two scripts:
    - run_engine locks the build directory to serialize builds (40-49);
    - restart_engine locks a per-port file (631-637) and owns the pid file (628);
    - both default to the same binary path.
  - restart_engine never rebuilds, so a module change needs `run_engine.py --build-only` first; the script does not say so.
- **Why it is a problem:** the only rollback logic for the live engine is unpinned, and deployment tooling falls back silently.
- **Simpler shape:** fail when an explicit `--modules-dir` is missing; delete `DS`, the `asOf` fallback key and the ignored arguments.
- **Blast radius:** run_engine.py, restart_engine.py, test_run_engine.py.
- **Test gate:** test_run_engine.py (11 tests). Missing: `test_restart_rolls_back_when_new_server_fails_verification`, `test_missing_explicit_modules_dir_is_an_error`.
- **Effort:** M

## 3. Looks odd but is fine

- **Name validation in both main.cpp and alpha_storage.** main's check sets the 400; alpha_storage's checks protect its own invariant for direct callers and cannot be reached from HTTP.
- **`exists` check (744) before the RENAME_NOREPLACE commit (837).** It fails fast before minutes of row replay; the commit still handles the race.
- **Top-K recompiles from `source_xml` (510-512, 559-561).** Deliberate, to avoid holding T×N signals per alpha in memory.
- **The accumulator's per-row alpha-order wait.** Required for deterministic float sums. The earliest unfinished alpha can always make progress, and `cancel()` wakes waiters on any failure (519, 530).
- **`newAsyncStreamResponse(..., true)` (1206).**
  - The `true` disables drogon's kickoff timeout, which long GA runs need.
  - There is no backpressure, but streaming events are summaries only (test 1189), so output is bounded by `stopAlphas` × about 1-2 KB.
- **An `error` event after headers.** It is the only possible shape once a 200 has gone out, and the frontend treats it as terminal (api.ts:328, useBacktest.ts:99-101).
- **restart_engine.py's SO_REUSEPORT guard.** Justified: drogon on Linux binds with SO_REUSEPORT (HttpServer.cc:72-73, TcpServer.h:53).
- **`parse_uint_option`.** It stops jsoncpp's internal range message from leaking, and test 1065-1072 pins it.
- **`kMaxRowId` used in both save and GET.** One constant applying the same JavaScript-safe rule in both directions.
- **Save is not cancelled when the client disconnects.** Correct for a write; a failing row does cancel the other rows (788).
- **`/api/health`.** Used only by the test harness (281); a cheap liveness check worth keeping.

## 4. Unverified

- **F9:** how much of the wall time the `std::async` overlap actually saves compared with the single-threaded jsoncpp write. Confirm by timing `append_signal_payload` and `newHttpJsonResponse` separately on the workspace panel with a scratch build.
- **F14:** whether real GA XMLs average ≥210 bytes once JSON-escaped. Confirm by measuring `len(json.dumps(xml))` over one real GA's candidates.
- **`catch (...)` at 161-162:** whether any non-`std::exception` can reach it. If one did, the client would wait until the 3600 s idle timeout. No such thrower exists in the owned code; Arrow and pugixml calls in other subsystems were not checked.
- **F8:** the exact byte cost of the duplicated metrics. `~/longshort_workspace/alphas` is empty, so there was no saved file to measure; the figure is an estimate.
- **Shutdown:** whether a worker's callback is still delivered after `app().run()` returns (1296-1298). Harmless if it is dropped; confirm with a SIGTERM during a simulate on a test port.

## 5. Top 5

1. **F4:** a one-line fix to a real behavior bug that contradicts its own comment; every multi-row save takes 32 cores.
2. **F6:** a one-line fix so the frontend's most frequent requests stop when the browser aborts them, instead of running to completion on uncapped threads; add the disconnect test with it.
3. **F2:** deletes a whole second GA event format (about 45 engine lines plus frontend handlers, aliases and a fixture pipeline) that production never uses; the three tests convert directly.
4. **F1 (after deciding F7):** the main structural simplification, turning the boolean product into 4 named request kinds and removing the top-K and combined-signal paths. It must wait for the decision on saved verbatim bodies.
5. **F9:** removes a dead histogram and a redundant scan from every signal payload; small, safe, and not blocked by saved files.

Next in line: F3 (status mapping) and F5 (thread bounds).