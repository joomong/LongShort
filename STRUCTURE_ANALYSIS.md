# Project Structure Report: LongShort

## Overview

| Item | Value |
|------|-------|
| Project type | Crypto Long-Short Alpha Research Platform |
| Purpose | Design, backtest, and optimize quantitative alpha signals on crypto futures (Binance USDT-margined) |
| Language(s) | Python 3 (backend), TypeScript (frontend) |
| Backend framework | FastAPI (Python) |
| Frontend framework | Next.js 14 + React 18 + Tailwind CSS |
| State management | Zustand |
| Visualization | Recharts (frontend) |
| Data format | Parquet (PyArrow) |
| Testing | pytest (backend), Vitest (frontend) |
| Key algorithm | Genetic Algorithm for alpha parameter optimization |

---

## Directory Tree

```
LongShort/
|-- CLAUDE.md                  # Dev instructions for Claude
|-- README.md                  # Project readme
|-- config.yaml                # Central config (data paths, symbols, backtest params)
|-- pytest.ini                 # pytest settings
|-- __init__.py
|
|-- Data/
|   `-- ohlcv/
|       `-- um_1d.parquet      # Daily OHLCV data (Binance USDT-M futures)
|
|-- PLAN/
|   |-- image.png
|   `-- optimization_plan.md   # Design docs
|
|-- framework/                 # Core engine (data loading, signal DSL, backtester)
|   |-- __init__.py
|   |-- backtester.py          # Backtester engine + IS/OS split + multi-alpha
|   |-- data_cache.py          # In-memory data cache (startup preload)
|   |-- dataloader.py          # PyArrow-based parquet loader + iterator
|   |-- exceptions.py          # XmlCompileError
|   |-- parallel.py            # Shared-memory parallel backtest (ProcessPool)
|   |-- signal_base.py         # Abstract Signal base class + tag registry
|   |-- xml_helpers.py         # XML attribute parsing helpers
|   `-- modules/               # Signal computation nodes (composable DAG)
|       |-- __init__.py        # Re-exports all modules
|       |-- sources.py         # Price (leaf node -- reads OHLCV columns)
|       |-- constant.py        # ConstantSignal (literal number)
|       |-- ops.py             # Add, Sub, Mul, Div, Le, Ge
|       |-- rank.py            # Cross-sectional rank
|       |-- zscore.py          # Cross-sectional z-score
|       |-- tsmean.py          # Time-series rolling mean (stateful)
|       |-- tszscore.py        # Time-series z-score (stateful)
|       |-- tsrank.py          # Time-series rank (stateful)
|       |-- delay.py           # Lag operator (stateful)
|       |-- normalize.py       # Per-symbol normalization
|       |-- DollarVolume.py    # Dollar volume signal
|       `-- event.py           # Event-driven holding mask (stateful)
|
|-- api/                       # FastAPI backend
|   |-- __init__.py
|   |-- main.py                # App creation, CORS, router registration
|   |-- expr_parser.py         # Safe arithmetic expr -> Signal DAG (via ast)
|   |-- xml_alpha.py           # XML -> Signal compiler (phases 1-3)
|   |-- xml_alpha_sources.py   # compile_with_sources(): returns signal + source XML
|   |-- models/
|   |   |-- __init__.py
|   |   `-- schemas.py         # Pydantic request/response models
|   |-- routers/
|   |   |-- __init__.py
|   |   |-- backtest.py        # POST /api/backtest/run
|   |   |-- data.py            # GET /api/symbols, GET /api/data-range
|   |   |-- alphas.py          # Alpha CRUD: POST/GET /api/alphas
|   |   `-- ga.py              # POST /api/ga/run, POST /api/ga/run-stream
|   |-- services/
|   |   |-- __init__.py
|   |   |-- backtest_service.py  # Business logic: single & multi-alpha backtest
|   |   |-- alpha_service.py     # Alpha CRUD (file system)
|   |   |-- ga_engine.py         # GA core: search space, genome, GA loop
|   |   `-- ga_service.py        # Wires GA engine to backtest evaluation
|   |-- api_plan.md
|   `-- README.md
|
|-- frontend/                  # Next.js SPA
|   |-- app/
|   |   |-- page.tsx           # Main page (split-panel layout)
|   |   |-- layout.tsx
|   |   `-- globals.css
|   |-- components/
|   |   |-- editor/            # XML editor + control buttons
|   |   |-- layout/            # MainLayout (resizable split panel)
|   |   |-- modals/            # Save/Load alpha modals
|   |   |-- multi-alpha/       # SummaryMetrics, AlphaRankingTable
|   |   |-- providers/         # ThemeProvider
|   |   `-- visualization/     # Metrics, heatmap, distribution, charts, snapshots
|   |-- hooks/
|   |   `-- useBacktest.ts     # Orchestrates GA streaming + backtest run
|   |-- lib/
|   |   |-- api.ts             # HTTP client (fetch wrappers)
|   |   |-- colorScale.ts
|   |   |-- metricColors.ts
|   |   |-- snapshotColors.ts
|   |   `-- themeColors.ts
|   |-- store/
|   |   |-- alphaStore.ts      # Zustand global state
|   |   `-- themeStore.ts
|   |-- types/
|   |   `-- index.ts           # TypeScript interfaces (aligned to backend)
|   |-- package.json
|   |-- tailwind.config.ts
|   `-- vitest.config.ts
|
|-- tests/                     # pytest test suite
|   |-- conftest.py            # Shared fixtures (parquet, mocks)
|   `-- unit/
|       |-- test_backtester.py
|       |-- test_data_cache.py
|       |-- test_data_cache_edge.py
|       |-- test_data_cache_integration.py
|       |-- test_dataloader.py
|       |-- test_expr_parser.py
|       |-- test_xml_alpha.py
|       |-- test_xml_alpha_event_compile.py
|       |-- test_api_backtest.py
|       |-- test_api_ga.py
|       |-- test_api_phase2.py
|       |-- test_event_signal.py
|       |-- test_ga_engine.py
|       |-- test_ga_service.py
|       |-- test_metrics_calculations.py
|       |-- test_reliability_golden.py
|       `-- test_alpha_detail_view.py
|
`-- frontend/__tests__/        # Vitest frontend tests
    |-- alphaStore.test.ts
    `-- enforceSettingsDates.test.ts
```

