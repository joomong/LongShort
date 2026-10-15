# VERIFY_frontend — adversarial check of FINDINGS_F1 (15) and FINDINGS_F2 (17)

Verifier: verify_frontend, 2026-10-14. Working tree read-only. Live servers untouched.

**Probes.** Scratch config at `verify_frontend/vitest.probe.config.mts`: root = frontend, include = scratch, `@/` aliased to frontend/, and react plus @testing-library/react aliased into frontend/node_modules. Command: `./node_modules/.bin/vitest run --config <scratch>/vitest.probe.config.mts <name>`. Three probe files were run and then deleted:
- store.test.ts (fuzz, view paths, config split)
- api.test.ts (adapter fallbacks, stream cancel, fixture indices)
- loader.test.tsx (abort sequences)

**Side effect.** Vitest rewrote its own results cache at `frontend/node_modules/.vite/vitest/.../results.json`. That file is gitignored and is rewritten by every `npm test`. No source, test or config file changed.

**Contract sources read:**
- CLAUDE.md
- cpp-engine/docs/math_contract.md:173-229 (GA and streamed results)
- the docs/data_contract.md grep
- MAP_engine §3.2 and §7

## 1. Per-finding verification

### FINDINGS_F1

**F1-F1 [S] viewMode duplicates `selectedAlphaIndex !== null` — CONFIRMED**
- My own fuzz ran 400 random sequences of 40 steps (16,000 steps) across beginGA, start/end round, append (NaN and failed candidates), finish, stop, tab, enter/exit, toggleAll and cacheCombined. Result: `viewMode<->selected violations 0`.
- Every writer pairs them: alphaStore.ts:225, 281, 285-286, 359, 369, 380, 389, 118 and 421.
- The only exception is `setViewMode` (:271). Repo-wide grep finds no caller except gaResults.test:98.
- Severity is honest. No contract conflict: CLAUDE.md's "Multi-alpha results stay in performance detail" is preserved by `selectedAlphaIndex !== null`. Deletion only.

**F1-F2 [S] five default-selection rules — PARTLY**
- Reproduced:
  - live single finish gives `single null`
  - after clicking the Round 1 tab: `detail 0`
  - saved load: `detail 0`
  - append selects the first arrival (index 0, sharpe 0.1), while selectResultTab keeps it.
- Not a bug: CLAUDE.md:8 documents the saved-load case: "a single-alpha result … opens on them when its GA finishes or stops … (saved-result loads open on Performance)". Only the Round-tab click path is an undocumented divergence.
- **Contract conflict:** the proposed rule `alphas.length===1 && gaFinished && !explicitPerformance` would open saved loads (gaFinished=true, alphaStore.ts:367) on Signals, which breaks that clause.
- **Over-engineering:** it adds a new `explicitPerformance` flag to the state. The deletion-only alternative is to make selectResultTab apply the same `singleAlphaSignals` guard as finishGA/stopGA, or simply accept the tab path.

**F1-F3 [P] the complete/generation_end path is unreachable — CONFIRMED (frontend); the engine part conflicts with the contract**
- The engine emits `complete` only after the `stream_candidates` early return (main.cpp:1154-1162 returns before 1163-1191). api.ts:309 always sends `streamCandidates: true`.
- The preserve-detail rule (alphaStore.ts:216-219) needs `viewMode==='detail'` before any candidate exists. enterDetailView refuses a missing index (:277), so the rule is dead.
- **Contract:** the proposal to delete "together with the engine's `streamCandidates:false` path" contradicts math_contract.md:197-199: "Legacy requests without this flag retain generation events and the best-alpha complete payload. `popSize` and `generations` remain request aliases". Removing the frontend handlers alone conflicts with nothing.

**F1-F4 [T] mocks test behaviour production never shows — CONFIRMED**
- metrics_pipeline.spec:40 (combined expanded) and :84 (`requests===1`) hold only on the setResult path. In that path gaCombinedKey stays '' and every alpha is `detailLoaded:true` (useBacktest.ts:95), so neither loader fires.
- The hand-built streams in ga_default.spec:24-29 and single_alpha_view.spec:25-30 have no `search_start`. A repo-wide grep finds `search_start` only in live_engine.spec:67.
- single_alpha_view sends one chunk (:31).
- Severity is honest.

