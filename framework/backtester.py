# STAR/framework/backtester.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable, Optional

import numpy as np
import pandas as pd


SignalLike = pd.Series | Mapping[str, float] | list[float] | np.ndarray

BARS_PER_YEAR = 365
TRANSACTION_FEE_RATE = 0.0010  # 10 bps round-trip fee


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
    turnover: float = 0.0       # average turnover
    min_bet: float = 0.0        # average absolute position
    max_weight: float = 0.0     # max single position weight
    ir: float = float('nan')    # for multi-alpha only
    corr: float = float('nan')  # for multi-alpha only


# Backward-compatible aliases
ISMetrics = PeriodMetrics
OSMetrics = PeriodMetrics


@dataclass(frozen=True)
class BacktestPeriodResult:
    """IS/OS 분리 백테스트 결과."""
    is_metrics: PeriodMetrics
    os_metrics: PeriodMetrics
    is_ret_series: pd.Series
    os_ret_series: pd.Series
    # Detail view time series (optional for backward compat)
    is_turnover_series: Optional[pd.Series] = None
    os_turnover_series: Optional[pd.Series] = None
    is_min_bet_series: Optional[pd.Series] = None
    os_min_bet_series: Optional[pd.Series] = None
    is_max_weight_series: Optional[pd.Series] = None
    os_max_weight_series: Optional[pd.Series] = None
    is_long_ratio: float = 0.0
    is_short_ratio: float = 0.0


@runtime_checkable
class AlphaLike(Protocol):
    def compute(self, timestamp: pd.Timestamp, data_slice: pd.DataFrame) -> SignalLike: ...


@runtime_checkable
class StrategyLike(Protocol):
    def generate_weights(self, raw_signals: pd.Series, **kwargs: Any) -> pd.Series | Mapping[str, float]: ...


def signal_ratio_weights(raw_signals: pd.Series) -> pd.Series:
    """시그널의 절대값 비율에 따라 자본을 배분. gross=1 정규화."""
    signals = raw_signals.fillna(0)
    total_strength = signals.abs().sum()
    if total_strength == 0:
        return pd.Series(0.0, index=signals.index)
    return signals / total_strength


class SignalRatioStrategy:
    """StrategyLike wrapper around signal_ratio_weights."""
    def generate_weights(self, raw_signals: pd.Series, **kwargs: Any) -> pd.Series:
        return signal_ratio_weights(raw_signals)


@dataclass(frozen=True)
class BacktestResult:
    ret_series: pd.Series
    avg_turnover: float
    initial_capital: float
    turnover_series: Optional[pd.Series] = None
    min_bet_series: Optional[pd.Series] = None
    max_weight_series: Optional[pd.Series] = None
    long_weight_series: Optional[pd.Series] = None
    short_weight_series: Optional[pd.Series] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ret_series", pd.Series(self.ret_series).sort_index())
        object.__setattr__(self, "avg_turnover", float(self.avg_turnover))
        object.__setattr__(self, "initial_capital", float(self.initial_capital))

    def summary(self) -> str:
        if self.ret_series.empty:
            return ("--- 백테스트 결과 요약 ---\n결과 시계열이 비어 있습니다.\n--------------------------")
        m = _compute_core_metrics(self.ret_series)
        return (
            f"--- 백테스트 결과 요약 ---\n"
            f"총 수익률: {m['total_return']:,.2%}\n"
            f"연율화 샤프 지수: {m['sharpe']:.2f}\n"
            f"최대 낙폭 (MDD): {m['mdd']:,.2%}\n"
            f"평균 턴오버(편도/일): {self.avg_turnover:.2%}\n"
            f"--------------------------"
        )