---

## Module Dependency Map

```
                                  ┌─────────────────────────────────────────────┐
                                  │             frontend/ (Next.js)             │
                                  │  page.tsx -> useBacktest -> lib/api.ts      │
                                  │  store/alphaStore.ts (Zustand)              │
                                  │  components/visualization/*                 │
                                  └──────────────────┬──────────────────────────┘
                                                     │ HTTP (localhost:8000)
                                                     v
┌────────────────────────────────────────────────────────────────────────────────┐
│                            api/ (FastAPI)                                      │
│                                                                                │
│  main.py                                                                       │
│    ├── routers/backtest.py ──> services/backtest_service.py                    │
│    ├── routers/data.py ──────> framework/data_cache.py                         │
│    ├── routers/alphas.py ────> services/alpha_service.py                       │
│    └── routers/ga.py ────────> services/ga_service.py ──> services/ga_engine.py│
│                                                                                │
│  xml_alpha.py ──────> expr_parser.py                                          │
│  xml_alpha_sources.py ──> xml_alpha.py                                         │
└────────────────────────────────┬───────────────────────────────────────────────┘
                                 │ imports
                                 v
┌────────────────────────────────────────────────────────────────────────────────┐
│                          framework/  (Core Engine)                             │
│                                                                                │
│  signal_base.py (ABC)                                                          │
│       ^                                                                        │
│       │ subclassed by                                                          │
│  modules/                                                                      │
│  ├── sources.py (Price)                                                        │
│  ├── constant.py (ConstantSignal)                                             │
│  ├── ops.py (Add, Sub, Mul, Div, Le, Ge)                                     │
│  ├── rank.py, zscore.py            <-- cross-sectional                        │
│  ├── tsmean.py, tszscore.py, tsrank.py, delay.py  <-- time-series (stateful)  │
│  ├── normalize.py, DollarVolume.py                                            │
│  └── event.py (EventSignal)        <-- event-driven (stateful)                │
│                                                                                │
│  backtester.py ──> dataloader.py / data_cache.py                              │
│  parallel.py ────> backtester.py (ProcessPoolExecutor + shared memory)         │
│  data_cache.py ──> dataloader.py                                              │
└────────────────────────────────────────────────────────────────────────────────┘
                                 │ reads
                                 v
                          Data/ohlcv/um_1d.parquet
```

---

## Entry Points

