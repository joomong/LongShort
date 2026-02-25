from __future__ import annotations

from dataclasses import dataclass

import numpy as np


BARS_PER_YEAR = 365
EPS = 1e-12


@dataclass(frozen=True)
class OracleMetrics:
    pnl: float
    cagr: float
    sharpe: float
    mdd: float
    turnover: float
    min_bet: float
    max_weight: float
    nonzero_position: int


def _finite_mean(arr: np.ndarray) -> float:
    if arr.size == 0:
        return 0.0
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return 0.0
    return float(np.mean(finite))


def _finite_max(arr: np.ndarray) -> float:
    if arr.size == 0:
        return 0.0
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return 0.0
    return float(np.max(finite))


def compute_metrics_from_series(
    ret_series: np.ndarray,
    turnover_series: np.ndarray,
    min_bet_series: np.ndarray,
    max_weight_series: np.ndarray,
    *,
    long_weight_series: np.ndarray | None = None,
    short_weight_series: np.ndarray | None = None,
    nonzero_count_series: np.ndarray | None = None,
) -> OracleMetrics:
    """Independent implementation of framework.backtester._compute_metrics()."""
    r = np.asarray(ret_series, dtype=np.float64)
    finite = np.isfinite(r)
    if not np.any(finite):
        return OracleMetrics(
            pnl=0.0,
            cagr=0.0,
            sharpe=0.0,
            mdd=0.0,
            turnover=0.0,
            min_bet=0.0,
            max_weight=0.0,
            nonzero_position=0,
        )
    r = r[finite]

    vol = float(np.nanstd(r, ddof=1)) if r.size >= 2 else 0.0
    sharpe = float((np.nanmean(r) / (vol + EPS)) * np.sqrt(BARS_PER_YEAR)) if vol > 0 else 0.0

    equity = np.cumprod(1.0 + r)
    pnl = float(equity[-1] - 1.0)
    cagr = float(equity[-1] ** (BARS_PER_YEAR / max(r.size, 1)) - 1.0)
    dd = equity / (np.maximum.accumulate(equity) + EPS) - 1.0
    mdd = float(np.nanmin(dd))

    to = _finite_mean(np.asarray(turnover_series, dtype=np.float64))
    mb = _finite_mean(np.asarray(min_bet_series, dtype=np.float64))
    mw = _finite_max(np.asarray(max_weight_series, dtype=np.float64))

    nonzero_position = 0
    if nonzero_count_series is not None and nonzero_count_series.size > 0:
        nz = np.asarray(nonzero_count_series, dtype=np.float64)
        nonzero_position = int(np.sum(nz > 0))
    elif long_weight_series is not None and short_weight_series is not None:
        lw = np.asarray(long_weight_series, dtype=np.float64)
        sw = np.asarray(short_weight_series, dtype=np.float64)
        nonzero_position = int(np.nansum((lw > 0) | (sw > 0)))

    return OracleMetrics(
        pnl=pnl,
        cagr=cagr,
        sharpe=sharpe,
        mdd=mdd,
        turnover=to,
        min_bet=mb,
        max_weight=mw,
        nonzero_position=nonzero_position,
    )


def to_api_units(metrics: OracleMetrics) -> dict[str, float | int]:
    """Independent implementation of framework.backtester.metrics_to_api_dict()."""
    out: dict[str, float | int] = {
        "pnl": metrics.pnl,
        "cagr": metrics.cagr,
        "sharpe": metrics.sharpe,
        "mdd": metrics.mdd,
        "turnover": metrics.turnover,
        "min_bet": metrics.min_bet,
        "max_weight": metrics.max_weight,
        "nonzero_position": metrics.nonzero_position,
    }
    for key in ("pnl", "cagr", "mdd", "turnover"):
        out[key] = float(out[key]) * 100.0
    return out


@dataclass(frozen=True)
class OracleBacktestSeries:
    returns: np.ndarray
    ret_series: np.ndarray
    turnover: np.ndarray
    min_bet: np.ndarray
    max_weight: np.ndarray
    long_weight: np.ndarray
    short_weight: np.ndarray
    nonzero_count: np.ndarray


def simulate_backtest_series(
    close_2d: np.ndarray,
    signals_2d: np.ndarray,
    *,
    fee_rate: float,
    mode: str,
) -> OracleBacktestSeries:
    """Independent implementation of framework.backtester._backtest_loop()."""
    close_2d = np.asarray(close_2d, dtype=np.float64)
    signals_2d = np.asarray(signals_2d, dtype=np.float64)
    if close_2d.shape != signals_2d.shape:
        raise ValueError("close_2d and signals_2d must have the same shape")
    if mode not in {"D0", "D1"}:
        raise ValueError(f"Unknown mode: {mode!r}")

    T, N = close_2d.shape

    ret = np.zeros(T, dtype=np.float64)
    to = np.zeros(T, dtype=np.float64)
    mb = np.zeros(T, dtype=np.float64)
    mw = np.zeros(T, dtype=np.float64)
    lw = np.zeros(T, dtype=np.float64)
    sw = np.zeros(T, dtype=np.float64)
    nz = np.zeros(T, dtype=np.float64)

    prev_weights = np.zeros(N, dtype=np.float64)
    prev_target = np.zeros(N, dtype=np.float64)

    for t in range(T):
        cur_close = close_2d[t]

        if t > 0:
            prev_close = close_2d[t - 1]
            port_ret = 0.0
            for i in range(N):
                p = prev_close[i]
                c = cur_close[i]
                if p > 0.0 and c > 0.0:
                    port_ret += prev_weights[i] * (c / p - 1.0)
            ret[t] = port_ret

        abs_sum = 0.0
        for i in range(N):
            c = cur_close[i]
            s = signals_2d[t, i]
            if np.isfinite(c) and np.isfinite(s):
                abs_sum += abs(s)

        target = np.zeros(N, dtype=np.float64)
        if abs_sum > EPS:
            for i in range(N):
                c = cur_close[i]
                s = signals_2d[t, i]
                if np.isfinite(c) and np.isfinite(s):
                    target[i] = s / abs_sum

        exec_w = target if mode == "D0" else prev_target

        turnover = float(np.sum(np.abs(exec_w - prev_weights)))
        to[t] = turnover
        ret[t] -= turnover * fee_rate

        abs_w = np.abs(exec_w)
        mb[t] = float(abs_w.sum() / N) if N > 0 else 0.0
        mw[t] = float(abs_w.max()) if N > 0 else 0.0
        lw[t] = float(exec_w[exec_w > 0.0].sum())
        sw[t] = float((-exec_w[exec_w < 0.0]).sum())
        nz[t] = float(np.count_nonzero(abs_w > EPS))

        prev_weights = exec_w.copy()
        prev_target = target.copy()

    ret_series = ret[1:] if T > 1 else np.array([], dtype=np.float64)
    return OracleBacktestSeries(
        returns=ret,
        ret_series=ret_series,
        turnover=to,
        min_bet=mb,
        max_weight=mw,
        long_weight=lw,
        short_weight=sw,
        nonzero_count=nz,
    )
