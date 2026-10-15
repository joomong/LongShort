audit_f1 is done, but the report file was not written: the harness blocked the Write to `/tmp/claude-1008/-home-jgo/042a58d2-2f52-4758-9e82-1dda9e8bb386/scratchpad/refactor_audit/FINDINGS_F1.md` ("Subagents should return findings as text"). The full report follows in the template format. Save it to that path if you need the file. The scratch dir audit_f1/ is deleted, the repo is untouched, and `df` reads 67% before and after.

**Top 5**
1. **F3** — The engine only sends the `complete`/`generation_end` GA events when `streamCandidates` is false, and the frontend always sends true. About 60 lines of frontend code, one fixture and 7 of 11 e2e specs run on that unused path.
2. **F4** — The e2e mocks test behaviour production never shows: combined opened by default, `requests===1`, no `search_start`, all events in one chunk. There is no captured streaming fixture.
3. **F1** — `viewMode` (11 writers) always equals `selectedAlphaIndex !== null`. A fuzz probe of 16,000 random steps found 0 cases where they differ; only the test-only `setViewMode` breaks it.
4. **F2** — Five different rules pick the default alpha and view. Yesterday's fix covers live finish/stop only: a saved single-alpha load and a Round-tab click still open Performance.
5. **F5** — All three loaders abort requests on navigation. Measured: one aborted combined request per GA round, metricsOnly sent then aborted when the events arrive in separate chunks, and a Signals/Performance toggle sequence gives 5 requests with 4 aborted.

---

# FINDINGS_F1 — frontend result/view state machine and request flow

Auditor: audit_f1. Working tree of /home/jgo/longshort_platform/frontend, 2026-10-14. Repo untouched.

Probes ran from scratch with a scratch-only vitest config (root = frontend, include = scratch dir, aliases for `@`, `react` and `@testing-library/react` pointing into frontend/). The command was `vitest run --config <scratch>/vitest.probe.config.mjs --dir <scratch> <name>`, with probe files invariants.test.ts, requests.test.tsx and sort.test.tsx. The probes are deleted; their outputs are quoted below. The plain `npx vitest run <abs path>` from the brief does not work: it reports "No test files found" because the path is outside `include`, and `--dir` alone fails on `/@fs` access. MAP = MAP_frontend.md.

## 0. Scope read

- **store/alphaStore.ts (456)**
  - One zustand store holding settings/config, the result, and the view selection (viewMode, selectedAlphaIndex, selectedSignalAlphaIndex, symbol/timestamp).
  - Also holds the save selection, GA session state (rounds, tab, combined key/cache) and snapshots.
  - A result is installed by setResult, beginGA + append/finish, or loadSavedResult. `combinedView` recomputes the tab's combined key and placeholder on every install or tab change.
- **hooks/useBacktest.ts (140)**
  - `run` aborts the previous run, bumps requestId, resets the store, calls beginGA, then streams /api/ga/stream. Each event goes to a store action, gated by requestId.
  - `finally` cleans up (stopGA, isRunning, gaProgress). `stop` bumps requestId, aborts, and does the same cleanup.
- **app/page.tsx (221)**
  - Layout. ChartArea shows the detail panel when `viewMode==='detail'`, the signal panels when `alphas.length===1`, and the Signals/Performance tabs once a single-alpha GA has finished.
  - Config is fetched once behind a module guard. Shift+Enter calls run.
- **lib/gaCombined.ts (16)** — `gaCombinedGroup` takes the non-failed alphas with XML in the tab, sorted by candidateId; the key is the id list, or '' when there are fewer than 2. `pendingGACombined` returns a NaN placeholder.
- **lib/signalSelection.ts (29)** — Picks the signal payload (combined, top-level result, or alphas[i]). Finds the nearest timestamp by binary search and clamps the symbol/timestamp to the heatmap.
- **GADetailLoader.tsx (42)** — For the selected alpha, once complete and not yet loaded: fetches the saved row or a metricsOnly simulate. Strips the KEEP fields, then calls cacheGADetail with the revision. Cleanup aborts.
- **GACombinedLoader.tsx (38)** — For the tab's combined key, once ready and not cached: fetches the saved batch or a combinedOnly simulate. In-flight work is held in a ref by key; a separate effect aborts on [key, revision].
- **SignalPanelSelector.tsx (65)** — For a single-alpha result outside detail with no heatmap: runs a full simulate, then cacheAlphaSignals. Has local loading/error/retry state; cleanup aborts.
- **ResultsTabs.tsx (36)** — Stats plus Round N tabs, keyboard navigation with wrap, calls selectResultTab.
- **SettingsBar.tsx (125)**
  - Date, number and select inputs that call setSettings; clamping happens in the store.
  - Status text comes from gaProgress/isRunning. Execution and To are disabled for "older servers".