| Entry Point | File | How to Run | Description |
|-------------|------|------------|-------------|
| FastAPI backend | `/home/jgo/qv-project/LongShort/api/main.py` | `uvicorn api.main:app` | Primary API server |
| Next.js frontend | `/home/jgo/qv-project/LongShort/frontend/app/page.tsx` | `npm run dev` (from `frontend/`) | Web UI on `localhost:3000` |
| Tests (backend) | `/home/jgo/qv-project/LongShort/tests/` | `pytest` | Python test suite |
| Tests (frontend) | `/home/jgo/qv-project/LongShort/frontend/` | `npm test` | Vitest test suite |

---

## API Endpoints

| Method | Path | Router File | Description |
|--------|------|-------------|-------------|
| `POST` | `/api/backtest/run` | `/home/jgo/qv-project/LongShort/api/routers/backtest.py:22` | Run backtest (single or multi-alpha) |
| `GET` | `/api/symbols` | `/home/jgo/qv-project/LongShort/api/routers/data.py:15` | List available trading symbols |
| `GET` | `/api/data-range` | `/home/jgo/qv-project/LongShort/api/routers/data.py:21` | Get min/max date + fee rate |
| `POST` | `/api/alphas` | `/home/jgo/qv-project/LongShort/api/routers/alphas.py:19` | Save alpha XML to disk |
| `GET` | `/api/alphas` | `/home/jgo/qv-project/LongShort/api/routers/alphas.py:28` | List saved alphas |
| `GET` | `/api/alphas/{name}` | `/home/jgo/qv-project/LongShort/api/routers/alphas.py:33` | Get a saved alpha by name |
| `POST` | `/api/ga/run` | `/home/jgo/qv-project/LongShort/api/routers/ga.py:24` | Run GA parameter search (blocking) |
| `POST` | `/api/ga/run-stream` | `/home/jgo/qv-project/LongShort/api/routers/ga.py:45` | GA search with NDJSON streaming progress |

---

## Call Flows

### Flow 1: Backtest Run (Single Alpha)

```
POST /api/backtest/run { xml, settings }
  -> routers/backtest.py:run_backtest()
    -> xml_alpha_sources.py:compile_with_sources(xml)
       -> xml_alpha.py:_expand_pipeline(elems)     # Phase 2 expansion (pipe/range/choice)
       -> xml_alpha.py:_compile_pipeline(ops)       # Build Signal DAG
          -> expr_parser.py:parse_expr(expr)        # AST-safe parse "close", "return", etc.
             -> modules/sources.py:Price("close")
             -> modules/delay.py:Delay(sig, period)
          -> Signal._tag_registry -> modules/*.from_xml()  # <rank/>, <zscore/>, etc.
    -> services/backtest_service.py:run_single_alpha()
       -> framework/data_cache.py:DataCacheIterator  # iterate cached data
       -> framework/backtester.py:Backtester.run()
          for each (timestamp, data_slice):
            -> alpha.compute(timestamp, data_slice)  # walk Signal DAG
            -> strategy.generate_weights(raw_signals) # SignalRatioStrategy
            -> calculate portfolio return, turnover, min_bet, max_weight
       -> _split_result_by_date(result, osStart)     # IS/OS split
       -> heatmap_from_viz_symbols(sym_df)           # signal heatmap data
       -> range_and_hist(raw_signals)                # distribution histogram
    <- { combined, alphas[{isMetrics, osMetrics, retSeries, ...}], heatmap, distribution }
```

### Flow 2: GA Parameter Search (Streaming)

```
POST /api/ga/run-stream { xml, settings, topN }
  -> routers/ga.py:run_ga_stream_endpoint()
    -> Thread(worker)
       -> ga_engine.py:extract_search_space(xml)   # find pipe/range variable dimensions
       -> ga_engine.py:run_ga(space, config, evaluate_batch, on_progress)
          for each generation:
            -> evaluate_batch(xml_list)              # batch compile + backtest
               -> compile_with_sources(xml) for each
               -> run_backtest()            # parallel via ProcessPoolExecutor
                  -> parallel.py:run_parallel_backtests()
                     -> shared memory allocation
                     -> ProcessPoolExecutor._backtest_worker() x N
                        -> DataLoader + Backtester.run() per alpha
                     -> read results from shared memory
            -> tournament_select, crossover, mutate  # GA operators
            -> on_progress(event) -> queue.put(NDJSON line)
       -> queue.put({"type": "complete", "data": final_response})
    <- StreamingResponse(NDJSON)
```

