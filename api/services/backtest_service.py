"""Business logic for backtest execution."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from api.models.schemas import BacktestSettings
from framework.backtester import (
    Backtester,
    PeriodSettings,
    PeriodResult,
    BacktestPeriodResult,
    AlphaResult,
    MultipleBacktestResult,
    metrics_to_api_dict,
    run_multiple_backtest,
    _build_period_result,
    _split_series_by_date,
)
from framework.data import DataCache


# ---------------------------------------------------------------------------
# Helpers -- viz (single alpha only)
# ---------------------------------------------------------------------------

def heatmap_from_viz_symbols(sym_df: pd.DataFrame) -> Dict[str, Any]:
    """Build heatmap JSON from backtester viz DataFrame."""
    if sym_df.empty:
        return {"timestamps": [], "symbols": [], "values": []}

    df = sym_df.copy()
    df = df.reset_index().rename(columns={"date": "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    timestamps = sorted(df["timestamp"].unique())
    symbols = sorted(df["symbol"].unique())

    piv = df.pivot_table(index="timestamp", columns="symbol", values="signal", aggfunc="first")
    piv = piv.reindex(index=timestamps, columns=symbols)

    col_mean = piv.mean(axis=0)
    col_std = piv.std(axis=0).replace(0, np.nan)
    piv = (piv - col_mean) / col_std

    values = piv.to_numpy(dtype=float)

    values_list: List[List[Optional[float]]] = []
    for row in values:
        values_list.append([None if not np.isfinite(v) else float(v) for v in row])

    return {
        "timestamps": [pd.Timestamp(ts).isoformat() for ts in timestamps],
        "symbols": symbols,
        "values": values_list,
    }


def range_and_hist(values: np.ndarray, bins: int = 100) -> tuple[Dict[str, float], List[int]]:
    """Compute signal range and histogram from raw signal values."""
    flat = values.flatten()
    flat = flat[np.isfinite(flat)]
    if flat.size == 0:
        return {"min": 0.0, "max": 0.0}, [0] * bins

    vmin = float(np.min(flat))
    vmax = float(np.max(flat))

    hist, _ = np.histogram(flat, bins=bins)
    return {"min": vmin, "max": vmax}, hist.astype(int).tolist()


# ---------------------------------------------------------------------------
# Helpers -- common
# ---------------------------------------------------------------------------

def resolve_mode(bar_interval: str) -> str:
    """Map API barInterval string to backtest mode."""
    if bar_interval == "intraday":
        return "D0"
    if bar_interval in {"D0", "D1"}:
        return bar_interval
    if bar_interval.lower() == "1d":
        return "D1"
    raise ValueError(f"Unknown barInterval: {bar_interval!r}")


def _validate_data_cache(data_cache: DataCache) -> None:
    """Raise if DataCache has no data."""
    if not hasattr(data_cache, 'arrays') or data_cache.arrays is None:
        raise ValueError("DataCache is empty — no data loaded. Check config.yaml and data paths.")
    if len(data_cache.timestamps) == 0:
        raise ValueError("DataCache is empty — no data loaded. Check config.yaml and data paths.")


def _ret_series_to_dict(series: pd.Series) -> Dict[str, float]:
    """Convert return series to {date_str: value} dict."""
    return {str(k): float(v) for k, v in series.items()}


def _equity_series_to_dict(ret_series: pd.Series, initial_capital: float) -> Dict[str, float]:
    """Compute equity curve from return series and return as {date_str: value} dict."""
    equity = (1 + ret_series).cumprod() * initial_capital
    return {str(k): float(v) for k, v in equity.items()}


def _serialize_period(period: PeriodResult, initial_capital: float) -> Dict[str, Any]:
    """Serialize a PeriodResult to API dict."""
    return {
        "metrics": metrics_to_api_dict(period.metrics),
        "retSeries": _ret_series_to_dict(period.ret_series),
        "equitySeries": _equity_series_to_dict(period.ret_series, initial_capital),
        "turnoverSeries": _ret_series_to_dict(period.turnover_series),
        "minBetSeries": _ret_series_to_dict(period.min_bet_series),
        "maxWeightSeries": _ret_series_to_dict(period.max_weight_series),
        "longRatio": period.long_ratio,
        "shortRatio": period.short_ratio,
    }


def _serialize_alpha_result(ar: AlphaResult, initial_capital: float, xml: str | None = None) -> Dict[str, Any]:
    """Serialize an AlphaResult to API dict."""
    return {
        "name": ar.name,
        "rank": ar.rank,
        "xml": xml,
        "isMetrics": metrics_to_api_dict(ar.is_period.metrics),
        "osMetrics": metrics_to_api_dict(ar.os_period.metrics),
        "isRetSeries": _ret_series_to_dict(ar.is_period.ret_series),
        "osRetSeries": _ret_series_to_dict(ar.os_period.ret_series),
        "isEquitySeries": _equity_series_to_dict(ar.is_period.ret_series, initial_capital),
        "osEquitySeries": _equity_series_to_dict(ar.os_period.ret_series, initial_capital),
        "isTurnoverSeries": _ret_series_to_dict(ar.is_period.turnover_series),
        "osTurnoverSeries": _ret_series_to_dict(ar.os_period.turnover_series),
        "isMinBetSeries": _ret_series_to_dict(ar.is_period.min_bet_series),
        "osMinBetSeries": _ret_series_to_dict(ar.os_period.min_bet_series),
        "isMaxWeightSeries": _ret_series_to_dict(ar.is_period.max_weight_series),
        "osMaxWeightSeries": _ret_series_to_dict(ar.os_period.max_weight_series),
        "longRatio": ar.is_period.long_ratio,
        "shortRatio": ar.is_period.short_ratio,
    }


# ---------------------------------------------------------------------------
# Unified backtest entry point
# ---------------------------------------------------------------------------

def run_backtest(
    compiled: list,
    compiled_xmls: list[str] | None,
    settings: BacktestSettings,
    mode: str,
    data_cache: DataCache,
) -> Dict[str, Any]:
    """Unified backtest execution for both single and multi alpha."""
    _validate_data_cache(data_cache)

    capital = data_cache.initial_capital
    is_single = len(compiled) == 1

    if is_single:
        return _run_single(compiled[0], compiled_xmls[0] if compiled_xmls else None, settings, mode, data_cache)

    return _run_multiple(compiled, compiled_xmls, settings, mode, data_cache)


def _run_single(compiled_signal, compiled_xml: str | None, settings: BacktestSettings, mode: str, data_cache: DataCache) -> Dict[str, Any]:
    """Single alpha: heatmap + distribution + metrics."""
    capital = data_cache.initial_capital

    bt = Backtester(
        cache=data_cache,
        alpha_recipe=compiled_signal,
        capital=capital,
        mode=mode,
        start_date=settings.isStart,
        end_date=settings.osEnd,
    )

    ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

    heatmap = heatmap_from_viz_symbols(viz["symbols"]) if viz else {"timestamps": [], "symbols": [], "values": []}
    raw_signals = viz["symbols"]["signal"].to_numpy(dtype=float) if viz and not viz["symbols"].empty else np.array([])
    signal_range, distribution = range_and_hist(raw_signals)

    is_ret, os_ret = _split_series_by_date(ret_s, settings.osStart)
    is_to, os_to = _split_series_by_date(to_s, settings.osStart)
    is_mb, os_mb = _split_series_by_date(mb_s, settings.osStart)
    is_mw, os_mw = _split_series_by_date(mw_s, settings.osStart)
    is_lw, os_lw = _split_series_by_date(lw_s, settings.osStart)
    is_sw, os_sw = _split_series_by_date(sw_s, settings.osStart)

    is_period = _build_period_result(is_ret, is_to, is_mb, is_mw, is_lw, is_sw)
    os_period = _build_period_result(os_ret, os_to, os_mb, os_mw, os_lw, os_sw)

    alpha_result = AlphaResult(name="alpha_0", is_period=is_period, os_period=os_period, rank=1)

    return {
        "combined": None,
        "initialCapital": capital,
        "alphas": [_serialize_alpha_result(alpha_result, capital, compiled_xml)],
        "heatmap": heatmap,
        "signalRange": signal_range,
        "distribution": distribution,
        "sortedBy": "sharpe",
    }


def _run_multiple(compiled_alphas: list, compiled_xmls: list[str] | None, settings: BacktestSettings, mode: str, data_cache: DataCache) -> Dict[str, Any]:
    """Multiple alphas: metrics + combined."""
    capital = data_cache.initial_capital

    period_settings = PeriodSettings(
        is_start=settings.isStart,
        is_end=settings.isEnd,
        os_start=settings.osStart,
        os_end=settings.osEnd,
    )

    n_jobs = -1 if len(compiled_alphas) > 2 else 1
    multi_result = run_multiple_backtest(
        alphas=compiled_alphas,
        settings=period_settings,
        capital=capital,
        data_cache=data_cache,
        mode=mode,
        n_jobs=n_jobs,
    )

    combined = multi_result.combined_result
    response = {
        "combined": {
            "isMetrics": metrics_to_api_dict(combined.is_period.metrics),
            "osMetrics": metrics_to_api_dict(combined.os_period.metrics),
            "isRetSeries": _ret_series_to_dict(combined.is_period.ret_series),
            "osRetSeries": _ret_series_to_dict(combined.os_period.ret_series),
        },
        "initialCapital": capital,
        "alphas": [
            _serialize_alpha_result(
                ar, capital,
                compiled_xmls[i] if compiled_xmls and i < len(compiled_xmls) else None,
            )
            for i, ar in enumerate(multi_result.alpha_results)
        ],
        "sortedBy": multi_result.sorted_by,
    }

    return response
