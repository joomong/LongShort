# STAR/framework/backtester.py
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional
import warnings

import numpy as np
import pandas as pd

import numba


BARS_PER_YEAR = 365


# ===========================================================================
# IS/OS Period Settings and Results
# ===========================================================================


@dataclass(frozen=True)
class PeriodSettings:
    """IS/OS 기간 설정."""

    is_start: str  # "YYYY-MM-DD"
    is_end: str
    os_start: str
    os_end: str


@dataclass(frozen=True)
class PeriodMetrics:
    """Unified period metrics for both IS and OS."""

    pnl: float
    cagr: float
    sharpe: float
    mdd: float
    turnover: float = 0.0
    min_bet: float = 0.0
    max_weight: float = 0.0
    nonzero_position: int = 0
    ir: float = float("nan")
    corr: float = float("nan")


@dataclass(frozen=True)
class PeriodResult:
    """단일 기간(IS 또는 OS) 백테스트 결과."""

    metrics: PeriodMetrics
    timestamps: np.ndarray  # datetime64[ns, UTC]
    ret_series: np.ndarray
    turnover_series: np.ndarray
    min_bet_series: np.ndarray
    max_weight_series: np.ndarray
    nonzero_position_series: np.ndarray = field(
        default_factory=lambda: np.array([], dtype=np.int16)
    )
    long_ratio: float = 0.0
    short_ratio: float = 0.0


@dataclass(frozen=True)
class BacktestPeriodResult:
    """IS/OS 분리 백테스트 결과."""

    is_period: PeriodResult
    os_period: PeriodResult


def _compute_metrics(
    ret_series: np.ndarray,
    turnover_series: np.ndarray,
    min_bet_series: np.ndarray,
    max_weight_series: np.ndarray,
    long_weight_series: np.ndarray = None,
    short_weight_series: np.ndarray = None,
    nz_series: np.ndarray = None,
) -> PeriodMetrics:
    """Compute PeriodMetrics directly from numpy arrays."""
    r = np.asarray(ret_series, dtype=np.float64)
    finite_r = np.isfinite(r)
    if not np.any(finite_r):
        return PeriodMetrics(pnl=0.0, cagr=0.0, sharpe=0.0, mdd=0.0)
    r = r[finite_r]

    # Match pandas std default (ddof=1) used by legacy metric checks.
    vol = float(np.nanstd(r, ddof=1)) if r.size >= 2 else 0.0
    sharpe = (
        (np.nanmean(r) / (vol + 1e-12)) * np.sqrt(BARS_PER_YEAR) if vol > 0 else 0.0
    )
    equity = np.cumprod(1.0 + r)
    total_return = float(equity[-1] - 1.0)
    n_days = r.shape[0]
    cagr = float(equity[-1] ** (BARS_PER_YEAR / max(n_days, 1)) - 1.0)
    dd = equity / (np.maximum.accumulate(equity) + 1e-12) - 1.0
    mdd = float(np.nanmin(dd))

    def _finite_mean(arr: np.ndarray) -> float:
        if arr.size == 0:
            return 0.0
        finite = arr[np.isfinite(arr)]
        return float(np.mean(finite)) if finite.size > 0 else 0.0

    def _finite_max(arr: np.ndarray) -> float:
        if arr.size == 0:
            return 0.0
        finite = arr[np.isfinite(arr)]
        return float(np.max(finite)) if finite.size > 0 else 0.0

    turnover = _finite_mean(np.asarray(turnover_series, dtype=np.float64))
    min_bet = _finite_mean(np.asarray(min_bet_series, dtype=np.float64))
    max_weight = _finite_max(np.asarray(max_weight_series, dtype=np.float64))

    # Calculate nonzero_position: count timestamps where any position exists
    nonzero_position = 0
    if nz_series is not None and nz_series.size > 0:
        nonzero_position = int(np.sum(nz_series > 0))
    elif long_weight_series is not None and short_weight_series is not None:
        nonzero_position = int(
            np.nansum((long_weight_series > 0) | (short_weight_series > 0))
        )

    return PeriodMetrics(
        pnl=total_return,
        cagr=cagr,
        sharpe=sharpe,
        mdd=mdd,
        turnover=turnover,
        min_bet=min_bet,
        max_weight=max_weight,
        nonzero_position=nonzero_position,
    )