### Flow 3: Signal DAG Computation (single timestamp)

```
alpha.compute(timestamp, data_slice)
  example XML: <data expr="close" delay="1"/> <tsmean days="20"/> <zscore/>

  ZScore.compute(ts, ds)
    -> TsMean.compute(ts, ds)           # stateful: rolling 20-day buffer
       -> Delay.compute(ts, ds)         # stateful: lag-1 buffer
          -> Price.compute(ts, ds)      # ds["close"]
       <- pd.Series (delayed close)
    <- pd.Series (20-day mean of delayed close)
  <- pd.Series (cross-sectional z-score)
```

### Flow 4: Frontend Backtest Trigger

```
User presses Shift+Enter (or clicks Run)
  -> page.tsx:RightPanel.handleKeyDown
    -> hooks/useBacktest.ts:run()
       -> store/alphaStore.ts: setIsRunning(true), setResult(null)
       -> lib/api.ts:runGAStream(xml, settings, topN, onEvent)
          <- streaming NDJSON events
          on "progress": update gaProgress in store
          on "generation_end": update result with partial alphas
          on "complete": set final result in store
       -> store reacts -> LeftPanel re-renders
          single alpha: BacktestMetrics, SignalDistribution, SignalHeatmap, SignalChart
          multi alpha: SummaryMetrics, AlphaRankingTable
          detail view: AlphaDetailView (equity, turnover, min_bet, max_weight time series)
```

---

## Key Classes and Data Structures

### Signal DAG (`/home/jgo/qv-project/LongShort/framework/signal_base.py`)

| Class | Tag | Location | Stateful | Description |
|-------|-----|----------|----------|-------------|
| `Signal` | -- | `signal_base.py:9` | -- | Abstract base. Tag registry via `__init_subclass__`. Operator overloads (+,-,*,/,<=,>=). |
| `Price` | -- | `modules/sources.py:5` | No | Leaf: reads a column (open/high/low/close/volume) from `data_slice` |
| `ConstantSignal` | -- | `modules/constant.py` | No | Leaf: returns a constant float for all symbols |
| `Add/Sub/Mul/Div` | -- | `modules/ops.py` | No | Binary arithmetic on two Signal children |
| `Le/Ge` | -- | `modules/ops.py:32` | No | Comparison operators, returns 0.0/1.0 |
| `Rank` | `rank` | `modules/rank.py:5` | No | Cross-sectional percentile rank |
| `ZScore` | `zscore` | `modules/zscore.py:5` | No | Cross-sectional z-score (robust option) |
| `TsMean` | `tsmean` | `modules/tsmean.py:7` | Yes | Rolling mean over N days (deque per symbol) |
| `TSZScore` | `tszscore` | `modules/tszscore.py` | Yes | Time-series z-score |
| `TSRank` | `tsrank` | `modules/tsrank.py` | Yes | Time-series rank |
| `Delay` | `delay` | `modules/delay.py:7` | Yes | Lag operator (deque per symbol) |
| `EventSignal` | `event` | `modules/event.py:13` | Yes | Event-driven holding mask with trigger + max_holddays |
| `NormalizeBySymbol` | -- | `modules/normalize.py` | -- | Per-symbol normalization |
| `DollarVolume` | -- | `modules/DollarVolume.py` | -- | close * volume signal |

### Backtester (`/home/jgo/qv-project/LongShort/framework/backtester.py`)

| Class/Dataclass | Location | Description |
|------------------|----------|-------------|
| `Backtester` | `backtester.py:121` | Main engine. Iterates DataLoader, calls alpha.compute, strategy.generate_weights, tracks returns/turnover. Modes: D0 (same-day), D1 (next-day). |
| `BacktestResult` | `backtester.py:92` | Frozen dataclass: `ret_series`, `avg_turnover`, `turnover_series`, `min_bet_series`, `max_weight_series`, `long_weight_series`, `short_weight_series` |
| `PeriodSettings` | `backtester.py:22` | IS/OS date boundaries (is_start, is_end, os_start, os_end) |
| `PeriodMetrics` | `backtester.py:31` | pnl, cagr, sharpe, mdd, turnover, min_bet, max_weight, ir, corr |
| `BacktestPeriodResult` | `backtester.py:49` | IS + OS metrics and return series |
| `AlphaResult` | `backtester.py:582` | Individual alpha within multi-alpha backtest |
| `MultipleBacktestResult` | `backtester.py:602` | Combined multi-alpha result with `to_dict()` for API serialization |
| `CombinedAlpha` | `backtester.py:559` | Sums signals from multiple alphas |
| `SignalRatioStrategy` | `backtester.py:86` | Default strategy: normalize signals by absolute sum to get weights |

