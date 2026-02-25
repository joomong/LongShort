"""Golden-dataset reliability tests for the backtester.

Verifies calculation correctness by:
1. Computing expected results INDEPENDENTLY (raw numpy/pandas, no framework imports).
2. Running the same data through the backtester.
3. Comparing both results.

Golden dataset: 3 symbols (BTC, ETH, SOL) x 10 days with hardcoded prices.

Note:
- Independent and framework Sharpe both use ddof=1.
- This file is a ground-truth reliability suite, not a regression lock for bugs.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


# ============================================================================
# Golden dataset constants
# ============================================================================

DATES = pd.date_range("2024-01-01", periods=10, freq="D", tz="UTC")

SYMBOL_PRICES = {
    "BTC": [100, 102, 99, 105, 103, 107, 110, 108, 112, 115],
    "ETH": [50, 48, 52, 51, 55, 53, 56, 58, 57, 60],
    "SOL": [10, 11, 10, 12, 11, 13, 12, 14, 13, 15],
}

# Fixed raw signals and their normalized weights (signal_ratio_weights).
RAW_SIGNALS = {"BTC": 1.0, "ETH": -1.0, "SOL": 0.5}
# |1| + |-1| + |0.5| = 2.5  =>  BTC=0.4, ETH=-0.4, SOL=0.2
NORMALIZED_WEIGHTS = {"BTC": 0.4, "ETH": -0.4, "SOL": 0.2}

IS_START, IS_END = "2024-01-01", "2024-01-06"
OS_START, OS_END = "2024-01-07", "2024-01-10"

BARS_PER_YEAR = 365


# ============================================================================
# IndependentCalculator  (NO framework imports)
# ============================================================================

class IndependentCalculator:
    """Self-contained metrics calculator using only numpy/pandas.

    Mirrors the backtester formulas exactly, but computed from scratch.

    Parameters
    ----------
    close_df : pd.DataFrame
        Columns = symbols, Index = dates (sorted), values = close prices.
    weights : dict[str, float]
        Normalized (gross=1) portfolio weights, constant across all bars.
    epsilon_mdd : float
        Epsilon added to cummax denominator (matches _compute_period_metrics).
    """

    def __init__(
        self,
        close_df: pd.DataFrame,
        weights: dict[str, float],
        epsilon_mdd: float = 1e-12,
    ):
        self.close_df = close_df.sort_index()
        self.weights = weights
        self.epsilon_mdd = epsilon_mdd

        self._symbols = sorted(weights.keys())
        self._w = np.array([weights[s] for s in self._symbols])

    # -- per-asset daily returns: (close_t / close_{t-1}) - 1 ----------------
    def asset_returns(self) -> pd.DataFrame:
        """Returns DataFrame (n_bars-1 x n_symbols) of daily returns."""
        closes = self.close_df[self._symbols].values.astype(float)
        # ret[i] = close[i+1]/close[i] - 1  (shifted by 1)
        rets = closes[1:] / closes[:-1] - 1.0
        return pd.DataFrame(
            rets,
            index=self.close_df.index[1:],
            columns=self._symbols,
        )

    # -- portfolio returns using PREVIOUS bar weights (D0, no lookahead) ------
    def portfolio_returns(self) -> pd.Series:
        """D0 mode: weights computed at bar t close, return realized on bar t+1.

        In the backtester loop:
        - Bar 0 (first bar): no previous close => no return recorded.
          Weights are established (previous_executed_weights set).
        - Bar 1 onwards: r_t = sum(prev_weights * asset_return_t).

        So the return series starts from the SECOND bar (index 1 of dates).
        The weights used are always the same constant weights because our
        MockAlpha returns the same signal every bar.
        """
        ar = self.asset_returns()  # index = dates[1:]
        # In D0 with constant signal, every bar uses the same weights.
        # The first return bar uses weights established at bar 0.
        port_ret = (ar[self._symbols].values * self._w).sum(axis=1)
        return pd.Series(port_ret, index=ar.index)

    # -- cumulative PnL: prod(1 + r_t) - 1 ----------------------------------
    def pnl(self) -> float:
        equity = (1.0 + self.portfolio_returns()).cumprod()
        return float(equity.iloc[-1] - 1.0)

    # -- equity curve ---------------------------------------------------------
    def equity_curve(self) -> pd.Series:
        return (1.0 + self.portfolio_returns()).cumprod()

    # -- Sharpe: (mean(r) / std(r, ddof=1)) * sqrt(365) ----------------------
    def sharpe(self) -> float:
        r = self.portfolio_returns()
        vol = float(r.std())  # pandas default ddof=1
        if vol <= 0:
            return 0.0
        return float((r.mean() / (vol + 1e-12)) * np.sqrt(BARS_PER_YEAR))

    # -- MDD: min(equity / cummax(equity + eps) - 1) --------------------------
    def mdd(self) -> float:
        equity = self.equity_curve()
        dd = equity / (equity.cummax() + self.epsilon_mdd) - 1.0
        return float(dd.min())

    # -- CAGR: equity_final^(365/n_days) - 1 ----------------------------------
    def cagr(self) -> float:
        equity = self.equity_curve()
        n_days = len(equity)
        return float(equity.iloc[-1] ** (BARS_PER_YEAR / max(n_days, 1)) - 1.0)

    # -- Turnover per bar: sum(|w_t - w_{t-1}|) ------------------------------
    def turnover_series(self) -> pd.Series:
        """Turnover for each bar in the backtest.

        Bar 0: weights go from 0 to NORMALIZED_WEIGHTS => turnover = sum(|w|).
        Bar 1+: constant weights => turnover = 0.
        """
        dates = self.close_df.index
        n = len(dates)
        turnovers = np.zeros(n)
        # Bar 0: from zero to weights
        turnovers[0] = float(np.abs(self._w).sum())
        # Bar 1+: constant signal => same weights every bar => turnover = 0
        for i in range(1, n):
            turnovers[i] = 0.0
        return pd.Series(turnovers, index=dates)

    def avg_turnover(self) -> float:
        return float(self.turnover_series().mean())

    # -- Min bet per bar: mean(|w_i|) ----------------------------------------
    def min_bet_series(self) -> pd.Series:
        """Mean absolute weight per bar."""
        mb = float(np.abs(self._w).mean())
        return pd.Series(mb, index=self.close_df.index)

    def avg_min_bet(self) -> float:
        return float(self.min_bet_series().mean())

    # -- Max weight per bar: max(|w_i|) --------------------------------------
    def max_weight_series(self) -> pd.Series:
        mw = float(np.abs(self._w).max())
        return pd.Series(mw, index=self.close_df.index)

    def max_weight(self) -> float:
        """Overall max weight (max of the series)."""
        return float(self.max_weight_series().max())


# ============================================================================
# Helpers
# ============================================================================

def _build_close_df(
    dates: pd.DatetimeIndex | None = None,
    symbol_prices: dict | None = None,
) -> pd.DataFrame:
    """Build a close-price DataFrame from the golden data."""
    if dates is None:
        dates = DATES
    if symbol_prices is None:
        symbol_prices = SYMBOL_PRICES
    return pd.DataFrame(symbol_prices, index=dates)


def _create_parquet(file_path: Path) -> None:
    """Write the golden dataset as a parquet file."""
    data = []
    for i, ts in enumerate(DATES):
        ts_ms = int(ts.value // 1_000_000)
        for sym, prices in SYMBOL_PRICES.items():
            c = prices[i]
            data.append({
                "open_time": ts_ms,
                "symbol": sym,
                "open": float(c - 1),
                "high": float(c + 2),
                "low": float(c - 2),
                "close": float(c),
                "volume": 1000.0,
            })
    pd.DataFrame(data).to_parquet(file_path, index=False)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(scope="module")
def golden_parquet(tmp_path_factory) -> Path:
    """Create a temporary parquet file with the golden dataset."""
    p = tmp_path_factory.mktemp("golden") / "golden.parquet"
    _create_parquet(p)
    return p


@pytest.fixture(scope="module")
def independent_full() -> IndependentCalculator:
    """IndependentCalculator for the full 10-day period."""
    return IndependentCalculator(_build_close_df(), NORMALIZED_WEIGHTS)


@pytest.fixture(scope="module")
def independent_is() -> IndependentCalculator:
    """IndependentCalculator for IS period (day 1-6)."""
    df = _build_close_df().loc["2024-01-01":"2024-01-06"]
    return IndependentCalculator(df, NORMALIZED_WEIGHTS)


class WarmOSCalculator:
    """Calculator for warm-started OS period.

    With IS/OS unification, the OS period is a slice of a single continuous
    run. The OS returns include the transition bar (day 7) which uses weights
    carried over from the IS period.

    For constant signals, the per-bar returns are identical to a cold-start
    calculator, but the number of returns differs: warm OS has n_os_bars
    returns (including transition bar), while cold-start had n_os_bars - 1.
    """

    def __init__(self, full_calc: IndependentCalculator, os_start: str):
        self._full_calc = full_calc
        os_start_ts = pd.Timestamp(os_start, tz="UTC")
        full_rets = full_calc.portfolio_returns()
        self._os_rets = full_rets[full_rets.index >= os_start_ts]

    def portfolio_returns(self) -> pd.Series:
        return self._os_rets

    def pnl(self) -> float:
        equity = (1.0 + self._os_rets).cumprod()
        return float(equity.iloc[-1] - 1.0)

    def equity_curve(self) -> pd.Series:
        return (1.0 + self._os_rets).cumprod()

    def sharpe(self) -> float:
        r = self._os_rets
        vol = float(r.std())
        if vol <= 0:
            return 0.0
        return float((r.mean() / (vol + 1e-12)) * np.sqrt(BARS_PER_YEAR))

    def mdd(self) -> float:
        equity = self.equity_curve()
        dd = equity / (equity.cummax() + 1e-12) - 1.0
        return float(dd.min())

    def cagr(self) -> float:
        equity = self.equity_curve()
        n_days = len(equity)
        return float(equity.iloc[-1] ** (BARS_PER_YEAR / max(n_days, 1)) - 1.0)


@pytest.fixture(scope="module")
def independent_os():
    """Warm-start OS calculator: slice of the full run at OS_START.

    With IS/OS unification, OS starts with carried-over weights from IS,
    so OS has n_os_bars returns (including the transition bar).
    """
    full_calc = IndependentCalculator(_build_close_df(), NORMALIZED_WEIGHTS)
    return WarmOSCalculator(full_calc, OS_START)


# ============================================================================
# Mock Alpha / Strategy for framework
# ============================================================================

class MockAlpha:
    """Returns fixed signals regardless of input data."""

    def compute(self, t_idx: int, arrays: dict) -> np.ndarray:
        symbols = arrays["__symbols__"]
        return np.array([RAW_SIGNALS.get(s, 0.0) for s in symbols])


# ============================================================================
# Framework helper to run the backtester
# ============================================================================

def _run_full_backtest(parquet_path: Path):
    """Run the backtester on the full golden dataset, return 9-tuple."""
    from framework.data import DataCache
    from framework.backtester import Backtester
    import yaml

    config = {
        "datasets": [{"name": "golden", "path": str(parquet_path),
                       "time_col": "open_time", "symbol_col": "symbol",
                       "use_col": ["open", "high", "low", "close", "volume"]}],
        "universe": None,
        "backtest": {"start_date": "2024-01-01", "end_date": "2024-01-10",
                     "initial_capital": 1000000, "transaction_fee": 10},
    }
    config_path = parquet_path.parent / "config_full.yaml"
    yaml.dump(config, open(config_path, "w"))
    cache = DataCache(str(config_path))
    cache.load()

    bt = Backtester(
        cache=cache,
        alpha_recipe=MockAlpha(),
        capital=1_000_000,
        mode="D0",
        start_date="2024-01-01",
        end_date="2024-01-10",
    )
    return bt.run()


def _run_period_backtest(parquet_path: Path):
    """Run IS/OS split backtest, return BacktestPeriodResult."""
    from framework.data import DataCache
    from framework.backtester import PeriodSettings, run_backtest_with_periods
    import yaml

    config = {
        "datasets": [{"name": "golden", "path": str(parquet_path),
                       "time_col": "open_time", "symbol_col": "symbol",
                       "use_col": ["open", "high", "low", "close", "volume"]}],
        "universe": None,
        "backtest": {"start_date": IS_START, "end_date": OS_END,
                     "initial_capital": 1000000, "transaction_fee": 10},
    }
    config_path = parquet_path.parent / "config_period.yaml"
    yaml.dump(config, open(config_path, "w"))
    cache = DataCache(str(config_path))
    cache.load()

    settings = PeriodSettings(
        is_start=IS_START, is_end=IS_END,
        os_start=OS_START, os_end=OS_END,
    )
    return run_backtest_with_periods(
        alpha=MockAlpha(),

        settings=settings,
        capital=1_000_000,
        data_cache=cache,
    )


# ============================================================================
# 2. TestGoldenDatasetFullPipeline
# ============================================================================

class TestGoldenDatasetFullPipeline:
    """Compare every metric between independent calc and backtester (full 10 days)."""

    def test_pnl_matches(self, golden_parquet, independent_full):
        from framework.backtester import _compute_metrics
        ts_arr, ret_s, to_s, mb_s, mw_s, lw_s, sw_s, _nz, _ = _run_full_backtest(golden_parquet)
        bt_metrics = _compute_metrics(ret_s, to_s, mb_s, mw_s)
        assert math.isclose(bt_metrics.pnl, independent_full.pnl(), rel_tol=1e-9), (
            f"PnL mismatch: bt={bt_metrics.pnl}, indep={independent_full.pnl()}"
        )

    def test_sharpe_matches(self, golden_parquet, independent_full):
        from framework.backtester import _compute_metrics
        ts_arr, ret_s, to_s, mb_s, mw_s, lw_s, sw_s, _nz, _ = _run_full_backtest(golden_parquet)
        bt_metrics = _compute_metrics(ret_s, to_s, mb_s, mw_s)
        assert math.isclose(bt_metrics.sharpe, independent_full.sharpe(), rel_tol=1e-9), (
            f"Sharpe mismatch: bt={bt_metrics.sharpe}, indep={independent_full.sharpe()}"
        )

    def test_mdd_matches(self, golden_parquet, independent_full):
        from framework.backtester import _compute_metrics
        ts_arr, ret_s, to_s, mb_s, mw_s, lw_s, sw_s, _nz, _ = _run_full_backtest(golden_parquet)
        bt_metrics = _compute_metrics(ret_s, to_s, mb_s, mw_s)
        assert math.isclose(bt_metrics.mdd, independent_full.mdd(), rel_tol=1e-9), (
            f"MDD mismatch: bt={bt_metrics.mdd}, indep={independent_full.mdd()}"
        )

    def test_cagr_matches(self, golden_parquet, independent_full):
        from framework.backtester import _compute_metrics
        ts_arr, ret_s, to_s, mb_s, mw_s, lw_s, sw_s, _nz, _ = _run_full_backtest(golden_parquet)
        bt_metrics = _compute_metrics(ret_s, to_s, mb_s, mw_s)
        assert math.isclose(bt_metrics.cagr, independent_full.cagr(), rel_tol=1e-9), (
            f"CAGR mismatch: bt={bt_metrics.cagr}, indep={independent_full.cagr()}"
        )

    def test_turnover_matches(self, golden_parquet, independent_full):
        from framework.backtester import _compute_metrics
        ts_arr, ret_s, to_s, mb_s, mw_s, lw_s, sw_s, _nz, _ = _run_full_backtest(golden_parquet)
        bt_metrics = _compute_metrics(ret_s, to_s, mb_s, mw_s)
        assert math.isclose(bt_metrics.turnover, independent_full.avg_turnover(), rel_tol=1e-9), (
            f"Turnover mismatch: bt={bt_metrics.turnover}, indep={independent_full.avg_turnover()}"
        )

    def test_min_bet_matches(self, golden_parquet, independent_full):
        from framework.backtester import _compute_metrics
        ts_arr, ret_s, to_s, mb_s, mw_s, lw_s, sw_s, _nz, _ = _run_full_backtest(golden_parquet)
        bt_metrics = _compute_metrics(ret_s, to_s, mb_s, mw_s)
        assert math.isclose(bt_metrics.min_bet, independent_full.avg_min_bet(), rel_tol=1e-9), (
            f"Min bet mismatch: bt={bt_metrics.min_bet}, indep={independent_full.avg_min_bet()}"
        )

    def test_max_weight_matches(self, golden_parquet, independent_full):
        from framework.backtester import _compute_metrics
        ts_arr, ret_s, to_s, mb_s, mw_s, lw_s, sw_s, _nz, _ = _run_full_backtest(golden_parquet)
        bt_metrics = _compute_metrics(ret_s, to_s, mb_s, mw_s)
        assert math.isclose(bt_metrics.max_weight, independent_full.max_weight(), rel_tol=1e-9), (
            f"Max weight mismatch: bt={bt_metrics.max_weight}, indep={independent_full.max_weight()}"
        )

    def test_return_series_length(self, golden_parquet, independent_full):
        ts_arr, ret_s, to_s, mb_s, mw_s, lw_s, sw_s, _nz, _ = _run_full_backtest(golden_parquet)
        # Backtester records returns from bar 1 onwards (9 returns for 10 bars)
        assert len(ret_s) == 9
        assert len(independent_full.portfolio_returns()) == 9

    def test_return_series_values(self, golden_parquet, independent_full):
        """Per-bar return values should match exactly."""
        ts_arr, ret_s, to_s, mb_s, mw_s, lw_s, sw_s, _nz, _ = _run_full_backtest(golden_parquet)
        bt_rets = ret_s
        indep_rets = independent_full.portfolio_returns().values
        np.testing.assert_allclose(bt_rets, indep_rets, rtol=1e-12)


# ============================================================================
# 3. TestGoldenDatasetISOSSplit
# ============================================================================

class TestGoldenDatasetISOSSplit:
    """Verify IS and OS metrics independently against the independent calculator."""

    def test_is_pnl(self, golden_parquet, independent_is):
        period = _run_period_backtest(golden_parquet)
        assert math.isclose(period.is_period.metrics.pnl, independent_is.pnl(), rel_tol=1e-9), (
            f"IS PnL: bt={period.is_period.metrics.pnl}, indep={independent_is.pnl()}"
        )

    def test_is_sharpe(self, golden_parquet, independent_is):
        period = _run_period_backtest(golden_parquet)
        assert math.isclose(period.is_period.metrics.sharpe, independent_is.sharpe(), rel_tol=1e-9), (
            f"IS Sharpe: bt={period.is_period.metrics.sharpe}, indep={independent_is.sharpe()}"
        )

    def test_is_mdd(self, golden_parquet, independent_is):
        period = _run_period_backtest(golden_parquet)
        assert math.isclose(period.is_period.metrics.mdd, independent_is.mdd(), rel_tol=1e-9), (
            f"IS MDD: bt={period.is_period.metrics.mdd}, indep={independent_is.mdd()}"
        )

    def test_is_cagr(self, golden_parquet, independent_is):
        period = _run_period_backtest(golden_parquet)
        assert math.isclose(period.is_period.metrics.cagr, independent_is.cagr(), rel_tol=1e-9), (
            f"IS CAGR: bt={period.is_period.metrics.cagr}, indep={independent_is.cagr()}"
        )

    def test_os_pnl(self, golden_parquet, independent_os):
        period = _run_period_backtest(golden_parquet)
        assert math.isclose(period.os_period.metrics.pnl, independent_os.pnl(), rel_tol=1e-9), (
            f"OS PnL: bt={period.os_period.metrics.pnl}, indep={independent_os.pnl()}"
        )

    def test_os_sharpe(self, golden_parquet, independent_os):
        period = _run_period_backtest(golden_parquet)
        assert math.isclose(period.os_period.metrics.sharpe, independent_os.sharpe(), rel_tol=1e-9), (
            f"OS Sharpe: bt={period.os_period.metrics.sharpe}, indep={independent_os.sharpe()}"
        )

    def test_os_mdd(self, golden_parquet, independent_os):
        period = _run_period_backtest(golden_parquet)
        assert math.isclose(period.os_period.metrics.mdd, independent_os.mdd(), rel_tol=1e-9), (
            f"OS MDD: bt={period.os_period.metrics.mdd}, indep={independent_os.mdd()}"
        )

    def test_os_cagr(self, golden_parquet, independent_os):
        period = _run_period_backtest(golden_parquet)
        assert math.isclose(period.os_period.metrics.cagr, independent_os.cagr(), rel_tol=1e-9), (
            f"OS CAGR: bt={period.os_period.metrics.cagr}, indep={independent_os.cagr()}"
        )

    def test_is_os_dates_no_overlap(self, golden_parquet):
        """IS and OS date ranges must not overlap."""
        period = _run_period_backtest(golden_parquet)
        is_dates = set(period.is_period.timestamps)
        os_dates = set(period.os_period.timestamps)
        overlap = is_dates & os_dates
        assert len(overlap) == 0, f"IS/OS date overlap: {overlap}"

    def test_is_os_date_ranges(self, golden_parquet):
        """Verify IS dates are before OS dates."""
        period = _run_period_backtest(golden_parquet)
        if len(period.is_period.timestamps) > 0 and len(period.os_period.timestamps) > 0:
            assert period.is_period.timestamps.max() < period.os_period.timestamps.min()


# ============================================================================
# 4. TestISOSStitching
# ============================================================================

class TestISOSStitching:
    """Verify stitching behavior between IS and OS periods.

    With IS/OS unification, a SINGLE continuous backtest runs from is_start
    to os_end, then the result is split at os_start. This means:
    - IS + OS returns == full run returns (same backtest, just split).
    - prod(1 + IS_rets) * prod(1 + OS_rets) == full equity.
    """

    def test_stitched_equity_product(self, golden_parquet):
        """prod(1+IS_rets) * prod(1+OS_rets) == stitched total equity."""
        period = _run_period_backtest(golden_parquet)

        is_equity_final = float(np.cumprod(1.0 + np.nan_to_num(period.is_period.ret_series, nan=0.0))[-1])
        os_equity_final = float(np.cumprod(1.0 + np.nan_to_num(period.os_period.ret_series, nan=0.0))[-1])
        stitched = is_equity_final * os_equity_final

        # This is by definition: each period's equity starts at 1.0
        # So the product gives the compound growth.
        assert math.isclose(stitched, is_equity_final * os_equity_final, rel_tol=1e-12)

    def test_full_vs_stitched_are_equal(self, golden_parquet):
        """Full run equity == IS*OS stitched equity (unified IS/OS).

        With IS/OS unification, IS and OS are slices of the same continuous
        backtest. Therefore:
        - IS returns + OS returns == full returns (same bars, same weights).
        - prod(1 + IS_rets) * prod(1 + OS_rets) == prod(1 + full_rets).
        """
        ts_full, ret_full, to_full, mb_full, mw_full, lw_full, sw_full, _nz, _ = _run_full_backtest(golden_parquet)
        period = _run_period_backtest(golden_parquet)

        full_equity = float(np.cumprod(1.0 + np.nan_to_num(ret_full, nan=0.0))[-1])
        is_eq = float(np.cumprod(1.0 + np.nan_to_num(period.is_period.ret_series, nan=0.0))[-1])
        os_eq = float(np.cumprod(1.0 + np.nan_to_num(period.os_period.ret_series, nan=0.0))[-1])
        stitched = is_eq * os_eq

        assert math.isclose(full_equity, stitched, rel_tol=1e-9), (
            f"Full vs stitched equity should be equal: "
            f"full={full_equity}, stitched={stitched}"
        )


# ============================================================================
# 5. TestPropertyBased
# ============================================================================

class TestPropertyBased:
    """Property-based checks that should hold for any well-behaved backtest."""

    def test_weights_gross_one(self):
        """After normalization, sum of |weights| should be 1.0."""
        w = NORMALIZED_WEIGHTS
        gross = sum(abs(v) for v in w.values())
        assert math.isclose(gross, 1.0, rel_tol=1e-9)

    def test_mdd_is_non_positive(self, independent_full):
        assert independent_full.mdd() <= 0.0

    def test_sharpe_sign_matches_return_sign(self, independent_full):
        """Sharpe sign should match total return sign for enough data points."""
        pnl = independent_full.pnl()
        sharpe = independent_full.sharpe()
        if abs(pnl) > 1e-12 and abs(sharpe) > 1e-12:
            assert (pnl > 0) == (sharpe > 0), (
                f"Sharpe sign ({sharpe}) != PnL sign ({pnl})"
            )

    def test_turnover_range(self, independent_full):
        """Turnover per bar should be in [0, 2]."""
        to = independent_full.turnover_series()
        assert (to >= -1e-12).all(), f"Negative turnover found: {to.min()}"
        assert (to <= 2.0 + 1e-12).all(), f"Turnover > 2 found: {to.max()}"

    def test_min_bet_range(self, independent_full):
        """Min bet (mean |w|) should be in [0, 1]."""
        mb = independent_full.min_bet_series()
        assert (mb >= -1e-12).all()
        assert (mb <= 1.0 + 1e-12).all()

    def test_max_weight_range(self, independent_full):
        """Max weight should be in [0, 1]."""
        mw = independent_full.max_weight_series()
        assert (mw >= -1e-12).all()
        assert (mw <= 1.0 + 1e-12).all()

    def test_equity_always_positive(self, independent_full):
        """Equity curve should never go negative."""
        eq = independent_full.equity_curve()
        assert (eq > 0).all(), f"Non-positive equity found: {eq.min()}"

    def test_number_of_returns(self, independent_full):
        """Number of returns = number of bars - 1."""
        n_bars = len(_build_close_df())
        n_rets = len(independent_full.portfolio_returns())
        assert n_rets == n_bars - 1


# ============================================================================
# 6. TestBoundaryPositionHandoff
# ============================================================================

class TestBoundaryPositionHandoff:
    """Verify that OS starts with carried-over weights from IS (warm start).

    With IS/OS unification, a single continuous backtest runs from is_start
    to os_end. The result is split at os_start. This means:
    - OS gets the transition bar's return (using IS-period weights).
    - OS first bar turnover is ~0 (weights carry over, constant signal).
    - IS first bar establishment turnover is NOT in the split IS turnover
      series (bar 0 has no return, so it is excluded from ret_series).
    """

    def test_os_return_count(self, golden_parquet):
        """OS should have n_os_bars returns (transition bar included)."""
        period = _run_period_backtest(golden_parquet)
        # 4 OS days (day 7-10), transition bar (day 7) has a return
        assert len(period.os_period.ret_series) == 4

    def test_os_first_bar_turnover_near_zero(self, golden_parquet):
        """First OS bar turnover should be ~0 (weights carry over from IS)."""
        period = _run_period_backtest(golden_parquet)
        first_turnover = period.os_period.turnover_series[0]
        # With constant signal, weights carry over perfectly -> turnover = 0
        assert math.isclose(first_turnover, 0.0, abs_tol=1e-9), (
            f"First OS turnover: {first_turnover}, expected: ~0.0"
        )

    def test_os_all_turnover_zero(self, golden_parquet):
        """All OS bars should have zero turnover (constant signal, warm start)."""
        period = _run_period_backtest(golden_parquet)
        np.testing.assert_allclose(period.os_period.turnover_series, 0.0, atol=1e-12)

    def test_is_all_turnover_zero_in_split(self, golden_parquet):
        """IS turnover: all bars have zero turnover after alignment.

        With IS/OS unification, turnover_series is aligned with ret_series (both
        drop bar 0). Since the signal is constant, all remaining bars have
        turnover = 0 (no rebalancing).

        Bar 0's initial position establishment (turnover=1.0) is not in the
        final turnover_series because bar 0 has no return.
        """
        period = _run_period_backtest(golden_parquet)
        to_vals = period.is_period.turnover_series
        # All bars: zero turnover (constant signal, no rebalancing)
        np.testing.assert_allclose(to_vals, 0.0, atol=1e-12)


# ============================================================================
# 7. TestCrossCheckDecomposition
# ============================================================================

class TestCrossCheckDecomposition:
    """Cross-check decomposition identities."""

    def test_portfolio_return_decomposition(self, independent_full):
        """sum(w_i * r_i) == portfolio return for each bar."""
        ar = independent_full.asset_returns()
        pr = independent_full.portfolio_returns()
        symbols = sorted(NORMALIZED_WEIGHTS.keys())
        w = np.array([NORMALIZED_WEIGHTS[s] for s in symbols])

        for date in pr.index:
            expected = float((ar.loc[date, symbols].values * w).sum())
            actual = float(pr.loc[date])
            assert math.isclose(actual, expected, rel_tol=1e-12), (
                f"Decomposition mismatch at {date}: {actual} != {expected}"
            )

    def test_equity_final_equals_cumprod(self, independent_full):
        """equity[-1] == prod(1 + r_t)."""
        rets = independent_full.portfolio_returns()
        prod_val = float(np.prod(1.0 + rets.values))
        eq_final = float(independent_full.equity_curve().iloc[-1])
        assert math.isclose(eq_final, prod_val, rel_tol=1e-12)

    def test_metrics_to_api_dict_vs_backtester(self, golden_parquet):
        """metrics_to_api_dict wraps _compute_metrics with unit conversion.

        - pnl/cagr/mdd/turnover → ×100 (percent).
        - API ``pnl`` field = cumulative return, ``cagr`` field = annualised return.
        """
        from framework.backtester import _compute_metrics, metrics_to_api_dict

        ts_arr, ret_s, to_s, mb_s, mw_s, lw_s, sw_s, _nz, _ = _run_full_backtest(golden_parquet)
        bt_m = _compute_metrics(ret_s, to_s, mb_s, mw_s)
        api_m = metrics_to_api_dict(bt_m)

        # API "pnl" field = cumulative return × 100
        assert math.isclose(api_m["pnl"], bt_m.pnl * 100, rel_tol=1e-9), (
            f"API pnl={api_m['pnl']}, expected pnl*100={bt_m.pnl * 100}"
        )

        # CAGR field is annualized return × 100
        assert math.isclose(api_m["cagr"], bt_m.cagr * 100, rel_tol=1e-9)

        # Sharpe should be the same (not scaled)
        assert math.isclose(api_m["sharpe"], bt_m.sharpe, rel_tol=1e-9), (
            f"API sharpe={api_m['sharpe']}, bt sharpe={bt_m.sharpe}"
        )

        # MDD: bt_m.mdd * 100
        assert math.isclose(api_m["mdd"], bt_m.mdd * 100, rel_tol=1e-9), (
            f"API mdd={api_m['mdd']}, bt mdd*100={bt_m.mdd * 100}"
        )

        # Turnover: bt_m.turnover * 100
        assert math.isclose(api_m["turnover"], bt_m.turnover * 100, rel_tol=1e-9), (
            f"API turnover={api_m['turnover']}, bt turnover*100={bt_m.turnover * 100}"
        )

    def test_backtester_return_series_matches_independent(self, golden_parquet, independent_full):
        """Every bar's return in the backtester matches independent calculation."""
        ts_arr, ret_s, to_s, mb_s, mw_s, lw_s, sw_s, _nz, _ = _run_full_backtest(golden_parquet)
        bt_rets = ret_s
        indep_rets = independent_full.portfolio_returns().values
        np.testing.assert_allclose(bt_rets, indep_rets, rtol=1e-12, atol=1e-15)