class Backtester:
    """Backtest engine.

    Key conventions (see simulator_plan.md §2.3):
    - Portfolio return is always computed with *previous executed* weights to avoid lookahead.
    - D0: compute weights at bar t close, execute at bar t close → return is realized on bar t+1.
    - D1: compute weights at bar t close, execute at bar t+1 close → return is realized on bar t+2.

    Notes:
    - Turnover is defined as Σ|Δw| (no 0.5 factor for L/S; range 0~2 when gross=1).
    """

    def __init__(
        self,
        data_loader,
        alpha_recipe: AlphaLike,
        strategy: StrategyLike,
        capital: float,
        *,
        mode: str = "D0",  # "D0" | "D1" | "intraday" (TODO)
        collect_viz: bool = True,
        transaction_fee_rate: float = TRANSACTION_FEE_RATE,
    ):
        self.loader = data_loader
        self.alpha = alpha_recipe
        self.strategy = strategy

        cap = float(capital)
        if not np.isfinite(cap) or cap <= 0:
            raise ValueError(f"capital must be a finite positive number, got {capital!r}")
        self.initial_capital = cap

        self.mode = str(mode)
        self.collect_viz = bool(collect_viz)
        self.transaction_fee_rate = float(transaction_fee_rate)

    @staticmethod
    def _validate_alpha_output(raw, *, timestamp: pd.Timestamp, universe: pd.Index) -> pd.Series:
        """Validate and align Alpha output.

        Expected contract (simulator_plan.md §2.4):
        - Return a 1D vector (pd.Series / dict / array-like) representing per-symbol signals.
        - Symbols must be within current universe (data_slice.index).
        - Values must be numeric (float-castable). NaN is allowed; +/-inf is not.

        Returns:
            pd.Series aligned to `universe` (index), dtype=float.
        """
        if isinstance(raw, pd.Series):
            s = raw.copy()
            if s.index.has_duplicates:
                raise ValueError(f"Alpha.compute() returned duplicate symbols at {timestamp!s}")
            extra = s.index.difference(universe)
            if len(extra) > 0:
                raise ValueError(
                    "Alpha.compute() returned symbols not in data_slice.index "
                    f"at {timestamp!s}: {list(extra)[:5]}"
                )
            s = s.reindex(universe)
        elif isinstance(raw, dict):
            extra = pd.Index(raw.keys()).difference(universe)
            if len(extra) > 0:
                raise ValueError(
                    "Alpha.compute() returned symbols not in data_slice.index "
                    f"at {timestamp!s}: {list(extra)[:5]}"
                )
            s = pd.Series(raw, index=universe)
        else:
            # array-like: must match universe length
            try:
                s = pd.Series(raw)
            except Exception as e:
                raise TypeError(
                    f"Alpha.compute() must return pd.Series/dict/array-like, got {type(raw)}"
                ) from e
            if len(s) != len(universe):
                raise ValueError(
                    "Alpha.compute() returned a vector of length "
                    f"{len(s)} but universe length is {len(universe)} at {timestamp!s}"
                )
            s.index = universe

        # Coerce to float and validate finiteness (NaN ok)
        try:
            s = s.astype(float)
        except Exception as e:
            raise TypeError(
                f"Alpha.compute() output values must be numeric (float-castable) at {timestamp!s}"
            ) from e

        bad_inf = np.isinf(s.to_numpy(dtype=float, na_value=np.nan))
        if bool(np.any(bad_inf)):
            raise ValueError(f"Alpha.compute() returned +/-inf values at {timestamp!s}")

        return s

    def run(self) -> BacktestResult:
        ret_history = {}
        turnover_history = {}
        minbet_history = {}
        max_weight_history = {}
        long_weight_history = {}
        short_weight_history = {}
        viz_rows = []  # 시각화용(심볼 레벨) 프레임 누적

        if self.mode not in {"D0", "D1", "intraday"}:
            raise ValueError(f"Unknown backtest mode: {self.mode!r}")
        if self.mode == "intraday":
            raise NotImplementedError("intraday mode is not implemented yet")

        previous_executed_weights = pd.Series(dtype=float)
        previous_target_weights = pd.Series(dtype=float)  # used to stage execution for D1
        previous_close = None

        for timestamp, data_slice in self.loader:
            # data_slice: index=instrument, columns: ['open','high','low','close', ...]
            idx_current = data_slice.index
            # Use the union universe so we can account for liquidations when symbols disappear (delisting / missing bars).
            idx_all = idx_current if previous_close is None else idx_current.union(previous_close.index)

            previous_executed_weights = previous_executed_weights.reindex(idx_all, fill_value=0.0)
            previous_target_weights = previous_target_weights.reindex(idx_all, fill_value=0.0)

            # 1) 수익률(자산) 및 포트 r_t
            # - D0: weights_t are executed at the same bar close, so return is realized on the next bar.
            # - D1: weights_t are executed on the next bar close, so return is realized one bar later.
            if previous_close is not None:
                prev_close_aligned = previous_close.reindex(idx_current)

                # EC-BT-007: close가 0 또는 NaN인 경우 해당 종목 수익률을 0으로 처리
                cur_close = data_slice["close"].astype(float)
                prev_close = prev_close_aligned.astype(float)
                invalid = (
                    cur_close.isna()
                    | prev_close.isna()
                    | (cur_close <= 0)
                    | (prev_close <= 0)
                )

                asset_ret = (cur_close / prev_close) - 1.0
                asset_ret = asset_ret.replace([np.inf, -np.inf], 0.0)
                asset_ret = asset_ret.mask(invalid, 0.0).fillna(0.0)

                # Compute portfolio return only on instruments with a current close (no lookahead).
                r_t = float((previous_executed_weights.reindex(idx_current, fill_value=0.0) * asset_ret).sum())
                ret_history[timestamp] = r_t
            else:
                asset_ret = pd.Series(0.0, index=idx_current)  # 첫 바

            # 2) raw signal -> target weights (전략 사용)
            # raw_signals = self.alpha.compute(timestamp, data_slice)         # pd.Series-like
            # raw_signals = pd.Series(raw_signals, index=idx).fillna(0.0)
            # target_weights = self.strategy.generate_weights(raw_signals)    # pd.Series
            # target_weights = pd.Series(target_weights, index=idx).fillna(0.0)

            raw = self.alpha.compute(timestamp, data_slice)
            # Keep missing values for visualization (e.g., delay warm-up) so the heatmap can render "no data" correctly.
            raw_signals_viz = self._validate_alpha_output(raw, timestamp=timestamp, universe=idx_current)
            # For strategy/weights, treat missing as 0 to stay robust.
            raw_signals = raw_signals_viz.fillna(0.0)

            try:
                target_weights = self.strategy.generate_weights(
                    raw_signals,
                    timestamp=timestamp,
                    data_slice=data_slice,
                    previous_close=previous_close,
                )
            except TypeError:
                # 기존 전략 호환: 추가 인자를 받지 않으면 원래 시그니처로 호출
                target_weights = self.strategy.generate_weights(raw_signals)

            target_weights = pd.Series(target_weights, index=idx_current).fillna(0.0)

            # Execution weights depend on mode.
            # - D0: execute today's computed weights at today's close
            # - D1: execute yesterday's computed weights at today's close
            executed_weights = target_weights if self.mode == "D0" else previous_target_weights
            executed_weights = pd.Series(executed_weights, index=idx_current).fillna(0.0)

            # 3) 턴오버/미니멈 베팅 (based on executed positions)
            # Include liquidations when symbols disappear: executed weight becomes 0 on idx_all.
            executed_weights_all = executed_weights.reindex(idx_all, fill_value=0.0)
            turnover = float((executed_weights_all - previous_executed_weights).abs().sum())
            turnover_history[timestamp] = turnover
            # Deduct transaction fee from this bar's return
            if timestamp in ret_history:
                ret_history[timestamp] -= turnover * self.transaction_fee_rate
            minbet_history[timestamp] = float(executed_weights_all.abs().mean())
            max_weight_history[timestamp] = float(executed_weights.abs().max()) if len(executed_weights) > 0 else 0.0
            long_weight_history[timestamp] = float(executed_weights[executed_weights > 0].sum()) if len(executed_weights) > 0 else 0.0
            short_weight_history[timestamp] = float(executed_weights[executed_weights < 0].abs().sum()) if len(executed_weights) > 0 else 0.0

            # 4) 시각화용 로우 저장(옵션)
            if self.collect_viz:
                df_row = data_slice[['open','high','low','close']].copy()
                df_row['signal']  = raw_signals_viz
                df_row['weight']  = executed_weights
                df_row['ret']     = asset_ret
                df_row['symbol']  = df_row.index
                df_row['date']    = pd.Timestamp(timestamp)
                viz_rows.append(df_row.reset_index(drop=True))

            # 5) 상태 업데이트
            previous_executed_weights = executed_weights_all
            previous_target_weights = target_weights.reindex(idx_all, fill_value=0.0)
            previous_close = data_slice['close']

        # 시계열 정리
        ret_series = pd.Series(ret_history).fillna(0.0).sort_index()
        to_series  = pd.Series(turnover_history).fillna(0.0).sort_index()
        mb_series  = pd.Series(minbet_history).fillna(0.0).sort_index()
        mw_series  = pd.Series(max_weight_history).fillna(0.0).sort_index()
        lw_series  = pd.Series(long_weight_history).fillna(0.0).sort_index()
        sw_series  = pd.Series(short_weight_history).fillna(0.0).sort_index()
        avg_turnover = float(to_series.mean()) if len(to_series) else 0.0

        result = BacktestResult(ret_series=ret_series,
                                avg_turnover=avg_turnover,
                                initial_capital=self.initial_capital,
                                turnover_series=to_series,
                                min_bet_series=mb_series,
                                max_weight_series=mw_series,
                                long_weight_series=lw_series,
                                short_weight_series=sw_series)

        viz_bundle = None
        if self.collect_viz:
            sym_df = pd.concat(viz_rows, ignore_index=True) if viz_rows else pd.DataFrame()
            if 'date' in sym_df.columns:
                sym_df = sym_df.set_index('date')  # 보케에서 datetime 인덱스 사용
            # 포트폴리오 DF
            r = ret_series.fillna(0.0)
            equity = (1.0 + r).cumprod() * self.initial_capital
            port_df = pd.DataFrame({
                'equity': equity,
                'mdd': (equity / equity.cummax() - 1.0),
                'turnover': to_series.reindex(r.index).fillna(0.0),
                'min_bet': mb_series.reindex(r.index).fillna(0.0)
            })
            viz_bundle = {'symbols': sym_df, 'portfolio': port_df}

        return (result, viz_bundle)