### GA Engine (`/home/jgo/qv-project/LongShort/api/services/ga_engine.py`)

| Class/Dataclass | Location | Description |
|------------------|----------|-------------|
| `GeneSpec` | `ga_engine.py:63` | One axis of search space (elem_index, attr_name, choices) |
| `SearchSpace` | `ga_engine.py:73` | Complete search space with gene list + element templates |
| `Individual` | `ga_engine.py:237` | Genome (list of ints), xml, fitness (IS Sharpe), xml_hash |
| `GAConfig` | `ga_engine.py:304` | Hyperparameters: pop_size=50, generations, elite=5, mutation=0.1, crossover=0.7, early_stop=5 |
| `EvalCache` | `ga_engine.py:328` | Dict cache of xml_hash -> fitness to avoid redundant backtest |
| `GAResult` | `ga_engine.py:350` | best_individuals, generations_run, total_evaluations |

### Pydantic Schemas (`/home/jgo/qv-project/LongShort/api/models/schemas.py`)

| Model | Description |
|-------|-------------|
| `BacktestSettings` | `isStart`, `isEnd`, `osStart`, `osEnd`, `barInterval` (D0/D1/intraday), `nJobs`, `gaPopulation`. Validates date ordering. |
| `BacktestRunRequest` | `xml: str` + `settings: BacktestSettings` |
| `DataRangeResponse` | `minDate`, `maxDate`, `transactionFeeRate` |
| `SaveAlphaRequest` | `name` (alphanumeric), `xml` |
| `AlphaInfo` | `name`, `createdAt` |
| `AlphaDetailResponse` | `name`, `xml`, `createdAt` |
| `GARunRequest` | `xml`, `settings`, `topN` |

### Frontend TypeScript Types (`/home/jgo/qv-project/LongShort/frontend/types/index.ts`)

| Type | Key Fields |
|------|------------|
| `BacktestRunResponse` | `combined`, `alphas[]`, `heatmap?`, `signalRange?`, `distribution?`, `initialCapital` |
| `BacktestAlphaResult` | `name`, `rank`, `isMetrics`, `osMetrics`, `xml?`, `isRetSeries?`, `isEquitySeries?`, time-series maps |
| `BacktestSettings` | `isStart`, `isEnd`, `osStart`, `osEnd`, `barInterval`, `nJobs`, `gaPopulation` |
| `HeatmapData` | `timestamps[]`, `symbols[]`, `values[][]` |
| `PinnedSnapshot` | `id`, `name`, `color`, `retSeries` (equity overlay comparison) |
| `GAProgress` | `generation`, `totalGenerations`, `evaluated`, `populationSize` |
| `ViewMode` | `'single' | 'multi' | 'detail'` |

---

## XML Alpha DSL

The core user interface is an XML-based domain-specific language for defining alpha signals. Examples:

```xml
<!-- Simple: delayed close price -->
<data expr="close" delay="1"/>

<!-- Pipeline: close -> 20-day mean -> cross-sectional z-score -->
<data expr="close" delay="1"/>
<tsmean days="20"/>
<zscore/>

<!-- Phase 2: Parameter sweep via pipe and range syntax -->
<data expr="close|volume" delay="1...5"/>
<tsmean days="10...10...60"/>
<zscore/>

<!-- Phase 3: Choice blocks and shuffling -->
<data expr="close" delay="1"/>
<choice>
  <rank/>
  <zscore/>
</choice>
```

**Expansion rules** (handled by `/home/jgo/qv-project/LongShort/api/xml_alpha.py`):

| Syntax | Meaning | Example | Expands to |
|--------|---------|---------|------------|
| `a\|b\|c` | Pipe alternatives | `expr="close\|volume"` | 2 alphas |
| `10...10...60` | Range (start...step...end) | `days="10...10...60"` | 6 values: 10,20,30,40,50,60 |
| `<choice>` | Alternative pipeline steps | `<choice><rank/><zscore/></choice>` | 2 alphas |
| `<shuffle>` | Permute child order | `<shuffle><rank/><tsmean days="5"/></shuffle>` | 2 alphas |
| `<choice bind="same">` | Synchronized choices across occurrences | -- | Same index chosen for all bound blocks |

