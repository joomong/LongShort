# MAP_frontend — data map of /home/jgo/longshort_platform/frontend (working tree, 2026-10-14)

Agent: map_frontend. Read-only. No judgment, no fixes. Every number below comes from a command or a file:line
recorded here; scripts used are in `refactor_audit/map_frontend/{graph.mjs,complexity.mjs}` (text only).

Scope: `app/ components/ hooks/ lib/ store/ types/ workers/` = 46 TS/TSX source files, 5,228 lines
(app 273, components 2,962, hooks 140, lib 989, store 496, types 352, workers 16; + `app/globals.css` 125,
`tailwind.config.ts` 54). Tests: 25 vitest files in `__tests__/` (2,233 lines) + 2 helper modules
(metricFixtures.ts 43, pipelineFixtures.ts 22) + 4 fixture data files + 11 Playwright specs in `tests/e2e/`
(749 lines) + `tests/helpers/mockRun.ts` (17).
`scripts/` has 5 files (`prepare-monaco.mjs` used by `predev/prebuild`; the other 4 are not referenced by anything).

---

## 0. Commands run (verbatim) and whether they worked

| # | Command (cwd = frontend/) | Result |
|---|---|---|
| 1 | `npx --yes madge --extensions ts,tsx --ts-config tsconfig.json --circular app components hooks lib store` | worked (network fetch into `~/.npm/_npx`, repo untouched). Output: `Processed 45 files (384ms)` / `✔ No circular dependency found!` exit 0 |
| 2 | `npx --yes knip` | worked (network). Output reproduced in §3.0 |
| 3 | `./node_modules/.bin/tsc --noEmit --noUnusedLocals --noUnusedParameters --incremental false` | exit 0, **zero diagnostics in every file** (empty output). Local binary used instead of `npx tsc` so nothing is downloaded; `--incremental false` so `tsconfig.tsbuildinfo` is not rewritten (its mtime 16:58:06 predates the run) |
| 4 | `CI=1 ./node_modules/.bin/vitest run --coverage` | **failed without installing**: `MISSING DEPENDENCY Cannot find dependency '@vitest/coverage-v8'` exit 1. `node_modules/@vitest/` holds only expect/mocker/pretty-format/runner/snapshot/spy/utils. → no per-file line coverage; §1.4 "not imported by any test" is the substitute |
| 5 | `CI=1 ./node_modules/.bin/vitest run` | `Test Files 25 passed (25)`, `Tests 124 passed (124)`, Duration 3.28s |
| 6 | `npm run lint` | skipped per brief (already clean) |
| 7 | `git status --short frontend/package.json frontend/package-lock.json` | empty (unchanged); `package.json`/`package-lock.json` mtimes 2026-09-21. The ~40 pre-existing `M` entries under frontend/ are the working-tree state the audit was told to read |

Scratch: `du -sh …/refactor_audit/map_frontend` = **144K** (graph.mjs, complexity.mjs, graph.json, complexity.json,
graph_report.txt, tsc_unused.txt — all text). `df -h /`: 67% → 69% during the session; `~/.npm/_npx` grew to 113M
from the madge/knip downloads (outside the repo and outside scratch).

---

## 1. Inventory

### 1.1 Source files (lines | exported symbols | importers non-test / test)

Importers = distinct repo files with an `import … from`, `import()` or `vi.mock()` of this module (script: graph.mjs).
`(dyn)` = only reachable through `next/dynamic` import in app/page.tsx.

| file | lines | exports | importers nt/t |
|---|---|---|---|
| app/layout.tsx | 52 | metadata, default | 0/0 (Next entry) |
| app/page.tsx | 221 | default | 0/0 (Next entry) |
| components/editor/ControlButtons.tsx | 135 | default, AlphaStorageButtons | 1/2 |
| components/editor/XmlEditor.tsx | 61 | default | 1(dyn)/1 |
| components/layout/MainLayout.tsx | 115 | default | 1/0 |
| components/modals/LoadAlphaModal.tsx | 164 | default | 1(dyn)/1 |
| components/modals/SaveAlphaModal.tsx | 177 | default | 1(dyn)/1 |
| components/multi-alpha/AlphaRankingTable.tsx | 230 | default | 1(dyn)/3 |
| components/multi-alpha/SummaryMetrics.tsx | 62 | default | **0**/1 |
| components/providers/ThemeProvider.tsx | 12 | default | 1/0 |
| components/visualization/AlphaDetailView.tsx | 76 | default | 1(dyn)/0 |
| components/visualization/AlphaXmlPanel.tsx | 37 | default | 1/0 |
| components/visualization/BacktestMetrics.tsx | 131 | default | **0**/1 |
| components/visualization/DetailSummaryTable.tsx | 60 | default | 1/1 |
| components/visualization/DetailTimeSeries.tsx | 510 | DetailMetricKey, PnlSeriesPoint, seriesToChartData, buildPnlBundleFromRet, buildMddDataFromEquity, default | 1/1 |
| components/visualization/GACombinedLoader.tsx | 38 | default | 1/2 |
| components/visualization/GADetailLoader.tsx | 42 | default | 1/2 |
| components/visualization/ResultsTabs.tsx | 36 | default | 1/1 |
| components/visualization/SettingsBar.tsx | 125 | default | 1/3 |
| components/visualization/SignalChart.tsx | 340 | default | 1(dyn)/0 |
| components/visualization/SignalDistribution.tsx | 110 | default | 1(dyn)/1 |
| components/visualization/SignalHeatmap.tsx | 393 | default | 1(dyn)/0 |
| components/visualization/SignalPanelSelector.tsx | 65 | default | 1/1 |
| components/visualization/SnapshotManager.tsx | 43 | default | 1(dyn)/0 |
| hooks/useBacktest.ts | 140 | useBacktest | 1/2 |
| lib/api.ts | 405 | adaptSignalPayload, adaptSimulateResponse, adaptGACompleteToResponse, adaptGACandidate, getConfig, runSimulate, runCombinedAlphas, runGAStream, AlphaExistsError, saveAlpha, getAlphaList, getSavedResults, getSavedRow, getSavedBatch, deleteAlpha | 8/10 |
| lib/chartInteraction.ts | 161 | clampRange, wheelPixels, chartInteraction | 2/1 |
| lib/colorScale.ts | 44 | Z_CLIP, zscoreToColor, zscoreToRgb, signalToColor, parseHex | 1/1 |
| lib/gaCombined.ts | 16 | gaCombinedGroup, pendingGACombined | 2/0 |
| lib/heatmapStats.ts | 56 | SignalRows, robustCrossSection | 2/2 |
| lib/heatmapWorkers.ts | 51 | normalizeHeatmap | 1/0 |
| lib/metricColors.ts | 13 | getSignCellColor, getMetricCellColor | 3/1 |
| lib/reportUnits.ts | 4 | REPORT_BOOK_DOLLARS, MILLION_DOLLARS, REPORT_BOOK_M | 2/0 |
| lib/signalPanelLayout.ts | 3 | SIGNAL_DISTRIBUTION_HEIGHT, SIGNAL_HEATMAP_HEIGHT | 2/0 |
| lib/signalSelection.ts | 29 | selectSignalSource, nearestTimestamp, selectSignalCell | 5/1 |
| lib/snapshotColors.ts | 6 | SNAPSHOT_COLORS, getNextColor | 1/0 |
| lib/symbolLabels.ts | 11 | symbolLabel, symbolMatches | 3/0 |
| lib/themeColors.ts | 34 | Theme, ThemeColors, getThemeColors | 5/1 |
| lib/universeMembership.ts | 27 | OUT_OF_UNIVERSE_RGB, OUT_OF_UNIVERSE_HEX, isMember, splitByMembership | 2/1 |
| lib/uplotAdapters.ts | 83 | ContainerSize, useContainerSize, utcDate, useUPlot | 2/1 |
| lib/uplotUtils.ts | 46 | dateStrToUnix, fmtDate, fmtMonth, timeAxisValues, getDPR, toMultiSeriesData, toAlignedData, toIsOsAlignedData, buildUPlotAxes, TOOLTIP_STYLE | 3/2 |
| store/alphaStore.ts | 456 | MAX_CACHED_SIGNAL_ALPHAS, canShortcutRun, enforceSettingsDates, useAlphaStore | 21/16 |
| store/themeStore.ts | 40 | useThemeStore, initializeTheme | 6/0 |
| types/index.ts | 352 | 33 types (StarSummary … ViewMode) | 12/10 |
| workers/heatmap.worker.ts | 16 | (none; `self.onmessage`) | 0/0 by import; loaded via `new URL('../workers/heatmap.worker.ts', import.meta.url)` at lib/heatmapWorkers.ts:25 |

### 1.2 Test files → source files imported directly (lines | tests | src imports | helpers)

