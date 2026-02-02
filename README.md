# qv-crypto-ls / LongShort

Crypto long-short research project.

## Backend (FastAPI)

- Entry: `api/main.py`
- API docs: `api/README.md`
- XML workflow/spec: `PLAN/XML_WORKFLOW.md`

## Frontend (Next.js)

- Location: `frontend/`

> Note: the backend API contract is documented in `api/README.md` and `api/models/schemas.py`.

## Changelog

### Single Alpha min_bet / max_weight
- `compute_metrics()` now accepts a `BacktestResult` object and returns `min_bet` (mean of min_bet_series) and `max_weight` (max of max_weight_series) in addition to pnl, sharpe, mdd, turnover.
- Frontend IS metrics table displays MinBet and MaxWt columns.

## Design Notes

### Signal Histogram

`range_and_hist()` in `api/services/backtest_service.py` computes the histogram
from **raw signal values** (전체 기간, 전체 심볼의 모든 signal 값) — no z-score or
other transformation is applied. 기본 bin 수는 **1000**.
