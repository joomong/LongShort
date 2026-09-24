# Repository working guide

## Current implementation

- `cpp-engine/`: C++17 engine, XML compiler, metrics, GA and Drogon HTTP server.
- `frontend/`: Next.js 14 / React / Zustand / uPlot; TypeScript and Vitest/Playwright.
- `modules/`: per-config user modules. `tools/run_engine.py` discovers and incrementally builds them before starting the server.
- Python tools generate module/catalog metadata and run independent validation. Production field values are read by C++ Arrow/Parquet.

See [README.md](README.md), [current logic/data report](docs/logic_completion_2026-09-11.md), [module SDK](modules/README.md) and [data configuration examples](cpp-engine/examples/datasets/README.md).

## Working conventions

Make reviewable changes, preserve user work and run checks appropriate to the changed behavior. Read the implementation before relying on historical reviews. Reports dated 2026-09-08 describe an earlier state; later reports explain which findings were fixed or reclassified.

Preserve existing running servers. When isolated integration tests need a server, use a separate port and stop only processes started for that test. Do not modify shared source data to make tests pass. Synthetic fixtures belong in a temporary directory or the tracked test fixture directory.

The user may explicitly authorize parallel developer work; coordinate file ownership and perform independent reviews in that case. Follow the user's requested scope and existing authorization.

## Engine and data contracts

- Close-to-close holding PnL, one-way turnover costs, fixed-book additive cumulative PnL/drawdown. D0=0 and D1=1 are both implemented; default D1 and default fee 5bp remain.
- Data delay in XML and execution mode are independent. New editor XML defaults to delay=0. Preserve explicit saved XML delays.
- `avgBet` means average held-symbol count; `minBet` means minimum held-symbol count. Legacy JSON keys differ from internal names; see `json_serializer.cpp`.
- Prices remain intact outside the universe. Rank/Zscore and target normalization use the signal-date mask. Under D1 the masked targets execute on the next bar.
- Thin-v1 (`base.cache/manifest.json` layout): raw `base_data.parquet` supplies prices; cached `returns` is the split-adjusted signal return and `returns_total` is the sole holding P&L input. `adj_factor` supplies the split share basis; thin `events.parquet` only retires sources and adds destination positions at builder-valued ratios. `universe.name` selects a named interval membership set, and event destinations are loaded transitively. The loader rejects top-level `events:` and `backtest.corporate_actions:` with thin-v1. The rc2f3 example is a transitional validation cache with unresolved/default settlement and pending-child cases in its manifest; it is not a cleaned production release. See `cpp-engine/examples/datasets/README.md` and `docs/backtest_cache_simplification_plan_2026-09-23.md`.
- Generic nonthin long/wide datasets and nonthin wide `base.cache` remain loadable. For these inputs, XML `returns` is derived from close; the thin `returns_total` and position moves are absent. `backtest.missing_close_policy` defaults to `carry` and freezes held positions without a quote, then recognises the accumulated close move on resumption; `error` fails fast. The old `events:`, `backtest.corporate_actions:`, `default_delisting_return`, and `stale_quote_settle_bars` settings are rejected by the current config loader. `docs/corporate_actions_engine_2026-09-17.md` records the pre-removal accounting contract; `build_events.py` remains a cache_v21 builder dependency.
- Costs (`backtest.costs.borrow`, off by default): short financing Σ|w_short|·rate/day_count·calendar days from an interval table or `default_rate`, taken from net P&L only (gross unchanged); with neither table nor default the run reports `costs.borrowApplied=false`. No broker figure is a default.
- `derived/returns_total.parquet` in cache_v2.1 is a builder intermediate for the old ledger path; thin-v1 publishes a separate `returns_total.parquet` at the cache root. Keep their contracts distinct.
- `sim_start_index` separates module/target warmup from trading. No historical holdings or fees arise in warmup; initial execution fees belong to the trading period.
- `combined_mode=normalized` uses normalized equal-alpha contributions and final portfolio normalization. `raw` preserves the original signal-scale weighting. Both accumulate in a deterministic alpha order.
- C++ data loading aligns stable symbol IDs and timestamps. Wide fields can be lazy; aliases share one buffer. `returns` is close-derived for nonthin layouts and a cached field for thin-v1. AFL catalog names are metadata, not a request to load every factor.
- `base: {cache, groups}` registers default price and group fields. A nonthin cache expands into eager wide `BaseData/{open,high,low,close,volume,VWAP,marketCap_USD}.parquet`; thin-v1 reads long `base_data.parquet` and cached `returns.parquet`. Both use `base_groups` (`sector`, `industry`) with as-of alignment. A missing base file or group panel with no finite code on a priced bar fails startup. Rebuild the group panels for the chosen cache when its dates or IDs change.
- `<neutralize group="sector|industry"/>` demeans the signal within the group read from the `sector`/`industry` field on the signal date: the signal-date universe mask is applied first (like rank/zscore), only finite values with a finite code >= 0 enter the group mean, everything else becomes NaN, a singleton group yields 0, and there is no scaling or minimum size — identical to the team `cs_group_neut_1d`. Unknown or unregistered groups are compile errors; `neutralize` is a reserved tag for discovered modules.
- `alignment=asof` uses the most recent row at or before each target timestamp, retaining nulls in that row. Timestamps must represent data availability. The user chose restart-based data refresh without version tracking or automatic reload. `end_date: latest` (also the YAML default) trims only the all-NaN close tail over selected symbols; explicit dates retain their requested range.

## Commands and validation

Launch with `python3 cpp-engine/tools/run_engine.py --config /path/to/config.yaml`. Dependency prefix/PyArrow options are in [the build guide](docs/module_discovery_build_2026-09-09.md). CMake builds should use a separate build directory.

Full C++ checks: build with `LONGSHORT_BUILD_TESTS=ON`, then run CTest. To run HTTP tests without skipping, generate a fixture using `cpp-engine/tools/make_http_test_fixture.py` and provide `LONGSHORT_TEST_SERVER` and `LONGSHORT_TEST_CONFIG`. Exact commands and latest counts are in [the data/build report](docs/logic_completion_data_2026-09-11.md).

`cpp-engine/tools/run_logic_tests.py` is an in-memory core harness; it excludes Parquet/config/HTTP integration. Do not report its results as full-server validation. Its `--with-tool-tests` also runs the module/catalog tooling checks.

In `frontend/`: `npm ci`, `npm test`, `npm run build`, `npm run lint`. Production helpers must not be replaced with test stubs. `npm run test:e2e` supports `PLAYWRIGHT_PORT`, `PLAYWRIGHT_CHROME_PATH`, `PLAYWRIGHT_USE_BUILD`, and `PLAYWRIGHT_ENGINE_URL`. Use the last variable for live C++ integration and distinguish it from fixture-based browser tests.

Current API routes: `/api/config`, `/api/simulate`, `/api/ga/stream`, `/api/alphas`. Preserve actual XML when saving an expanded alpha. Raw signal payloads have sampling metadata and are separate from normalized positions.