# ===========================================================================
# IS/OS helpers
# ===========================================================================

def _compute_core_metrics(ret_series: pd.Series) -> dict:
    """Compute core metrics (sharpe, equity, mdd, cagr, pnl) from a return series.

    Returns dict with keys: sharpe, equity, mdd, cagr, total_return, n_days.
    """
    r = ret_series.fillna(0.0)
    vol = r.std()
    sharpe = (r.mean() / (vol + 1e-12)) * np.sqrt(BARS_PER_YEAR) if vol > 0 else 0.0
    equity = (1.0 + r).cumprod()
    total_return = float(equity.iloc[-1] - 1.0)
    n_days = len(r)
    cagr = float(equity.iloc[-1] ** (BARS_PER_YEAR / max(n_days, 1)) - 1.0)
    dd = equity / (equity.cummax() + 1e-12) - 1.0
    mdd = float(dd.min())
    return {
        "sharpe": sharpe,
        "equity": equity,
        "mdd": mdd,
        "cagr": cagr,
        "total_return": total_return,
        "n_days": n_days,
    }


def metrics_to_api_dict(m) -> dict:
    """PeriodMetrics → API dict with frontend-ready units.

    Unit conversions (decimal → percent, ×100):
        pnl, cagr, mdd, turnover

    Note on PnL vs CAGR:
        - Framework ``pnl`` = cumulative return  (equity[-1] - 1).
        - Framework ``cagr`` = annualised return  (equity[-1]^(365/n) - 1).
        - Frontend metrics table column labelled "PnL" displays **CAGR** (annualised).
        - Actual cumulative PnL ($) is computed from ``retSeries`` in the detail page.
        Therefore this function overwrites the ``pnl`` value with ``cagr`` so that the
        frontend "PnL" column shows the annualised return.
    """
    d: dict = {}
    for f in m.__dataclass_fields__:
        v = getattr(m, f)
        if isinstance(v, float) and np.isnan(v):
            continue  # skip NaN fields (ir, corr when single-alpha)
        d[f] = v

    # --- percent conversions (decimal → %) ---
    for key in ("pnl", "cagr", "mdd", "turnover"):
        if key in d:
            d[key] *= 100.0

    # Frontend "PnL" column = CAGR (annualised return), not cumulative return.
    if "cagr" in d:
        d["pnl"] = d["cagr"]

    return d


