## Summary (audit_f2)

The full report is below. **It was not saved to `FINDINGS_F2.md`**: the harness refused the write ("Subagents should return findings as text"). Save the text below to `.../refactor_audit/FINDINGS_F2.md` if you want it on disk.

Top 5:
1. **F2 (S/T)**: `mockRun` drives 7 of 11 e2e specs through a GA `complete` event. The engine never sends that event when `streamCandidates:true`, which the frontend always sets. When the engine does send it, it has one batch, but the specs feed 2 batches plus combined. The production candidate-stream path for multi-alpha results is therefore unguarded.
2. **F3 (P)**: the adapter's `?? resp.<top-level>` fallbacks hide missing engine fields. In a probe, deleting one batch's `timeseries` silently showed the combined PnL under the name `alpha_1`.
3. **F1 (S)**: performance series make a round trip (array → per-day dicts → merge → sort → re-sum → re-split) that recomputes the engine's own `cumPnl` and drawdown. The probe shows the result is an exact copy (max error 0). The OS start is decided 3 times.
4. **F5 (T)**: the Sep-11 fixtures are stale. They still carry `mode` and lack about 15 keys the engine now writes. Their drawdown values are net-based, while the engine now computes gross. `cppFixtures.contract` only checks the adapter against its own output.
5. **F7 (I/T)**: if a GA stream line fails to parse or a handler throws, `runGAStream` never cancels the reader (0 calls in the probe). The engine keeps running the GA until the next run.

Other findings:
- **F4**: three histograms are computed per signal request; log vs linear branches in 13 places, and the legacy one is all the fixtures exercise.
- **F6**: the heatmap worker path is never reached by any test (e2e max is 20k cells against a 500k threshold), while production heatmaps are about 4M cells. Three caches guard the same work.
- **F8**: positional request builders; the run settings are rebuilt at 4 call sites and GA defaults live in 4 layers.
- **F9**: error parsing still handles the old Python backend's `detail` shape.
- **F10**: dead fee-based gross fallback (with a 10bp default) and an unreachable `minBet` chart.
- **F11**: chart setup duplicated 3 times, and 5 different formatters for the same signal value.
- **F12**: the hard-coded theme palette has drifted from `globals.css`.
- **F13**: unmounted `BacktestMetrics`, and fields that are written but never read.
- **F14**: the Pin snapshot feature is live, untested, and keeps pinned series across runs.
- **F15**: SignalChart re-sends its full-series request on unrelated settings edits.
- **F16**: e2e assertions on pixels, CSS literals and removed text.
- **F17**: SignalHeatmap's ref and callback scaffolding.

Scratch probes are deleted and nothing in the repo changed. `df` stayed at 67%.

---

# FINDINGS_F2 — frontend API client, types, charts, fixtures (audit_f2, 2026-10-14)

Working tree of /home/jgo/longshort_platform/frontend, read-only. Engine cross-checks read from cpp-engine/src/api/json_serializer.cpp, cpp-engine/src/main.cpp, cpp-engine/src/metrics/timeseries_output.cpp and cpp-engine/tests/test_http_contract.cpp. MAP_frontend.md numbers are reused and cited as "MAP §x".

Probes: three vitest files under refactor_audit/audit_f2/, run with a scratch config (`npx vitest run --config <scratch>/vitest.probe.config.mts`, root = scratch, `@` → frontend). Results are quoted inline as "probe P1..P4". All scratch files have been deleted.

## 0. Scope read

- `lib/api.ts (405)`:
  - Maps wire summaries to internal names (`adaptStarSummary`).
  - Converts timeseries arrays into per-date IS/OS dicts of daily diffs (`buildSplitSeriesFromTimeseries`).
  - Builds combined/alphas from batches, with top-level fallbacks.
  - Fetch wrappers for simulate, the NDJSON GA stream and alpha CRUD.
- `types/index.ts (352)`:
  - `Star*` wire types mirror json_serializer.
  - `BacktestAlphaResult`, `BacktestRunResponse`, `PeriodMetrics` and `SignalPayload` are internal display types (header comment says "backward-compatible").
  - Also settings, saved-alpha, snapshot and GA progress types.
- `lib/chartInteraction.ts (161)`: uPlot plugin that builds a fixed tooltip and date badge outside React. Adds drag-pan, wheel-zoom and double-click reset, batched through rAF. `clampRange` keeps the zoom width inside the data bounds.
- `lib/heatmapWorkers.ts (51)`:
  - Below 500k cells, or when no `Worker` exists, it normalizes synchronously.
  - Otherwise it flattens rows into Float32Arrays, splits them across up to 2 module workers and reassembles the row views.
  - Results are cached in its own WeakMap.
  - Cancel terminates the workers; a worker failure falls back to the sync path.
- `workers/heatmap.worker.ts (16)`: rebuilds row views from the flat buffer, runs `robustCrossSection`, transfers the result back.
- `lib/heatmapStats.ts (56)`: per-date median/MAD z-score (population SD when MAD is 0) into Float32Array rows. Cached in a second WeakMap keyed by values identity and column count.
- `lib/colorScale.ts (44)`: blends z from the background toward red/blue. `zscoreToRgb` is used for pixels; the `zscoreToColor`/`signalToColor` string helpers are test-only (MAP §3.1).
- `lib/themeColors.ts (34)`: hard-coded light/dark palettes. CSS variables override them only when the requested theme equals the DOM theme.
- `lib/uplotAdapters.ts (83)`:
  - `useContainerSize`: ResizeObserver plus rAF measurement.
  - `useUPlot`: recreates the plot when the options identity changes, keeps the x-zoom when `resetKey` is unchanged, otherwise calls setData/setSize.
