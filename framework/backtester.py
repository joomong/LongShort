# STAR/framework/backtester.py
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

import numpy as np
import pandas as pd

try:
    import numba
    _HAS_NUMBA = True
except ImportError:
    _HAS_NUMBA = False


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
    ir: float = float('nan')
    corr: float = float('nan')


@dataclass(frozen=True)
class PeriodResult:
    """단일 기간(IS 또는 OS) 백테스트 결과."""
    metrics: PeriodMetrics
    ret_series: pd.Series
    turnover_series: pd.Series
    min_bet_series: pd.Series
    max_weight_series: pd.Series
    long_ratio: float = 0.0
    short_ratio: float = 0.0


@dataclass(frozen=True)
class BacktestPeriodResult:
    """IS/OS 분리 백테스트 결과."""
    is_period: PeriodResult
    os_period: PeriodResult


def _compute_metrics(ret_series: pd.Series, turnover_series: pd.Series,
                     min_bet_series: pd.Series, max_weight_series: pd.Series) -> PeriodMetrics:
    """Compute PeriodMetrics directly from series data."""
    r = ret_series.fillna(0.0)
    if r.empty:
        return PeriodMetrics(pnl=0.0, cagr=0.0, sharpe=0.0, mdd=0.0)

    vol = r.std()
    sharpe = (r.mean() / (vol + 1e-12)) * np.sqrt(BARS_PER_YEAR) if vol > 0 else 0.0
    equity = (1.0 + r).cumprod()
    total_return = float(equity.iloc[-1] - 1.0)
    n_days = len(r)
    cagr = float(equity.iloc[-1] ** (BARS_PER_YEAR / max(n_days, 1)) - 1.0)
    dd = equity / (equity.cummax() + 1e-12) - 1.0
    mdd = float(dd.min())

    turnover = float(turnover_series.mean()) if len(turnover_series) > 0 else 0.0
    min_bet = float(min_bet_series.mean()) if len(min_bet_series) > 0 else 0.0
    max_weight = float(max_weight_series.max()) if len(max_weight_series) > 0 else 0.0

    return PeriodMetrics(
        pnl=total_return, cagr=cagr, sharpe=sharpe, mdd=mdd,
        turnover=turnover, min_bet=min_bet, max_weight=max_weight,
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

    if "cagr" in d:
        d["pnl"] = d["cagr"]

    return d


# ===========================================================================
# Backtest Loop Kernel (Numba JIT or numpy fallback)
# ===========================================================================

def _backtest_loop_numpy(close_2d, signals_2d, fee_rate, mode_int):
    """Pure numpy backtest loop — fallback when numba is unavailable.

    Parameters
    ----------
    close_2d : ndarray (T, N) — close prices
    signals_2d : ndarray (T, N) — raw alpha signals (pre-normalization)
    fee_rate : float — round-trip transaction fee rate
    mode_int : int — 0 = D0, 1 = D1

    Returns
    -------
    (ret, turnover, min_bet, max_weight, long_weight, short_weight) — each (T,)
    """
    T, N = close_2d.shape
    ret = np.zeros(T)
    turnover = np.zeros(T)
    min_bet = np.zeros(T)
    max_weight = np.zeros(T)
    long_weight = np.zeros(T)
    short_weight = np.zeros(T)

    prev_weights = np.zeros(N)
    prev_target = np.zeros(N)

    for t in range(T):
        cur_close = close_2d[t]

        # Portfolio return from previous weights
        if t > 0:
            prev_close = close_2d[t - 1]
            valid = (prev_close > 0) & (cur_close > 0)
            asset_ret = np.where(valid, cur_close / prev_close - 1.0, 0.0)
            ret[t] = np.dot(prev_weights, asset_ret)

        # Normalize signals → target weights (mask NaN close symbols)
        sig = signals_2d[t]
        present = np.isfinite(cur_close)
        masked_sig = sig * present
        abs_sum = np.abs(masked_sig).sum()
        if abs_sum > 1e-12:
            target = masked_sig / abs_sum
        else:
            target = np.zeros(N)

        # Execution weights depend on mode
        exec_w = target if mode_int == 0 else prev_target

        # Turnover
        to = np.abs(exec_w - prev_weights).sum()
        turnover[t] = to
        ret[t] -= to * fee_rate

        # Min bet (mean absolute weight) and max weight
        abs_w = np.abs(exec_w)
        min_bet[t] = abs_w.sum() / N if N > 0 else 0.0
        max_weight[t] = abs_w.max() if N > 0 else 0.0

        # Long/short weight sums
        long_weight[t] = np.maximum(exec_w, 0.0).sum()
        short_weight[t] = np.abs(np.minimum(exec_w, 0.0)).sum()

        prev_weights = exec_w.copy()
        prev_target = target.copy()

    return ret, turnover, min_bet, max_weight, long_weight, short_weight


if _HAS_NUMBA:
    @numba.jit(nopython=True, cache=True)
    def _backtest_loop_numba(close_2d, signals_2d, fee_rate, mode_int):
        """Numba-JIT backtest loop kernel."""
        T, N = close_2d.shape
        ret = np.zeros(T)
        turnover = np.zeros(T)
        min_bet = np.zeros(T)
        max_weight = np.zeros(T)
        long_weight = np.zeros(T)
        short_weight = np.zeros(T)

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

            # Normalize signals → target weights (mask NaN close symbols)
            abs_sum = 0.0
            for i in range(N):
                c = cur_close[i]
                if c == c:  # NaN check in numba (NaN != NaN)
                    abs_sum += abs(signals_2d[t, i])

            target = np.zeros(N)
            if abs_sum > 1e-12:
                for i in range(N):
                    c = cur_close[i]
                    if c == c:
                        target[i] = signals_2d[t, i] / abs_sum

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
            for i in range(N):
                aw = abs(exec_w[i])
                abs_w_sum += aw
                if aw > abs_w_max:
                    abs_w_max = aw
                if exec_w[i] > 0.0:
                    lw += exec_w[i]
                elif exec_w[i] < 0.0:
                    sw += -exec_w[i]

            min_bet[t] = abs_w_sum / N if N > 0 else 0.0
            max_weight[t] = abs_w_max
            long_weight[t] = lw
            short_weight[t] = sw

            prev_weights = exec_w.copy()
            prev_target = target.copy()

        return ret, turnover, min_bet, max_weight, long_weight, short_weight

    _backtest_loop = _backtest_loop_numba
else:
    _backtest_loop = _backtest_loop_numpy


class Backtester:
    """Backtest engine — numpy-native path.

    Key conventions:
    - Portfolio return is computed with *previous executed* weights to avoid lookahead.
    - D0: compute weights at bar t close, execute at bar t close → return realized on bar t+1.
    - D1: compute weights at bar t close, execute at bar t+1 close → return realized on bar t+2.
    - Turnover = Σ|Δw| (no 0.5 factor; range 0~2 when gross=1).

    Requires cache= DataCache (numpy path).
    """

    def __init__(
        self,
        alpha_recipe=None,
        capital: float = 0.0,
        *,
        cache=None,
        mode: str = "D0",
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
    def _validate_alpha_output(raw: np.ndarray, *, t_idx: int, n_symbols: int) -> np.ndarray:
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
        """2-pass backtest: signal DAG (Python) → backtest loop (Numba if available)."""
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
            start_idx = 0
            for i, ts in enumerate(ts_pd):
                if ts >= start_ts:
                    start_idx = i
                    break
        else:
            start_idx = 0

        if self.end_date:
            end_ts = pd.Timestamp(self.end_date, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
            end_idx = T - 1
            for i in range(T - 1, -1, -1):
                if ts_pd[i] <= end_ts:
                    end_idx = i
                    break
        else:
            end_idx = T - 1

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
        viz_rows = []

        for bar_i, t_idx in enumerate(range(start_idx, end_idx + 1)):
            raw = self.alpha.compute(t_idx, arrays_for_compute)
            raw = self._validate_alpha_output(raw, t_idx=t_idx, n_symbols=N)
            signals_2d[bar_i] = np.nan_to_num(raw, nan=0.0)

            if self.collect_viz:
                viz_rows.append({
                    "t_idx": t_idx,
                    "timestamp": timestamps[t_idx],
                    "signal": raw.copy(),
                })

        # === Pass 2: Backtest loop (Numba or numpy fallback) ===
        close_2d = cache.arrays["close"][start_idx:end_idx + 1]
        mode_int = 0 if self.mode == "D0" else 1

        ret_arr, to_arr, mb_arr, mw_arr, lw_arr, sw_arr = _backtest_loop(
            close_2d, signals_2d, self.transaction_fee_rate, mode_int
        )

        # Build pandas result objects
        ts_arr = timestamps[start_idx:end_idx + 1]
        ts_pd = pd.DatetimeIndex(ts_arr)
        if ts_pd.tz is None and len(ts_pd) > 0:
            ts_pd = ts_pd.tz_localize("UTC")
        # First bar has no return from prev close — drop only from ret_series
        ret_series = pd.Series(ret_arr, index=ts_pd).iloc[1:] if len(ts_pd) > 0 else pd.Series(dtype=float)
        to_series = pd.Series(to_arr, index=ts_pd)
        mb_series = pd.Series(mb_arr, index=ts_pd)
        mw_series = pd.Series(mw_arr, index=ts_pd)
        lw_series = pd.Series(lw_arr, index=ts_pd)
        sw_series = pd.Series(sw_arr, index=ts_pd)

        viz_bundle = None
        if self.collect_viz and viz_rows:
            rows = []
            for bar_i, vr in enumerate(viz_rows):
                # Reconstruct weights from signals for viz
                sig = signals_2d[bar_i]
                cur_close = close_2d[bar_i]
                present = np.isfinite(cur_close)
                masked_sig = sig * present
                abs_sum = np.abs(masked_sig).sum()
                weights = masked_sig / abs_sum if abs_sum > 0 else np.zeros_like(masked_sig)

                for s_idx, sym in enumerate(symbols):
                    rows.append({
                        "date": pd.Timestamp(vr["timestamp"]),
                        "symbol": sym,
                        "open": cache.arrays.get("open", cache.arrays["close"])[vr["t_idx"], s_idx],
                        "high": cache.arrays.get("high", cache.arrays["close"])[vr["t_idx"], s_idx],
                        "low": cache.arrays.get("low", cache.arrays["close"])[vr["t_idx"], s_idx],
                        "close": cache.arrays["close"][vr["t_idx"], s_idx],
                        "signal": vr["signal"][s_idx],
                        "weight": weights[s_idx],
                        "ret": 0.0,
                    })
            sym_df = pd.DataFrame(rows)
            if not sym_df.empty:
                sym_df = sym_df.set_index("date")

            r = ret_series.fillna(0.0)
            equity = (1.0 + r).cumprod() * self.initial_capital
            port_df = pd.DataFrame({
                'equity': equity,
                'mdd': (equity / equity.cummax() - 1.0),
                'turnover': to_series.reindex(r.index).fillna(0.0),
                'min_bet': mb_series.reindex(r.index).fillna(0.0),
            })
            viz_bundle = {'symbols': sym_df, 'portfolio': port_df}

        return (ret_series, to_series, mb_series, mw_series, lw_series, sw_series, viz_bundle)


def _build_period_result(ret_series: pd.Series, to_series: pd.Series,
                         mb_series: pd.Series, mw_series: pd.Series,
                         lw_series: pd.Series, sw_series: pd.Series) -> PeriodResult:
    """Build a PeriodResult from raw series data."""
    metrics = _compute_metrics(ret_series, to_series, mb_series, mw_series)

    long_ratio = 0.0
    short_ratio = 0.0
    if len(lw_series) > 0:
        total_w = lw_series + sw_series
        safe_total = total_w.replace(0, np.nan)
        long_ratio = float((lw_series / safe_total).mean())
        short_ratio = float((sw_series / safe_total).mean())
        if np.isnan(long_ratio):
            long_ratio = 0.0
        if np.isnan(short_ratio):
            short_ratio = 0.0

    return PeriodResult(
        metrics=metrics,
        ret_series=ret_series,
        turnover_series=to_series,
        min_bet_series=mb_series,
        max_weight_series=mw_series,
        long_ratio=long_ratio,
        short_ratio=short_ratio,
    )


def _split_series_by_date(series: pd.Series, split_date: str) -> tuple[pd.Series, pd.Series]:
    """Split a series into IS (< split_date) and OS (>= split_date) portions."""
    split_ts = pd.Timestamp(split_date, tz="UTC")
    idx = series.index
    if hasattr(idx, 'tz') and idx.tz is None:
        series = series.copy()
        series.index = series.index.tz_localize("UTC")
        idx = series.index
    return series[idx < split_ts], series[idx >= split_ts]


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

    bt = Backtester(
        cache=data_cache,
        alpha_recipe=alpha,
        capital=capital,
        mode=mode,
        collect_viz=False,
        start_date=settings.is_start,
        end_date=settings.os_end,
    )
    ret_s, to_s, mb_s, mw_s, lw_s, sw_s, _ = bt.run()

    os_start = settings.os_start

    is_ret, os_ret = _split_series_by_date(ret_s, os_start)
    is_to, os_to = _split_series_by_date(to_s, os_start)
    is_mb, os_mb = _split_series_by_date(mb_s, os_start)
    is_mw, os_mw = _split_series_by_date(mw_s, os_start)
    is_lw, os_lw = _split_series_by_date(lw_s, os_start)
    is_sw, os_sw = _split_series_by_date(sw_s, os_start)

    is_period = _build_period_result(is_ret, is_to, is_mb, is_mw, is_lw, is_sw)
    os_period = _build_period_result(os_ret, os_to, os_mb, os_mw, os_lw, os_sw)

    return BacktestPeriodResult(is_period=is_period, os_period=os_period)


# ===========================================================================
# Multi-Alpha Backtest
# ===========================================================================

class CombinedAlpha:
    """Combines multiple alphas by summing their signals (numpy path)."""

    def __init__(self, alphas: list):
        self.alphas = alphas

    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        combined = None
        for alpha in self.alphas:
            try:
                s = alpha.compute(t_idx, arrays)
            except Exception:
                continue
            s = np.nan_to_num(np.asarray(s, dtype=np.float64), nan=0.0)
            if combined is None:
                combined = s.copy()
            else:
                combined = combined + s
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
    rank: int = 0


@dataclass(frozen=True)
class MultipleBacktestResult:
    """Multi-alpha backtest result."""
    alpha_results: list  # List[AlphaResult]
    combined_result: BacktestPeriodResult
    sorted_by: str = "sharpe"


def _compute_ir_corr(
    alpha_ret: pd.Series, combined_ret: pd.Series
) -> tuple[float, float]:
    """Compute IR and Corr between alpha and combined returns."""
    a, c = alpha_ret.align(combined_ret, join="inner", fill_value=0.0)
    a = a.fillna(0.0)
    c = c.fillna(0.0)

    if len(a) < 2:
        return float('nan'), float('nan')

    excess = a - c
    excess_std = excess.std()
    ir = (excess.mean() / (excess_std + 1e-12)) * np.sqrt(BARS_PER_YEAR) if excess_std > 0 else 0.0

    corr = float(a.corr(c))
    if np.isnan(corr):
        corr = 0.0

    return float(ir), corr


def run_multiple_backtest(
    alphas: list,
    strategy=None,
    settings: PeriodSettings = None,
    capital: float = 0.0,
    *,
    data_cache=None,
    mode: str = "D0",
    n_jobs: int = 1,
) -> MultipleBacktestResult:
    """Run backtest for multiple alphas and a combined portfolio."""
    if data_cache is None:
        raise ValueError("data_cache must be provided")

    import os as _os
    effective_jobs = (_os.cpu_count() or 1) if n_jobs == -1 else max(1, n_jobs)

    individual_results: list[BacktestPeriodResult | None] = []

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
    else:
        from .parallel import run_parallel_backtests
        par_results = run_parallel_backtests(
            alphas=alphas,
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

    combined_alpha = CombinedAlpha(alphas)
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

        is_ir, is_corr = _compute_ir_corr(pr.is_period.ret_series, combined_result.is_period.ret_series)
        os_ir, os_corr = _compute_ir_corr(pr.os_period.ret_series, combined_result.os_period.ret_series)

        new_is = replace(pr.is_period.metrics, ir=is_ir, corr=is_corr)
        new_os = replace(pr.os_period.metrics, ir=os_ir, corr=os_corr)

        name = getattr(alpha, 'name', f"alpha_{i}")
        alpha_result_list.append(AlphaResult(
            name=name,
            is_period=replace(pr.is_period, metrics=new_is),
            os_period=replace(pr.os_period, metrics=new_os),
        ))

    alpha_result_list.sort(key=lambda ar: ar.is_period.metrics.sharpe, reverse=True)
    for rank, ar in enumerate(alpha_result_list, start=1):
        ar.rank = rank

    return MultipleBacktestResult(
        alpha_results=alpha_result_list,
        combined_result=combined_result,
        sorted_by="sharpe",
    )