def _compute_period_metrics(result: BacktestResult) -> PeriodMetrics:
    """Compute period metrics (IS or OS) from a BacktestResult.

    Computes ALL fields including turnover, min_bet, max_weight from any BacktestResult.
    """
    r = result.ret_series.fillna(0.0)
    if r.empty:
        return PeriodMetrics(
            pnl=0.0, cagr=0.0, sharpe=0.0, mdd=0.0,
            turnover=0.0, min_bet=0.0, max_weight=0.0,
        )
    m = _compute_core_metrics(r)
    turnover = result.avg_turnover
    min_bet = float(result.min_bet_series.mean()) if result.min_bet_series is not None else 0.0
    max_weight = float(result.max_weight_series.max()) if result.max_weight_series is not None else 0.0
    return PeriodMetrics(
        pnl=m["total_return"], cagr=m["cagr"], sharpe=m["sharpe"], mdd=m["mdd"],
        turnover=turnover, min_bet=min_bet, max_weight=max_weight,
    )


# Backward-compatible aliases
_compute_is_metrics = _compute_period_metrics
_compute_os_metrics = _compute_period_metrics


def _split_result_by_date(result: BacktestResult, split_date: str) -> tuple[BacktestResult, BacktestResult]:
    """Split a BacktestResult into IS (< split_date) and OS (>= split_date) portions."""
    split_ts = pd.Timestamp(split_date, tz="UTC")
    r = result.ret_series

    is_mask = r.index < split_ts
    os_mask = ~is_mask

    def _sub_result(mask: pd.Series) -> BacktestResult:
        sub_r = r[mask]
        idx = sub_r.index
        sub_to = result.turnover_series.reindex(idx) if result.turnover_series is not None else None
        sub_mb = result.min_bet_series.reindex(idx) if result.min_bet_series is not None else None
        sub_mw = result.max_weight_series.reindex(idx) if result.max_weight_series is not None else None
        sub_lw = result.long_weight_series.reindex(idx) if result.long_weight_series is not None else None
        sub_sw = result.short_weight_series.reindex(idx) if result.short_weight_series is not None else None
        avg_to = float(sub_to.mean()) if sub_to is not None and len(sub_to) > 0 else 0.0
        return BacktestResult(
            ret_series=sub_r,
            avg_turnover=avg_to,
            initial_capital=result.initial_capital,
            turnover_series=sub_to,
            min_bet_series=sub_mb,
            max_weight_series=sub_mw,
            long_weight_series=sub_lw,
            short_weight_series=sub_sw,
        )

    return _sub_result(is_mask), _sub_result(os_mask)