When a template has variable dimensions, the GA engine (`ga_engine.py`) treats each dimension as a gene and evolves parameter combinations.

---

## Data Flow (End-to-End)

```
                Parquet File                         config.yaml
          Data/ohlcv/um_1d.parquet          (paths, columns, dates, symbols)
                    |                                    |
                    v                                    v
           framework/dataloader.py            framework/data_cache.py
           (PyArrow filter + iterate)         (preload into memory at startup)
                    |                                    |
                    v                                    v
              DataLoader iterator  <-- OR -->  DataCacheIterator
              (disk-based, for                (in-memory, used by
               parallel workers)               main process)
                    |
                    +--------------------------------------------+
                    |                                            |
                    v                                            v
          framework/backtester.py:Backtester.run()    (parallel.py workers)
                    |
          for each bar (timestamp, data_slice):
                    |
                    v
          Signal DAG .compute(ts, ds)
          (xml_alpha compiled pipeline)
                    |
                    v
          strategy.generate_weights()
          (SignalRatioStrategy: normalize by |signal| sum)
                    |
                    v
          Portfolio return = sum(prev_weights * asset_returns)
          Turnover = sum(|delta_weights|)
          Fee deduction = turnover * 0.001 (10bps)
                    |
                    v
          BacktestResult -> split IS/OS -> PeriodMetrics
                    |
                    v
          API response JSON -> Frontend store -> UI components
```

---

## Configuration

### `config.yaml` (`/home/jgo/qv-project/LongShort/config.yaml`)

| Section | Key | Value | Description |
|---------|-----|-------|-------------|
| `datasets` | `path` | `Data/ohlcv/um_1d.parquet` | Parquet data file path |
| `columns` | `time` | `open_time` | Timestamp column (ms epoch) |
| `columns` | `symbol` | `symbol` | Symbol identifier column |
| `columns` | `ohlcv` | `[open, high, low, close, volume]` | OHLCV column names |
| `backtest` | `start_date` | `2020-01-01` | Data range start |
| `backtest` | `end_date` | `2025-08-31` | Data range end |
| `backtest` | `initial_capital` | `1,000,000` | Starting capital |
| `backtest` | `transaction_fee_rate` | `0.0010` | 10 bps round-trip fee |
| `symbols` | -- | 17 symbols (BTCUSDT, ETHUSDT, ...) | Universe of traded instruments |

### Frontend Environment

| Variable | Default | Description |
|----------|---------|-------------|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Backend API base URL |

### Hardcoded Constants

| Constant | File | Value | Description |
|----------|------|-------|-------------|
| `BARS_PER_YEAR` | `backtester.py:13` | `365` | For annualization (crypto = 365 days) |
| `TRANSACTION_FEE_RATE` | `backtester.py:14` | `0.0010` | Default fee rate |
| `MAX_ALPHA_COUNT` | `xml_alpha.py:44` | `1000` | Max alphas from expansion |
| `ALLOWED_COLUMNS` | `expr_parser.py:17` | `{open, high, low, close, volume}` | Safe columns in `expr` |

---

## Testing

### Backend (pytest)

| Test File | Location | Covers |
|-----------|----------|--------|
| `test_dataloader.py` | `/home/jgo/qv-project/LongShort/tests/unit/test_dataloader.py` | PyArrow data loading, date filtering, chunking |
| `test_data_cache.py` | `.../test_data_cache.py` | DataCache load, get_slice, get_timestamps |
| `test_data_cache_edge.py` | `.../test_data_cache_edge.py` | Edge cases for data cache |
| `test_data_cache_integration.py` | `.../test_data_cache_integration.py` | Integration tests for data cache |
| `test_backtester.py` | `.../test_backtester.py` | Backtester engine, D0/D1 modes, turnover |
| `test_metrics_calculations.py` | `.../test_metrics_calculations.py` | Sharpe, MDD, CAGR calculations |
| `test_expr_parser.py` | `.../test_expr_parser.py` | Expression parser (AST safety) |
| `test_xml_alpha.py` | `.../test_xml_alpha.py` | XML compiler phases 1-3 |
| `test_xml_alpha_event_compile.py` | `.../test_xml_alpha_event_compile.py` | Event signal XML compilation |
| `test_event_signal.py` | `.../test_event_signal.py` | EventSignal compute behavior |
| `test_api_backtest.py` | `.../test_api_backtest.py` | Backtest API endpoint |
| `test_api_ga.py` | `.../test_api_ga.py` | GA API endpoint |
| `test_api_phase2.py` | `.../test_api_phase2.py` | Phase 2 expansion API tests |
| `test_ga_engine.py` | `.../test_ga_engine.py` | GA operators, genome, search space |
| `test_ga_service.py` | `.../test_ga_service.py` | GA service wiring |
| `test_reliability_golden.py` | `.../test_reliability_golden.py` | Golden-master regression tests |
| `test_alpha_detail_view.py` | `.../test_alpha_detail_view.py` | Alpha detail view data |