- **AlphaXmlPanel.tsx (37)** — Tokenizes and colours the selected alpha's (or the combined) XML; React escapes it.
- **AlphaRankingTable.tsx (230)**
  - A BatchRow (combined, select-all) plus RankRows. Rows are filtered by tab and sorted by IS sharpe, then candidateId.
  - Clicking a row calls enterDetailView; the circles toggle the save selection.
- **SummaryMetrics.tsx (62)** — Combined IS/OS card. Not mounted anywhere (MAP §3.1).
- **ControlButtons.tsx (135)** — Run/Stop/theme buttons and a running indicator. AlphaStorageButtons opens the save/load modals.
- **SaveAlphaModal.tsx (177)**
  - Builds rows from the successful candidates.
  - Blocks the "results" levels while running, with no result, or when the editor XML differs.
  - Posts the editor XML with toolbar metadata (xml level) or run metadata (results levels).
- **LoadAlphaModal.tsx (164)** — Lists saved alphas. Load sets xml, name and settings. For results levels it rebuilds candidates from the saved rows and calls loadSavedResult; for xml level it calls exitDetailView.
- **Tests read in full:** alphaStore, gaResults, GADetailLoader, GACombinedLoader, signalSelection, simPeriod, useBacktest.pipeline, SaveAlphaModal.roundtrip, SettingsBar.universe, enforceSettingsDates, AlphaRankingTable.render, SummaryMetrics.render, cppFixtures.contract, pipelineFixtures, metricFixtures, tests/helpers/mockRun, and all 11 e2e specs. Engine side: main.cpp:1040-1200 and test_http_contract.cpp:1161-1194.

## 1. Flow across the owned files

**Run GA**
- ControlButtons or the page keydown calls `useBacktest.run` (useBacktest.ts:33).
- `run` calls setIsRunning(true), setResult(null), setSelectedCell, setGAProgress, then `beginGA` (:41-56), then `runGAStream` (api.ts:306).
- Event handling:
  - `search_start` → direct `setState` patch of runSettings (:72).
  - `round_start` → `startGARound` (store:374). It "follows" by switching tab and clearing the selection.
  - `candidate` → `appendGACandidate` (store:383). It auto-selects the **first arrived** candidate.
  - `round_end` → `endGARound`.
  - `search_complete` → `finishGA` (store:393). It switches to Stats if following, and applies `singleAlphaSignals`.
- `finally` calls stopGA (a no-op once finished) and clears isRunning and gaProgress.

**Loaders**
- They react to store changes: GADetailLoader:19 (selected alpha, after its round ends), GACombinedLoader:24 (tab key, when ready), SignalPanelSelector:31 (single alpha, not in detail).

**Click row** — AlphaRankingTable:159 → `enterDetailView(i)` (store:275) sets viewMode, selectedAlphaIndex, selectedSignalAlphaIndex and the cell. GADetailLoader then fetches metricsOnly if not loaded.

**Click tab** — ResultsTabs:21 → `selectResultTab` (store:405) recomputes the combined view. It keeps the selection if visible, otherwise picks the **best IS sharpe**. GACombinedLoader then fetches.

**Single-alpha views** — page.tsx:84-85 call exitDetailView or enterDetailView(0).

**Save / Load**
- Save: SaveAlphaModal:60 → `saveAlpha`.
- Load: LoadAlphaModal:38 → setXmlCode and setSettings, then `loadSavedResult` (store:361) or exitDetailView. The GA loaders then read the saved files.

**Decisions made more than once**
1. **Is detail shown?** viewMode (11 writers) and `selectedAlphaIndex !== null` (F1).
2. **Default selected alpha:** setResult:217-220, appendGACandidate:386, selectResultTab:409-417, loadSavedResult:364, and singleAlphaSignals in finishGA/stopGA (F2).
3. **Visible rows of a tab:** store:301-302, :386, :409, ARTable:93-97, :196, and gaCombined:5 (F9).
4. **Run finished:** isRunning (GACombinedLoader:14, modals) versus gaFinished (page.tsx:120) (F7).
5. **Execution price:** SettingsBar:94, useBacktest:47, SaveAlphaModal:48, store:324.
6. **simEnd fallback:** useBacktest:56, :92, :107; SaveAlphaModal:45, :53; store:328; SettingsBar:32 (F11).
7. **Out-of-range split:** fetchConfig:330-334 versus enforceSettingsDates:168-179, with different results (F11).

**Loader comparison (Q4)**