def run_backtest_with_periods(
    alpha: AlphaLike,
    strategy: StrategyLike,
    settings: PeriodSettings,
    capital: float,
    *,
    data_loader_kwargs: Optional[dict] = None,
    data_cache=None,
    mode: str = "D0",
    transaction_fee_rate: float = TRANSACTION_FEE_RATE,
) -> BacktestPeriodResult:
    """Run IS/OS split backtest and return period results.

    Runs a SINGLE continuous backtest from is_start to os_end, then splits
    the result at os_start.  This ensures OS starts with carried-over weights
    from the IS period (warm start) rather than fresh zero weights.

    Data source (one required):
        data_loader_kwargs: dict passed to DataLoader (reads from disk).
        data_cache: DataCache instance (uses in-memory cache).
    """
    if data_cache is not None:
        from .data_cache import DataCacheIterator
        full_loader = DataCacheIterator(data_cache, start_date=settings.is_start, end_date=settings.os_end)
    elif data_loader_kwargs is not None:
        from .dataloader import DataLoader
        base_kwargs = dict(data_loader_kwargs)
        full_kwargs = {**base_kwargs, "start_date": settings.is_start, "end_date": settings.os_end}
        full_loader = DataLoader(**full_kwargs)
    else:
        raise ValueError("Either data_loader_kwargs or data_cache must be provided")

    # --- Single continuous run ---
    bt = Backtester(full_loader, alpha, strategy, capital, mode=mode, collect_viz=False, transaction_fee_rate=transaction_fee_rate)
    full_result, _ = bt.run()

    # --- Split at OS start ---
    is_result, os_result = _split_result_by_date(full_result, settings.os_start)

    is_metrics = _compute_period_metrics(is_result)
    os_metrics = _compute_period_metrics(os_result)

    # Compute long/short ratios from IS result
    is_long_ratio = 0.0
    is_short_ratio = 0.0
    if is_result.long_weight_series is not None and len(is_result.long_weight_series) > 0:
        total = is_result.long_weight_series + is_result.short_weight_series
        safe_total = total.replace(0, np.nan)
        is_long_ratio = float((is_result.long_weight_series / safe_total).mean())
        is_short_ratio = float((is_result.short_weight_series / safe_total).mean())
        if np.isnan(is_long_ratio):
            is_long_ratio = 0.0
        if np.isnan(is_short_ratio):
            is_short_ratio = 0.0

    return BacktestPeriodResult(
        is_metrics=is_metrics,
        os_metrics=os_metrics,
        is_ret_series=is_result.ret_series,
        os_ret_series=os_result.ret_series,
        is_turnover_series=is_result.turnover_series,
        os_turnover_series=os_result.turnover_series,
        is_min_bet_series=is_result.min_bet_series,
        os_min_bet_series=os_result.min_bet_series,
        is_max_weight_series=is_result.max_weight_series,
        os_max_weight_series=os_result.max_weight_series,
        is_long_ratio=is_long_ratio,
        is_short_ratio=is_short_ratio,
    )