def metrics_to_api_dict(m: PeriodMetrics) -> dict:
    """PeriodMetrics → API dict with frontend-ready units."""
    d: dict = {}
    for f in m.__dataclass_fields__:
        v = getattr(m, f)
        if isinstance(v, float) and np.isnan(v):
            continue
        d[f] = v

    for key in ("pnl", "cagr", "mdd", "turnover"):
        if key in d:
            d[key] *= 100.0

    return d


# ===========================================================================
# Backtest Loop Kernel (Numba JIT or numpy fallback)
# ===========================================================================


@numba.jit(nopython=True, cache=True)
def _backtest_loop(close_2d, signals_2d, fee_rate, mode_int):
    """Numba-JIT backtest loop kernel."""
    T, N = close_2d.shape
    ret = np.zeros(T)
    turnover = np.zeros(T)
    min_bet = np.zeros(T)
    max_weight = np.zeros(T)
    long_weight = np.zeros(T)
    short_weight = np.zeros(T)
    nonzero_count = np.zeros(T)

    prev_weights = np.zeros(N)
    prev_target = np.zeros(N)

    for t in range(T):
        cur_close = close_2d[t]

        # Portfolio return from previous weights
        if t > 0:
            prev_close = close_2d[t - 1]
            port_ret = 0.0
            for i in range(N):
                if prev_close[i] > 0.0 and cur_close[i] > 0.0:
                    port_ret += prev_weights[i] * (cur_close[i] / prev_close[i] - 1.0)
            ret[t] = port_ret

        # Normalize signals -> target weights.
        # A symbol is tradable for weighting when close is finite and signal is finite.
        abs_sum = 0.0
        for i in range(N):
            c = cur_close[i]
            s = signals_2d[t, i]
            if c == c and s == s:
                abs_sum += abs(s)

        target = np.zeros(N)
        if abs_sum > 1e-12:
            for i in range(N):
                c = cur_close[i]
                s = signals_2d[t, i]
                if c == c and s == s:
                    target[i] = s / abs_sum

        # Execution weights depend on mode
        if mode_int == 0:
            exec_w = target
        else:
            exec_w = prev_target

        # Turnover
        to = 0.0
        for i in range(N):
            to += abs(exec_w[i] - prev_weights[i])
        turnover[t] = to
        ret[t] -= to * fee_rate

        # Min bet (mean absolute weight) and max weight
        abs_w_sum = 0.0
        abs_w_max = 0.0
        lw = 0.0
        sw = 0.0
        nz = 0
        for i in range(N):
            aw = abs(exec_w[i])
            abs_w_sum += aw
            if aw > abs_w_max:
                abs_w_max = aw
            if exec_w[i] > 0.0:
                lw += exec_w[i]
            elif exec_w[i] < 0.0:
                sw += -exec_w[i]
            if aw > 1e-12:
                nz += 1

        min_bet[t] = abs_w_sum / N if N > 0 else 0.0
        max_weight[t] = abs_w_max
        long_weight[t] = lw
        short_weight[t] = sw
        nonzero_count[t] = nz

        prev_weights = exec_w.copy()
        prev_target = target.copy()

    return ret, turnover, min_bet, max_weight, long_weight, short_weight, nonzero_count


