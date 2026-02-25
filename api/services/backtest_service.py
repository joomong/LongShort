"""Business logic for backtest execution."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from api.models.schemas import BacktestSettings
from framework.backtester import (
    PeriodSettings,
    PeriodResult,
    AlphaResult,
    metrics_to_api_dict,
    run_backtest as run_framework_backtest,
)
from framework.data import DataCache


# ---------------------------------------------------------------------------
# Helpers -- viz (single alpha only)
# ---------------------------------------------------------------------------


def heatmap_from_viz(viz: Dict[str, Any] | None) -> Dict[str, Any]:
    """Build heatmap JSON from backtester viz bundle.

    Supports current numpy bundle (`timestamps`, `symbols`, `signals`) and
    legacy DataFrame bundle (`symbols` DataFrame).
    """
    if not viz:
        return {"timestamps": [], "symbols": [], "values": []}

    # Fast path: numpy-native viz bundle.
    if "signals" in viz and "timestamps" in viz and "symbols" in viz:
        timestamps = np.asarray(viz["timestamps"])
        symbols = [str(s) for s in np.asarray(viz["symbols"]).tolist()]
        signals = np.asarray(viz["signals"], dtype=np.float64)
        if timestamps.size == 0 or signals.size == 0:
            return {"timestamps": [], "symbols": symbols, "values": []}
        values_list: List[List[Optional[float]]] = []
        for row in signals:
            values_list.append([None if not np.isfinite(v) else float(v) for v in row])
        return {
            "timestamps": [pd.Timestamp(ts).isoformat() for ts in timestamps],
            "symbols": symbols,
            "values": values_list,
        }

    # Legacy fallback: DataFrame with (date, symbol, signal) rows.
    sym_df = viz.get("symbols")
    if isinstance(sym_df, pd.DataFrame):
        if sym_df.empty:
            return {"timestamps": [], "symbols": [], "values": []}
        df = sym_df.copy()
        df = df.reset_index().rename(columns={"date": "timestamp"})
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        timestamps = sorted(df["timestamp"].unique())
        symbols = sorted(df["symbol"].unique())
        piv = df.pivot_table(
            index="timestamp", columns="symbol", values="signal", aggfunc="first"
        )
        piv = piv.reindex(index=timestamps, columns=symbols)
        values = piv.to_numpy(dtype=float)
        values_list: List[List[Optional[float]]] = []
        for row in values:
            values_list.append([None if not np.isfinite(v) else float(v) for v in row])
        return {
            "timestamps": [pd.Timestamp(ts).isoformat() for ts in timestamps],
            "symbols": symbols,
            "values": values_list,
        }

    return {"timestamps": [], "symbols": [], "values": []}


def range_and_hist(
    values: np.ndarray, bins: int = 100
) -> tuple[Dict[str, float], List[int]]:
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
    if not hasattr(data_cache, "arrays") or data_cache.arrays is None:
        raise ValueError(
            "DataCache is empty — no data loaded. Check config.yaml and data paths."
        )
    if len(data_cache.timestamps) == 0:
        raise ValueError(
            "DataCache is empty — no data loaded. Check config.yaml and data paths."
        )


def _ret_series_to_dict(
    timestamps: np.ndarray, values: np.ndarray
) -> Dict[str, Optional[float]]:
    """Convert timestamp + value arrays to {date_str: value} dict."""
    if timestamps.size == 0 or values.size == 0:
        return {}

    # Convert datetime64[ns, UTC] to date strings
    if timestamps.dtype.kind == "M":
        date_strings = pd.DatetimeIndex(timestamps).strftime("%Y-%m-%d")
    else:
        date_strings = timestamps.astype(str)

    return {
        str(date): (None if not np.isfinite(val) else float(val))
        for date, val in zip(date_strings, values)
    }


def _equity_series_to_dict(
    timestamps: np.ndarray, ret_series: np.ndarray, initial_capital: float
) -> Dict[str, Optional[float]]:
    """Compute equity curve from return series and return as {date_str: value} dict."""
    if ret_series.size == 0:
        return {}

    equity = np.cumprod(1 + ret_series) * initial_capital
    return _ret_series_to_dict(timestamps, equity)


def _serialize_period(period: PeriodResult, initial_capital: float) -> Dict[str, Any]:
    """Serialize a PeriodResult to API dict."""
    return {
        "metrics": metrics_to_api_dict(period.metrics),
        "retSeries": _ret_series_to_dict(period.timestamps, period.ret_series),
        "equitySeries": _equity_series_to_dict(
            period.timestamps, period.ret_series, initial_capital
        ),
        "turnoverSeries": _ret_series_to_dict(
            period.timestamps, period.turnover_series
        ),
        "minBetSeries": _ret_series_to_dict(period.timestamps, period.min_bet_series),
        "maxWeightSeries": _ret_series_to_dict(
            period.timestamps, period.max_weight_series
        ),
        "nonzeroPositionSeries": _ret_series_to_dict(
            period.timestamps, period.nonzero_position_series
        ),
        "longRatio": period.long_ratio,
        "shortRatio": period.short_ratio,
    }


def _serialize_alpha_result(
    ar: AlphaResult, initial_capital: float, xml: str | None = None
) -> Dict[str, Any]:
    """Serialize an AlphaResult to API dict."""
    return {
        "name": ar.name,
        "rank": ar.rank,
        "xml": xml,
        "isMetrics": metrics_to_api_dict(ar.is_period.metrics),
        "osMetrics": metrics_to_api_dict(ar.os_period.metrics),
        "isRetSeries": _ret_series_to_dict(
            ar.is_period.timestamps, ar.is_period.ret_series
        ),
        "osRetSeries": _ret_series_to_dict(
            ar.os_period.timestamps, ar.os_period.ret_series
        ),
        "isEquitySeries": _equity_series_to_dict(
            ar.is_period.timestamps, ar.is_period.ret_series, initial_capital
        ),
        "osEquitySeries": _equity_series_to_dict(
            ar.os_period.timestamps, ar.os_period.ret_series, initial_capital
        ),
        "isTurnoverSeries": _ret_series_to_dict(
            ar.is_period.timestamps, ar.is_period.turnover_series
        ),
        "osTurnoverSeries": _ret_series_to_dict(
            ar.os_period.timestamps, ar.os_period.turnover_series
        ),
        "isMinBetSeries": _ret_series_to_dict(
            ar.is_period.timestamps, ar.is_period.min_bet_series
        ),
        "osMinBetSeries": _ret_series_to_dict(
            ar.os_period.timestamps, ar.os_period.min_bet_series
        ),
        "isMaxWeightSeries": _ret_series_to_dict(
            ar.is_period.timestamps, ar.is_period.max_weight_series
        ),
        "osMaxWeightSeries": _ret_series_to_dict(
            ar.os_period.timestamps, ar.os_period.max_weight_series
        ),
        "isNonzeroPositionSeries": _ret_series_to_dict(
            ar.is_period.timestamps, ar.is_period.nonzero_position_series
        ),
        "osNonzeroPositionSeries": _ret_series_to_dict(
            ar.os_period.timestamps, ar.os_period.nonzero_position_series
        ),
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
    if not compiled:
        raise ValueError("No compiled alphas provided")

    capital = data_cache.initial_capital
    is_single = len(compiled) == 1

    period_settings = PeriodSettings(
        is_start=settings.isStart,
        is_end=settings.isEnd,
        os_start=settings.osStart,
        os_end=settings.osEnd,
    )

    # Respect user-selected nJobs for multi-alpha; keep single-alpha on one worker.
    n_jobs = 1 if is_single else (settings.nJobs if settings.nJobs > 0 else -1)
    multi_result = run_framework_backtest(
        alphas=compiled,
        settings=period_settings,
        capital=capital,
        data_cache=data_cache,
        mode=mode,
        n_jobs=n_jobs,
        collect_viz=is_single,
    )

    combined = multi_result.combined_result
    xml_by_idx: Dict[int, str] = {}
    if compiled_xmls:
        for i, xml in enumerate(compiled_xmls):
            xml_by_idx[i] = xml

    response = {
        "combined": {
            "isMetrics": metrics_to_api_dict(combined.is_period.metrics),
            "osMetrics": metrics_to_api_dict(combined.os_period.metrics),
            "isRetSeries": _ret_series_to_dict(
                combined.is_period.timestamps, combined.is_period.ret_series
            ),
            "osRetSeries": _ret_series_to_dict(
                combined.os_period.timestamps, combined.os_period.ret_series
            ),
        },
        "initialCapital": capital,
        "alphas": [
            _serialize_alpha_result(
                ar,
                capital,
                xml_by_idx.get(ar.source_idx),
            )
            for ar in multi_result.alpha_results
        ],
        "sortedBy": multi_result.sorted_by,
    }

    # Multi-alpha: insert Combined as first entry in alphas array (rank=0)
    if not is_single:
        combined_as_alpha = AlphaResult(
            name="Combined",
            is_period=combined.is_period,
            os_period=combined.os_period,
            source_idx=-1,
            rank=0,
        )
        combined_serialized = _serialize_alpha_result(combined_as_alpha, capital, xml=None)
        response["alphas"] = [combined_serialized] + response["alphas"]

    if is_single:
        viz = multi_result.viz_bundle
        heatmap = heatmap_from_viz(viz)
        if viz and "signals" in viz:
            raw_signals = np.asarray(viz["signals"], dtype=float)
        elif viz and "symbols" in viz and isinstance(viz["symbols"], pd.DataFrame):
            raw_signals = viz["symbols"]["signal"].to_numpy(dtype=float)
        else:
            raw_signals = np.array([])
        signal_range, distribution = range_and_hist(raw_signals)
        response["heatmap"] = heatmap
        response["signalRange"] = signal_range
        response["distribution"] = distribution

    return response