| test | lines | it/test | source modules imported | helper/fixture |
|---|---|---|---|---|
| __tests__/adaptSimulateResponse.test.ts | 297 | 7 | lib/api, types | – |
| __tests__/alphaStore.test.ts | 164 | 12 | store/alphaStore, lib/api (vi.mock) | metricFixtures |
| __tests__/apiClient.contract.test.ts | 180 | 10 | lib/api (fetch mocked) | – |
| __tests__/chartInteraction.test.ts | 37 | 4 | lib/chartInteraction, lib/heatmapStats, lib/uplotUtils | – |
| __tests__/cppFixtures.contract.test.ts | 36 | 3 | (none directly) | pipelineFixtures |
| __tests__/DetailTimeSeries.derived.test.ts | 105 | 7 | components/visualization/DetailTimeSeries | – |
| __tests__/enforceSettingsDates.test.ts | 75 | 8 | store/alphaStore, types | – |
| __tests__/heatmapStats.test.ts | 56 | 6 | lib/heatmapStats | – |
| __tests__/signalColors.test.ts | 23 | 2 | lib/colorScale, lib/themeColors, lib/metricColors | – |
| __tests__/universeMembership.test.ts | 18 | 2 | lib/universeMembership | – |
| __tests__/uplotAdapters.test.ts | 73 | 7 | lib/uplotUtils, lib/uplotAdapters | – |
| __tests__/AlphaRankingTable.render.test.tsx | 125 | 4 | components/multi-alpha/AlphaRankingTable, store | metricFixtures |
| __tests__/BacktestMetrics.render.test.tsx | 40 | 1 | components/visualization/BacktestMetrics, store | metricFixtures |
| __tests__/DetailSummaryTable.render.test.tsx | 76 | 4 | components/visualization/DetailSummaryTable, store | pipelineFixtures |
| __tests__/GACombinedLoader.test.tsx | 48 | 2 | GACombinedLoader, lib/api (vi.mock full stub), store | metricFixtures |
| __tests__/GADetailLoader.test.tsx | 55 | 2 | GADetailLoader, store, lib/api (vi.mock partial) | fixtures/cpp_single.json, metricFixtures |
| __tests__/gaResults.test.tsx | 152 | 8 | ResultsTabs, AlphaRankingTable, SettingsBar, ControlButtons, store | metricFixtures |
| __tests__/logDistribution.test.tsx | 56 | 3 | SignalDistribution, lib/api, store, types | fixtures/cpp_single.json |
| __tests__/SaveAlphaModal.roundtrip.test.tsx | 208 | 10 | SaveAlphaModal, LoadAlphaModal, ControlButtons, AlphaRankingTable, GADetailLoader, GACombinedLoader, store, types | fixtures/cpp_single.json, cpp_multi.json |
| __tests__/SettingsBar.universe.test.tsx | 54 | 3 | SettingsBar, store | – |
| __tests__/signalSelection.test.tsx | 96 | 5 | store, lib/signalSelection, SignalPanelSelector, lib/api (vi.mock partial), types | – |
| __tests__/simPeriod.test.tsx | 60 | 5 | SettingsBar, hooks/useBacktest, store, lib/api (vi.mock full stub) | – |
| __tests__/SummaryMetrics.render.test.tsx | 31 | 1 | components/multi-alpha/SummaryMetrics, store | pipelineFixtures |
| __tests__/useBacktest.pipeline.test.tsx | 98 | 6 | store, hooks/useBacktest, lib/api (vi.mock full stub), types | metricFixtures |
| __tests__/XmlEditor.test.tsx | 70 | 2 | XmlEditor, store (vi.mock next/dynamic) | metricFixtures |
| __tests__/metricFixtures.ts (helper) | 43 | – | types | – |
| __tests__/pipelineFixtures.ts (helper) | 22 | – | lib/api, types | fixtures/*.json, cpp_ga_events.ndjson |
| tests/e2e/alpha_selection.spec.ts | 39 | 1 | (browser only) | mockRun, cpp_multi.json, capture.json |
| tests/e2e/chart_interaction.spec.ts | 166 | 4 | – | mockRun, capture.json, cpp_multi.json |
| tests/e2e/detail_layout.spec.ts | 55 | 1 | – | mockRun, cpp_multi.json, capture.json |
| tests/e2e/execution_price.spec.ts | 34 | 1 | – | mockRun, capture.json, cpp_multi.json |
| tests/e2e/ga_default.spec.ts | 57 | 1 | – | capture.json, cpp_multi.json (hand-built candidate stream) |
| tests/e2e/live_engine.spec.ts | 102 | 1 (skipped unless PLAYWRIGHT_ENGINE_URL) | types | capture.json |
| tests/e2e/metrics_pipeline.spec.ts | 87 | 1 | – | mockRun, cpp_multi.json, cpp_single.json, capture.json |
| tests/e2e/signal_panels.spec.ts | 71 | 2 | – | mockRun, cpp_single.json, capture.json |
| tests/e2e/sim_period.spec.ts | 36 | 1 | – | mockRun, capture.json, cpp_multi.json |
| tests/e2e/single_alpha_view.spec.ts | 68 | 1 | – | capture.json, cpp_single.json (hand-built candidate stream) |
| tests/e2e/smoke.spec.ts | 34 | 3 | – | capture.json |
| tests/helpers/mockRun.ts | 17 | – | types | – |

Fixture provenance: `__tests__/fixtures/{cpp_single.json 38KB, cpp_multi.json 90KB, cpp_ga_events.ndjson 28KB,
capture.json}` dated 2026-09-11, produced by `scripts/capture_engine_fixtures.mjs`. Their key sets are those of the
Sep-11 engine: top-level `mode: 1` present; **absent**: `signalDistributionLog`, `executionPrice`, `simStart/simEnd`,
`universe`, `combinedMode`-era `missingData`, `signalPlot.membership`, `timeseries.frozenGross`, yearly `ir/winLose/
fitness`; capture.config lacks `supportsSimEnd/executionPrice(s)/universes/defaultUniverse/alphasDir/symbolLabels`.
cpp_ga_events.ndjson contains only `generation_end` + `complete` (the non-streaming GA path; see §7).

### 1.3 Source files not imported by any test (directly)

app/layout.tsx, app/page.tsx, components/layout/MainLayout.tsx, components/providers/ThemeProvider.tsx,
components/visualization/AlphaDetailView.tsx, components/visualization/AlphaXmlPanel.tsx,
components/visualization/SignalChart.tsx, components/visualization/SignalHeatmap.tsx,
components/visualization/SnapshotManager.tsx, lib/gaCombined.ts, lib/heatmapWorkers.ts, lib/reportUnits.ts,
lib/signalPanelLayout.ts, lib/snapshotColors.ts, lib/symbolLabels.ts, store/themeStore.ts, workers/heatmap.worker.ts
(17 of 46). Of these, SignalChart/SignalHeatmap/AlphaDetailView/SnapshotManager/AlphaXmlPanel are exercised only by
the Playwright specs through the browser (not run here; see §0 #4).

---

## 2. Import graph (repo-internal, source only)

madge: **0 cycles** (45 files processed; madge was given app/components/hooks/lib/store — types/ and workers/ were
reached transitively). My own graph (graph.mjs, includes types/workers): adjacency below, also acyclic.

```
app/layout.tsx -> components/providers/ThemeProvider
app/page.tsx -> MainLayout, ControlButtons, SettingsBar, AlphaXmlPanel, SignalPanelSelector, ResultsTabs,
                GADetailLoader, GACombinedLoader, hooks/useBacktest, store/alphaStore,
                (dyn) XmlEditor, AlphaDetailView, AlphaRankingTable, SignalDistribution, SignalHeatmap,
                (dyn) SignalChart, SnapshotManager, SaveAlphaModal, LoadAlphaModal
components/editor/ControlButtons -> store/alphaStore, store/themeStore
components/editor/XmlEditor -> store/alphaStore, store/themeStore
components/layout/MainLayout -> (none)
components/modals/LoadAlphaModal -> store/alphaStore, lib/api, types(type)
components/modals/SaveAlphaModal -> store/alphaStore, lib/api, types(type)
components/multi-alpha/AlphaRankingTable -> store/alphaStore, lib/metricColors, types(type)
components/multi-alpha/SummaryMetrics -> store/alphaStore, lib/metricColors
components/providers/ThemeProvider -> store/themeStore
components/visualization/AlphaDetailView -> store/alphaStore, DetailTimeSeries, DetailSummaryTable
components/visualization/AlphaXmlPanel -> store/alphaStore
components/visualization/BacktestMetrics -> store/alphaStore, lib/metricColors, types(type)
components/visualization/DetailSummaryTable -> types(type), lib/reportUnits
components/visualization/DetailTimeSeries -> store/themeStore, store/alphaStore, lib/themeColors, lib/uplotUtils,
                lib/chartInteraction, lib/uplotAdapters, types(type), lib/reportUnits
components/visualization/GACombinedLoader -> lib/api, lib/gaCombined, store/alphaStore
components/visualization/GADetailLoader -> lib/api, store/alphaStore
components/visualization/ResultsTabs -> store/alphaStore
components/visualization/SettingsBar -> store/alphaStore, types(type)
components/visualization/SignalChart -> store/alphaStore, lib/signalSelection, lib/symbolLabels, lib/api,
                store/themeStore, lib/themeColors, lib/snapshotColors, lib/uplotUtils, lib/chartInteraction,
                lib/uplotAdapters, lib/universeMembership
components/visualization/SignalDistribution -> store/alphaStore, lib/signalSelection, lib/symbolLabels, lib/signalPanelLayout
components/visualization/SignalHeatmap -> store/alphaStore, lib/signalSelection, store/themeStore, lib/themeColors,
                lib/colorScale, lib/symbolLabels, lib/heatmapWorkers, lib/signalPanelLayout, lib/universeMembership
components/visualization/SignalPanelSelector -> store/alphaStore, lib/api, lib/signalSelection
components/visualization/SnapshotManager -> store/alphaStore
hooks/useBacktest -> store/alphaStore, lib/api, types(type)
lib/api -> types(type)
lib/chartInteraction -> lib/uplotUtils
lib/colorScale -> lib/themeColors
lib/gaCombined -> types(type)
lib/heatmapWorkers -> lib/heatmapStats
lib/signalSelection -> types(type)
lib/uplotUtils -> lib/themeColors(type)
store/alphaStore -> types(type), lib/api, lib/signalSelection, lib/gaCombined
lib/heatmapStats, lib/metricColors, lib/reportUnits, lib/signalPanelLayout, lib/snapshotColors, lib/symbolLabels,
lib/themeColors, lib/universeMembership, lib/uplotAdapters, store/themeStore -> (none)
workers/heatmap.worker -> ../lib/heatmapStats (relative import)
```

Fan-in: store/alphaStore 21 non-test importers (every component except MainLayout/ThemeProvider/DetailSummaryTable);
types/index 12; lib/api 8; store/themeStore 6; lib/signalSelection 5; lib/themeColors 5.
Only direct source→source edge that is not "component → store/lib/types": AlphaDetailView → DetailTimeSeries,
DetailSummaryTable (the only component that imports another component besides app/page.tsx).

---

## 3. Dead / unused

### 3.0 knip output (verbatim, `npx --yes knip`)
```
Unused files (4)
scripts/capture_engine_fixtures.mjs
scripts/pw_debug.mjs
scripts/screenshot_detail.cjs
scripts/screenshot_detail.mjs
Unused dependencies (1)
uplot-react  package.json:23:6
Unused devDependencies (1)
@testing-library/user-event  package.json:31:6
Unlisted dependencies (1)
postcss-load-config  postcss.config.mjs:1:10
Unused exports (8)
gaStreamEventsFixture               __tests__/pipelineFixtures.ts:18:14
deleteAlpha               function  lib/api.ts:402:23
Z_CLIP                              lib/colorScale.ts:8:14
nearestTimestamp          function  lib/signalSelection.ts:13:17
SNAPSHOT_COLORS                     lib/snapshotColors.ts:1:14
getDPR                              lib/uplotUtils.ts:14:14
TOOLTIP_STYLE                       lib/uplotUtils.ts:43:14
MAX_CACHED_SIGNAL_ALPHAS            store/alphaStore.ts:98:14
Unused exported types (9)
StarYearly             interface  types/index.ts:42:18
StarBatchEntry         interface  types/index.ts:83:18
StarGAGenerationEvent  interface  types/index.ts:125:18
StarGAErrorEvent       interface  types/index.ts:146:18
StarGASearchEvent      type       types/index.ts:160:13
SignalRange            interface  types/index.ts:186:18
ISMetrics              type       types/index.ts:216:13
OSMetrics              type       types/index.ts:217:13
SavedAlphaRow          interface  types/index.ts:307:18
```

### 3.1 Exports with 0 non-test usages (graph.mjs cross-checked with `grep -rn` over the whole frontend)

Referenced nowhere at all (not even in own file beyond the definition):
- lib/api.ts:402 `deleteAlpha` — 1 ref total (its definition). No UI calls DELETE /api/alphas/{name}.
- lib/uplotUtils.ts:14 `getDPR`, :43 `TOOLTIP_STYLE` — 1 ref each.
- store/alphaStore.ts:404 `clearGA` action — 2 refs, both in the store (interface + impl); never called by source or tests.

Exported, used only inside their own file (could be module-private):
- lib/colorScale.ts:8 `Z_CLIP` (3 internal uses), lib/signalSelection.ts:13 `nearestTimestamp` (used at :27),
  lib/snapshotColors.ts:1 `SNAPSHOT_COLORS` (used at :5), store/alphaStore.ts:98 `MAX_CACHED_SIGNAL_ALPHAS` (used :250),
  lib/uplotUtils.ts `timeAxisValues` (used by buildUPlotAxes :41), lib/uplotAdapters.ts `utcDate` (used :63),
  lib/chartInteraction.ts `clampRange`/`wheelPixels` (used :56,:119-120), lib/colorScale.ts `zscoreToColor`
  (used by signalToColor :41), lib/metricColors.ts `getSignCellColor` (used :12), types: StarYearly, StarBatchEntry,
  StarGAGenerationEvent, StarGAErrorEvent, StarGASearchEvent, SignalRange, ISMetrics, OSMetrics, SavedAlphaRow,
  lib/themeColors.ts `Theme`, lib/uplotAdapters.ts `ContainerSize`, DetailTimeSeries.tsx `PnlSeriesPoint`.

Used only by tests (0 non-test importers, ≥1 test importer):
- components/visualization/BacktestMetrics.tsx (default) — only `__tests__/BacktestMetrics.render.test.tsx`. Not in app/page.tsx.
- components/multi-alpha/SummaryMetrics.tsx (default) — only `__tests__/SummaryMetrics.render.test.tsx`. Not in app/page.tsx.
- lib/colorScale.ts:36 `signalToColor` (comment "Legacy raw-value mapping kept for callers outside the heatmap") — only `signalColors.test.ts`.
- lib/uplotUtils.ts:32 `toIsOsAlignedData` — only `uplotAdapters.test.ts`.
- lib/uplotUtils.ts:9 `fmtMonth` — internal (:12) + `uplotAdapters.test.ts`.
- store/alphaStore.ts:148 `enforceSettingsDates` — internal (:203,:335) + 2 tests (17 test refs).
- store/alphaStore.ts:236 `selectSignalAlpha` action — 0 source callers, 3 refs in `signalSelection.test.tsx`.
- store/alphaStore.ts:271 `setViewMode` action — 0 source callers, 1 ref in `gaResults.test.tsx`.
- DetailTimeSeries.tsx `seriesToChartData`, `buildPnlBundleFromRet`, `buildMddDataFromEquity` — internal + `DetailTimeSeries.derived.test.ts`.
- __tests__/pipelineFixtures.ts:18 `gaStreamEventsFixture` — internal to the helper.

Typed fields never produced by any adapter (frontend-only phantoms, types/index.ts): `PeriodMetrics.ir?` (:212),
`PeriodMetrics.corr?` (:213), `BacktestAlphaResult.isMinBetSeries?/osMinBetSeries?` (:250-251; read by
DetailTimeSeries.tsx:419,432 → always `[]`; `METRICS` in AlphaDetailView.tsx:8-14 never selects `'minBet'`, so the
`metric === 'minBet'` branch DetailTimeSeries.tsx:474-485 is unreachable from the UI), `BacktestRunResponse.initialCapital?`
(:270), `BacktestRunResponse.sortedBy?` (:271, only set by `__tests__/metricFixtures.ts:81`).

### 3.2 package.json dependencies vs source grep

| dependency | referenced in | note |
|---|---|---|
| uplot-react | package.json only | **unused** (knip agrees) |
| @testing-library/user-event | package.json only | **unused** devDep |
| react-dom | package.json only | implicit Next runtime dep, no direct import |
| monaco-editor | scripts/prepare-monaco.mjs only (copies `min/vs` into public/monaco) | build-time |
| playwright (non-@playwright/test) | scripts/pw_debug.mjs, scripts/screenshot_detail.{mjs,cjs} | those 3 scripts are knip "unused files" |
| @types/*, eslint*, postcss, tailwindcss, typescript, jsdom | config files only | normal |
| postcss-load-config | postcss.config.mjs:1 | **unlisted** (knip) |
| all others | ≥1 source/test file | – |

---

## 4. Store anatomy — store/alphaStore.ts (456 lines; `create<AlphaState>` body = lines 185-456, one expression)

Helpers: `combinedView` (:105-112, calls `gaCombinedGroup` + `pendingGACombined`), `singleAlphaSignals` (:115-119),
`resetSplit` (:137-145), `enforceSettingsDates` (:148-183), `selectSignalCell`/`selectSignalSource` (lib/signalSelection).

### 4.1 State field → writers (action: line) and readers (file(count of `s.`/`state.`/`store.`/`getState().` sites outside the store))

| field | writers (count) | readers outside store |
|---|---|---|
| xmlCode | setXmlCode:188 (1) | ControlButtons(1), XmlEditor(1), SaveAlphaModal(1), useBacktest(2, ref+subscribe) |
| alphaName | setAlphaName:193 (1) | page(1) |
| backtestError | setBacktestError:197 (1) | page(1) |
| settings | setSettings:201, fetchConfig:335 (2) | LoadAlphaModal(1), SaveAlphaModal(1), SettingsBar(1), SignalChart(1), SignalPanelSelector(1), useBacktest(2) |
| **result** | setResult:222, cacheAlphaSignals:256, beginGA:355, loadSavedResult:366(combinedView), startGARound:380(combinedView, cond), appendGACandidate:388(combinedView), finishGA:396(combinedView, cond), selectResultTab:419(combinedView), cacheGACombined:429(cond), cacheGADetail:436 (**10**) + external `useAlphaStore.setState` hooks/useBacktest.ts:72 | page(3), SaveAlphaModal(1), AlphaRankingTable(5), SummaryMetrics(1), AlphaDetailView(1), AlphaXmlPanel(2), BacktestMetrics(1), GACombinedLoader(3), GADetailLoader(4), SignalChart(1), SignalPanelSelector(1), useBacktest(1), lib/signalSelection(1) — plus every `selectSignalSource(s)` selector (SignalHeatmap:37, SignalChart:109, SignalDistribution:18-23 ×6, SignalPanelSelector:17) |
| isRunning | setIsRunning:261 (1) | ControlButtons(1), LoadAlphaModal(2), SaveAlphaModal(1), GACombinedLoader(1), SettingsBar(1); page via canShortcutRun |
| resultRevision | setResult:223, beginGA:356, loadSavedResult:368 (3) | GACombinedLoader(1), GADetailLoader(1), SignalChart(1), SignalPanelSelector(1) |
| **signalCacheOrder** | setResult:224, selectSignalAlpha:240, cacheAlphaSignals:255, beginGA:359, loadSavedResult:371 (**5**) | **none** (store-internal only) |
| **selectedSignalAlphaIndex** | setResult:227, selectSignalAlpha:239, enterDetailView:281, beginGA:357, loadSavedResult:369, startGARound:380, appendGACandidate:389, selectResultTab:420 (**8**) | SignalChart(1), SignalPanelSelector(1), lib/signalSelection(1) |
| **selectedSymbol** | setResult:228, selectSignalAlpha:242, cacheAlphaSignals:257(cond), setSelectedCell:267, enterDetailView:282, beginGA:359, loadSavedResult:371, selectResultTab:422 (**8**) | SignalChart(1), SignalHeatmap(1) |
| **selectedTimestamp** | same 8 sites as selectedSymbol (**8**) | SignalHeatmap(1) |
| **viewMode** | setResult:225, setViewMode:271, enterDetailView:281, exitDetailView:285, beginGA:359, loadSavedResult:369, startGARound:380, appendGACandidate:389, finishGA:397(singleAlphaSignals), stopGA:402(singleAlphaSignals), selectResultTab:421 (**11**) | page(1), SignalPanelSelector(1) |
| **selectedAlphaIndex** | setResult:226, enterDetailView:281, exitDetailView:286, beginGA:357, loadSavedResult:369, startGARound:380, appendGACandidate:389, finishGA:397, stopGA:402, selectResultTab:420 (**10**) | AlphaRankingTable(2), AlphaDetailView(1), AlphaXmlPanel(1), BacktestMetrics(1), GADetailLoader(4) |
| **selectedAlphaIndices** | setResult:229, toggleAlphaSelection:298, toggleAllAlphaSelection:307, clearAlphaSelection:309, beginGA:358, loadSavedResult:370 (**6**) | AlphaRankingTable(1), BacktestMetrics(1) |
| **selectedAlphaIndexSet** | same 6 sites (**6**), always `new Set(selectedAlphaIndices)` | AlphaRankingTable(2) |
| isSaveModalOpen | setIsSaveModalOpen:313 (1) | page(1), SaveAlphaModal(1) |
| isLoadModalOpen | setIsLoadModalOpen:315 (1) | page(1), LoadAlphaModal(1) |
| config | fetchConfig:335 (1) | LoadAlphaModal(3), SaveAlphaModal(1), DetailTimeSeries(1), SettingsBar(1), SignalChart(1), SignalDistribution(1), SignalHeatmap(1), useBacktest(6) |
| gaProgress | setGAProgress:344 (1) | ControlButtons(1 truthiness), SettingsBar(1 `.generation`) |
| gaSession | beginGA:353, loadSavedResult:367, clearGA:404 (3) | page(1), AlphaRankingTable(3), GACombinedLoader(1), GADetailLoader(1) |
| **gaFinished** | beginGA:353, loadSavedResult:367, finishGA:394, stopGA:400, clearGA:404 (**5**) | page(1) |
| **gaRounds** | beginGA:353, loadSavedResult:367, startGARound:379, endGARound:392, finishGA:395, stopGA:401, clearGA:404 (**7**) | GACombinedLoader(1), GADetailLoader(1), ResultsTabs(1) |
| **activeResultTab** | beginGA:353, loadSavedResult:367, startGARound:380(cond), finishGA:396(cond), clearGA:404, selectResultTab:420 (**6**) | AlphaRankingTable(5), GACombinedLoader(3), ResultsTabs(1) |
| **gaCombinedKey** | beginGA:354, loadSavedResult:366, startGARound:380(cond), appendGACandidate:388, finishGA:396(cond), clearGA:404, selectResultTab:419 (**7**, 5 via combinedView) | GACombinedLoader(2) |
| **gaCombinedCache** | beginGA:354, loadSavedResult:368, clearGA:404, cacheGACombined:428 (**4**) | GACombinedLoader(1) |
| pinnedSnapshots | addSnapshot:444, removeSnapshot:448, updateSnapshotName:452 (3) | SignalChart(1), SnapshotManager(1) |

Fields written from ≥4 actions (14): result(10+1 external), signalCacheOrder(5), selectedSignalAlphaIndex(8),
selectedSymbol(8), selectedTimestamp(8), viewMode(11), selectedAlphaIndex(10), selectedAlphaIndices(6),
selectedAlphaIndexSet(6), gaFinished(5), gaRounds(7), activeResultTab(6), gaCombinedKey(7), gaCombinedCache(4).
Derived-from-other-state fields kept as state: `selectedAlphaIndexSet` (= `new Set(selectedAlphaIndices)` at every
write), `gaCombinedKey` (= `gaCombinedGroup(result.alphas, activeResultTab).key`, recomputed in GACombinedLoader.tsx:19
and :29 anyway), `result.combined` under GA (= `gaCombinedCache[gaCombinedKey] ?? pending`, combinedView:109-110),
`signalCacheOrder` (no reader).

### 4.2 Actions

| action | lines | fields written | derived computation | non-test callers (file:count) / test refs |
|---|---|---|---|---|
| setXmlCode | 188-189 | xmlCode | identity short-circuit | LoadAlphaModal, XmlEditor / 7 |
| setAlphaName | 193 | alphaName | – | LoadAlphaModal, page / 0 |
| setBacktestError | 197 | backtestError | – | page, LoadAlphaModal, useBacktest(3) / 0 |
| setSettings | 201-208 | settings | enforceSettingsDates(config.simStart/simEnd) | SettingsBar(5), LoadAlphaModal(1) / 4 |
| setResult | 212-232 | result, resultRevision, signalCacheOrder, viewMode, selectedAlphaIndex, selectedSignalAlphaIndex, selectedSymbol, selectedTimestamp, selectedAlphaIndices, selectedAlphaIndexSet | selectSignalCell(selectSignalSource(...)); preserve-detail rule :216-219 | useBacktest(2: `setResult(null)` :43, `setResult(adapted)` :97) / 18 |
| selectSignalAlpha | 236-244 | selectedSignalAlphaIndex, signalCacheOrder, selectedSymbol, selectedTimestamp | selectSignalCell | **none** / 3 |
| cacheAlphaSignals | 245-259 | signalCacheOrder, result (alphas[i] merged; LRU eviction >5 clears 7 signal fields :252), selectedSymbol/Timestamp (cond) | selectSignalCell; revision guard :246 | SignalPanelSelector:47 / 3 |
| setIsRunning | 261 | isRunning | – | useBacktest(3: :41,:122,:135) / 5 |
| setSelectedCell | 266-267 | selectedSymbol, selectedTimestamp | – | useBacktest:44, SignalDistribution:41, SignalHeatmap(3) / 0 |
| setViewMode | 271 | viewMode (refused when alphas>1 and mode≠detail) | – | **none** / 1 |
| enterDetailView | 275-283 | viewMode, selectedAlphaIndex, selectedSignalAlphaIndex, selectedSymbol, selectedTimestamp | selectSignalCell | page:85, AlphaRankingTable(4), BacktestMetrics(2) / 11 |
| exitDetailView | 284-287 | viewMode, selectedAlphaIndex (no-op when alphas>1) | – | page:84, LoadAlphaModal:64 / 3 |
| toggleAlphaSelection | 292-299 | selectedAlphaIndices, selectedAlphaIndexSet | – | AlphaRankingTable:174, BacktestMetrics:54 / 6 |
| toggleAllAlphaSelection | 300-308 | selectedAlphaIndices, selectedAlphaIndexSet | visible-rows filter (same predicate as AlphaRankingTable.tsx:94,96,196) | AlphaRankingTable:123 / 5 |
| clearAlphaSelection | 309 | selectedAlphaIndices, selectedAlphaIndexSet | – | AlphaRankingTable:212 / 2 |
| setIsSaveModalOpen / setIsLoadModalOpen | 313 / 315 | one flag each | – | ControlButtons, modals / 2, 3 |
| fetchConfig | 319-340 | config, settings | network getConfig; resetSplit + enforceSettingsDates; `supportsSimEnd && state.config` rule :328 | page:189 / 7 |
| setGAProgress | 344 | gaProgress | – | useBacktest(5: :48,:75,:83,:123,:136) / 1 |
| beginGA | 352-360 | 15 fields (gaSession, gaFinished, gaRounds, activeResultTab, gaCombinedKey, gaCombinedCache, result, resultRevision, selectedAlphaIndex, selectedSignalAlphaIndex, selectedAlphaIndices, selectedAlphaIndexSet, signalCacheOrder, selectedSymbol, selectedTimestamp, viewMode) | – | useBacktest:56 / 14 |
| loadSavedResult | 361-373 | 14 fields + combinedView | rounds from alphas; combinedView('stats') | LoadAlphaModal:61 / 0 |
| startGARound | 374-382 | gaRounds; if "follow": gaCombinedKey, result, activeResultTab, selectedAlphaIndex, selectedSignalAlphaIndex, viewMode | combinedView(round) | useBacktest:74 / 14 |
| appendGACandidate | 383-391 | result, gaCombinedKey; cond selectedAlphaIndex, selectedSignalAlphaIndex, viewMode | combinedView; dedupe by candidateId | useBacktest:77 / 17 |
| endGARound | 392 | gaRounds | – | useBacktest:79 / 8 |
| finishGA | 393-398 | gaFinished, gaRounds; cond gaCombinedKey, result, activeResultTab; cond viewMode, selectedAlphaIndex | combinedView('stats'), singleAlphaSignals | useBacktest:81,:98 / 4 |
| stopGA | 399-403 | gaFinished, gaRounds; cond viewMode, selectedAlphaIndex | singleAlphaSignals | useBacktest:100,:121,:134 / 6 |
| clearGA | 404 | gaSession, gaFinished, gaRounds, activeResultTab, gaCombinedKey, gaCombinedCache | – | **none / none** |
| selectResultTab | 405-424 | gaCombinedKey, result, activeResultTab, selectedAlphaIndex, selectedSignalAlphaIndex, viewMode, selectedSymbol, selectedTimestamp | combinedView(tab); sorts eligible by IS sharpe then candidateId (:410-414 — same comparator as AlphaRankingTable.tsx:197-201); selectSignalCell | ResultsTabs:21,:27 / 3 |
| cacheGACombined | 425-431 | gaCombinedCache, result.combined (cond key match) | revision guard | GACombinedLoader:31,:33 / 2 |
| cacheGADetail | 432-437 | result.alphas[i] | revision guard | GADetailLoader:35,:37 / 1 |
| addSnapshot / removeSnapshot / updateSnapshotName | 441-455 | pinnedSnapshots (max 8 at :443) | – | SignalChart:300, SnapshotManager:32,:27 / 0 |

Three places compute "visible rows for the active tab" with the same predicate: store :301-302, AlphaRankingTable.tsx:93-94,
:95-97, :196; and `gaCombinedGroup` (lib/gaCombined.ts:5) filters `!error && xml && (tab==='stats' || round===tab)`.

### 4.3 Direct `useAlphaStore.setState` outside the store
- hooks/useBacktest.ts:72 (`search_start` event → patch `result.runSettings`). Only source site.
- Tests: 34 sites across 15 test files (reset with `setState(initial, true)` or partial patches) — listed by
  `grep -rn setState __tests__`.

---

## 5. Effects and requests

### 5.1 useEffect / useCallback bodies that perform (or trigger) a network request

| site | deps | endpoint(s) | abort | guards |
|---|---|---|---|---|
| hooks/useBacktest.ts:33 `run` useCallback | 0 | POST /api/ga/stream (runGAStream) | `abortControllerRef` aborted on re-run (:34-37), on `stop` (:129-131), on unmount (:29) | `requestIdRef` compared in onEvent :68, catch :111, finally :119 |
| hooks/useBacktest.ts:17 useEffect | 0 | – (subscribes xmlCode/settings into refs; cleanup bumps requestId + aborts) | yes | – |
| app/page.tsx:186 useEffect | 0 | GET /api/config via `store.fetchConfig()` | none | module-level `configFetched` boolean :181 |
| app/page.tsx:194 useEffect | 1 [run] | keydown Shift+Enter → `run()` | – | `canShortcutRun(getState())` |
| components/visualization/GADetailLoader.tsx:19 useEffect | **11** | GET /api/alphas/{name}/results/rows/{id} (saved) **or** POST /api/simulate `{metricsOnly:true}` | AbortController, abort in cleanup :39 | `controller.signal.aborted` :29,:37; `resultRevision` in cacheGADetail :433; skip when `evaluating`/`detailLoaded`/`detailError`/`error` :20 |
| components/visualization/GACombinedLoader.tsx:24 useEffect | **9** | GET /api/alphas/{name}/results/batch (saved, Stats only) **or** POST /api/simulate `{xmls, combinedOnly, metricsOnly}` | AbortController held in `work` ref; aborted by the separate effect :23 on `[key, revision]` | `work.current?.key === key` :25; `signal.aborted` :31,:33; revision in cacheGACombined :426; `ready` = round not running :14-15 |
| components/visualization/SignalPanelSelector.tsx:31 useEffect | **16** | POST /api/simulate (full signals, `collectSignals:true` default) | AbortController, cleanup :53 | `active` flag; revision in cacheAlphaSignals :246; skipped when `viewMode==='detail'`/heatmap present/`index<0`/`alpha.error` :34; `retry` counter re-fires |
| components/visualization/SignalChart.tsx:126 useEffect | **7** | POST /api/simulate `{signalSeriesOnly:true, signalPlotSymbols:[symbol]}` | AbortController, cleanup :157 | `active` flag; `seriesCache` Map (≤32) keyed `revision:alphaIndex:symbol`, cleared by effect :120 on `[revision]`; only when `heatmap.sampled && rowStride>1` :122 |
| components/modals/LoadAlphaModal.tsx:19 useEffect | 1 | GET /api/alphas (getAlphaList via `fetchAlphas`) | none | none (sets state after await; no mounted check) |
| components/modals/LoadAlphaModal.tsx:38 `handleLoad` (event handler, not effect) | – | GET /api/alphas/{name}/results | none | `isRunning` checked before and after await :40,:50 |
| components/modals/SaveAlphaModal.tsx:60 `handleSave` (event handler) | – | POST /api/alphas | none | – |
| store/alphaStore.ts:319 `fetchConfig` | – | GET /api/config | none | try/catch swallow :337 |

Other useEffect sites (no network): XmlEditor:21, MainLayout:32, ThemeProvider:7, ResultsTabs:13, SignalChart:62
(table scroll), :120 (cache clear), SignalHeatmap:43 (worker normalize, cancel on cleanup), :97, :218, :223, :228,
uplotAdapters:11,:37,:50,:76 (two without dep arrays, by design per comment :10/:45).

### 5.2 Call sites of each lib/api.ts export (outside lib/api.ts; tests in parentheses)

| export | source call sites | tests |
|---|---|---|
| getConfig | store/alphaStore.ts:321 | alphaStore, simPeriod, GACombinedLoader (mocked) |
| runSimulate | GADetailLoader.tsx:25, SignalPanelSelector.tsx:42, SignalChart.tsx:143; internally runCombinedAlphas :284 | apiClient.contract, GADetailLoader, signalSelection (mocked) |
| runCombinedAlphas | GACombinedLoader.tsx:30 | GACombinedLoader (mocked) |
| runGAStream | hooks/useBacktest.ts:59 | apiClient.contract, simPeriod, useBacktest.pipeline (mocked) |
| adaptSimulateResponse | GADetailLoader.tsx:30; internally :220,:287,:398 | adaptSimulateResponse, logDistribution, pipelineFixtures |
| adaptGACompleteToResponse | hooks/useBacktest.ts:91 | simPeriod (mocked), pipelineFixtures |
| adaptGACandidate | hooks/useBacktest.ts:77, LoadAlphaModal.tsx:59 | useBacktest.pipeline (mocked) |
| adaptSignalPayload | SignalPanelSelector.tsx:45; internally :164,:189 | – |
| saveAlpha, AlphaExistsError | SaveAlphaModal.tsx:75, :80 | SaveAlphaModal.roundtrip (fetch mocked) |
| getAlphaList | LoadAlphaModal.tsx:30 | SaveAlphaModal.roundtrip |
| getSavedResults | LoadAlphaModal.tsx:48 | SaveAlphaModal.roundtrip |
| getSavedRow | GADetailLoader.tsx:24 | SaveAlphaModal.roundtrip |
| getSavedBatch | GACombinedLoader.tsx:30 | SaveAlphaModal.roundtrip |
| deleteAlpha | **none** | none |

`/api/simulate` is therefore reached from 5 shapes: full signals (SignalPanelSelector), metrics-only detail
(GADetailLoader), combined-only (runCombinedAlphas), signal-series-only (SignalChart), and saved-row/batch replays
(GET, same response shape). All 4 POST shapes send `collectSignals: true, collectSignalsTopK: 5` from the single
`runSimulate` body (lib/api.ts:261).

---

## 6. API contract cross-check (frontend lib/api.ts ⇄ cpp-engine/src/main.cpp + api/json_serializer.cpp)

### 6.1 Request fields

| endpoint | frontend sends (lib/api.ts line) | engine reads (main.cpp line) |
|---|---|---|
| GET /api/config | – (:244) | – |
| POST /api/simulate (:258-263) | xml, simStart, isStart, osStart, threads, collectSignals:true, collectSignalsTopK:5, + optional simEnd, signalPlotSymbols, universe, executionPrice, metricsOnly, signalSeriesOnly, xmls, combinedOnly | parse_run_options :203-292: executionPrice, isStart, osStart, simStart, simEnd, threads, collectSignals, metricsOnly, signalSeriesOnly, collectSignalsTopK, signalPlotMaxCells, signalPlotSymbols, combinedMode, missingClose, universe; simulate_response :432-449: combinedOnly, xmls, xml. Rejects defaultDelistingReturn/staleQuoteSettleBars/settlementLog/distributionLog :278-281 |
| POST /api/ga/stream (:306-311) | xml, simStart, isStart, osStart, firstSearch, stopRound, stopAlphas, roundSize, threads, streamCandidates:true, universe, executionPrice, simEnd | :1045 xml; parse_run_options (same 15 keys); :1049 firstSearch (alias popSize), :1050 roundSize, :1051 stopRound (alias generations), :1052 stopAlphas, :1059 streamCandidates |
| POST /api/alphas (:362-371) | name, xml, level, metadata{simStart,simEnd,isStart,osStart,universe,executionPrice}, rows[{id,round,xml}] | save_alpha :687-742: name, xml, level, metadata.(6 keys via kAlphaMetadataKeys :642), rows[].id/round/xml; metadata is re-parsed through parse_run_options :714 |
| GET /api/alphas, GET …/results, …/results/batch, …/results/rows/{id}, DELETE …/{name} | path only | path only (:1224, :1257, :1265, :1273-1278, :1238) |

**Sent but never read by the engine: none.** Every key the frontend sends is read. Two data points on the sent set:
`collectSignals`/`collectSignalsTopK` are sent on every simulate call but only matter when `!metricsOnly && !combinedOnly`
(main.cpp:559, :580); `threads` is parsed with fallback 0 → hardware (:246,:475,:1053).

Engine request keys the frontend never sends: `signalPlotMaxCells` (:251), `combinedMode` (:269), `missingClose` (:275),
`popSize`/`generations` aliases (:1049,:1051 — sent only by `scripts/capture_engine_fixtures.mjs:45` and the Sep-11 fixtures),
`streamCandidates:false` path (:1154-1163 vs :1164-1191). Endpoint never called by the frontend: GET /api/health (:922),
DELETE /api/alphas/{name} (`deleteAlpha` has no caller).

### 6.2 Response fields

Engine writes (per endpoint) and whether the frontend reads them. "typed" = declared in types/index.ts; "read" = a
non-type source file dereferences it (grep results recorded in the session; see §0 scripts).

**GET /api/config** (main.cpp:938-998): symbols, symbolLabels, simStart, simEnd, supportsSimEnd(always true :970), feeRate,
barsPerYear, combinedMode, executionPrice, executionPrices (always ≥['close'] :975-978), missingClosePolicy, thinLayout,
alphasDir, universes, defaultUniverse, T, N, columns, loadedFields, loadedDataBytes.
- read: symbolLabels, simStart, simEnd, supportsSimEnd, feeRate, executionPrice, executionPrices, alphasDir, universes,
  defaultUniverse, N (SettingsBar:119).
- typed, never read: `symbols` (required in StarConfigResponse:106), barsPerYear, T, columns, loadedFields, loadedDataBytes.
- not typed at all: combinedMode, missingClosePolicy, thinLayout.

**POST /api/simulate** (simulate_response :547-593; per batch metrics_payload :365-374 + diagnostics :359-363 + index/xml;
combined :572-589): executionPrice, simStart, simEnd, batches[], combined{…}, combinedMode, universe, and (single or
combined) top-level yearly/timeseries/isStartIndex/osStartIndex/missingData (copy_top_metrics :376-379) + signalPlot/
signalRange/distribution/signalDistributionLog.
- summary (json_serializer.cpp:16-37): return, sharpe, mdd, turnover, maxWeight, minBet, pnl, winRate, margin, longRatio,
  shortRatio, nonzeroPosition, avgMaxBet, minBetMin, quality, sharpe400, sharpe80, minsoc. Adapter adaptStarSummary
  (api.ts:47-65) maps 15 of 18 (drops **winRate**; longRatio/shortRatio copied onto the alpha at :176-177,:201-202).
  Displayed: AlphaRankingTable reads avgPositionCount, avgMaxBet, max_weight, minPositionCount, quality, ann_ret/pnl, sharpe,
  sharpe400, turnover (:75-83); BacktestMetrics (unmounted) reads margin/activeBarCount. **Adapted but never displayed:**
  `mdd`, `sharpe80`, `minsoc`, `longRatio`, `shortRatio`; `margin`/`activeBarCount` only in the unmounted BacktestMetrics.
- yearly (serializer :39-58): year, longSM, shortSM, pnl, annRet, sharpe, ir, maxDD, winLose, dailyTvr, margin, fitness —
  all 12 read by DetailSummaryTable.tsx:41-52. (Top-level `BacktestRunResponse.yearly` set at api.ts:146,:210 is never read: `result?.yearly` has 0 hits.)
- timeseries (serializer :60-104): timestamps, cumPnl, cumPnlGross, turnover, maxWeight, nonzeroCount, drawdown, frozenGross.
  Adapter uses 6 (:112-136); **drawdown** typed (StarTimeseries:39) but never read (frontend recomputes drawdown from gross
  equity in DetailTimeSeries.tsx:111-126); **frozenGross** not typed.
- signalPlot (serializer :181-221): timestamps, symbols, values, membership, originalRows, originalSymbols, rowStride,
  symbolStride, sampled. Read: timestamps, symbols, values, membership, rowStride (SignalChart:122), sampled (:122).
  Typed, never read: originalRows, originalSymbols, symbolStride.
- distribution (serializer :248-331): bins, counts, min, max, rangeLo, rangeHi, exampleSymbol, exampleDate. All read via
  adaptSignalPayload (api.ts:67-81) except nothing dropped; `rangeLo ?? min` fallback at :75-76.
- signalDistributionLog (serializer :344-457): scale, linthresh, min, max, rangeLo, rangeHi, totalCount, edges, bins,
  counts, exampleSymbol, exampleDate, exampleValue, linear{same}. Read by SignalDistribution: counts, bins, min/max,
  edges (:75), linear (:26), exampleSymbol/exampleDate. **Typed, never read:** scale, linthresh, totalCount, exampleValue,
  rangeLo/rangeHi of the log object.
- missingData (main.cpp:331-357, 10 keys) — **not typed, never read**. `combinedMode`, top-level `universe` — not typed.
- isStartIndex/osStartIndex — read by adapter (:159-160,:184-185).

**POST /api/ga/stream** events (main.cpp:1088-1191):
- search_start {simStart, simEnd, executionPrice, universe, type}: all 4 read (useBacktest:72).
- round_start {round, size}: both read (:74-75).
- candidate {index, xml, round, origin, status, error?, summary, yearly, missingData}: adaptGACandidate (api.ts:223-237)
  reads index, round, xml, error, summary, yearly; **origin never read; status read only in LoadAlphaModal.tsx:59;
  missingData untyped/unread**.
- round_end {round, bestSharpe, generation, totalGenerations, bestXml} (engine writes the generation_end keys too, :1144-1150):
  only `round` read (:79); `bestSharpe` typed, unread.
- search_complete {bestXml, stopReason, evaluated, + metadata}: handler calls `finishGA()` only (:80-81); **no field read**.
- generation_end / complete: produced only when `streamCandidates` is false (:1114,:1122,:1144 vs :1154-1163). The frontend
  always sends `streamCandidates:true` (api.ts:309). Frontend handlers for both exist (useBacktest:82-98) and the
  `complete` payload fields (bestXml, simStart, simEnd, executionPrice, batches, yearly, timeseries, …) are read there;
  `combinedMode`, `universe` on complete are not typed.
- error {error}: read (:99-101).

**POST /api/alphas** response {name, saved, level, path} (:840-845): SaveAlphaModal.tsx:75 discards the result; `saved`
untyped. **GET /api/alphas** [{name, xml, level, metadata}]: all read. **GET …/results** {rows[], hasBatch}: `hasBatch`
typed (SavedAlphaResults:330), never read (GACombinedLoader decides from `savedSource && key===stats key` :29); rows
read. **DELETE** {name, deleted}: no caller.

### 6.3 Summary lists
- Sent but never read by engine: **none**.
- Engine response fields never read by the frontend (typed or not): config.{symbols, barsPerYear, T, columns,
  loadedFields, loadedDataBytes, combinedMode, missingClosePolicy, thinLayout}; simulate.{combinedMode, universe,
  missingData.*, timeseries.drawdown, timeseries.frozenGross, summary.winRate, signalPlot.{originalRows, originalSymbols,
  symbolStride}, signalDistributionLog.{scale, linthresh, totalCount, exampleValue, rangeLo, rangeHi}}; adapted-but-
  undisplayed summary.{mdd, sharpe80, minsoc, longRatio, shortRatio} (+margin/nonzeroPosition only in unmounted
  BacktestMetrics); ga.{candidate.origin, candidate.missingData, round_end.bestSharpe/generation/totalGenerations/bestXml,
  search_complete.{bestXml, stopReason, evaluated}, complete.{combinedMode, universe}}; alphas.{saved, path, hasBatch,
  deleted}.

---

## 7. Compat / legacy inventory (data only; comment text quoted where the code gives a reason)

| # | file:line | what | reason given in code |
|---|---|---|---|
| 1 | types/index.ts:1, :170 | header comments "backward-compatible frontend types" / "(backward-compatible with existing components)" | – |
| 2 | types/index.ts:14,21,24 + json_serializer.cpp:23,:31 | `minBet`, `nonzeroPosition`, `minBetMin` wire keys mapped to avgPositionCount/activeBarCount/minPositionCount (api.ts:53,:57,:59) | "Legacy wire key" |
| 3 | lib/api.ts:43-44 | "C++ STAR engine now returns display-ready values… Field mapping only." | – |
| 4 | lib/api.ts:157-160, :165-168, :175, :183-185, :190-193, :200 | per-batch field `?? resp.<top-level>` fallbacks (timeseries, isStartIndex, osStartIndex, signalPlot, signalRange, distribution, signalDistributionLog, yearly) | none in code; test name `adaptSimulateResponse.test.ts:143 'mixes per-batch and top-level fallback detail fields correctly'`. Engine always writes per-batch timeseries/isStartIndex/osStartIndex/yearly (metrics_payload :554) |
| 5 | lib/api.ts:211-215 | top-level heatmap/signalRange/distribution/signalDistributionLog duplicated onto BacktestRunResponse | "Reuse the authoritative single/combined payload; JSON contains duplicate wire objects." Consumed by lib/signalSelection.ts:7,:9 (`result.combined?.heatmap ? result.combined : result`) |
| 6 | lib/api.ts:75-76 | `distribution.rangeLo ?? distribution.min` | – (engine always writes rangeLo/rangeHi, serializer :279-280) |
| 7 | lib/api.ts:72, :193, :215; SignalDistribution.tsx:25,:56 | `signalDistributionLog ?? null`; Log button disabled when absent, title "Run again with the updated engine to load log bins" | test `logDistribution.test.tsx:34 'does not present an older linear-only response as logarithmic'` |
| 8 | lib/api.ts:119-120; DetailTimeSeries.tsx:58, :83-90 | `hasGross` check on `cumPnlGross`; fallback gross = net + turnover×fee when absent | "Engine gross (pre-fee) returns; preferred over net + turnover * fee." Engine always writes cumPnlGross (serializer :73-76) |
| 9 | DetailTimeSeries.tsx:371 | `s.config?.feeRate ?? 0.001` | – (engine default fee is 1.5bp per CLAUDE.md; 0.001 = 10bp) |
| 10 | types/index.ts:113; store/alphaStore.ts:328; SettingsBar.tsx:32-33; useBacktest.ts:92,:107 | `supportsSimEnd?` optional + branches disabling "To" date | tests `simPeriod.test.tsx:27 'disables To for older servers'`. Engine always writes `supportsSimEnd: true` (main.cpp:970) |
| 11 | types/index.ts:112-114; SettingsBar.tsx:94-98; SaveAlphaModal.tsx:42,:48,:56; useBacktest.ts:47; store:324 | `executionPrices?.length ? settings.executionPrice : config.executionPrice ?? 'close'` | test `SettingsBar.universe.test.tsx:35 'disables unsupported older-server selection'`; CLAUDE.md "Older APIs show their fixed execution value with selection disabled". Engine always writes executionPrices (:975) |
| 12 | types/index.ts:125-131 StarGAGenerationEvent, :133-144 StarGACompleteEvent; useBacktest.ts:82-98; api.ts:219-221 adaptGACompleteToResponse; api.ts:328 | handlers for `generation_end` and `complete` events | useBacktest.ts:93 "Compatible complete events already contain daily performance." Engine emits these only with `streamCandidates:false` (main.cpp:1144,:1154-1191); frontend always sends true (api.ts:309) |
| 13 | tests/helpers/mockRun.ts:4-16 | converts a simulate JSON fixture into a one-line `complete` NDJSON for `**/api/ga/stream` | "Existing chart fixtures exercise the compatible complete event over the GA transport." Used by 7/11 e2e specs (alpha_selection, chart_interaction, detail_layout, execution_price, metrics_pipeline, signal_panels, sim_period) |
| 14 | tests/e2e/single_alpha_view.spec.ts:19 | comment | "The real engine streams candidates; the compatible complete event used by chart fixtures never reaches this path." |
| 15 | __tests__/pipelineFixtures.ts:19-22, cppFixtures.contract.test.ts, simPeriod.test.tsx:42, useBacktest.pipeline.test.tsx:31,:79, apiClient.contract.test.ts:128-143 | tests built on the `complete` event | fixture `cpp_ga_events.ndjson` has only generation_end+complete (captured with `popSize/generations`, scripts/capture_engine_fixtures.mjs:45) |
| 16 | __tests__/fixtures/*.json, capture.json:settings | `mode: 1` present in fixtures/settings; tests assert absence: apiClient.contract.test.ts:53, execution_price.spec.ts:6,:12, live_engine.spec.ts:67 (`not.toHaveProperty('mode')`) | test title "sent on each run without an interval mode" |
| 17 | scripts/screenshot_detail.mjs:11-21, screenshot_detail.cjs:17 | POST `/api/backtest/run` with `settings.barInterval:'D1'`, `isEnd/osEnd` | endpoint does not exist in main.cpp; knip "unused files" |
| 18 | scripts/capture_engine_fixtures.mjs:16,:45 | sends `mode: 1`, `popSize`, `generations` | engine aliases still read (main.cpp:1049,:1051); `mode` ignored |
| 19 | lib/colorScale.ts:35-42 `signalToColor` | – | "Legacy raw-value mapping kept for callers outside the heatmap" — only caller is `signalColors.test.ts` |
| 20 | store/alphaStore.ts:24 comment | "Backtest Result (single endpoint: /api/backtest/run)" | stale endpoint name (route is /api/simulate + /api/ga/stream) |
| 21 | store/alphaStore.ts:317 comment | "Config (replaces separate dataRange + symbols)" | – |
| 22 | GADetailLoader.tsx:7-8, :32-35 `KEEP` set | strips name/rank/xml/signal fields from a metrics-only body before merging | "a metrics-only body carries null signal fields that must not erase signals already loaded for this alpha" |
| 23 | SignalPanelSelector.tsx:45; SignalChart.tsx:146 | `batch?.signalPlot ? batch : response`, `batches[0]?.signalPlot ?? response.signalPlot` | – (for a single alpha the engine writes signals at the top level only, main.cpp:567-570; per-batch signals only for multi-alpha batches, :559-563; so both call sites take the `response` branch for the single-alpha requests they make) |
| 24 | SaveAlphaModal.tsx:41-58 `metadataFor` | `settings.simEnd || config?.simEnd || ''`, `universe || defaultUniverse || ''`, `run.executionPrice ?? fallbackPrice` | – |
| 25 | LoadAlphaModal.tsx:52-53, :66-67 | saved universe not in `config.universes` → `defaultUniverse ?? ''` + warning text | – |
| 26 | useBacktest.ts:92 | `simEnd: event.simEnd ?? (supportsSimEnd ? settings.simEnd : config.simEnd) ?? ''` | – |
| 27 | SettingsBar.tsx:98 | `(config?.executionPrices ?? [config?.executionPrice ?? 'close'])` | – |
| 28 | AlphaRankingTable.tsx:80, BacktestMetrics.tsx:66,:75, SummaryMetrics.tsx:44,:55 | `ann_ret ?? pnl` | – (adapter always sets both :50,:55) |
| 29 | store/alphaStore.ts:252 | eviction resets 7 signal fields to `undefined` individually | – |
| 30 | `??` sites total: 131 lines in source (grep `\?\?`); of these 42 are on API/config fields (api.ts 27; SettingsBar 6; useBacktest 7; store 4; SaveAlphaModal 3; LoadAlphaModal 2; SignalChart 3; SignalPanelSelector 4; DetailTimeSeries 1; SignalDistribution 5; ranking/metrics tables 5) | – |
| 31 | json_serializer.cpp:455 (engine side, for context) | nested `linear` histogram | "keep the historical quantile-clipped linear API intact for older clients" |

Optional response fields with a frontend default (types/index.ts): StarConfigResponse `symbolLabels?`, `executionPrice?`,
`supportsSimEnd?`, `executionPrices?`, `universes?`, `defaultUniverse?`, `alphasDir?`, `loadedFields?`, `loadedDataBytes?`
(engine writes all but symbolLabels unconditionally, main.cpp:944-996); StarBatchEntry `xml?`, `yearly?`, `timeseries?`,
`isStartIndex?`, `osStartIndex?` (engine always writes, :554-558); StarBatchResponse `simStart?`, `simEnd?`,
`executionPrice?`, `combined?` (combined absent for single, :567); StarGACompleteEvent `simStart?`, `simEnd?`,
`executionPrice?` (engine always writes, :1181-1183); StarDistribution `rangeLo?`, `rangeHi?`, `exampleSymbol?`,
`exampleDate?`, `edges?`, `linear?` (always written); StarSignalPayload all four `?` (absent when metricsOnly/combinedOnly).

---

## 8. Size / complexity

### 8.1 Files > 200 lines (source)
DetailTimeSeries.tsx 510, alphaStore.ts 456, api.ts 405, SignalHeatmap.tsx 393, types/index.ts 352, SignalChart.tsx 340,
AlphaRankingTable.tsx 230, app/page.tsx 221. Tests > 200: adaptSimulateResponse.test.ts 297, SaveAlphaModal.roundtrip.test.tsx 208.

### 8.2 Functions / components > 80 lines (brace-matched; React.memo wrappers corrected by hand)
| file:lines | name | lines |
|---|---|---|
| SignalHeatmap.tsx:26-391 | SignalHeatmap | 366 |
| SignalChart.tsx:107-340 | SignalChart | 234 |
| SaveAlphaModal.tsx:14-177 | SaveAlphaModal | 164 |
| LoadAlphaModal.tsx:10-164 | LoadAlphaModal | 155 |
| DetailTimeSeries.tsx:363-510 | DetailTimeSeries | 148 |
| lib/chartInteraction.ts:16-161 | chartInteraction (plugin factory; `ready` hook closure :21-156 = 136) | 146 |
| hooks/useBacktest.ts:8-140 | useBacktest (`run` callback :33-125 = 93, onEvent closure :67-103 = 37) | 133 |
| DetailTimeSeries.tsx:160-277 | PnlDualChart | 118 |
| SettingsBar.tsx:10-125 | SettingsBar | 116 |
| MainLayout.tsx:14-115 | MainLayout | 102 |
| SignalDistribution.tsx:17-110 | SignalDistribution | 94 |
| ControlButtons.tsx:12-95 | ControlButtons | 84 |
| DetailTimeSeries.tsx:279-361 | BigChart | 83 |
| store/alphaStore.ts:185-456 | `create<AlphaState>(...)` single object literal with 33 inline actions | 272 |
Just under: lib/api.ts adaptSimulateResponse :141-217 (77), SignalDataTable :40-105 (66).

### 8.3 Components with ≥6 `useAlphaStore(` subscriptions (per component; counts from source)
| component | subs | lines of the selectors |
|---|---|---|
| SignalChart | 9 | 109,110,111,114,115,116,117,159(useShallow),160 (+useThemeStore :161) |
| GACombinedLoader | 9 | 9-18 (two selectors derive from `gaCombinedCache`/`gaRounds`) |
| SignalDistribution | 8 | 18-23 (six `selectSignalSource(s)?.x` selectors), 34, 35 |
| SaveAlphaModal | 7 | 15-21 |
| GADetailLoader | 7 | 11-18 |
| SignalPanelSelector | 6 | 9,13,14,15,16,17 |
Below threshold: SignalHeatmap 5 (+useThemeStore), SettingsBar 5, AlphaRankingTable 5 (+BatchRow 4, +RankRow 2), ResultRow 4 (BacktestMetrics), page.tsx 7 across 4 components, AlphaStorageButtons 3.
Components subscribing to the whole `result` object (`s => s.result`): SaveAlphaModal:17, SummaryMetrics:24, AlphaDetailView:17,
SignalChart:114, SignalPanelSelector:9; whole `settings`: SaveAlphaModal:19, SettingsBar:11, SignalChart:117, SignalPanelSelector:16;
whole `config`: SaveAlphaModal:20, SettingsBar:13.

### 8.4 Ternary chains with ≥3 nested levels
| file:lines | depth | context |
|---|---|---|
| AlphaXmlPanel.tsx:9-17 | 4 | token colour class |
| SaveAlphaModal.tsx:34-38 | 4 | `resultsBlocked` message |
| ResultsTabs.tsx:24 | 4 | keyboard navigation index |
| DetailTimeSeries.tsx:84-90 | 3 (+1 inner) | gross return fallback |
| app/page.tsx:101-102 | 3 | keyboard navigation index |
| store/alphaStore.ts:217-219 | 3 (+1 inner) | `selectedAlphaIndex` in setResult |
| store/alphaStore.ts:220, :364, :416-417, :422 | 2 + `??` | signal index selection |
| app/page.tsx:131-143 (JSX) | 2 outer + 1 inner | ChartArea branches |
| LoadAlphaModal.tsx:104-116 (JSX) | 2 | loading / empty / list |
2-level chains elsewhere: AlphaRankingTable.tsx:25-29,:131,:200; SettingsBar.tsx:85,:100; lib/uplotUtils.ts:12; lib/signalSelection.ts:9; SignalChart.tsx:36; store:413.

### 8.5 useEffect with ≥8 deps
- SignalPanelSelector.tsx:31 — **16** deps [hasResult, canShowSignals, revision, index, xml, heatmap, simStart, simEnd, isStart, osStart, threads, universe, executionPrice, viewMode, retry, alpha?.error].
- GADetailLoader.tsx:19 — **11** deps [session, evaluating, index, alpha?.xml, alpha?.error, alpha?.detailLoaded, alpha?.detailError, alpha?.candidateId, settings, revision, savedSource].
- GACombinedLoader.tsx:24 — **9** deps [session, key, ready, cached, settings, revision, group, savedSource, alphas].
(SignalChart.tsx:126 has 7; next largest is 4.)

---

## 9. Hotspot table (data only)

file | lines | importers nt/t | exports with 0 non-test importers | store writes (action/setState call sites) | useEffect count | test files importing
---|---|---|---|---|---|---
store/alphaStore.ts | 456 | 21/16 | 2 (MAX_CACHED_SIGNAL_ALPHAS, enforceSettingsDates) + actions clearGA/selectSignalAlpha/setViewMode uncalled | 33 actions defined; fetchConfig writes 2 fields | 0 | 16
lib/api.ts | 405 | 8/10 | 1 (deleteAlpha) | 0 | 0 | 10
types/index.ts | 352 | 12/10 | 9 types (own-file use only) | – | – | 10
components/visualization/DetailTimeSeries.tsx | 510 | 1/1 | 4 (PnlSeriesPoint, seriesToChartData, buildPnlBundleFromRet, buildMddDataFromEquity) | 0 | 0 (useMemo ×14) | 1
components/visualization/SignalHeatmap.tsx | 393 | 1(dyn)/0 | 0 | 3 (setSelectedCell) | 5 | 0
components/visualization/SignalChart.tsx | 340 | 1(dyn)/0 | 0 | 1 (addSnapshot) + 1 network effect | 3 | 0
hooks/useBacktest.ts | 140 | 1/2 | 0 | 24 (setIsRunning 3, setBacktestError 3, setResult 2, setSelectedCell 1, setGAProgress 5, beginGA 1, startGARound 1, appendGACandidate 1, endGARound 1, finishGA 2, stopGA 3, direct setState 1) | 1 (+2 useCallback with network) | 2
components/multi-alpha/AlphaRankingTable.tsx | 230 | 1(dyn)/3 | 0 | 7 | 0 | 3
app/page.tsx | 221 | 0/0 | 0 | 5 | 2 | 0 (e2e only)
components/modals/LoadAlphaModal.tsx | 164 | 1(dyn)/1 | 0 | 8 | 1 (network, no abort) | 1
components/modals/SaveAlphaModal.tsx | 177 | 1(dyn)/1 | 0 | 2 | 0 (handler network) | 1
components/visualization/SettingsBar.tsx | 125 | 1/3 | 0 | 5 | 0 | 3
components/visualization/SignalPanelSelector.tsx | 65 | 1/1 | 0 | 1 | 1 (16 deps, network) | 1
components/visualization/GADetailLoader.tsx | 42 | 1/2 | 0 | 2 | 1 (11 deps, network) | 2
components/visualization/GACombinedLoader.tsx | 38 | 1/2 | 0 | 2 | 2 (9 deps, network) | 2
components/visualization/SignalDistribution.tsx | 110 | 1(dyn)/1 | 0 | 1 | 0 | 1
components/visualization/BacktestMetrics.tsx | 131 | **0**/1 | 1 (default) | 3 | 0 | 1
components/multi-alpha/SummaryMetrics.tsx | 62 | **0**/1 | 1 (default) | 0 | 0 | 1
lib/chartInteraction.ts | 161 | 2/1 | 0 (clampRange/wheelPixels own-file+test) | 0 | 0 | 1
lib/uplotUtils.ts | 46 | 3/2 | 2 (getDPR, TOOLTIP_STYLE) + 1 test-only (toIsOsAlignedData) | 0 | 0 | 2
lib/colorScale.ts | 44 | 1/1 | 1 test-only (signalToColor) + Z_CLIP own-file | 0 | 0 | 1
lib/uplotAdapters.ts | 83 | 2/1 | 0 | 0 | 4 | 1
components/editor/ControlButtons.tsx | 135 | 1/2 | 0 | 2 | 0 | 2
components/layout/MainLayout.tsx | 115 | 1/0 | 0 | 0 | 1 | 0
components/visualization/AlphaDetailView.tsx | 76 | 1(dyn)/0 | 0 | 0 | 0 | 0
components/visualization/ResultsTabs.tsx | 36 | 1/1 | 0 | 2 | 1 | 1
remaining 20 files (≤65 lines each) | – | see §1.1 | see §3.1 | ≤1 | ≤1 | see §1.2

---

## Unverified / not done
- Playwright e2e specs were not executed (would start a Next dev server on a port; brief forbids 50006/50007 and the
  suite needs Chromium). Their coverage of SignalChart/SignalHeatmap/AlphaDetailView/SnapshotManager/page.tsx is
  inferred from `page.route`/`getByTestId` usage, not measured.
- No line coverage numbers (coverage provider not installed; §0 #4).
- Function-length figures are brace-matched approximations (comments/strings stripped), hand-corrected for the
  `React.memo(function …)` wrappers that the script mis-spanned (RankRow, SignalDataTable, PnlDualChart, BigChart).