Fixtures in `/home/jgo/qv-project/LongShort/tests/conftest.py` provide synthetic parquet files (3-day/3-symbol, 10-day/5-symbol, single-timestamp) and mock alpha/strategy classes.

### Frontend (Vitest)

| Test File | Covers |
|-----------|--------|
| `alphaStore.test.ts` | Zustand store logic |
| `enforceSettingsDates.test.ts` | Date constraint enforcement |

---

## Architecture Summary

```
┌─────────────────────────────────────────────────────────────┐
│                     USER (Browser)                          │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │              Next.js Frontend (:3000)                  │  │
│  │                                                       │  │
│  │  ┌──────────┐  ┌──────────────┐  ┌────────────────┐  │  │
│  │  │ XML      │  │ Settings Bar │  │ Visualization  │  │  │
│  │  │ Editor   │  │ (IS/OS dates │  │ - Metrics      │  │  │
│  │  │ (Monaco) │  │  barInterval │  │ - Heatmap      │  │  │
│  │  │          │  │  nJobs, GA)  │  │ - Distribution │  │  │
│  │  └────┬─────┘  └──────┬───────┘  │ - Equity chart │  │  │
│  │       │               │          │ - Snapshots    │  │  │
│  │       v               v          │ - Ranking tbl  │  │  │
│  │  ┌─────────────────────────┐     └────────────────┘  │  │
│  │  │ useBacktest() hook      │              ^           │  │
│  │  │ -> GA streaming via     │──────────────┘           │  │
│  │  │    NDJSON fetch         │                          │  │
│  │  └───────────┬─────────────┘                          │  │
│  │              │ Zustand store (alphaStore)              │  │
│  └──────────────┼────────────────────────────────────────┘  │
│                 │ HTTP POST /api/ga/run-stream               │
└─────────────────┼───────────────────────────────────────────┘
                  v
┌─────────────────────────────────────────────────────────────┐
│              FastAPI Backend (:8000)                         │
│                                                             │
│  Startup: DataCache.load() -> config.yaml -> Parquet -> RAM │
│                                                             │
│  ┌──────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │ XML      │  │ Backtest     │  │ GA Engine            │  │
│  │ Compiler │->│ Service      │<-│ (search space,       │  │
│  │          │  │ (single/     │  │  crossover, mutate,  │  │
│  │ Phase 1: │  │  multi)      │  │  tournament select)  │  │
│  │ pipeline │  │              │  │                      │  │
│  │ Phase 2: │  │ Heatmap,     │  │ Evaluates via batch  │  │
│  │ expand   │  │ Distribution │  │ multi-backtest       │  │
│  │ Phase 3: │  │              │  │                      │  │
│  │ shuffle  │  └──────┬───────┘  └──────────────────────┘  │
│  └──────────┘         │                                     │
│                       v                                     │
│  ┌──────────────────────────────────────────────────────┐   │
│  │                 framework/                            │   │
│  │  Backtester <-> DataCache/DataLoader                  │   │
│  │  Signal DAG   <- modules/*                            │   │
│  │  Parallel     <- ProcessPool + shared memory          │   │
│  └──────────────────────────────────────────────────────┘   │
│                       │                                     │
│                       v                                     │
│               Data/ohlcv/um_1d.parquet                      │
└─────────────────────────────────────────────────────────────┘
```

The system follows a clean three-tier architecture: the **frontend** provides an interactive XML editor and visualization dashboard; the **API layer** handles request parsing, XML compilation, and orchestration; the **framework layer** implements the core backtesting engine with a composable Signal DAG, data loading, and parallelization. The GA engine sits in the API services layer and uses the framework's backtester as its fitness function, streaming intermediate results to the frontend via NDJSON.