# ===========================================================================
# Multi-Alpha Backtest
# ===========================================================================

class CombinedAlpha:
    """Combines multiple alphas by summing their signals."""

    def __init__(self, alphas: list):
        self.alphas = alphas

    def compute(self, timestamp: pd.Timestamp, data_slice: pd.DataFrame) -> pd.Series:
        combined = None
        for alpha in self.alphas:
            try:
                s = alpha.compute(timestamp, data_slice)
            except Exception:
                continue  # skip failing alpha
            s = pd.Series(s, index=data_slice.index)
            if combined is None:
                combined = s.copy()
            else:
                combined = combined.add(s, fill_value=0)
        if combined is None:
            return pd.Series(0.0, index=data_slice.index)
        return combined


@dataclass
class AlphaResult:
    """Individual alpha result within a multi-alpha backtest."""
    name: str
    is_metrics: PeriodMetrics
    os_metrics: PeriodMetrics
    is_ret_series: pd.Series
    os_ret_series: pd.Series
    rank: int = 0
    # Detail view time series
    is_turnover_series: Optional[pd.Series] = None
    os_turnover_series: Optional[pd.Series] = None
    is_min_bet_series: Optional[pd.Series] = None
    os_min_bet_series: Optional[pd.Series] = None
    is_max_weight_series: Optional[pd.Series] = None
    os_max_weight_series: Optional[pd.Series] = None
    is_long_ratio: float = 0.0
    is_short_ratio: float = 0.0