**F1-F5 [I] loaders abort on navigation — PARTLY**
- The mechanism reproduces. With separate `act` per event and a 2-round GA, the probe gives `[combined aborted, metricsOnly aborted, combined aborted, metricsOnly ok, combined ok]`. A single-alpha run gives `[metricsOnly aborted, full ok]`.
- The probe also shows an abort F1 did not list: startGARound's follow (alphaStore.ts:380) clears the auto-selected candidate, which aborts its metricsOnly.
- **Severity overstated:** no size or time is given. The engine checks `!req->connected()` per signal block (main.cpp:1018, observe at :487), so an aborted request stops within one block. How often it happens depends on TCP chunking, which F1 itself lists as unverified.
- **Over-engineering:** "abort only on revision change" removes the only concurrency bound. Rapid row clicks would leave N metricsOnly simulations running on the shared engine. Re-selecting an alpha whose request is still in flight would issue a duplicate, unless an in-flight registry is added (a new layer). There is no deletion-only alternative.

**F1-F6 [P] multi-alpha signal LRU is unreachable — CONFIRMED**
- SignalPanelSelector.tsx:11-12, :34 fetch only for `alphas.length===1` and index ≥ 0. gaCombinedGroup needs ≥ 2 alphas (gaCombined.ts:7), so with one alpha the index is always 0, signalCacheOrder has at most 1 entry, and eviction (:250-253) never runs.
- `selectSignalAlpha` has 0 source callers (repo grep: only signalSelection.test:65, 76, 86).
- No conflict: math_contract.md:225 limits signal plots to single-alpha results.

**F1-F7 [S] "running" is spread over four fields — CONFIRMED**
- useBacktest.ts:41/48 and :122-123/:135-136 set and clear the fields together. Readers of gaProgress: ControlButtons:89, SettingsBar:85, plus alphaStore.ts:216 (the dead F1-F3 rule).
- Severity: closer to P (dead labels) than S. The gaFinished/isRunning gap lasts only until the stream closes.

**F1-F8 [P] gaSession is always true when a result exists — CONFIRMED**
- Fuzz: `result=>gaSession violations 0`.
- Test sites installing results via setResult or setState: **21 in 7 files**, not 23 (grep counts per file: 4, 5, 1, 3, 6, 1, 1).
- clearGA has 0 references repo-wide, including docs and tools.

**F1-F9 [S] tab-visibility predicate and sort written 5 times — CONFIRMED**
- The comparators at alphaStore.ts:410-414 and AlphaRankingTable.tsx:197-201 are the same expression.
- The simpler shape adds one shared function. That is a merge, not a layer, and there is no deletion-only alternative.

**F1-F10 [P] save-selection circles have no consumer — CONFIRMED**
- SaveAlphaModal reads no selection field (:15-21). The comment at AlphaRankingTable.tsx:171 says "reserved for submit".
- No doc contract covers it. This is a product decision, not accidental dead code.

**F1-F11 [P] config split rule implemented twice — CONFIRMED, severity understated**
- The probe reproduces exactly: data start 2016-01-04 gives `fetchConfig -> … 2023-07-01` versus `setSettings -> … 2024-01-01`.
- New evidence: `~/longshort_workspace/config.yaml:26` has `start_date: '2016-01-01'`, a holiday, and `/api/config.simStart` is the trade-start bar (main.cpp:967). So `isStart 2016-01-01 < simStart`, and the default osStart is overwritten to about 2023-07 on **every page load** in the current workspace. That is a user-visible behaviour defect, not patchwork.
- **Contract conflict:** dropping the `executionPrices` fallback breaks CLAUDE.md:19 "Older APIs show their fixed execution value with selection disabled". That clause is pinned by SettingsBar.universe.test:35. The supportsSimEnd fallback has no doc clause.

**F1-F12 [P] optional GA budget fields — CONFIRMED**
- types/index.ts:283-285. The only writers are SettingsBar:61, 70, 76 and LoadAlphaModal:56, and none of them passes undefined.
- Same root as F2-F8.

**F1-F13 [P] redundant resets and ref mirrors — CONFIRMED.** The impact is negligible (two O(1) subscriptions, one extra revision bump); the P tag is fair.

**F1-F14 [P] modal and button dead branches — PARTLY**
- Confirmed:
  - the open gates are redundant (page.tsx:55-56)
  - `vertical` is never passed (0 hits in app/, tests and e2e)
  - SummaryMetrics has 0 references outside its own test, repo-wide including .md, .mjs and .py
  - 'no run settings' is unreachable in production