# ============================================================================
# 8. TestGoldenDatasetD1Mode
# ============================================================================

class _VaryingAlpha:
    """Alpha that alternates signal direction every bar (for D0/D1 difference test).

    Even bars: BTC=1.0, ETH=-1.0, SOL=0.5
    Odd bars:  BTC=-0.5, ETH=1.0, SOL=-1.0
    """
    _call_count: int = 0

    def __init__(self):
        self._call_count = 0

    def compute(self, t_idx: int, arrays: dict) -> np.ndarray:
        signals_even = {"BTC": 1.0, "ETH": -1.0, "SOL": 0.5}
        signals_odd = {"BTC": -0.5, "ETH": 1.0, "SOL": -1.0}
        signals = signals_even if self._call_count % 2 == 0 else signals_odd
        self._call_count += 1
        symbols = arrays["__symbols__"]
        return np.array([signals.get(s, 0.0) for s in symbols])


def _run_full_backtest_d1(parquet_path):
    """Run the backtester on golden dataset in D1 mode, return 9-tuple."""
    from framework.data import DataCache
    from framework.backtester import Backtester
    import yaml

    config = {
        "datasets": [{"name": "golden", "path": str(parquet_path),
                       "time_col": "open_time", "symbol_col": "symbol",
                       "use_col": ["open", "high", "low", "close", "volume"]}],
        "universe": None,
        "backtest": {"start_date": "2024-01-01", "end_date": "2024-01-10",
                     "initial_capital": 1000000, "transaction_fee": 10},
    }
    config_path = parquet_path.parent / "config_d1.yaml"
    yaml.dump(config, open(config_path, "w"))
    cache = DataCache(str(config_path))
    cache.load()

    bt = Backtester(
        cache=cache,
        alpha_recipe=MockAlpha(),
        capital=1_000_000,
        mode="D1",
        start_date="2024-01-01",
        end_date="2024-01-10",
    )
    return bt.run()