class Backtester:
    """Backtest engine — numpy-native path.

    Key conventions:
    - Portfolio return is computed with *previous executed* weights to avoid lookahead.
    - D0: compute weights at bar t close, execute at bar t close → return realized on bar t+1.
    - D1: compute weights at bar t close, execute at bar t+1 close → return realized on bar t+2.
    - Turnover = Σ|Δw| (range 0~2 when gross=1).

    Requires cache= DataCache (numpy path).
    """

    def __init__(
        self,
        alpha_recipe=None,
        capital: float = 0.0,
        *,
        cache=None,
        mode: str = "D0",  # D0 | D1 | intraday
        collect_viz: bool = True,
        start_date: str | None = None,
        end_date: str | None = None,
    ):
        self.cache = cache
        self.alpha = alpha_recipe

        self.initial_capital = float(capital)

        self.mode = str(mode)
        self.collect_viz = bool(collect_viz)
        self.transaction_fee_rate = float(cache.transaction_fee_rate)
        self.start_date = start_date
        self.end_date = end_date

    @staticmethod
    def _validate_alpha_output(
        raw: np.ndarray, *, t_idx: int, n_symbols: int
    ) -> np.ndarray:
        """Validate alpha output."""
        if not isinstance(raw, np.ndarray):
            raw = np.asarray(raw, dtype=np.float64)
        if raw.ndim != 1 or len(raw) != n_symbols:
            raise ValueError(
                f"Alpha.compute() returned array of shape {raw.shape}, expected ({n_symbols},) at t_idx={t_idx}"
            )
        if np.any(np.isinf(raw)):
            raise ValueError(f"Alpha.compute() returned +/-inf values at t_idx={t_idx}")
        return raw.astype(np.float64)

    def run(self):
        """2-pass backtest: signal DAG (Python) → backtest loop (Numba)."""
        # Reset signal state to ensure clean execution (prevents state pollution
        # when the same Signal object is reused across multiple backtest runs).
        reset_fn = getattr(self.alpha, 'reset', None)
        if reset_fn:
            reset_fn()

        cache = self.cache
        timestamps = cache.timestamps
        symbols = cache.symbols
        N = len(symbols)
        T = len(timestamps)

        # Determine start/end indices
        ts_pd = pd.DatetimeIndex(timestamps)
        if ts_pd.tz is None:
            ts_pd = ts_pd.tz_localize("UTC")

        if self.start_date:
            start_ts = pd.Timestamp(self.start_date, tz="UTC")
            start_idx = int(ts_pd.searchsorted(start_ts, side="left"))
        else:
            start_idx = 0

        if self.end_date:
            end_ts = (
                pd.Timestamp(self.end_date, tz="UTC")
                + pd.Timedelta(days=1)
                - pd.Timedelta(milliseconds=1)
            )
            end_idx = int(ts_pd.searchsorted(end_ts, side="right")) - 1
        else:
            end_idx = T - 1

        start_idx = max(0, min(start_idx, T))
        end_idx = max(-1, min(end_idx, T - 1))
        if start_idx > end_idx:
            raise ValueError("No data in requested date range.")

        if self.mode not in {"D0", "D1", "intraday"}:
            raise ValueError(f"Unknown backtest mode: {self.mode!r}")
        if self.mode == "intraday":
            raise NotImplementedError("intraday mode is not implemented yet")

        # Build arrays dict for compute calls
        arrays_for_compute = dict(cache.arrays)
        arrays_for_compute["__n_symbols__"] = N
        arrays_for_compute["__symbols__"] = symbols

        n_bars = end_idx - start_idx + 1

        # === Pass 1: Signal DAG execution (Python) ===
        signals_2d = np.zeros((n_bars, N), dtype=np.float64)
        signals_raw = np.full((n_bars, N), np.nan) if self.collect_viz else None

        for bar_i, t_idx in enumerate(range(start_idx, end_idx + 1)):
            raw = self.alpha.compute(t_idx, arrays_for_compute)
            raw = self._validate_alpha_output(raw, t_idx=t_idx, n_symbols=N)
            if signals_raw is not None:
                signals_raw[bar_i] = raw
            signals_2d[bar_i] = raw

        # === Pass 2: Backtest loop (Numba) ===
        close_2d = cache.arrays["close"][start_idx : end_idx + 1]
        mode_int = 0 if self.mode == "D0" else 1

        ret_arr, to_arr, mb_arr, mw_arr, lw_arr, sw_arr, nz_arr = _backtest_loop(
            close_2d, signals_2d, self.transaction_fee_rate, mode_int
        )

        # Build numpy result arrays
        ts_arr = timestamps[start_idx : end_idx + 1]
        # Ensure UTC timezone for timestamps
        if ts_arr.dtype.kind != "M":
            ts_arr = ts_arr.astype("datetime64[ns]")
        # First bar has no return from prev close — drop only from ret_series
        ret_arr = ret_arr[1:] if len(ts_arr) > 1 else np.array([], dtype=np.float64)
        # Align timestamps with ret_arr (drop first timestamp)
        ts_arr_ret = (
            ts_arr[1:] if len(ts_arr) > 1 else np.array([], dtype="datetime64[ns]")
        )

        # Convert to UTC timezone
        # Convert to UTC timezone - pandas handles timezone conversion better
        if ts_arr_ret.size > 0:
            ts_arr_ret = pd.DatetimeIndex(ts_arr_ret).tz_localize("UTC").values
        ts_arr_full = np.array([], dtype="datetime64[ns]")
        if ts_arr.size > 0:
            ts_arr_full = pd.DatetimeIndex(ts_arr).tz_localize("UTC").values

        viz_bundle = None
        if self.collect_viz:
            viz_bundle = {
                "timestamps": ts_arr_full,
                "symbols": np.asarray(symbols, dtype=object),
                "signals": signals_raw,
            }

        return (
            ts_arr_ret,  # timestamps for return series
            ret_arr,  # return values
            to_arr,  # turnover (full length)
            mb_arr,  # min_bet (full length)
            mw_arr,  # max_weight (full length)
            lw_arr,  # long_weight (full length)
            sw_arr,  # short_weight (full length)
            nz_arr,  # nonzero_count (full length)
            viz_bundle,
        )