- Wrong: LoadAlphaModal:64 `exitDetailView` is not dead. For a single-alpha previous result it switches to Signals; with more than one alpha it is a no-op (alphaStore.ts:284). It is a behaviour question, not a dead branch.

**F1-F15 [S] loader error policies differ — CONFIRMED**
- GADetailLoader.tsx:20 skips when `detailError` is set. GACombinedLoader.tsx:25 treats `cached` (holding detailError, :33-34) as done.
- **Over-engineering:** "don't cache errors" would lose the message, because AlphaDetailView.tsx:31 renders `alpha.detailError`. "Same Retry everywhere" adds UI. The deletion-only alternative is to clear `detailError` when the alpha or tab is re-selected.

### FINDINGS_F2

**F2-F1 [S] performance round trip recomputes the engine arrays — PARTLY**
- The "exact copy" proof only covers `isStartIndex = 0`. Both fixtures have isStartIndex 0 for all 4 series (probe).
- The frontend rebases PnL at IS start (api.ts:117 `prevIsCumPnl`). Its drawdown peak starts at 0 at IS start (DetailTimeSeries.tsx:115-123). The engine's `cumPnl`/`drawdown` start at simStart (timeseries_output.cpp:39-56).
- So when isStart > simStart, plotting the engine's `drawdown` slice changes the displayed drawdown. PnL slices still need a subtraction.
- The structure finding (dict round trip, 3 OS-split decisions) holds. No contract is broken, but the displayed drawdown semantics would change.

**F2-F2 [S][T] 7/11 e2e specs use a complete event the engine never sends — CONFIRMED**
- The engine's complete appends exactly one batch (main.cpp:1189-1190).
- chart_interaction.spec:11, :20 set the top-level `signalPlot` to the 6-symbol plot and put the single-symbol series only in `batches[0]`. The spec therefore passes only through SignalChart.tsx:146's per-batch branch; reading top-level would fail `plot.symbols[0] !== selectedSymbol` (:147).
- For single /api/simulate the engine writes signals at top level only (main.cpp:559 `!single`, :567-570).
- Same root as F1-F3 and F1-F4.

**F2-F3 [P] adapter fallbacks hide missing fields — CONFIRMED**
- Probe: deleting `batches[1].timeseries` gives `alpha1 series == combined series true name alpha_1`; deleting `combined` gives combined sharpe −3.5632 = batch 0.
- Every production caller passes one batch: GADetailLoader:30, api.ts:287, api.ts:398, and the engine complete (one batch). The multi-batch branch is therefore reachable **only from test mocks**.
- **Over-engineering:** "throws on missing required fields" adds validation. The deletion-only alternative is to delete the `?? resp.*` fallbacks and the multi-batch branch; a missing `timeseries` then fails naturally at api.ts:108 (`ts.timestamps`).

**F2-F4 [P] three histograms, legacy shape only in fixtures — CONFIRMED**
- main.cpp:403-413 always writes `distribution` and `signalDistributionLog` (which carries `.linear`, json_serializer.cpp:453-456) when not metricsOnly.
- SignalDistribution.tsx:27-31 shows the quantile `distribution` only when the log histogram is absent.
- `kSignalHistogramBins = 200` (json_serializer.h:33), while the fixture has 50 counts.
- Read as I, the engine cost is unquantified. The engine-side deletion contradicts only a code comment (json_serializer.cpp:454), not a doc.

**F2-F5 [T] fixtures are a Sep-11 snapshot — CONFIRMED**
- `mode` is present in the fixtures, while grep finds 0 `"mode"` in main.cpp.
- Fixture `drawdown` equals the net drawdown exactly (0.0); against gross it differs by 1.98e-3 (single) and 3.79e-3 (multi).
- The NDJSON types are `generation_end, complete`.
- No engine test reads these files.

**F2-F6 [T][P] heatmap worker path untested, duplicate caches — PARTLY**
- Confirmed:
  - The default cap is 4M cells (main.cpp:194, :251), and the frontend never sends `signalPlotMaxCells`.
  - jsdom has no Worker.
  - The largest e2e heatmap is 2000×10.