@dataclass(frozen=True)
class MultipleBacktestResult:
    """Multi-alpha backtest result."""
    alpha_results: list  # List[AlphaResult]
    combined_result: BacktestPeriodResult
    sorted_by: str = "sharpe"

    def to_dict(self, initial_capital: float = 0) -> dict:
        """Convert to API response format."""
        def _series_dict(s):
            return {str(k): float(v) for k, v in s.items()}

        def _equity_dict(ret_series, cap):
            if cap <= 0:
                return None
            equity = (1 + ret_series).cumprod() * cap
            return {str(k): float(v) for k, v in equity.items()}

        return {
            "combined": {
                "isMetrics": metrics_to_api_dict(self.combined_result.is_metrics),
                "osMetrics": metrics_to_api_dict(self.combined_result.os_metrics),
                "isRetSeries": _series_dict(self.combined_result.is_ret_series),
                "osRetSeries": _series_dict(self.combined_result.os_ret_series),
            },
            "initialCapital": initial_capital,
            "alphas": [
                {
                    "name": ar.name,
                    "rank": ar.rank,
                    "isMetrics": metrics_to_api_dict(ar.is_metrics),
                    "osMetrics": metrics_to_api_dict(ar.os_metrics),
                    "isRetSeries": _series_dict(ar.is_ret_series),
                    "osRetSeries": _series_dict(ar.os_ret_series),
                    "isEquitySeries": _equity_dict(ar.is_ret_series, initial_capital),
                    "osEquitySeries": _equity_dict(ar.os_ret_series, initial_capital),
                    "isTurnoverSeries": _series_dict(ar.is_turnover_series) if ar.is_turnover_series is not None else None,
                    "osTurnoverSeries": _series_dict(ar.os_turnover_series) if ar.os_turnover_series is not None else None,
                    "isMinBetSeries": _series_dict(ar.is_min_bet_series) if ar.is_min_bet_series is not None else None,
                    "osMinBetSeries": _series_dict(ar.os_min_bet_series) if ar.os_min_bet_series is not None else None,
                    "isMaxWeightSeries": _series_dict(ar.is_max_weight_series) if ar.is_max_weight_series is not None else None,
                    "osMaxWeightSeries": _series_dict(ar.os_max_weight_series) if ar.os_max_weight_series is not None else None,
                    "longRatio": ar.is_long_ratio,
                    "shortRatio": ar.is_short_ratio,
                }
                for ar in self.alpha_results
            ],
        }


def _compute_ir_corr(
    alpha_ret: pd.Series, combined_ret: pd.Series
) -> tuple[float, float]:
    """Compute IR and Corr between alpha and combined returns."""
    # Align the two series
    a, c = alpha_ret.align(combined_ret, join="inner", fill_value=0.0)
    a = a.fillna(0.0)
    c = c.fillna(0.0)

    if len(a) < 2:
        return float('nan'), float('nan')

    # IR = mean(excess) / std(excess) * sqrt(365)
    excess = a - c
    excess_std = excess.std()
    ir = (excess.mean() / (excess_std + 1e-12)) * np.sqrt(BARS_PER_YEAR) if excess_std > 0 else 0.0

    # Corr
    corr = float(a.corr(c))
    if np.isnan(corr):
        corr = 0.0

    return float(ir), corr


