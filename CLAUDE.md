
# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LongShort is a crypto long-short alpha research platform for designing, backtesting, and optimizing quantitative alpha signals on Binance USDT-margined futures. The system uses a 3-tier architecture:

- **Framework layer** (`framework/`): Core backtesting engine with Signal DAG, data loading (PyArrow/Parquet), and parallel execution
- **API layer** (`api/`): FastAPI backend with XML-to-Signal compiler, GA optimizer, and backtest orchestration
- **Frontend layer** (`frontend/`): Next.js SPA with Monaco XML editor, real-time GA streaming, and visualization

Key technologies: FastAPI, Next.js 14, React 18, Zustand, Recharts, pytest, Vitest.

## Development Commands

### Backend (FastAPI)
```bash
# Run API server (from project root)
. .venv/bin/activate
uvicorn api.main:app --reload --port 8000

# Run all tests
pytest

# Run specific test file
pytest tests/unit/test_backtester.py

# Run single test
pytest tests/unit/test_backtester.py::test_function_name -v

# Run with coverage
pytest --cov=framework --cov=api
```

### Frontend (Next.js)
```bash
cd frontend

# Development server
npm run dev          # localhost:3000

# Build
npm run build

# Production server
npm start

# Tests
npm test             # run once
npm run test:watch   # watch mode

# Lint
npm run lint
```

### Legacy Bokeh Dashboard
```bash
bokeh serve --show app.py
```

## Architecture

### Signal DAG System

The core abstraction is a directed acyclic graph (DAG) of `Signal` nodes defined in `framework/signal_base.py`. Each signal node implements `.compute(timestamp, data_slice)` and can be composed via operator overloading (+, -, *, /, <=, >=).

**Key module types in `framework/modules/`:**
- **Sources**: `Price` (OHLCV leaf nodes), `ConstantSignal`
- **Cross-sectional**: `Rank`, `ZScore` (stateless, operate across symbols at a single timestamp)
- **Time-series**: `TsMean`, `TSZScore`, `TSRank`, `Delay` (stateful, maintain buffers per symbol)
- **Operators**: `Add`, `Sub`, `Mul`, `Div`, `Le`, `Ge`
- **Event-driven**: `EventSignal` (stateful holding mask with trigger condition)

All signal modules register via `Signal._tag_registry` using `__init_subclass__`, enabling XML compilation.

### XML Alpha DSL

Users define alphas in XML. The compiler (`api/xml_alpha.py`) has 3 phases:

1. **Phase 1**: Parse pipeline (sequential elements)
2. **Phase 2**: Expand templates with pipe (`a|b`), range (`10...10...60`), `<choice>`, `<shuffle>`
3. **Phase 3**: Compile to Signal DAG via `expr_parser.py` (AST-safe expression parsing) and tag registry

Example XML:
```xml
<data expr="close" delay="1"/>
<tsmean days="20"/>
<zscore/>
```

The GA engine (`api/services/ga_engine.py`) treats expansion dimensions as genes and evolves parameter combinations using tournament selection, crossover, and mutation.

### Data Flow

1. **Startup**: `DataCache.load()` (in `framework/data_cache.py`) preloads `Data/ohlcv/um_1d.parquet` into RAM using config from `config.yaml`
2. **Backtest request**: Frontend → `/api/backtest/run` → `xml_alpha_sources.py:compile_with_sources()` → Signal DAG
3. **Execution**: `framework/backtester.py:Backtester.run()` iterates `DataCacheIterator`, calling `alpha.compute()` per timestamp
4. **Parallel GA**: `parallel.py` uses `ProcessPoolExecutor` with shared memory for multi-alpha evaluation
5. **Response**: IS/OS metrics, return series, heatmap, histogram → Zustand store → Recharts visualization

### Key Files

- `framework/backtester.py`: Core engine, IS/OS split, multi-alpha aggregation, metrics calculation
- `api/xml_alpha.py`: XML compiler (phases 1-3), expansion logic
- `api/services/ga_engine.py`: Genetic algorithm with search space extraction
- `api/services/backtest_service.py`: Business logic, heatmap/histogram generation
- `frontend/hooks/useBacktest.ts`: Orchestrates GA streaming + backtest execution
- `frontend/store/alphaStore.ts`: Global Zustand state (result, settings, viewMode, snapshots)

## Conventions

### Code Style
- Make small, reviewable diffs
- After changes: show diff/snippet + run minimal smoke test
- Prefer existing running servers; avoid starting new ones
- Finish tasks in <=10 minutes (avoid long-running interactive sessions)

### XML Format
- Use self-closing tags without wrappers: `<data expr="close" delay="1"/>`
- Safe expressions via AST parsing in `expr_parser.py` (allowed: `open`, `high`, `low`, `close`, `volume`, arithmetic)

### Backtester Visualization
- Viz dataframes should have datetime-like index named `'date'`
- Signal histogram uses raw signal values (no z-score transformation), 1000 bins default
- `compute_metrics()` returns `min_bet` (mean of min_bet_series) and `max_weight` (max of max_weight_series)

### Testing
- Backend: pytest with fixtures in `tests/conftest.py` (synthetic parquet files, mock alphas)
- Frontend: Vitest with `@testing-library/react`
- Test files follow `test_*.py` naming
- Golden-master regression tests in `test_reliability_golden.py`

## Configuration

- `config.yaml`: Data paths, symbols (17 USDT-margined futures), date range, fees (10bps), initial capital
- `pytest.ini`: Test paths and options (`-v --tb=short`)
- Backend constants: `BARS_PER_YEAR=365`, `MAX_ALPHA_COUNT=1000`, `ALLOWED_COLUMNS`
- Frontend env: `NEXT_PUBLIC_API_URL` (default: `http://localhost:8000`)

## API Endpoints

- `POST /api/backtest/run`: Single/multi-alpha backtest
- `POST /api/ga/run-stream`: GA search with NDJSON streaming
- `GET /api/symbols`: List available symbols
- `GET /api/data-range`: Date range + fee rate
- `POST/GET /api/alphas`: Alpha CRUD (filesystem-based)

See `api/README.md` and `api/models/schemas.py` for detailed API contract.