- "Three caches" is overstated. There are **two** WeakMaps (heatmapWorkers.ts:3, heatmapStats.ts:55). The per-worker buffer is a transfer, and SignalHeatmap.tsx:175 is a staleness guard.
- The timings (619 ms etc.) were not re-measured.

**F2-F7 [I][T] runGAStream never cancels the reader — CONFIRMED (frontend); engine runtime inferred**
- Probe: bad line gives `SyntaxError … reads 2 cancel calls 0`; throwing handler gives `reads 1 cancel calls 0`.
- The orphan lasts until the next run (useBacktest.ts:35) or unmount (:29).
- That the engine keeps running relies on main.cpp:1066 `req->connected()`. A runtime check on an isolated engine port is still unverified.

**F2-F8 [S] positional request builders, GA defaults in 4 layers — CONFIRMED**
- Understated in one respect: `collectSignals` is inert too, not only top-K (see seam V2).
- Same root as F1-F12.

**F2-F9 [P] FastAPI-era error parsing — CONFIRMED**
- The engine writes only `"error"` (main.cpp:604, 961, 1022, 1030, 1197, 1211).
- The `detail` branches are pinned only by apiClient.contract.test:98-103 and :153-157.

**F2-F10 [P] dead gross-fee fallback, unreachable minBet chart — CONFIRMED**
- AlphaDetailView.tsx:8-14 has no `minBet`.
- The 0.001 fee default contradicts CLAUDE.md's 1.5 bp default, so the deletion moves the code toward the contract.
- Subsumed by F2-F1.

**F2-F11 [S] chart boilerplate and 5 signal formatters — CONFIRMED**
- SignalChart.tsx:34-37 (`toFixed(6)`), SignalDistribution.tsx:9-15 (`toExponential(2)`), SignalHeatmap.tsx:279 and SignalChart.tsx:94 (`toFixed(4)`), SignalChart.tsx:280 (`toFixed(3)`).
- The deletion-only part is sharing the formatter. The "base uPlot options helper" is a new helper (optional).

**F2-F12 [P] theme palette drift — CONFIRMED, impact overstated**
- 4 light values differ, and backgroundElevated, positive and negative have 0 consumers.
- The palette is used only when the requested theme differs from the DOM theme (themeColors.ts:27), i.e. the first paint before `initializeTheme`. In that case the whole palette is the wrong theme, so the 4-value drift is cosmetic and transient.

**F2-F13 [P] dead display surface — PARTLY**
- Confirmed:
  - BacktestMetrics has 0 references repo-wide, and its labels "MinBet"/"Alpha" contradict CLAUDE.md:20.
  - `rank` has no reader.
  - longRatio and shortRatio have no reader.
- Wrong: `ann_ret ?? pnl` is not dead. pendingGACombined (gaCombined.ts:14) and the failed-candidate blank (api.ts:224) lack `ann_ret`. The output is identical ('-') because `pnl` is NaN and formatPnl handles undefined, so the fallback is redundant rather than unreachable.

**F2-F14 [T][P] pin snapshots — PARTLY**
- Untested (0 hits) and the `retSeries`-holds-signals misnaming are confirmed.
- "Stale across runs" is a judgement. Snapshots are named by symbol and listed in SnapshotManager, and cross-run comparison may be the feature's purpose (F1 lists it as unverified intent). No doc settles it, so clearing on run is a behaviour change, not a cleanup.

**F2-F15 [I] SignalChart request restarts on settings edits — CONFIRMED, severity overstated**
- The deps at :158 include `currentSettings` and `result?.runSettings`.
- The waste happens only while a row-sampled heatmap's series is loading or has failed (:127 early return).

**F2-F16 [P] e2e assertions on CSS or removed text — PARTLY**
- Confirmed:
  - smoke.spec:31-32 (`[class*="flex"]`) is vacuous.
  - The `reference-*.png` screenshots are never compared.
  - The 8 absent strings have 0 source hits.
- Overstated:
  - The geometry checks guard explicit layout decisions: detail_layout.spec:41 comments "no 900px cap leaving a gutter", and signal_panels:30-32 pins the `lib/signalPanelLayout` constants 64/192.
  - The absent-text checks are cheap guards against re-adding deliberately removed UI. They can still fail when someone re-adds that UI.

**F2-F17 [S] SignalHeatmap ref scaffolding — PARTLY**
- Counts confirmed: 17 `useRef`, 11 `useCallback`, 13 `.current =`.
- The ":91 17 symbols max" comment is wrong; the code does `slice(0, 200)` at :94.
- Wrong: "only drawBase needs a stable identity". `drawSelection` is also an effect dependency (:225).
- The S tag is really P (reader cost).