def run_multiple_backtest(
    alphas: list,
    strategy,
    settings: PeriodSettings,
    capital: float,
    *,
    data_loader_kwargs: Optional[dict] = None,
    data_cache=None,
    mode: str = "D0",
    n_jobs: int = 1,
    transaction_fee_rate: float = TRANSACTION_FEE_RATE,
) -> MultipleBacktestResult:
    """Run backtest for multiple alphas and a combined portfolio.

    Data source (one required):
        data_loader_kwargs: dict passed to DataLoader (reads from disk).
        data_cache: DataCache instance (uses in-memory cache).
        Note: parallel path (n_jobs>1) requires data_loader_kwargs.

    Args:
        n_jobs: Number of parallel workers.
            1  = sequential (default).
            >1 = that many worker processes.
            -1 = all CPU cores.

    Steps:
    1. Run individual alpha backtests (sequential or parallel)
    2. Run combined alpha backtest
    3. Compute IR and Corr for each alpha vs combined
    4. Sort by IS sharpe descending
    """
    if data_loader_kwargs is None and data_cache is None:
        raise ValueError("Either data_loader_kwargs or data_cache must be provided")

    import os as _os
    effective_jobs = (_os.cpu_count() or 1) if n_jobs == -1 else max(1, n_jobs)

    # Parallel path requires data_loader_kwargs (workers need disk access)
    if effective_jobs > 1 and data_loader_kwargs is None:
        effective_jobs = 1

    individual_results: list[BacktestPeriodResult | None] = []

    if effective_jobs == 1:
        # Sequential path — supports both data_cache and data_loader_kwargs
        for alpha in alphas:
            period_result = run_backtest_with_periods(
                alpha=alpha,
                strategy=strategy,
                settings=settings,
                capital=capital,
                data_loader_kwargs=data_loader_kwargs,
                data_cache=data_cache,
                mode=mode,
                transaction_fee_rate=transaction_fee_rate,
            )
            individual_results.append(period_result)
    else:
        # Parallel path via shared memory (requires data_loader_kwargs)
        from .parallel import run_parallel_backtests
        par_results = run_parallel_backtests(
            alphas=alphas,
            strategy=strategy,
            settings=settings,
            capital=capital,
            data_loader_kwargs=data_loader_kwargs,
            mode=mode,
            n_jobs=effective_jobs,
            transaction_fee_rate=transaction_fee_rate,
        )
        # par_results is [(idx, BacktestPeriodResult|None), ...]
        idx_map = {idx: res for idx, res in par_results}
        for i in range(len(alphas)):
            individual_results.append(idx_map.get(i))

    # Step 2: combined alpha backtest
    combined_alpha = CombinedAlpha(alphas)
    combined_result = run_backtest_with_periods(
        alpha=combined_alpha,
        strategy=strategy,
        settings=settings,
        capital=capital,
        data_loader_kwargs=data_loader_kwargs,
        data_cache=data_cache,
        mode=mode,
        transaction_fee_rate=transaction_fee_rate,
    )

    # Step 3: compute IR and Corr for each alpha vs combined
    alpha_result_list = []
    for i, (alpha, pr) in enumerate(zip(alphas, individual_results)):
        if pr is None:
            # Alpha failed during parallel execution; skip it
            continue

        is_ir, is_corr = _compute_ir_corr(pr.is_ret_series, combined_result.is_ret_series)
        os_ir, os_corr = _compute_ir_corr(pr.os_ret_series, combined_result.os_ret_series)

        # Replace ir/corr in metrics (frozen dataclass, need to reconstruct)
        is_m = pr.is_metrics
        new_is = PeriodMetrics(
            pnl=is_m.pnl, cagr=is_m.cagr, sharpe=is_m.sharpe,
            mdd=is_m.mdd, turnover=is_m.turnover, min_bet=is_m.min_bet,
            max_weight=is_m.max_weight, ir=is_ir, corr=is_corr,
        )
        os_m = pr.os_metrics
        new_os = PeriodMetrics(
            pnl=os_m.pnl, cagr=os_m.cagr, sharpe=os_m.sharpe, mdd=os_m.mdd,
            turnover=os_m.turnover, min_bet=os_m.min_bet, max_weight=os_m.max_weight,
            ir=os_ir, corr=os_corr,
        )

        name = getattr(alpha, 'name', f"alpha_{i}")
        alpha_result_list.append(AlphaResult(
            name=name,
            is_metrics=new_is,
            os_metrics=new_os,
            is_ret_series=pr.is_ret_series,
            os_ret_series=pr.os_ret_series,
            is_turnover_series=pr.is_turnover_series,
            os_turnover_series=pr.os_turnover_series,
            is_min_bet_series=pr.is_min_bet_series,
            os_min_bet_series=pr.os_min_bet_series,
            is_max_weight_series=pr.is_max_weight_series,
            os_max_weight_series=pr.os_max_weight_series,
            is_long_ratio=pr.is_long_ratio,
            is_short_ratio=pr.is_short_ratio,
        ))

    # Step 4: sort by IS sharpe descending, assign ranks
    alpha_result_list.sort(key=lambda ar: ar.is_metrics.sharpe, reverse=True)
    for rank, ar in enumerate(alpha_result_list, start=1):
        ar.rank = rank

    return MultipleBacktestResult(
        alpha_results=alpha_result_list,
        combined_result=combined_result,
        sorted_by="sharpe",
    )
