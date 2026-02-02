"""Business logic for backtest execution."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from api.models.schemas import BacktestSettings
from framework.backtester import (
    BacktestResult,
    Backtester,
    PeriodSettings,
    SignalRatioStrategy,
    _compute_period_metrics,
    _split_result_by_date,
    metrics_to_api_dict,
    run_multiple_backtest,
)
from framework.data_cache import DataCache, DataCacheIterator


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

    # Per-symbol time-series z-score
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
    """Compute signal range and histogram from raw signal values.

    No transformation is applied — the histogram represents the raw
    distribution of signal values as-is.
    """
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


def build_data_loader_kwargs(cache: DataCache) -> dict:
    """Common DataLoader kwargs derived from DataCache config."""
    return dict(
        file_path=cache.dataset_paths[0],
        time_col=cache.time_col,
        symbol_col=cache.symbol_col,
        chunk_row_size=None,
        select_columns=cache.ohlcv_columns,
        select_symbols=cache.symbols,
    )



# ---------------------------------------------------------------------------
# Single alpha: heatmap + distribution + metrics
# ---------------------------------------------------------------------------

def _validate_data_cache(data_cache: DataCache) -> None:
    """Raise if DataCache has no data."""
    if len(data_cache.get_timestamps()) == 0:
        raise ValueError("DataCache is empty — no data loaded. Check config.yaml and data paths.")


def _ret_series_to_dict(series: pd.Series) -> Dict[str, float]:
    """Convert return series to {date_str: value} dict."""
    return {str(k): float(v) for k, v in series.items()}


def _equity_series_to_dict(ret_series: pd.Series, initial_capital: float) -> Dict[str, float]:
    """Compute equity curve from return series and return as {date_str: value} dict."""
    equity = (1 + ret_series).cumprod() * initial_capital
    return {str(k): float(v) for k, v in equity.items()}


def run_single_alpha(compiled_signal, settings: BacktestSettings, mode: str, data_cache: DataCache, compiled_xml: str | None = None) -> Dict[str, Any]:
    _validate_data_cache(data_cache)
    # Run on full range (IS+OS) for heatmap viz
    iterator = DataCacheIterator(data_cache, start_date=settings.isStart, end_date=settings.osEnd)

    bt = Backtester(
        data_loader=iterator,
        alpha_recipe=compiled_signal,
        strategy=SignalRatioStrategy(),
        capital=data_cache.initial_capital,
        mode=mode,
        transaction_fee_rate=data_cache.transaction_fee_rate,
    )

    result, viz = bt.run()

    heatmap = heatmap_from_viz_symbols(viz["symbols"])

    # Histogram uses raw signal values (no z-score transformation)
    raw_signals = viz["symbols"]["signal"].to_numpy(dtype=float) if not viz["symbols"].empty else np.array([])
    signal_range, distribution = range_and_hist(raw_signals)

    # Split result into IS and OS by osStart date
    is_result, os_result = _split_result_by_date(result, settings.osStart)
    is_metrics = metrics_to_api_dict(_compute_period_metrics(is_result))
    os_metrics = metrics_to_api_dict(_compute_period_metrics(os_result))

    # Compute long/short ratios from IS result
    long_ratio = 0.0
    short_ratio = 0.0
    if is_result.long_weight_series is not None and len(is_result.long_weight_series) > 0:
        total = is_result.long_weight_series + is_result.short_weight_series
        safe_total = total.replace(0, np.nan)
        long_ratio = float((is_result.long_weight_series / safe_total).mean())
        short_ratio = float((is_result.short_weight_series / safe_total).mean())
        if np.isnan(long_ratio):
            long_ratio = 0.0
        if np.isnan(short_ratio):
            short_ratio = 0.0

    capital = data_cache.initial_capital

    return {
        "combined": None,
        "initialCapital": capital,
        "alphas": [
            {
                "name": "alpha_0",
                "rank": 1,
                "xml": compiled_xml,
                "isMetrics": is_metrics,
                "osMetrics": os_metrics,
                "isRetSeries": _ret_series_to_dict(is_result.ret_series),
                "osRetSeries": _ret_series_to_dict(os_result.ret_series),
                "isEquitySeries": _equity_series_to_dict(is_result.ret_series, capital),
                "osEquitySeries": _equity_series_to_dict(os_result.ret_series, capital),
                "isTurnoverSeries": _ret_series_to_dict(is_result.turnover_series) if is_result.turnover_series is not None else None,
                "osTurnoverSeries": _ret_series_to_dict(os_result.turnover_series) if os_result.turnover_series is not None else None,
                "isMinBetSeries": _ret_series_to_dict(is_result.min_bet_series) if is_result.min_bet_series is not None else None,
                "osMinBetSeries": _ret_series_to_dict(os_result.min_bet_series) if os_result.min_bet_series is not None else None,
                "isMaxWeightSeries": _ret_series_to_dict(is_result.max_weight_series) if is_result.max_weight_series is not None else None,
                "osMaxWeightSeries": _ret_series_to_dict(os_result.max_weight_series) if os_result.max_weight_series is not None else None,
                "longRatio": long_ratio,
                "shortRatio": short_ratio,
            }
        ],
        "heatmap": heatmap,
        "signalRange": signal_range,
        "distribution": distribution,
        "sortedBy": "sharpe",
    }


# ---------------------------------------------------------------------------
# Multiple alphas: metrics + combined (no heatmap/distribution)
# Future-ready: called when XML compiler returns multiple alphas.
# ---------------------------------------------------------------------------

def run_multiple_alphas(
    compiled_alphas: list,
    compiled_xmls: list[str] | None,
    settings: BacktestSettings,
    mode: str,
    data_cache: DataCache,
) -> Dict[str, Any]:
    _validate_data_cache(data_cache)
    period_settings = PeriodSettings(
        is_start=settings.isStart,
        is_end=settings.isEnd,
        os_start=settings.osStart,
        os_end=settings.osEnd,
    )

    n_jobs = -1 if len(compiled_alphas) > 2 else 1
    multi_result = run_multiple_backtest(
        alphas=compiled_alphas,
        strategy=SignalRatioStrategy(),
        settings=period_settings,
        capital=data_cache.initial_capital,
        data_cache=data_cache,
        data_loader_kwargs=build_data_loader_kwargs(data_cache) if n_jobs != 1 else None,
        mode=mode,
        n_jobs=n_jobs,
        transaction_fee_rate=data_cache.transaction_fee_rate,
    )

    capital = data_cache.initial_capital
    response = multi_result.to_dict(initial_capital=capital)

    # Attach per-alpha expanded XML by index (best-effort).
    if compiled_xmls:
        for i, ar in enumerate(response.get("alphas", [])):
            if i < len(compiled_xmls):
                ar["xml"] = compiled_xmls[i]

    response["sortedBy"] = multi_result.sorted_by
    return response