| | GADetailLoader | GACombinedLoader | SignalPanelSelector |
|---|---|---|---|
| trigger | selected index ≥0, xml, !error, !detailLoaded, !detailError, round not running (:17-20) | key≠'', tab ready (Stats: `!isRunning`; round: status≠running) (:14-15, :25) | 1 alpha, `viewMode!=='detail'`, no heatmap, index≥0 (:34) |
| deps | 11 | 9 | 16 (incl. viewMode, retry) |
| guard | signal.aborted + revision in cacheGADetail (store:433) | work.current.key===key + signal.aborted + revision in cacheGACombined (store:426) | `active` + signal.aborted + revision in cacheAlphaSignals (store:246) |
| cache | alpha.detailLoaded in result.alphas[i] | gaCombinedCache[key] | alpha.heatmap (+ LRU, see F6) |
| cancel | any dep change (cleanup :39) | key or revision change (separate effect :23) | any dep change (cleanup :53) |
| error | detailError stored, no retry | {pending, detailError} cached, no retry | local state + Retry button |

## 2. Findings

### F1 [S] `viewMode` duplicates `selectedAlphaIndex !== null` and is written in 11 places
- **Where:**
  - Writers: store:225, 271, 281, 285, 359, 369, 380, 389, 397 (via :118), 402 (via :118), 421.
  - Readers: page.tsx:118 and SignalPanelSelector.tsx:15, 34, 56. Type at types:352.
- **Evidence:**
  - Every reader tests only `=== 'detail'`; nothing reads 'single' versus 'multi'.
  - Fuzz probe: 400 random production action sequences of 40 steps each. Actions: beginGA, start/end round, append (including failed and NaN sharpe), finish, stop, tab, enter/exit detail, toggleAll, cacheCombined.
  - Invariant I1 is `(viewMode==='detail') === (selectedAlphaIndex!==null)`. Result: `violations 0 []`.
  - Only `setViewMode` breaks it (`after setViewMode(single) with 1 alpha single 0`). That action has 0 source callers and 1 test use (gaResults:98).
  - The same probe found 0 violations of "detail ⇒ selectedSignalAlphaIndex === selectedAlphaIndex".
- **Why it is a problem:** every action must keep two fields in sync, and the 11 writers are where the "never reached the view" class of bug lives.
- **Simpler shape:** delete `viewMode`, `setViewMode` and `ViewMode`; read `selectedAlphaIndex !== null` instead.
- **Blast radius:** store, page.tsx, SignalPanelSelector, types; test references in gaResults (8), alphaStore (4), GADetailLoader (2), XmlEditor (2).
- **Test gate:** gaResults:122 and alphaStore:131-148. Missing: `alphaStore.invariants.test.ts` (random action sequences asserting "detail ⇔ selection").
- **Effort:** S

### F2 [S] Five rules choose the default selection and view; the single-alpha signal view is a patch
- **Where:**
  - setResult :217-220: combined, else 0; a single alpha gets no selection.
  - appendGACandidate :386: first arrived.
  - selectResultTab :409-417: best IS sharpe.
  - loadSavedResult :364: combined, else alphas[0].
  - singleAlphaSignals :115-119, applied in finishGA :397 and stopGA :402.
  - The Signals/Performance tabs have a separate gate at page.tsx:120.
- **Evidence (probe):**
  - Two candidates arriving at sharpe 0.1 then 3: `append auto-selects index 0 (sharpe 0.1)`, while selectResultTab picks `1`.
  - Single alpha: `live finish ['single', null]`, but `after clicking Round 1 tab ['detail', 0]` and `saved load ['detail', 0]`.
  - So a live single-alpha GA opens Signals; the same alpha loaded from a save, or after a tab click, opens Performance.