- `lib/uplotUtils.ts (46)`: date/unix conversion and labels, record rows → AlignedData, shared axes. `getDPR` and `TOOLTIP_STYLE` are dead.
- Small libs:
  - `metricColors (13)`: sign → CSS class.
  - `reportUnits (4)`: $20M book constants.
  - `signalPanelLayout (3)`: panel heights 64 / 192.
  - `snapshotColors (6)`: 8-colour palette.
  - `symbolLabels (11)`: "TICKER (id)" label and search.
  - `universeMembership (27)`: membership string lookup and in/out series split.
- `DetailTimeSeries.tsx (510)`:
  - Merges the IS/OS dicts and re-accumulates net/gross PnL (with a fee-reconstruction fallback).
  - Recomputes drawdown from gross equity and re-splits it at the OS date.
  - Renders the PnL chart (gradient IS/OS stroke) or one of 5 `BigChart` metric charts (IS/OS as two merged series).
- `SignalChart.tsx (340)`:
  - Shows one symbol's signal column from the heatmap, or a fetched unsampled series (LRU of 32 per revision).
  - Merges pinned snapshots and draws in/out-of-universe series with uPlot.
  - Includes a virtualized data table and a Pin button.
- `SignalHeatmap.tsx (393)`:
  - Normalizes through the workers and paints a pixel-grid ImageData (each pixel shows its nearest cell).
  - Caches the base image so selection changes redraw only an overlay.
  - Imperative tooltip, symbol search, click-to-select.
- `SignalDistribution.tsx (110)`: chooses between log bins, the paired linear bins and the legacy quantile histogram. Renders DOM bars and a click strip that jumps to each bin's example cell.
- `DetailSummaryTable.tsx (60)`: yearly table in $20M report-book units (pnl pp → $M, margin → bps), newest year first.
- `BacktestMetrics.tsx (131)`: IS/OS metric rows. Not mounted anywhere (MAP §1.1); only its test imports it.
- `SnapshotManager.tsx (43)`: lists, renames and removes pinned snapshots.
- `AlphaDetailView.tsx (76)`: picks the selected or combined alpha from the whole `result`; 5 metric buttons, chart and yearly table.
- `XmlEditor.tsx (61)`: dynamic Monaco from /monaco/vs, controlled value; a ref comparison stops the editor echoing its own text back.
- `MainLayout.tsx (115)`: resizable two-pane layout (rAF-throttled drag, arrow keys).
- `ThemeProvider.tsx (12)`: calls `initializeTheme()` once on mount.
- `__tests__/fixtures/{cpp_single.json 1539, cpp_multi.json 3559, cpp_ga_events.ndjson 2, capture.json 41}`: Sep-11 captures from scripts/capture_engine_fixtures.mjs (`signalPlotMaxCells: 60`, `mode: 1`, non-streaming GA with popSize/generations).
- `pipelineFixtures.ts (22)`: adapts the captured JSON at import time.
- `metricFixtures.ts (43)`: hand-built results in the internal vocabulary.
- `tests/helpers/mockRun.ts (17)`: routes `/api/simulate` to a handler and wraps the same JSON as a one-line `complete` NDJSON for `/api/ga/stream`.
- `playwright.config.ts (31)`: chromium only, port 3000 by default, dev server with an isolated `.next-playwright` distDir.
- Tests read in full: adaptSimulateResponse (297), apiClient.contract (180), cppFixtures.contract (36), chartInteraction (37), DetailTimeSeries.derived (105), heatmapStats (56), signalColors (23), universeMembership (18), uplotAdapters (73), BacktestMetrics.render (40), DetailSummaryTable.render (76), logDistribution (56), XmlEditor (70), and all 11 e2e specs.

## 1. Flow across the owned files

**Signals (single alpha)**
1. SignalPanelSelector:42 calls `runSimulate` (api.ts:249), then `adaptSignalPayload` (api.ts:67), then `cacheAlphaSignals`.
2. The panels read through `selectSignalSource` (signalSelection.ts:3): SignalDistribution:18-23 (6 selectors), SignalHeatmap:37, SignalChart:109.
3. SignalHeatmap:43-52 calls `normalizeHeatmap` (heatmapWorkers.ts:5), which uses the worker or `robustCrossSection`, sets `normalized`, then calls `drawBase` (:135).
4. A click calls `setSelectedCell`. SignalChart:176-194 then reads the column from the heatmap, or for a row-sampled heatmap calls `runSimulate({signalSeriesOnly, signalPlotSymbols})` (SignalChart:143).

**Performance detail**
1. GADetailLoader:25 calls `runSimulate` with metricsOnly, then `adaptSimulateResponse` (api.ts:141).
2. `buildSplitSeriesFromTimeseries` (api.ts:103) splits the arrays at osStartIndex into 12 IS/OS daily-diff dicts.
3. AlphaDetailView:21 picks the alpha.
4. DetailTimeSeries:375-389 merges the dicts again; `buildPnlBundleFromRet` (:53) re-accumulates; `buildMddDataFromEquity` (:111) recomputes drawdown; :396-408 re-splits at osStartDate.
5. PnlDualChart (:160) or BigChart (:279, which merges again via `mergeIsOsSeries` :128) render through `useUPlot` and `chartInteraction`.
6. DetailSummaryTable reads `alpha.yearly`.