## 2. Cross-report overlap (same root, two reports)

| Pair | Root |
|---|---|
| F1-F3 ≡ F2-F2 | Unused `complete` transport kept alive by mockRun |
| F1-F4 ⊂ F2-F2 | Unused `complete` transport kept alive by mockRun |
| F2-F5 feeds F1-F3, F1-F4, F2-F2 | Unused `complete` transport kept alive by mockRun |
| F2-F3 ⊂ F2-F2 | The multi-batch adapter branch exists only for mocked multi-batch `complete` |
| F1-F12 ≡ F2-F8 | Optional GA budget fields, so defaults live in 4 layers |
| F1-F11 ↔ F2-F8 | Run settings are rebuilt per call site with "older server" fallbacks |
| F1-F14 ↔ F2-F13 | Unmounted metric components (SummaryMetrics, BacktestMetrics) kept alive by render tests |
| F1-F5 ↔ F2-F15 | Request effects keyed on broad dependencies abort or restart in-flight engine work |
| F1-F1 ↔ F1-F2 | `selectedAlphaIndex` doubles as "highlighted row" and "show Performance" |
| F2-F10 ⊂ F2-F1 | F2's own note |
| F1-F15 ↔ V1 (below) | A cached error makes a failed replay permanent |

## 3. Seam findings the reports missed

### V1 [S][T] Saved-result replays send a universe the engine rejects (runSettings is both provenance and replay request)
- **Where:**
  - LoadAlphaModal.tsx:52-53 resolves a fallback `universe`, but :62 stores `runSettings: { ...metadata }`, which keeps the original.
  - Replays read `runSettings.universe`: SignalPanelSelector.tsx:28/:42, SignalChart.tsx:138/:143, and GACombinedLoader.tsx:30 → api.ts:285.
  - The engine rejects it: data_panel.cpp:70 `throw std::invalid_argument("Unknown universe: " + name)`, returned as a 400 at main.cpp:1020-1025.
- **Evidence:**
  - SaveAlphaModal.roundtrip.test:157-168 pins `settings.universe 'TOP3000'` together with `runSettings.universe 'OLD_V1'`.
  - The UI then shows "using TOP3000", while every engine replay posts OLD_V1.
  - Saved Stats rows and the batch read files (GADetailLoader:23-24, GACombinedLoader:29), so they work. But the Round-N combined row and single-alpha Signals/series hit the engine.
- **Why it is a problem:**
  - A round-tab combined fails, and the failure is cached permanently (F1-F15).
  - Single-alpha signals show a 400, and their Retry can never succeed.
  - The warning text contradicts the requests actually sent.
- **Simpler shape:** replay requests use the universe the modal already resolved; provenance stays in the saved metadata. This means deriving `runSettings.universe` from the resolved value at :62.
- **Blast radius:** LoadAlphaModal; SaveAlphaModal.roundtrip.test:168, whose assertion would change.
- **Test gate:** missing a test of the form "a saved result with an unavailable universe loads Round-tab combined and Signals without a 400".
- **Effort:** S

### V2 [P] `collectSignals: true, collectSignalsTopK: 5` are inert on every request the frontend sends
- **Where:** api.ts:261; main.cpp:247, 250, 466, 559, 580.
- **Evidence:**
  - The single-alpha path uses `!options.metrics_only` as its collect flag (main.cpp:466) and never reads `collect_signals`.
  - `collect_signals` and `signal_top_k` are read only at :559 (multi-alpha, non-metricsOnly) and :580 (combined, non-metricsOnly).
  - The frontend's only multi-alpha request is combinedOnly + metricsOnly (api.ts:285). So both keys change nothing in any of the 4 POST shapes.
  - F2-F8 noticed only top-K; MAP §6.1 recorded it only as a data point.
- **Why it is a problem:** readers believe the frontend requests top-5 per-batch signals, which no response ever contains.
- **Simpler shape:** delete both keys from the runSimulate body.
- **Blast radius:** api.ts; apiClient.contract body assertions, if any.
- **Test gate:** single_alpha_view.spec:46 (full runs still return signals).
- **Effort:** S