- **Why it is a problem (yesterday's bug):**
  - The current shape does not make "single alpha never reaches the signal view" impossible; it patches only the path the bug came through.
  - `selectedAlphaIndex` means two things: "the highlighted row" and "show Performance".
  - appendGACandidate selects the first candidate, so a single-alpha run ends in Performance unless finishGA/stopGA clear the selection again under the four-way condition at :117.
  - loadSavedResult and selectResultTab skip that condition, so they still land in Performance. Every new install or tab path must remember singleAlphaSignals.
- **Simpler shape:** use one default rule (selectResultTab's best visible row, or combined) on every install path, and derive "show Signals" in page.tsx from `alphas.length===1 && gaFinished && !explicitPerformance` instead of clearing the selection.
- **Blast radius:** store (5 actions) and page.tsx. Unit tests: gaResults:20, 43, 122; GADetailLoader:14; alphaStore:131-148. E2e: ga_default, single_alpha_view, alpha_selection, metrics_pipeline.
- **Test gate:** gaResults:122 pins only live finish/stop. Missing: "loadSavedResult opens the same view as a live single-alpha finish" and "selectResultTab keeps Signals for a finished single alpha".
- **Effort:** M

### F3 [P] The `complete`/`generation_end` GA path is unreachable in production but carried end to end
- **Where:**
  - Frontend source: useBacktest.ts:82-98 (including the runSettings rebuild at :92); setResult store:212-232 with the preserve-detail rule :216-219; api.ts:219-221 adaptGACompleteToResponse; api.ts:328 terminal check; types:125-144; signalSelection.ts:7, :9 (top-level result.heatmap branches).
  - Frontend tests and fixtures: tests/helpers/mockRun.ts (7 e2e specs); pipelineFixtures.ts:18-22 with cppFixtures.contract:30-35; simPeriod.test:33-48; apiClient.contract:127-143; alphaStore.test:131-148; cpp_ga_events.ndjson.
  - Engine (not owned): main.cpp:1163-1191; test_http_contract.cpp:519, 908, 1039.
- **Evidence:**
  - The engine emits complete/generation_end only when streamCandidates is false: main.cpp:1059, :1144 (`stream_candidates ? "round_end" : "generation_end"`), and :1154-1162 returns before :1177.
  - The frontend always sends `streamCandidates: true` (api.ts:309), and it is the only caller.
  - In production, setResult is called only with null (useBacktest:43) and with the complete adapter (:97).
  - The preserve-detail rule needs `gaProgress && viewMode==='detail'` before `complete` arrives. Between beginGA (viewMode 'multi', 0 alphas) and `complete` no candidate exists, and enterDetailView refuses missing indices (store:277), so the rule can never fire.
- **Why it is a problem:** about 60 source lines, 1 fixture and 7/11 e2e specs exercise a transport the app never uses, and the setResult branch shapes the view rules that F1/F2 must keep consistent.
- **Simpler shape:** delete the non-streaming branches (frontend handlers, setResult's non-null path, adaptGACompleteToResponse, mockRun) together with the engine's `streamCandidates:false` path.
- **Blast radius:**
  - Frontend source: useBacktest, store, api, types, signalSelection.
  - Frontend tests: the 7 mockRun specs; simPeriod, useBacktest.pipeline, cppFixtures.contract, apiClient.contract, alphaStore.
  - Engine: main.cpp and 3 contract tests.
- **Test gate:** nothing fails today if streaming regresses while the complete path still works (see F4). Missing: a captured `cpp_ga_stream.ndjson`.
- **Effort:** M

### F4 [T] e2e and unit mocks test behaviour production never shows; streaming has no engine-captured fixture
- **Where:** mockRun.ts:4-16 (used by alpha_selection, chart_interaction, detail_layout, execution_price, metrics_pipeline, signal_panels, sim_period); ga_default.spec:13-34; single_alpha_view.spec:20-34; useBacktest.pipeline:43; simPeriod:42.
- **Evidence:**
  1. alpha_selection:17 and metrics_pipeline:40 assert the combined row is expanded right after a run. That comes from setResult (store:219, `combined ? -1`); a streamed run selects the first candidate instead (store:386).
  2. metrics_pipeline:84 asserts `requests===1`. A streamed multi-alpha run also issues combinedOnly and metricsOnly simulates.
  3. The single-run heatmaps in metrics_pipeline, signal_panels and chart_interaction come from the complete payload, not from SignalPanelSelector.
  4. The hand-built streams omit `search_start`, which the engine always sends first (main.cpp:1087-1091; test_http_contract:1161). So the runSettings patch at useBacktest:72 is never run by any test except live_engine, which is skipped without PLAYWRIGHT_ENGINE_URL.
  5. single_alpha_view:31 sends all events in one chunk, which hides F5. It asserts metricsOnly count 1 (:51); with separate chunks the probe gives 2 (1 aborted).
  6. adaptGACandidate is mocked (useBacktest.pipeline:43) and never runs on a real `candidate` payload. The only GA fixture (cpp_ga_events.ndjson, 2 lines) contains generation_end and complete.
- **Why it is a problem:** regressions in the real flow (default selection, request counts, the search_start echo, candidate adaptation) pass CI.
- **Simpler shape:** capture one streaming ndjson from the integration server and have mockRun replay it one event per chunk.
- **Blast radius:** mockRun, 9 e2e specs, useBacktest.pipeline, simPeriod, pipelineFixtures, scripts/capture_engine_fixtures.mjs.
- **Test gate:** missing `useBacktest.searchStart.test` and `adaptGACandidate.contract.test` against the captured stream.
- **Effort:** M

### F5 [I] Loaders abort on navigation although results land in revision-guarded caches; engine work is discarded and re-issued
- **Where:** GADetailLoader:39 (cleanup on 11 deps including `index`); GACombinedLoader:23 (abort on key change); SignalPanelSelector:53 (cleanup on 16 deps including `viewMode`).
- **Evidence (probe; runSimulate and runCombinedAlphas never resolve; one `act` per network chunk):**
  - Single alpha with round_end and search_complete in separate chunks: `[{"metricsOnly","aborted":true},{"full","aborted":false}]`.
  - Then clicking Performance adds `{"full","aborted":true},{"metricsOnly","aborted":false}`.
  - The same events in one chunk give `[{"full"}]` only.
  - Signals→Performance→Signals→Performance→Signals while loading: 5 requests, 4 aborted.
  - Two-round GA with separate chunks: `[{"n":2,"aborted":true},{"n":2,"aborted":true},{"n":4,"aborted":false}]`.
  - Each round's combined request is issued at round_end and aborted by the next round_start "follow" (store:377) or by finishGA's switch to Stats (:396). That is one aborted combinedOnly simulate per round, running while the GA is still searching.
  - The engine honours disconnects (main.cpp:1018).
- **Why it is a problem:** the request count depends on TCP chunking, and the heaviest request (full signals) restarts on every tab toggle.
- **Simpler shape:** abort only on `revision` change. cacheGADetail, cacheGACombined and cacheAlphaSignals already key by index/key and reject stale revisions.
- **Blast radius:** the three loaders; GACombinedLoader.test:33 (still holds, because it changes revision); signalSelection:47.
- **Test gate:** existing loader tests. Missing: "loaders do not abort on tab toggle" (the probe sequences as assertions).
- **Effort:** S

### F6 [P] Multi-alpha signal caching (5-entry LRU, `signalCacheOrder`, `selectSignalAlpha`) is unreachable
- **Where:** store:31-34, 98, 224, 236-244, 249-253, 255, 359, 371; SignalPanelSelector:21-22 (combined branch); signalSelection:7-9.
- **Evidence:**
  - Signals are fetched only when `alphas.length===1` (SignalPanelSelector:11-12, 34; `index<0` is rejected), so cacheAlphaSignals only ever gets index 0. The LRU holds at most 1 entry and the eviction at :250-253 never runs.
  - `signalCacheOrder` has 5 writers and 0 readers outside the store. `selectSignalAlpha` has 0 source callers.
  - With one alpha, `selectedSignalAlphaIndex` is 0 or null: combined needs ≥2 alphas (gaCombined:7), and both values resolve to alphas[0].
- **Why it is a problem:** signalSelection.test:63-94 tests 2- and 7-alpha results the UI cannot produce, and 13 write sites maintain a feature that no longer exists.
- **Simpler shape:** delete the LRU, signalCacheOrder, selectSignalAlpha and selectedSignalAlphaIndex; the signal source is `alphas[0]` when there is exactly one alpha.
- **Blast radius:** store, signalSelection, SignalPanelSelector, and the signal-panel selectors (F2-owned); 3 tests.
- **Test gate:** signalSelection:28 and :47 stay; :63-94 are deleted.
- **Effort:** S

### F7 [S] "Running" is spread over four fields; gaProgress is a derivable copy with dead fallback labels
- **Where:**
  - Fields: isRunning (written at useBacktest:41, 122, 135); gaProgress (useBacktest:48, 75, 83, 123, 136); gaFinished (store:353, 367, 394, 400); gaRounds[].status.
  - Readers: ControlButtons:89 (`gaProgress ? 'GA…' : 'Running…'`), SettingsBar:85, GACombinedLoader:14 (Stats ready = `!isRunning`), page:120 (gaFinished).
- **Evidence:**
  - gaProgress is set in the same synchronous block as isRunning (:41 then :48, before any await) and cleared together (:122-123, :135-136). So the 'Running…'/'Running' branches never show.
  - Only `.generation` is read, and it equals `gaRounds.at(-1).round`. totalGenerations, evaluated, populationSize and bestSharpe are never displayed.
  - Between search_complete (gaFinished=true) and `finally` (isRunning=false) the two "done" signals disagree, and different readers use different ones.
- **Reset table (Q6):**
  - Engine `error` event → stopGA and setBacktestError; then `finally` → stopGA (no-op), isRunning false, gaProgress null.
  - Transport throw → `catch` sets the error; `finally` does the same cleanup.
  - Stop → stop() cleans up once; `finally` returns early because requestId changed (:119).
  - A new run while running is blocked by ControlButtons:30 and canShortcutRun.
  - No path double-resets or leaves isRunning stuck.
- **Simpler shape:** delete gaProgress/setGAProgress, render `Round ${gaRounds.at(-1)?.round ?? 0}` while isRunning, and use one "done" signal everywhere.
- **Blast radius:** useBacktest, store, ControlButtons, SettingsBar; tests gaResults:143 and useBacktest.pipeline:54, 61, 96.
- **Test gate:** gaResults:143; useBacktest.pipeline:57, 86.
- **Effort:** S

### F8 [P] `gaSession` is always true whenever a result exists; `!gaSession` branches serve only tests
- **Where:** page:120; ARTable:94, 96, 196, 197; store:302, 384, 399, 426, 433; GADetailLoader:11, 20; GACombinedLoader:9, 25; clearGA store:404 (0 callers).
- **Evidence:**
  - A non-null result is installed only by beginGA, by loadSavedResult (both set gaSession), or by setResult(adapted) (useBacktest:97), which always follows beginGA (:56).
  - Probe invariant "result ⇒ gaSession": 0 violations.
  - The `!gaSession` legs are reached only by tests that install results via setResult/setState: 23 sites in 7 test files.
- **Why it is a problem:** there are two table orders and filters, and the tests exercise the legacy leg.
- **Simpler shape:** delete gaSession and clearGA, and sort/filter unconditionally.
- **Blast radius:** store, page, ARTable, both GA loaders; the 7 tests would install results via beginGA/loadSavedResult.
- **Test gate:** gaResults covers the production leg; AlphaRankingTable.render covers only the legacy leg.
- **Effort:** S

### F9 [S] The tab-visibility predicate and row sort are written five times
- **Where:** ARTable:93-94, 95-97, 196-201; store:301-302 (toggleAll), 386 (appendGACandidate), 409-414 (selectResultTab); gaCombined:5 (adds `!error && xml`).
- **Evidence:**
  - store:410-414 and ARTable:197-201 contain the same comparator expression.
  - Probe: `sort trials 150 mismatch 0` (table top row == selectResultTab's choice, with NaN sharpe, ties and failed alphas). So they agree today.
  - One intentional divergence: BatchRow's select-all counts failed rows (ARTable:93), while the combined row excludes them (gaCombined:5).
- **Why it is a problem:** any ordering or visibility change must be made in 5 places, and F2's default selection relies on the two comparators staying identical.
- **Simpler shape:** move ARTable's `rows` into lib/gaCombined.ts as `visibleRows(alphas, tab)` and call it from all five sites.
- **Blast radius:** store, ARTable, gaCombined; tests gaResults:43, 87 and ARTable.render.
- **Test gate:** gaResults:43 and :87. Missing: "selectResultTab picks the table's first row".
- **Effort:** S

### F10 [P] Save-selection circles have no consumer but live in the store
- **Where:** store:51-55, 229-230, 290-309, 358, 370; ARTable:93-99, 121-132, 171-182, 191, 209-214. The code says "reserved for submit; not used by saving" (ARTable:171) and "table selection is not used" (ControlButtons:104).
- **Evidence:**
  - SaveAlphaModal reads no selection field (:15-21). The only readers are ARTable and the unmounted BacktestMetrics.
  - Yet it carries 2 fields, 3 actions, resets in all 3 install paths, 2 BatchRow selectors that scan all alphas on every store change, and tests in 6 files including e2e alpha_selection and live_engine.
  - SaveAlphaModal.roundtrip:63 asserts that the selection does *not* matter.
- **Simpler shape:** delete the circles and selection state until submit exists, or keep them as local ARTable state.
- **Blast radius:** store, ARTable; tests alphaStore (5), ARTable.render (2), gaResults:87, SaveAlphaModal:63, e2e alpha_selection, live_engine:78.
- **Test gate:** the same tests, which would be deleted.
- **Effort:** S

### F11 [P] Settings dates: the out-of-range rule is implemented twice with different outcomes; "older server" fallbacks repeated
- **Where:**
  - fetchConfig store:326-335: out of range → resetSplit (70/30).
  - enforceSettingsDates :154-158, :172: below min → clamp.
  - `supportsSimEnd && state.config` at :328.
  - simEnd/execution fallbacks: SettingsBar:28, 32-33, 39, 45, 94-98; useBacktest:47, 56, 92, 107; SaveAlphaModal:42-48, 53-56.
- **Evidence (probe, bounds 2016-01-04..2026-09-15 as used in simPeriod.test:16):**
  - Result: `fetchConfig -> 2016-01-04 2016-01-04 2023-07-01 | setSettings same bounds -> 2016-01-04 2016-01-04 2024-01-01`. With data starting 2015-01-02, fetchConfig keeps `2024-01-01`.
  - So the default `osStart: '2024-01-01'` (store:125) survives only when the server's data starts on or before 2016-01-01. The default isStart 2016-01-01 is a holiday, so any data that starts on 2016 trading days triggers the reset.
  - `state.config` at :328 is truthy only on a second fetchConfig; page:181-190 fetches once, and only alphaStore.test:84 calls it twice.
  - The engine always sends `supportsSimEnd: true` and `executionPrices` (MAP §7 #10-11). So the disabled To/Execution branches serve only simPeriod:27 and SettingsBar.universe:49.
  - `settings.simEnd || config.simEnd` is redundant once enforceSettingsDates fills an empty simEnd (store:166).
- **Simpler shape:** fetchConfig calls only `enforceSettingsDates(s, min, max)` (delete its outOfRange/resetSplit block), and the supportsSimEnd/executionPrices fallbacks are dropped.
- **Blast radius:** store, SettingsBar, useBacktest, SaveAlphaModal; tests alphaStore:40, simPeriod:27, SettingsBar.universe:35.
- **Test gate:** enforceSettingsDates.test (8 tests); simPeriod:18, 50. Missing: "fetchConfig keeps the default split when only the start is clamped".
- **Effort:** S

### F12 [P] Optional GA budget fields force the same defaults in four places
- **Where:** types:283-285 (gaFirstSearch?, gaStopRound?, gaStopAlphas?); store:129-132; SettingsBar:60, 69, 75; useBacktest:52, 64, 65, 75, 108; api.ts:309 (`?? 400`, `?? 50`); engine main.cpp:1049-1052.
- **Evidence:** defaultSettings always sets all three and setSettings only merges, so every `?? 200/40/400` is unreachable. gaPopulation (roundSize 50) has no input at all.
- **Simpler shape:** make the three fields required and delete the `??` defaults outside defaultSettings.
- **Blast radius:** types, SettingsBar, useBacktest, api.
- **Test gate:** gaResults:143; useBacktest.pipeline:28.
- **Effort:** S

### F13 [P] useBacktest does redundant resets and mirrors the store in refs
- **Where:** useBacktest.ts:13-23 (two store-wide subscriptions copying xmlCode and settings into refs); :40 (already calls getState); :43-44 (setResult(null), setSelectedCell(null, null)) before beginGA at :56.
- **Evidence:**
  - beginGA (store:352-360) already resets everything :43-44 resets, and each of them bumps resultRevision (:223, :356), so one run bumps it twice.
  - The two subscriptions fire on every streamed candidate just to copy two fields that `run` could read from getState.
- **Simpler shape:** read xmlCode and settings from getState inside `run`; delete :13-23 and :43-44.
- **Blast radius:** useBacktest; useBacktest.pipeline (6 tests), simPeriod:33.
- **Test gate:** useBacktest.pipeline:28, 63, 86.
- **Effort:** S

### F14 [P] Modal and button dead branches
- **Where:**
  - In-component open checks: SaveAlphaModal:15, 27 and LoadAlphaModal:11, 19-23, 83. page:55-56 already mounts the modals only when open.
  - SaveAlphaModal:36 'no run settings' is unreachable; every install path sets runSettings.
  - SaveAlphaModal:32 `candidateId ?? index` and `round ?? 0` serve only the F3 path.
  - LoadAlphaModal:64 calls exitDetailView on an xml-level load, which changes the *previous* result's view.
  - LoadAlphaModal:25-36 has no abort or unmount guard.
  - ControlButtons:9, 17-23, 83, 88: the `vertical` prop is never passed (page:211).
  - SummaryMetrics has 0 importers and is kept alive only by its render test.
- **Coupling (Q7):**
  - SaveAlphaModal reads `result.alphas[].candidateId/round/xml/error`, `runSettings`, `sourceXml`, `settings`, `config.{executionPrice(s), simEnd, defaultUniverse, alphasDir}`, `isRunning`, `xmlCode`.
  - LoadAlphaModal writes xmlCode, alphaName, settings and backtestError, calls loadSavedResult or exitDetailView, and builds runSettings from saved metadata (:62).
  - Renaming runSettings, sourceXml or candidateId breaks both modals and both GA loaders.
- **Simpler shape:** delete the in-component open gates, the 'no run settings' branch, the `vertical` prop, SummaryMetrics with its test, and the exitDetailView call.
- **Blast radius:** both modals, ControlButtons, SummaryMetrics; tests SaveAlphaModal.roundtrip and SummaryMetrics.render.
- **Test gate:** SaveAlphaModal.roundtrip (10 tests).
- **Effort:** S

### F15 [S] Loader error policies differ: detail and combined errors are cached permanently, signals can be retried
- **Where:** GADetailLoader:20, 37; GACombinedLoader:25, 33-34; SignalPanelSelector:19-20, 62.
- **Evidence:**
  - Probe: a rejected combined fetch, then Round 1 → Stats, gives `combined after error 1 Error: boom` — no second request.
  - `detailError` stays on the alpha and blocks refetch (:20) until a new run. It is shown (AlphaDetailView:31) but cannot be retried.
  - SignalPanelSelector keeps its error locally and offers Retry.
- **Why it is a problem:** a transient engine error makes a row's performance, or the combined row, unrecoverable without re-running the GA.
- **Simpler shape:** don't cache errors (re-selecting retries), or give all three loaders the same Retry.
- **Blast radius:** the three loaders; no current test covers this.
- **Test gate:** missing "GADetailLoader retries after detailError on re-selection" and the combined equivalent.
- **Effort:** S

## 3. Looks odd but is fine

- **GADetailLoader KEEP set (:7-8, :34).** A metricsOnly body adapts to null signal fields that would otherwise erase cached signals. Pinned by GADetailLoader.test:34.
- **GACombinedLoader split abort effect (:22-23).** It is correct for the stated case: Round N and Stats with identical membership share one request. The probe's "same chunk" case shows that shared key is not aborted. The waste in F5 comes from *different* keys.
- **Date arithmetic.**
  - All arithmetic uses `YYYY-MM-DDT12:00:00Z` with setUTCDate and toISOString().slice(0,10) (store:138-144, :160-162), so it is timezone-safe; the year-boundary case is pinned by enforceSettingsDates.test:69.
  - Lexical ISO comparison is valid for 4-digit years.
  - `nextDay('')` would throw, but SettingsBar:18 blocks empty values except simEnd, and simEnd is guarded.
- **Sort agreement.** Table order and selectResultTab agree in 150/150 probe trials, including NaN sharpe.
- **stopGA after finishGA** is a guarded no-op (store:399), required because `finally` always calls it. Pinned by gaResults:135.
- **requestId + AbortController in useBacktest.** Every termination path resets the store exactly once (see the F7 reset table). Pinned by useBacktest.pipeline:63, 86.
- **Revision guards in the cache actions** correctly reject responses from previous runs. Pinned by gaResults:83, 118; GACombinedLoader:33; signalSelection:47.
- **startGARound "follow only if watching the latest round"** is deliberate. Pinned by gaResults:102.
- **appendGACandidate de-duplicates by candidateId** (store:384). Pinned by gaResults:113.
- **The module-level configFetched guard** absorbs StrictMode's double effect.
- **Engine-side cancellation.** The engine honours simulate disconnects (main.cpp:1018), so F5 is about work started and discarded, not runaway work.

## 4. Unverified

- **The real server's data start date.** F11's osStart overwrite only happens if data starts after 2016-01-01. Confirm via /api/config on a non-live integration engine (not 50007).
- **Chunk boundaries.** Whether real engine chunks split round_end / round_start / search_complete. They are separate `stream->send` calls (main.cpp:1072), so probably yes. Confirm by logging `reader.read()` boundaries against a test engine; this decides how often F5's aborts happen.
- **Saved result with an unavailable universe.** It keeps `runSettings.universe='OLD_V1'` (LoadAlphaModal:62; SaveAlphaModal.roundtrip:167). Round-tab combined (GACombinedLoader:30) and single-alpha signals (SignalPanelSelector:42) would post that universe to /api/simulate, which likely gives a 400. Confirm with an unknown-universe case in test_http_contract.
- **pinnedSnapshots** are never cleared by any install path (store:440-455). They may be intended for comparison; F2's SignalChart review would confirm.
- **React 18 batching.** The probe models "same chunk" as one `act`. In a browser, several `reader.read()` resolutions in one macrotask may also batch. A chunked browser mock would settle it.

## 5. Top 5

1. **F3** — Delete the dead `complete` path. It is the largest removal, it is what keeps F4's misleading tests green, and it shrinks the state that F1/F2 must keep coherent. M effort; low risk once a streaming fixture exists.
2. **F4** — Capture a streaming fixture and have mockRun replay it chunk by chunk. 7/11 e2e specs, plus the search_start and adaptGACandidate paths, currently test behaviour production never runs, so every other refactor here is unguarded. Highest risk; M effort.
3. **F1** — Derive "detail is shown" from `selectedAlphaIndex !== null`. Zero divergence over 16,000 fuzz steps, removes 11 writers, S effort, and the clearest proof of safety.
4. **F2** — Use one default-selection rule and derive "show Signals" in page.tsx. This is the root of yesterday's bug class: the fix covers live finish/stop, but the probe shows saved loads and tab clicks still diverge.
5. **F5** — Abort loaders only on revision change. S effort in three files. It removes one aborted engine request per GA round and the restart of the heaviest request on every Signals/Performance toggle, and the revision-keyed caches already make it safe.