**GA run**
- useBacktest:59 calls `runGAStream` (api.ts:291), which parses NDJSON (:326-347) and calls `onEvent`.
- Each `candidate` event goes through `adaptGACandidate` (api.ts:223).
- A `complete` event goes through `adaptGACompleteToResponse` (api.ts:219), which is just `adaptSimulateResponse`. Only test mocks reach this branch (F2).

**Decisions made more than once**
- Which alpha supplies signals: signalSelection.ts:6-9, SignalChart.tsx:123, SignalPanelSelector.tsx:21.
- Where OS starts: api.ts:110 (index), DT:401 (date string), DT:187-198 (pixel).
- Whether single-alpha signals sit in the batch or at top level: api.ts:190-193, SignalChart:146, SignalPanelSelector:45.
- Theme colours: CSS variables vs the themeColors.ts:3-14 palette. The localStorage theme is read in layout.tsx:35 and themeStore.ts:36.
- Snapshot cap of 8: alphaStore:443, SignalChart:329, length of `SNAPSHOT_COLORS`.
- GA defaults: alphaStore:129-132, useBacktest:52/64/65/75/108, api.ts:309, main.cpp:1049-1052.
- Signal value formatting: 5 different rules (F11).

## 2. Findings

### F1 [S] Performance series round trip (array → dict → merge → sort → re-sum → re-split) recomputes what the engine already sends
- **Where:** api.ts:83-138; DetailTimeSeries.tsx:53-109, :111-126, :128-150, :375-442, :396-408; types/index.ts:242-256.
- **Evidence:**
  - The engine sends `cumPnl`, `cumPnlGross` and `drawdown` arrays (json_serializer.cpp:68-96).
  - The adapter turns them into daily diffs split at the OS index (api.ts:95-101, :108-136). The component merges them (DT:375-389), sorts (DT:63-66, :47-48) and sums them again (DT:81-106).
  - Probe P2 on both fixtures: max |pnlData − (cumPnl − cumPnl[is−1]) × book| = 0.00e+0, so the result is an exact copy.
  - The frontend drawdown formula (DT:119-123, gross, peak starting at 0) is the engine's formula (timeseries_output.cpp:52-56).
  - The OS boundary is decided 3 times (api.ts:110, DT:401, DT:187-198).
  - BigChart colours IS/OS with two series plus a bridging point (DT:128-150); the PnL chart uses a gradient stroke (DT:186-199).
- **Why it is a problem:** about 150 lines and 12 optional fields re-derive engine arrays. All 8 `seriesToChartData` memos run even though one metric is shown. The three OS-split rules can disagree on an edge date.
- **Simpler shape:** keep `timeseries` plus `isStartIndex`/`osStartIndex` on the alpha and plot array slices directly, using `periodStroke` for every metric.
- **Blast radius:**
  - Code: api.ts, types (12 series fields + osStartDate), DetailTimeSeries.tsx, alphaStore:252 eviction list, GADetailLoader KEEP.
  - Tests: DetailTimeSeries.derived (7), adaptSimulateResponse (:110-140, :165-173, :235-240), cppFixtures.contract:14-15.
- **Test gate:** e2e chart_interaction:89-127 (hover "Net: $-1.01M", IS amber / OS green pixels) and metrics_pipeline:58-62. Missing: a vitest asserting the plotted PnL and drawdown equal the `cumPnlGross`/`drawdown` slices for a fixture with isStartIndex > 0.
- **Effort:** M

