from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import config
from framework.dataloader import DataLoader
from framework.backtester import Backtester
from framework.strategy import SignalRatioStrategy

from api.xml_alpha import compile_alpha_from_xml, XmlCompileError


# Match the symbols used in run.py for consistency
SELECT_SYMBOLS: List[str] = [
    "BTCUSDT",
    "ETHUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "TRXUSDT",
    "ADAUSDT",
    "HYPEUSDT",
    "LINKUSDT",
    "SUIUSDT",
    "AVAXUSDT",
    "SHIBUSDT",
    "DOTUSDT",
    "UNIUSDT",
    "AAVEUSDT",
    "1000PEPEUSDT",
]


class BacktestSettings(BaseModel):
    osStart: str
    osEnd: str
    isStart: str
    isEnd: str
    barInterval: str


class BacktestRunRequest(BaseModel):
    xml: str
    settings: BacktestSettings


app = FastAPI(title="LongShort API", version="0.1.0")

# Allow local Next.js dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"] ,
    allow_headers=["*"],
)


@app.get("/api/symbols")
def get_symbols() -> List[str]:
    return SELECT_SYMBOLS


def _compute_metrics(ret_series: pd.Series, avg_turnover: float) -> Dict[str, float]:
    # Basic daily metrics (matching frontend shape; units are roughly percent-like)
    r = ret_series.fillna(0.0)
    if len(r) == 0:
        return {"pnl": 0.0, "sharpe": 0.0, "mdd": 0.0, "turnover": 0.0, "ir": 0.0, "corr": 0.0}

    bars_per_year = 365.0
    equity = (1.0 + r).cumprod()
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)

    vol = float(r.std())
    sharpe = float((r.mean() / (vol + 1e-12)) * np.sqrt(bars_per_year)) if vol > 0 else 0.0

    dd = equity / equity.cummax() - 1.0
    mdd = float(dd.min())

    return {
        "pnl": total_return * 100.0,
        "sharpe": sharpe,
        "mdd": mdd * 100.0,
        "turnover": avg_turnover * 100.0,
        "ir": 0.0,
        "corr": 0.0,
    }


def _heatmap_from_viz_symbols(sym_df: pd.DataFrame) -> Dict[str, Any]:
    # sym_df index: date, columns include symbol, signal
    if sym_df.empty:
        return {"timestamps": [], "symbols": [], "values": []}

    df = sym_df.copy()
    df = df.reset_index().rename(columns={"date": "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    # Ensure stable ordering
    timestamps = sorted(df["timestamp"].unique())
    symbols = sorted(df["symbol"].unique())

    # Pivot to matrix [time][symbol]
    piv = df.pivot_table(index="timestamp", columns="symbol", values="signal", aggfunc="first")
    piv = piv.reindex(index=timestamps, columns=symbols)

    values = piv.to_numpy(dtype=float)

    return {
        # Use full ISO-8601 timestamps to avoid collisions when barInterval becomes intraday.
        "timestamps": [pd.Timestamp(ts).isoformat() for ts in timestamps],
        "symbols": symbols,
        "values": values.tolist(),
    }


def _range_and_hist(values: np.ndarray, bins: int = 20) -> tuple[Dict[str, float], List[int]]:
    flat = values.flatten()
    flat = flat[np.isfinite(flat)]
    if flat.size == 0:
        return {"min": 0.0, "max": 0.0}, [0] * bins

    vmin = float(np.min(flat))
    vmax = float(np.max(flat))

    hist, _ = np.histogram(flat, bins=bins)
    return {"min": vmin, "max": vmax}, hist.astype(int).tolist()


@app.post("/api/backtest/run")
def run_backtest(req: BacktestRunRequest) -> Dict[str, Any]:
    # Compile XML
    try:
        compiled = compile_alpha_from_xml(req.xml)
    except XmlCompileError as e:
        # Surface XML errors as a proper client error so the frontend can handle it.
        raise HTTPException(status_code=400, detail=str(e))

    # Determine backtest range (use IS start to OS end for now)
    start_date = req.settings.isStart
    end_date = req.settings.osEnd

    loader = DataLoader(
        file_path=config.DATA_FILE_PATH,
        start_date=start_date,
        end_date=end_date,
        time_col=config.TIME_COL,
        symbol_col=config.SYMBOL_COL,
        chunk_row_size=None,
        select_columns=getattr(config, "COLUMNS", None),
        select_symbols=SELECT_SYMBOLS,
    )

    strategy = SignalRatioStrategy()

    bt = Backtester(
        data_loader=loader,
        alpha_recipe=compiled.signal,
        strategy=strategy,
        capital=config.INITIAL_CAPITAL,
        collect_viz=True,
    )

    result, viz = bt.run(show_dashboard=False)

    heatmap = _heatmap_from_viz_symbols(viz["symbols"])
    values_np = np.array(heatmap["values"], dtype=float) if heatmap["values"] else np.array([])
    signal_range, distribution = _range_and_hist(values_np, bins=20)

    metrics = _compute_metrics(result.ret_series, result.avg_turnover)

    return {
        "heatmap": heatmap,
        "signalRange": signal_range,
        "distribution": distribution,
        "isMetrics": metrics,
        "osMetrics": {"pnl": metrics["pnl"], "sharpe": metrics["sharpe"]},
    }