def _build_period_result(
    timestamps: np.ndarray,
    ret_series: np.ndarray,
    to_series: np.ndarray,
    mb_series: np.ndarray,
    mw_series: np.ndarray,
    lw_series: np.ndarray,
    sw_series: np.ndarray,
    nz_series: np.ndarray,
) -> PeriodResult:
    """Build a PeriodResult from raw numpy arrays."""
    metrics = _compute_metrics(
        ret_series, to_series, mb_series, mw_series, lw_series, sw_series, nz_series
    )

    long_ratio = 0.0
    short_ratio = 0.0
    if lw_series.size > 0:
        total_w = lw_series + sw_series
        valid = total_w > 0
        if np.any(valid):
            long_ratio = float(np.mean(lw_series[valid] / total_w[valid]))
            short_ratio = float(np.mean(sw_series[valid] / total_w[valid]))

    return PeriodResult(
        metrics=metrics,
        timestamps=timestamps,
        ret_series=ret_series,
        turnover_series=to_series,
        min_bet_series=mb_series,
        max_weight_series=mw_series,
        nonzero_position_series=nz_series.astype(np.int16),
        long_ratio=long_ratio,
        short_ratio=short_ratio,
    )


def _align_aux_series_to_returns(
    ret_series: np.ndarray,
    to_series: np.ndarray,
    mb_series: np.ndarray,
    mw_series: np.ndarray,
    lw_series: np.ndarray,
    sw_series: np.ndarray,
    nz_series: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Align auxiliary series to ret_series length.

    Backtester.run() returns ret_series with length T-1 and auxiliary series with
    length T. This helper drops the first aux bar when needed and validates shape.
    """
    target_len = ret_series.size
    aux_series = (
        ("turnover", to_series),
        ("min_bet", mb_series),
        ("max_weight", mw_series),
        ("long_weight", lw_series),
        ("short_weight", sw_series),
        ("nonzero_count", nz_series),
    )
    aligned: list[np.ndarray] = []
    for name, arr in aux_series:
        if arr.size == target_len:
            aligned.append(arr)
        elif arr.size == target_len + 1:
            aligned.append(arr[1:])
        elif target_len == 0 and arr.size == 0:
            aligned.append(arr)
        else:
            raise ValueError(
                f"{name} length mismatch: got {arr.size}, expected {target_len} or {target_len + 1}"
            )
    return aligned[0], aligned[1], aligned[2], aligned[3], aligned[4], aligned[5]


def _split_arrays_by_date(
    timestamps: np.ndarray, *arrays: np.ndarray, split_date: str
) -> tuple[tuple[np.ndarray, ...], tuple[np.ndarray, ...]]:
    """Split sorted arrays by date using a single searchsorted boundary."""
    split_ts = np.datetime64(split_date + "T00:00:00", "ns")
    split_idx = int(np.searchsorted(timestamps, split_ts, side="left"))

    is_arrays = tuple(arr[:split_idx] for arr in arrays)
    os_arrays = tuple(arr[split_idx:] for arr in arrays)
    return (
        timestamps[:split_idx],
        *is_arrays,
    ), (
        timestamps[split_idx:],
        *os_arrays,
    )


def _build_backtest_period_result_from_arrays(
    timestamps: np.ndarray,
    ret_series: np.ndarray,
    to_series: np.ndarray,
    mb_series: np.ndarray,
    mw_series: np.ndarray,
    lw_series: np.ndarray,
    sw_series: np.ndarray,
    nz_series: np.ndarray,
    *,
    split_date: str,
) -> BacktestPeriodResult:
    """Build IS/OS period result from aligned run arrays."""
    to_series, mb_series, mw_series, lw_series, sw_series, nz_series = _align_aux_series_to_returns(
        ret_series, to_series, mb_series, mw_series, lw_series, sw_series, nz_series
    )

    (
        (is_ts, is_ret, is_to, is_mb, is_mw, is_lw, is_sw, is_nz),
        (os_ts, os_ret, os_to, os_mb, os_mw, os_lw, os_sw, os_nz),
    ) = _split_arrays_by_date(
        timestamps,
        ret_series,
        to_series,
        mb_series,
        mw_series,
        lw_series,
        sw_series,
        nz_series,
        split_date=split_date,
    )

    is_period = _build_period_result(is_ts, is_ret, is_to, is_mb, is_mw, is_lw, is_sw, is_nz)
    os_period = _build_period_result(os_ts, os_ret, os_to, os_mb, os_mw, os_lw, os_sw, os_nz)
    return BacktestPeriodResult(is_period=is_period, os_period=os_period)


def _run_backtest_periods_internal(
    alpha,
    settings: PeriodSettings,
    capital: float,
    *,
    data_cache,
    mode: str = "D0",
    collect_viz: bool = False,
) -> tuple[BacktestPeriodResult, Optional[dict]]:
    """Run a single alpha and return period result (+ optional viz bundle)."""
    bt = Backtester(
        cache=data_cache,
        alpha_recipe=alpha,
        capital=capital,
        mode=mode,
        collect_viz=collect_viz,
        start_date=settings.is_start,
        end_date=settings.os_end,
    )
    timestamps, ret_s, to_s, mb_s, mw_s, lw_s, sw_s, nz_s, viz = bt.run()
    period_result = _build_backtest_period_result_from_arrays(
        timestamps,
        ret_s,
        to_s,
        mb_s,
        mw_s,
        lw_s,
        sw_s,
        nz_s,
        split_date=settings.os_start,
    )
    return period_result, viz


def run_backtest_with_periods(
    alpha,
    strategy=None,
    settings: PeriodSettings = None,
    capital: float = 0.0,
    *,
    data_cache=None,
    mode: str = "D0",
) -> BacktestPeriodResult:
    """Run IS/OS split backtest using cache (numpy path)."""
    if data_cache is None:
        raise ValueError("data_cache must be provided")

    period_result, _ = _run_backtest_periods_internal(
        alpha=alpha,
        settings=settings,
        capital=capital,
        data_cache=data_cache,
        mode=mode,
        collect_viz=False,
    )
    return period_result


# ===========================================================================
# Multi-Alpha Backtest
# ===========================================================================


class CombinedAlpha:
    """Combines multiple alphas by summing their signals (numpy path)."""

    def __init__(self, alphas: list):
        self.alphas = alphas
        self._warned_alpha_ids: set[int] = set()

    def reset(self):
        self._warned_alpha_ids.clear()
        for alpha in self.alphas:
            reset_fn = getattr(alpha, 'reset', None)
            if reset_fn:
                reset_fn()

    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        combined = None
        for alpha in self.alphas:
            try:
                s = alpha.compute(t_idx, arrays)
            except Exception as exc:
                aid = id(alpha)
                if aid not in self._warned_alpha_ids:
                    self._warned_alpha_ids.add(aid)
                    alpha_name = getattr(alpha, "name", alpha.__class__.__name__)
                    warnings.warn(
                        f"Skipping alpha '{alpha_name}' after compute error: {exc}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                continue
            s = np.asarray(s, dtype=np.float64)
            if combined is None:
                combined = s.copy()
            else:
                combined_finite = np.isfinite(combined)
                s_finite = np.isfinite(s)
                both = combined_finite & s_finite
                only_s = (~combined_finite) & s_finite
                combined[both] = combined[both] + s[both]
                combined[only_s] = s[only_s]
        if combined is None:
            N = arrays["__n_symbols__"]
            return np.zeros(N, dtype=np.float64)
        return combined


@dataclass
class AlphaResult:
    """Individual alpha result within a multi-alpha backtest."""

    name: str
    is_period: PeriodResult
    os_period: PeriodResult
    source_idx: int = -1
    rank: int = 0


@dataclass(frozen=True)
class MultipleBacktestResult:
    """Multi-alpha backtest result."""

    alpha_results: list  # List[AlphaResult]
    combined_result: BacktestPeriodResult
    sorted_by: str = "sharpe"
    viz_bundle: Optional[dict] = None


def _compute_ir_corr(
    alpha_ret: np.ndarray, combined_ret: np.ndarray
) -> tuple[float, float]:
    """Compute IR and Corr between alpha and combined returns."""
    a = np.asarray(alpha_ret, dtype=np.float64)
    c = np.asarray(combined_ret, dtype=np.float64)

    # Ensure both arrays have the same length by taking the minimum
    min_len = min(len(a), len(c))
    if min_len < 2:
        return float("nan"), float("nan")

    a = a[:min_len]
    c = c[:min_len]
    finite = np.isfinite(a) & np.isfinite(c)
    if int(np.count_nonzero(finite)) < 2:
        return float("nan"), float("nan")
    a = a[finite]
    c = c[finite]

    excess = a - c
    excess_std = np.nanstd(excess)
    ir = (
        (np.nanmean(excess) / (excess_std + 1e-12)) * np.sqrt(BARS_PER_YEAR)
        if excess_std > 0
        else 0.0
    )

    # Compute correlation
    if np.all(a == c) or (np.nanstd(a) == 0 and np.nanstd(c) == 0):
        corr = 1.0
    elif np.nanstd(a) == 0 or np.nanstd(c) == 0:
        corr = 0.0
    else:
        corr_matrix = np.corrcoef(a, c)
        corr = float(corr_matrix[0, 1]) if corr_matrix.shape == (2, 2) else 0.0

    if np.isnan(corr):
        corr = 0.0

    return float(ir), corr


def run_backtest(
    alphas: list,
    strategy=None,
    settings: PeriodSettings = None,
    capital: float = 0.0,
    *,
    data_cache=None,
    mode: str = "D0",
    n_jobs: int = 1,
    collect_viz: bool = False,
) -> MultipleBacktestResult:
    """Run backtest for one or multiple alphas and a combined portfolio."""
    if data_cache is None:
        raise ValueError("data_cache must be provided")
    if not alphas:
        raise ValueError("alphas must not be empty")

    import os as _os

    effective_jobs = (_os.cpu_count() or 1) if n_jobs == -1 else max(1, n_jobs)

    # Single-alpha fast path: one run serves both individual and combined results.
    if len(alphas) == 1:
        pr, viz_bundle = _run_backtest_periods_internal(
            alpha=alphas[0],
            settings=settings,
            capital=capital,
            data_cache=data_cache,
            mode=mode,
            collect_viz=collect_viz,
        )
        is_ir, is_corr = _compute_ir_corr(pr.is_period.ret_series, pr.is_period.ret_series)
        os_ir, os_corr = _compute_ir_corr(pr.os_period.ret_series, pr.os_period.ret_series)
        alpha_name = getattr(alphas[0], "name", "alpha_0")
        ar = AlphaResult(
            name=alpha_name,
            is_period=replace(pr.is_period, metrics=replace(pr.is_period.metrics, ir=is_ir, corr=is_corr)),
            os_period=replace(pr.os_period, metrics=replace(pr.os_period.metrics, ir=os_ir, corr=os_corr)),
            source_idx=0,
            rank=1,
        )
        return MultipleBacktestResult(
            alpha_results=[ar],
            combined_result=pr,
            sorted_by="sharpe",
            viz_bundle=viz_bundle,
        )

    individual_results: list[BacktestPeriodResult | None] = []

    combined_alpha = CombinedAlpha(alphas)

    if effective_jobs == 1:
        for alpha in alphas:
            period_result = run_backtest_with_periods(
                alpha=alpha,
                strategy=strategy,
                settings=settings,
                capital=capital,
                data_cache=data_cache,
                mode=mode,
            )
            individual_results.append(period_result)
        combined_result = run_backtest_with_periods(
            alpha=combined_alpha,
            strategy=strategy,
            settings=settings,
            capital=capital,
            data_cache=data_cache,
            mode=mode,
        )
    else:
        from .parallel import run_parallel_backtests

        par_results = run_parallel_backtests(
            alphas=alphas + [combined_alpha],
            strategy=strategy,
            settings=settings,
            capital=capital,
            data_cache=data_cache,
            mode=mode,
            n_jobs=effective_jobs,
        )
        idx_map = {idx: res for idx, res in par_results}
        for i in range(len(alphas)):
            individual_results.append(idx_map.get(i))
        combined_result = idx_map.get(len(alphas))
        if combined_result is None:
            combined_result = run_backtest_with_periods(
                alpha=combined_alpha,
                strategy=strategy,
                settings=settings,
                capital=capital,
                data_cache=data_cache,
                mode=mode,
            )

    alpha_result_list = []
    for i, (alpha, pr) in enumerate(zip(alphas, individual_results)):
        if pr is None:
            continue

        is_ir, is_corr = _compute_ir_corr(
            pr.is_period.ret_series, combined_result.is_period.ret_series
        )
        os_ir, os_corr = _compute_ir_corr(
            pr.os_period.ret_series, combined_result.os_period.ret_series
        )

        new_is = replace(pr.is_period.metrics, ir=is_ir, corr=is_corr)
        new_os = replace(pr.os_period.metrics, ir=os_ir, corr=os_corr)

        name = getattr(alpha, "name", f"alpha_{i}")
        alpha_result_list.append(
            AlphaResult(
                name=name,
                is_period=replace(pr.is_period, metrics=new_is),
                os_period=replace(pr.os_period, metrics=new_os),
                source_idx=i,
            )
        )

    alpha_result_list.sort(key=lambda ar: ar.is_period.metrics.sharpe, reverse=True)
    for rank, ar in enumerate(alpha_result_list, start=1):
        ar.rank = rank

    return MultipleBacktestResult(
        alpha_results=alpha_result_list,
        combined_result=combined_result,
        sorted_by="sharpe",
    )