### F2 [S][T] 7 of 11 e2e specs use a GA `complete` event the engine never sends to this frontend
- **Where:**
  - tests/helpers/mockRun.ts:4-16, used by alpha_selection, chart_interaction, detail_layout, execution_price, metrics_pipeline, signal_panels and sim_period (MAP §7 #13).
  - The `complete`-only handlers it feeds: useBacktest.ts:82-98, api.ts:219-221.
- **Evidence:**
  - The engine emits `generation_end`/`complete` only when `streamCandidates` is false (main.cpp:1059, :1144, :1154-1191). The frontend always sends `streamCandidates: true` (api.ts:309).
  - Even then, the engine's `complete` carries exactly one batch (main.cpp:1189-1190). The mocks feed cpp_multi (2 batches + combined).
  - So multi-alpha e2e results are built by the adapter's multi branch with `detailLoaded: true` for every alpha (useBacktest:95). GADetailLoader and GACombinedLoader never fire in these specs.
  - chart_interaction:20 mocks the single-symbol series in `batches[0].signalPlot`. For a single alpha the engine writes signals at top level only (main.cpp:559 `!single`, :567-570; test_http_contract.cpp:1216-1222). The spec therefore passes only through SignalChart.tsx:146's dead `batches[0]?.signalPlot` branch. signal_panels:16 also writes the per-batch slot.
  - Only ga_default.spec and single_alpha_view.spec use the real candidate stream.
- **Why it is a problem:** the browser suite stays green while the production flow (candidate stream → detail and combined loaders) can break. It also keeps 3 handlers, 2 types and the multi-batch adapter branch alive for tests only.
- **Simpler shape:** have mockRun emit round_start/candidate/round_end/search_complete (as ga_default:19-33 already does) and have simulate mocks return top-level signals. Then delete the generation_end and complete handlers.
- **Blast radius:**
  - Test side: mockRun plus 7 specs, pipelineFixtures:18-22, cppFixtures.contract test 3, apiClient.contract:124-144, simPeriod/useBacktest.pipeline (F1 scope).
  - Code side: useBacktest:82-98, api.ts:219-221 and the multi branch :156-180, the `StarGAGenerationEvent`/`StarGACompleteEvent` types.
- **Test gate:** single_alpha_view and ga_default cover streaming. Missing: a multi-candidate streamed run asserting the metricsOnly and combinedOnly requests.
- **Effort:** L

### F3 [P] Adapter fallbacks hide missing engine fields and can render the wrong curve
- **Where:** api.ts:157-160, :165-168, :175, :183-185, :190-193, :200, :212-215, :72-79.
- **Evidence:**
  - The engine writes `timeseries`, `yearly`, `isStartIndex` and `osStartIndex` on every batch (metrics_payload, main.cpp:551). The top level holds copies of the combined or single result (`copy_top_metrics` :569, :588).
  - Probe P1: deleting `batches[1].timeseries` makes `alphas[1].isRetSeries` equal the combined series (true), still labelled `alpha_1`.
  - Deleting `combined` gives `combined.isMetrics.sharpe` = −3.563, which is batch 0's value, still labelled "combined" (`resp.combined ?? resp.batches[0]`, :157).
  - The multi-batch branch is reachable only through `complete` (F2). Every live caller passes one batch: GADetailLoader:30, api.ts:287 and :398.
  - The tails at :212-215 duplicate :190-193.
  - `adaptSignalPayload` turns absent fields into null (:72, :74-79). That forces GADetailLoader:7-8's 10-key KEEP set.
  - Tests pin the masking behaviour (adaptSimulateResponse.test :67, :143, :243).
- **Why it is a problem:** an engine regression would show the wrong alpha's PnL under a confident label instead of raising an error.
- **Simpler shape:** a single-batch adapter that reads only batch fields (top level for single-alpha signals), throws on missing required fields, and leaves absent signals `undefined`.
- **Blast radius:** api.ts, GADetailLoader KEEP, 3 tests in adaptSimulateResponse.test, cppFixtures.contract.
- **Test gate:** adaptSimulateResponse.test. Missing: "a batch without timeseries throws".
- **Effort:** S

### F4 [P] Three histograms per signal request; log/linear branches in 13 places; fixtures exercise only the legacy one
- **Where:**
  - Engine: main.cpp:403-413 (3 async histogram tasks); json_serializer.cpp:224-335 (quantile histogram), :447-457 (log + `.linear`, with the comment "for older clients").
  - Frontend: api.ts:71-79; SignalDistribution.tsx:19-23, :25, :27-31, :54-58, :76; alphaStore:252; types:59-74, :219-227.
- **Evidence:**
  - The engine always writes `signalDistributionLog` when it collects signals (main.cpp:409-413).
  - The legacy `distribution*` fields are displayed only when the log histogram is absent (SignalDistribution:27-31), which never happens with the current engine.
  - Branch count: SignalDistribution has 5 `isLog` ternaries, 3 `??` fallbacks, 3 Log-button attributes and 1 edges check; api.ts has 2; the store has 1. Total 13.
  - The fixtures have no log histogram. signal_panels.spec:35 therefore expects 50 bars while the engine bins are 200 (json_serializer.h:33). logDistribution.test:34 tests the "older linear-only response" on purpose.
- **Why it is a problem:** the engine runs an extra pass over every signal (T×N, with nth_element) per request for a fallback nobody sees. Readers must track 3 histogram shapes across 7 `SignalPayload` fields.
- **Simpler shape:** drop the quantile `distribution` and the 4 `distribution*` fields; use `signalDistributionLog` and its `linear` twin only.
- **Blast radius:** api.ts, types, SignalDistribution, alphaStore:252, logDistribution.test (test 2), signal_panels:35, single_alpha_view:17. Engine changes are optional.
- **Test gate:** logDistribution.test tests 1 and 3. Missing: an e2e with a captured 200-bin log histogram.
- **Effort:** M

### F5 [T] Fixtures are a Sep-11 snapshot; `cppFixtures.contract` is self-consistency only
- **Where:** __tests__/fixtures/* (dated 2026-09-11); cppFixtures.contract.test.ts:4-35; pipelineFixtures.ts:14-22.
- **Evidence:**
  - Fixtures still carry `mode`; `grep '"mode"' main.cpp` returns 0.
  - About 15 keys the engine now writes are absent: executionPrice, simStart, simEnd, universe, missingData (top level and per batch), timeseries.frozenGross, yearly ir/winLose/fitness, distribution rangeLo/rangeHi/exampleSymbol/exampleDate, signalDistributionLog, signalPlot.membership.
  - `capture.config` lacks supportsSimEnd, executionPrices, universes, defaultUniverse, alphasDir and symbolLabels (MAP §1.2).
  - The values are stale too. Fixture `drawdown` equals a net-based drawdown exactly (net 0.00e+0 vs gross 1.98e-3); the engine now computes it on gross (timeseries_output.cpp:54-56).
  - The NDJSON fixture contains only generation_end + complete (captured with popSize/generations, capture_engine_fixtures.mjs:31).
  - No engine test reads these files (grep = 0).
  - The contract test asserts adapter(fixture) == fixture fields, which says nothing about today's wire format.
- **Why it is a problem:** "contract" tests give false assurance and keep the F3, F4 and F10 fallbacks alive.
- **Simpler shape:** regenerate the 4 fixtures from the current engine with `streamCandidates: true`, and delete the hand-patched fields the specs inject (signal_panels:12-16, single_alpha_view:17, sim_period:10-11, execution_price:8-9).
- **Blast radius:** 4 fixture files, the capture script, DetailSummaryTable.render expected numbers (:31-33), chart_interaction:101, cppFixtures.contract.
- **Test gate:** cppFixtures.contract. Missing: a key-set diff against a fresh capture.
- **Effort:** M

### F6 [T][P] The heatmap worker path runs on every production heatmap but no test reaches it; three caches guard one computation
- **Where:** heatmapWorkers.ts:3, :8, :11-50; heatmapStats.ts:4, :14-15, :52-55; SignalHeatmap.tsx:38-52, :174-175.
- **Evidence:**
  - The engine's default `signal_max_cells` is 4,000,000 (main.cpp:194, :251) and the frontend never sends a cap. Production heatmaps are therefore about 4M cells, above the 500k worker threshold.
  - Probe P4 (V8): `robustCrossSection` takes 619 ms at 1333×3000, 605 ms at 800×5000 and 71 ms at 500k cells; the flatten step takes 17-20 ms.
  - The largest e2e panel is 2000×10 = 20k cells (signal_panels:12-13); chart_interaction is 1500×6. jsdom has no `Worker`, so the split/reassembly and cancel race in :16-50 have 0 coverage.
  - Caches:
    - One WeakMap in heatmapWorkers (worker results, :3, :36-39).
    - Another in heatmapStats (sync results, :4, :52-55).
    - A per-worker copy that dies with the worker (:46).
    - SignalHeatmap's own identity check (:175).
  - Both WeakMaps add a per-columns Map level, but columns is always `symbols.length` (SignalHeatmap:42, :45).
- **Why it is a problem:** the only production path is untested, and the two caches never see each other's results.
- **Simpler shape:** one WeakMap keyed by `values` in `normalizeHeatmap`; no cache inside `robustCrossSection`.
- **Blast radius:** heatmapWorkers.ts, heatmapStats.ts, heatmapStats.test:44-55 and chartInteraction.test:29-37 (both assert cache identity).
- **Test gate:** heatmapStats.test covers the maths. Missing: a test with at least 500k cells (e2e, or vitest with a Worker shim) checking per-date pixel signs as signal_panels:48-70 does.
- **Effort:** S

### F7 [I][T] `runGAStream` leaves the stream open on a parse or handler error, so the engine keeps running the GA
- **Where:** api.ts:322-347; useBacktest.ts:29, :35, :110-123.
- **Evidence:**
  - Probe P3: a stream whose second line is `{bad json` rejects with a SyntaxError after 2 reads, with `reader.cancel` called 0 times. A throwing `onEvent` behaves the same.
  - useBacktest's catch only sets the error. The controller is aborted only on re-run (:35), stop (:130) and unmount (:29).
  - The engine stops only when `!req->connected()` (main.cpp:1066-1068). With defaults of 20 threads and stopAlphas 400 (alphaStore:129-132), the GA keeps evaluating.
  - Minor: there is no final `decoder.decode()` flush; harmless because every engine line ends with "\n".
- **Why it is a problem:** one bad event or one store exception leaves a full GA running on the shared engine with no owner.
- **Simpler shape:** cancel the reader (or abort the request) in a `finally` inside `runGAStream`.
- **Blast radius:** api.ts (about 3 lines), apiClient.contract.
- **Test gate:** apiClient.contract:146-151 (truncated stream). Missing: "a malformed line cancels the reader".
- **Effort:** S

### F8 [S] Positional request builders; run settings split between positional arguments and an options object, rebuilt at 4 call sites
- **Where:** api.ts:249-257, :281-285, :291-305, :309; SignalChart:136-143; SignalPanelSelector:25-29, :42; GADetailLoader:25-26; useBacktest:59-108.
- **Evidence:**
  - `runSimulate(xml, simStart, isStart, osStart, signal?, threads?, extra{…})` has 4 source call sites. Each rebuilds the 7 settings from `result.runSettings ?? settings`, and two re-implement threads ← nJobs (SignalChart:137, SignalPanelSelector:27).
  - `runCombinedAlphas` passes an empty-string xml placeholder (:284).
  - In tests, 5 of 8 `runSimulate` calls pass `undefined` for the signal (apiClient:48, 59, 60, 68, 75).
  - `runGAStream` has 13 positional parameters and 1 source caller (useBacktest:59, which passes a conditional `undefined` at :107).
  - Its parameters are named popSize/generations but are sent as firstSearch/stopRound (:296-297, :309). 2 of 6 test calls pass `undefined, ''` placeholders (apiClient:70, 77).
  - GA defaults sit in 4 layers: the store (alphaStore:129-132), useBacktest `??` (:52, 64, 65, 75, 108), api.ts:309 `??`, and the engine (main.cpp:1049-1052).
  - `collectSignals: true, collectSignalsTopK: 5` are sent on every request (:261). The engine reads top-k only for multi-alpha, non-metricsOnly requests (main.cpp:559), which the frontend never sends.
- **Why it is a problem:** a new run setting needs edits in 5 files, and the parameter names contradict the wire names.
- **Simpler shape:** `runSimulate(xmlOrXmls, runSettings, options, signal)` and `runGAStream(xml, settings, onEvent, signal)`, with defaults only in the store.
- **Blast radius:** api.ts, the 4 callers, apiClient.contract, GA/simPeriod tests.
- **Test gate:** apiClient.contract:57-80; e2e execution_price/sim_period request bodies.
- **Effort:** S

### F9 [P] FastAPI-era error parsing and three copies of the error block
- **Where:** api.ts:21-39, :265-275, :313-320, :356-360, :245, :375, :402-405.
- **Evidence:**
  - The engine writes only `{"error": ...}` (main.cpp:604, :1022, :1030).
  - The `detail` string and `detail[].msg` branches (:25, :27-37) are kept alive only by apiClient.contract:103 and :157.
  - The parse-error block exists 3 times (runSimulate, runGAStream, `alphaError`).
  - `getConfig` and `getAlphaList` drop the engine's message (:245, :375).
  - `deleteAlpha` has no caller (MAP §3.1).
- **Why it is a problem:** dead branches for a deleted backend, and inconsistent error text depending on the endpoint.
- **Simpler shape:** one helper that reads `error`, used by all calls; delete the `detail` branches and `deleteAlpha`.
- **Blast radius:** api.ts; 2 tests in apiClient.contract.
- **Test gate:** apiClient.contract:111-122 and :167-179.
- **Effort:** S

### F10 [P] Dead gross-reconstruction fallback and unreachable `minBet` chart in DetailTimeSeries
- **Where:** DT:57-59, :68-73, :82-90, :370-372, :380-383, :418-421, :431-434, :474-485, :155, :294; api.ts:119-130; AlphaDetailView:8-14.
- **Evidence:**
  - The engine always writes `cumPnlGross` with the same length as timestamps (json_serializer.cpp:73-76), so `hasGross` (api.ts:120) is always true.
  - The fee fallback (DT:89) and the turnover merge (DT:380-383) serve only tests 1 and 3 of DetailTimeSeries.derived.
  - The `feeRate ?? 0.001` default (DT:371) is 10bp; the engine default is 1.5bp.
  - `METRICS` lacks `minBet`, and `is/osMinBetSeries` are never produced. The minBet branch and its memos are unreachable, yet `FMT_MINBET` is BigChart's default formatter (:294).
  - The 5 metric branches (:448-509) are the same JSX with different values.
- **Why it is a problem:** dead maths with a wrong default fee, an unreachable chart and a 60-line if-chain.
- **Simpler shape:** delete the fallback, the feeRate subscription and the minBet branch; drive the 5 metrics from one `{key, title, format}` table.
- **Blast radius:** DetailTimeSeries, types (2 fields), DetailTimeSeries.derived (tests 1 and 3).
- **Test gate:** e2e metrics_pipeline:58-62.
- **Effort:** S (subsumed by F1)

### F11 [S] Chart boilerplate duplicated across 3 uPlot charts; 5 formatters for the same signal value
- **Where:**
  - (a) Theme colours memo: DT:161-162, DT:295-296, SC:161+168.
  - (b) Container size: DT:164-165, DT:304-305, SC:162+165, plus SignalHeatmap's own ResizeObserver (:228-238).
  - (c) uPlot options skeleton: DT:200-214, DT:320-331, SC:260-284.
  - (d) Header JSX copy-pasted between the empty and non-empty branches: DT:252-259 vs :267-273; DT:343-346 vs :354-357.
  - (e) Symbol → index Map: SC:171-174 and SH:76-79, built from the same heatmap.
  - (f) Signal formatters: SC:34-37, SD:9-15, SH:279 `toFixed(4)`, SC:94 `toFixed(4)`, SC:280 `toFixed(3)`.
  - (g) Three tooltip mechanisms (`chartInteraction`, the SH:263-288/:379-388 div, native `title`) plus the unused `TOOLTIP_STYLE`.
- **Evidence (node check):**
  - 0.00001234 shows as "0.000012" in the chart tooltip, "1.23e-5" in the distribution, "0.0000" in the heatmap tooltip and table, and "0.000" on the y-axis.
  - 12345.678 shows as "12345.7", "12346" and "12345.6780".
- **Why it is a problem:** the same number looks different in adjacent panels, and small signals read as zero. Every option change has to be made 3 times.
- **Simpler shape:** one `formatSignalValue` in lib; one base uPlot options helper in uplotUtils; render the header once above the empty/non-empty switch.
- **Blast radius:** DT, SignalChart, SignalHeatmap, SignalDistribution, uplotUtils; chart_interaction:101; logDistribution:26.
- **Test gate:** chart_interaction.spec, logDistribution.test.
- **Effort:** S

### F12 [P] Hard-coded theme palette has drifted from globals.css; 4 of 9 keys have no live consumer
- **Where:** themeColors.ts:3-14, :23-33; globals.css:6-74; colorScale.ts:18-26, :35-42; layout.tsx:35; themeStore.ts:36.
- **Evidence:**
  - 4 of 9 light-palette values differ from globals.css:
    - backgroundElevated: `#e5e7eb` vs `#e4e4e8`
    - chartGrid: `#e5e7eb` vs `#e4e4e8`
    - chartText: `#4b5563` vs `#5b5b67`
    - neutral: `#9ca3af` vs `#858590`
  - The palette is what gets used whenever the requested theme differs from the DOM. That happens on first paint: the store starts as 'light' (themeStore:12) while the inline script may already have set dark (layout.tsx:35-36). It also happens in jsdom.
  - Consumers: backgroundElevated, positive and negative have 0; neutral has 1, and it is test-only.
  - `zscoreToColor` and `signalToColor` are test-only; `zscoreToRgb` re-implements the same blend.
- **Why it is a problem:** two sources of truth that have already diverged; the tests pin dead helpers instead of painted pixels.
- **Simpler shape:** read only the CSS variables (bare #fff/#000 as last resort), drop the unused keys and the string helpers, and seed the theme store from the DOM class.
- **Blast radius:** themeColors.ts, colorScale.ts, themeStore.ts, signalColors.test.
- **Test gate:** e2e signal_panels:48-70.
- **Effort:** S

### F13 [P] Dead display surface
- **Where:** BacktestMetrics.tsx (131) and its render test; types:194, :210-213, :236, :257-258, :270-271, :106; api.ts:51, :62-63, :171, :176-177, :196, :201-202, :229.
- **Evidence:**
  - BacktestMetrics has 0 importers. Its headers label avgPositionCount "MinBet" and activeBarCount "Alpha" (:109-111), contradicting CLAUDE.md's naming.
  - `rank` is written at 6 sites and read at 0.
  - `longRatio`/`shortRatio` are produced and read at 0.
  - `ir`, `corr`, `initialCapital` and `sortedBy` are phantom fields (MAP §3.1). `StarConfigResponse.symbols` is unread.
  - Legacy keys:
    - `ann_ret` is displayed (AlphaRankingTable:80) and `avgMaxBet` is displayed (:76).
    - `minsoc`, `sharpe80` and `mdd` are adapted but never displayed.
    - `margin`/`activeBarCount` appear only in the unmounted BacktestMetrics.
  - `ann_ret ?? pnl` (4 sites) is dead because the adapter always sets both.
- **Why it is a problem:** fields and a 131-line component that never reach the screen, with labels that contradict the naming contract.
- **Simpler shape:** delete BacktestMetrics and its test and the dead fields; write `ann_ret` directly.
- **Blast radius:** types, api.ts, BacktestMetrics and its test, metricFixtures, adaptSimulateResponse.test:107-123.
- **Test gate:** AlphaRankingTable.render.test.
- **Effort:** S

### F14 [T][P] Pin snapshots: live, untested, misnamed field, stale across runs
- **Where:** SignalChart:159-160, :196-231, :291-306, :327-335; SnapshotManager; page.tsx:137; alphaStore:439-455; snapshotColors.ts; types:335-340.
- **Evidence:**
  - The "+ Pin" button (SignalChart:327-335) and the mounted SnapshotManager (page.tsx:137) make the feature reachable.
  - Tests: 0 hits for snapshot or Pin in `__tests__` and `tests`.
  - `retSeries` actually stores signal values (SignalChart:293-295).
  - Snapshots are never cleared on a new run (MAP §4.1), so a series pinned from another alpha or run is overlaid on the current axis.
  - The cap of 8 is encoded 3 times, which makes the modulo fallback in snapshotColors.ts:5 unreachable.
- **Why it is a problem:** an untested feature that silently shows stale cross-run data.
- **Simpler shape:** rename the field to `values`, clear it on setResult/beginGA, use one cap constant (or delete the feature).
- **Blast radius:** SignalChart, SnapshotManager, alphaStore, snapshotColors, types.
- **Test gate:** none. Missing: "pin, run again, snapshot cleared".
- **Effort:** S

### F15 [I] SignalChart's full-series request restarts on unrelated settings edits
- **Where:** SignalChart.tsx:114, :117, :126-158 (deps at :158).
- **Evidence:**
  - The effect depends on the whole `currentSettings` object and on `result?.runSettings`.
  - The early return (:127) applies only when `fullSeries.key === seriesKey`.
  - So each toolbar keystroke aborts and re-sends a request that is still loading, and after an error each keystroke re-sends it.
  - The request body comes from `runSettings` (:136-139), so every re-send is identical.
- **Why it is a problem:** wasted single-symbol engine runs, each a full alpha simulation.
- **Simpler shape:** depend on seriesKey, alphaXml and needsFull, and read the settings via `getState()` when sending.
- **Blast radius:** SignalChart.tsx.
- **Test gate:** chart_interaction:151-166 (request counts).
- **Effort:** S

### F16 [P] E2E assertions that protect CSS or removed text, not behaviour
- **Where / evidence:**
  - detail_layout:33-51: aspect ratio ≥ 1.99, heights 140-480, ±1px widths; 4 tests × 13 numeric checks.
  - metrics_pipeline: :30 rightGap ≥ 23; :71-74 header ≤ 76px, lineHeight '16px', margin ≤ 45px.
  - signal_panels: :30-32 heights 64/192; :36 rgb(115,115,115); :39 marginRight 0px; :43-44 tooltip colours.
  - sim_period:28-32 (positions and widths) and smoke:21-25 (button coordinates).
  - smoke:28-33 asserts the first `[class*="flex"]` element is visible, which is always true.
  - There are 23 `toHaveCount(0)` assertions. 8 of them target strings with 0 hits in the source ("Run an alpha to explore", "Alpha Ranking", "+ Long / − Short", "Sampled ", "Close detail view", "Alpha XML", "Bar interval" ×3, "Run GA").
  - `test-results/reference-*.png` screenshots (metrics_pipeline:48, :78) are never compared.
- **Why it is a problem:** any Tailwind or font change breaks these specs, and the absent-string checks can never fail meaningfully.
- **Simpler shape:** delete these assertions and keep the behavioural ones (row counts, request bodies, pixel signs, zoom retention).
- **Blast radius:** 6 spec files.
- **Test gate:** n/a.
- **Effort:** S

### F17 [S] SignalHeatmap's latest-ref scaffolding serves no consumer that needs stable identities
- **Where:** SignalHeatmap.tsx:27-89, :109-334.
- **Evidence:**
  - The component has 17 useRef, 13 `ref.current =` writes, 11 useCallback and 10 rule-tag comments.
  - The callbacks go to native canvas/input/div elements, not to memoised children. Only `drawBase` needs a stable identity, as an effect dependency.
  - The comment at :91 says "17 symbols max", while the engine sends thousands.
  - It re-implements resize observation (:228-238) instead of using `useContainerSize`.
- **Why it is a problem:** high reader cost (366-line component, MAP §8.2) and a misleading comment.
- **Simpler shape:** plain handlers; two effects (normalize → base draw; selection → overlay) with real dependencies; `useContainerSize` for sizing.
- **Blast radius:** SignalHeatmap.tsx only.
- **Test gate:** e2e signal_panels and chart_interaction:151-166. No vitest exists.
- **Effort:** M

## 3. Looks odd but is fine

- **`rangeLo ?? min` (api.ts:75-76):** the engine omits rangeLo/rangeHi when there are no finite nonzero values (json_serializer.cpp:249-255), so the fallback is needed. MAP §7 #6 is wrong on this point.
- **Worker complexity:**
  - Production heatmaps are about 4M cells and normalizing them costs about 610 ms (P4). The same 4M random cells serialize to about 73 MB of JSON.
  - With about 3000 symbols the engine keeps every column and samples rows (json_serializer.cpp:172-178), so per-date z-scores stay exact.
  - The worker is justified; only caching and coverage are issues (F6).
- **drawBase:** 921,600 pixels take 80 ms, once per data, theme or resize change; selection changes use the cached overlay.
- **`API_BASE` default 8000:** equals the engine's LONGSHORT_PORT default (main.cpp:1283).
- **NDJSON partial-line buffering:** correct (:336-337, :344-347) and tested (apiClient:124-151).
- **`adaptGACandidate` blank metrics:** justified, because the engine omits `summary` for failed candidates (main.cpp:1126-1133). The NaN literal is duplicated in gaCombined.ts:14.
- **Whole-`result` subscriptions (AlphaDetailView:17, SignalChart:114):** they re-render once per GA candidate, but `appendGACandidate` keeps element references (alphaStore:384-385), so the memoised charts skip. No selector in the owned files returns a fresh reference per call.
- **`useUPlot` zoom across option changes:** preserved, because the range is read before destroy (:55-57).
- **`chartInteraction`:** genuinely shared by all three uPlot charts.
- **XmlEditor ref comparison:** prevents the Monaco echo loop.
- **MainLayout and ThemeProvider:** fine.
- **Playwright `NEXT_DIST_DIR`:** matches next.config.mjs.
- **DetailSummaryTable units:** match the serializer.
- **MAP erratum:** `sortedBy` is at metricFixtures.ts:42, not :81.

## 4. Unverified

- **Whether the e2e suite passes today.** Not run; would need `PLAYWRIGHT_PORT=<free> npm run test:e2e`.
- **Real 4M-cell payload size and Chrome `JSON.parse` time.** Needs an isolated engine on another port with the TOP3000 config.
- **Frontend PnL/MDD vs engine arrays when isStartIndex > 0.** The frontend rebases at isStart, the engine at simStart, and both fixtures have isStartIndex 0. Needs a regenerated fixture.
- **That the engine keeps evaluating after the browser stops reading (F7).** Inferred from main.cpp:1066-1068; confirm on an isolated engine.
- **Whether signal_panels.spec passes with 200 log bins.**

## 5. Top 5

1. **F2:** the e2e suite validates a transport and a multi-batch shape the engine cannot produce for this frontend. No refactor of the result flow is safely gated until the mocks use the candidate stream. Highest risk; effort L.
2. **F3:** silent wrong-curve fallbacks. Effort S, and removing them simplifies F1 and F4.
3. **F1:** the largest structural deletion (12 fields, about 150 lines, 3 OS-split rules), and provably reproduces the engine arrays exactly (P2). Effort M.
4. **F5:** fresh fixtures are a prerequisite for F2, F3 and F4; regenerating them is mechanical. Effort M.
5. **F7:** an orphaned GA burns the shared engine's 20 threads; the fix is a 3-line `finally`. Effort S.