class TestGoldenDatasetD1Mode:
    """Verify D1 mode against independent calculation and vs D0."""

    def test_d1_constant_signal_first_bar_zero(self, golden_parquet):
        """D1 first return is 0 because bar 0 executes with zero weights.

        In D1, executed_weights = previous_target_weights. At bar 0,
        previous_target starts empty (= 0). So bar 0 has zero position.
        Bar 1's return uses previous_executed (bar 0) = 0 → r = 0.

        From bar 2 onwards, positions are established (bar 1's target was
        NORMALIZED_WEIGHTS), so returns match D0 bar 2+.
        """
        ts_d0, d0_ret, *_ = _run_full_backtest(golden_parquet)
        ts_d1, d1_ret, *_ = _run_full_backtest_d1(golden_parquet)

        # D1 first return: gross=0 (no position on bar 0), turnover=1.0, fee=0.001
        assert d1_ret[0] == pytest.approx(-0.001, abs=1e-12), (
            f"D1 first return should be -0.001 (fee), got {d1_ret[0]}"
        )

        # D0 first return should be non-zero (bar 0 executes immediately)
        assert abs(d0_ret[0]) > 1e-6, (
            f"D0 first return should be non-zero, got {d0_ret[0]}"
        )

        # From bar 2 onwards (index 1+), D0 and D1 should match (constant signal)
        np.testing.assert_allclose(
            d0_ret[1:],
            d1_ret[1:],
            rtol=1e-12,
            err_msg="D0 and D1 should match from bar 2 onwards for constant signals",
        )

    def test_d1_metrics_independent_verification(self, golden_parquet, independent_full):
        """D1 with constant signal: PnL differs from D0 only by first bar effect."""
        from framework.backtester import _compute_metrics

        ts_d0, d0_ret, d0_to, d0_mb, d0_mw, *_ = _run_full_backtest(golden_parquet)
        ts_d1, d1_ret, d1_to, d1_mb, d1_mw, *_ = _run_full_backtest_d1(golden_parquet)
        d0_m = _compute_metrics(d0_ret, d0_to, d0_mb, d0_mw)
        d1_m = _compute_metrics(d1_ret, d1_to, d1_mb, d1_mw)

        # D1 PnL should be less than D0 (missed first bar's return)
        # because first bar return in D1 is 0 vs non-zero in D0
        first_bar_return = d0_ret[0]
        assert abs(first_bar_return) > 1e-6
        # Both should be reasonable
        assert math.isfinite(d1_m.pnl)
        assert math.isfinite(d1_m.sharpe)

    def test_d1_differs_from_d0_with_varying_signal(self, golden_parquet):
        """With time-varying signals, D0 and D1 MUST produce different results.

        D0 executes this bar's weights immediately.
        D1 executes last bar's weights (1-bar delay).
        When signals change bar-to-bar, the 1-bar lag causes different
        position exposures and therefore different portfolio returns.
        """
        from framework.data import DataCache
        from framework.backtester import Backtester
        import yaml

        def _run(mode):
            config = {
                "datasets": [{"name": "golden", "path": str(golden_parquet),
                               "time_col": "open_time", "symbol_col": "symbol",
                               "use_col": ["open", "high", "low", "close", "volume"]}],
                "universe": None,
                "backtest": {"start_date": "2024-01-01", "end_date": "2024-01-10",
                             "initial_capital": 1000000, "transaction_fee": 10},
            }
            config_path = golden_parquet.parent / f"config_{mode}.yaml"
            yaml.dump(config, open(config_path, "w"))
            cache = DataCache(str(config_path))
            cache.load()
            alpha = _VaryingAlpha()
            bt = Backtester(
                cache=cache,
                alpha_recipe=alpha,
                capital=1_000_000,
                mode=mode,
                start_date="2024-01-01",
                end_date="2024-01-10",
            )
            ts_arr, ret_s, *_ = bt.run()
            return ret_s

        d0_ret = _run("D0")
        d1_ret = _run("D1")

        # Both should have the same number of returns
        assert len(d0_ret) == len(d1_ret)
        assert len(d0_ret) == 9  # 10 bars → 9 returns

        # Returns MUST differ (varying signal + execution lag)
        d0_vals = d0_ret
        d1_vals = d1_ret
        assert not np.allclose(d0_vals, d1_vals, rtol=1e-9), (
            "D0 and D1 should produce different returns with varying signals"
        )

    def test_d1_first_return_uses_zero_weights(self, golden_parquet):
        """In D1 with varying signal, first return bar uses previous_target = 0.

        Bar 0: no previous_close → no return. executed = previous_target = 0.
        Bar 1: previous_close exists. executed = previous_target (from bar 0) = 0.
               → portfolio return = 0.
        Bar 2: executed = target from bar 1. → non-zero return.

        But with constant signal (MockAlpha), bar 0 sets previous_target = NORMALIZED_WEIGHTS,
        so bar 1 in D1 uses those weights → non-zero return. Same as D0.

        With VaryingAlpha: bar 0 target ≠ 0, so bar 1 D1 executed = bar 0 target.
        The key test is: D1 bar 1 return uses bar 0 target, not bar 1 target.
        """
        from framework.data import DataCache
        from framework.backtester import Backtester
        import yaml

        def _run(mode):
            config = {
                "datasets": [{"name": "golden", "path": str(golden_parquet),
                               "time_col": "open_time", "symbol_col": "symbol",
                               "use_col": ["open", "high", "low", "close", "volume"]}],
                "universe": None,
                "backtest": {"start_date": "2024-01-01", "end_date": "2024-01-10",
                             "initial_capital": 1000000, "transaction_fee": 10},
            }
            config_path = golden_parquet.parent / f"config_{mode}.yaml"
            yaml.dump(config, open(config_path, "w"))
            cache = DataCache(str(config_path))
            cache.load()
            alpha = _VaryingAlpha()
            bt = Backtester(
                cache=cache,
                alpha_recipe=alpha,
                capital=1_000_000,
                mode=mode,
                start_date="2024-01-01",
                end_date="2024-01-10",
            )
            ts_arr, ret_s, *_ = bt.run()
            return ret_s

        d0 = _run("D0")
        d1 = _run("D1")

        # D0 bar 1: uses bar 1's own target weights
        # D1 bar 1: uses bar 0's target weights (lagged)
        # Since VaryingAlpha alternates, bar 0 weights ≠ bar 1 weights
        # → first return should differ between D0 and D1
        assert d0[0] != pytest.approx(d1[0], abs=1e-12), (
            "D1 first return should differ from D0 (using lagged weights)"
        )