The other boundaries checked had no new finding:
- store↔api adapters: the KEEP set in GADetailLoader is a symptom of F2-F3.
- MAP_frontend §6 against MAP_engine §3.2: every key sent is parsed. The engine-only keys are signalPlotMaxCells, combinedMode, missingClose and the popSize/generations aliases.

## 4. Final table

| id | verdict | severity honest? | contract conflict? | over-engineering? | note |
|---|---|---|---|---|---|
| F1-F1 | CONFIRMED | yes | no | no | 16k fuzz steps, 0 violations; setViewMode test-only |
| F1-F2 | PARTLY | yes | **yes: CLAUDE.md:8 saved loads open Performance** | **yes: adds explicitPerformance flag** | Only the Round-tab path is undocumented divergence |
| F1-F3 | CONFIRMED | yes | **yes (engine part): math_contract:197-199** | no | Delete frontend handlers only; engine legacy path is contract |
| F1-F4 | CONFIRMED | yes | no | no | No search_start in any mocked stream; one-chunk delivery |
| F1-F5 | PARTLY | overstated (no size) | no | **yes: needs in-flight registry** | Aborts reproduced; abort-only-on-revision removes concurrency bound |
| F1-F6 | CONFIRMED | yes | no | no | LRU holds at most index 0 |
| F1-F7 | CONFIRMED | S→P | no | no | gaProgress also read by dead preserve-detail rule |
| F1-F8 | CONFIRMED | yes | no | no | 21 test sites/7 files, not 23 |
| F1-F9 | CONFIRMED | yes | no | no | Shared helper is a merge; no deletion-only alternative |
| F1-F10 | CONFIRMED | yes | no | no | "Reserved for submit" is product intent, undocumented |
| F1-F11 | CONFIRMED | **understated** | **yes: CLAUDE.md:19 older-API execution clause** | no | Workspace data start overwrites default osStart every load |
| F1-F12 | CONFIRMED | yes | no | no | Same root as F2-F8 |
| F1-F13 | CONFIRMED | yes | no | no | Negligible cost; readability only |
| F1-F14 | PARTLY | yes | no | no | exitDetailView on xml load is live behaviour, not dead |
| F1-F15 | CONFIRMED | yes | no | partly: "no cache" loses message | Deletion alternative: clear detailError on re-selection |
| F2-F1 | PARTLY | yes | no (display change) | no | Exactness proven only for isStartIndex=0; drawdown rebase differs |
| F2-F2 | CONFIRMED | yes | no | no | chart_interaction passes only via dead per-batch branch |
| F2-F3 | CONFIRMED | yes | no | **yes: validation; delete fallbacks instead** | Multi-batch branch reachable only from mocks |
| F2-F4 | CONFIRMED | yes (P) | no doc; engine comment only | no | 200 engine bins vs 50 in fixtures |
| F2-F5 | CONFIRMED | yes | no | no | Fixture drawdown is net-based exactly |
| F2-F6 | PARTLY | cache count overstated | no | no | Two WeakMaps, not three; timings not re-measured |
| F2-F7 | CONFIRMED | yes | no | no | cancel 0×; engine continuation inferred, not run |
| F2-F8 | CONFIRMED | slightly understated | no | no | collectSignals inert too (V2) |
| F2-F9 | CONFIRMED | yes | no | no | Engine writes only `error` |
| F2-F10 | CONFIRMED | yes | supports CLAUDE.md 1.5bp | no | Subsumed by F2-F1 |
| F2-F11 | CONFIRMED | yes | no | mild: options helper optional | Formatter outputs verified from code |
| F2-F12 | CONFIRMED | impact overstated | no | no | Palette used only on first paint |
| F2-F13 | PARTLY | yes | supports CLAUDE.md naming | no | `ann_ret ?? pnl` redundant, not unreachable |
| F2-F14 | PARTLY | yes | no | no | Cross-run pins may be intended |
| F2-F15 | CONFIRMED | overstated (narrow window) | no | no | Only while loading/after error, row-sampled heatmaps |
| F2-F16 | PARTLY | overstated for layout checks | no | no | Geometry checks guard explicit layout decisions |
| F2-F17 | PARTLY | S→P | no | no | drawSelection also needs stable identity; comment wrong |
| V1 | NEW | S/T | no | no | Saved replays post unavailable universe → 400, cached |
| V2 | NEW | P | no | no | collectSignals/TopK inert for all frontend requests |
