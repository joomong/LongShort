# tests/unit/test_alpha_detail_view.py
"""
Alpha Detail View feature tests.

Tests for the new long/short weight tracking, turnover/min_bet/max_weight
time series, and long/short ratio fields added to PeriodResult,
BacktestPeriodResult, AlphaResult, and the API layer.

Test ID convention:
- TC-ADV-*: Alpha Detail View tests
"""
import pytest
import pandas as pd
import numpy as np
import yaml
from pathlib import Path
from unittest.mock import MagicMock, patch

from framework.backtester import (
    Backtester, BacktestPeriodResult,
    PeriodMetrics, PeriodSettings, PeriodResult,
    run_backtest_with_periods, AlphaResult, MultipleBacktestResult,
    run_multiple_backtest, _split_series_by_date,
)
from framework.data import DataCache
from tests.conftest import MockAlpha


# =============================================================================
# Helper function for creating DataCache
# =============================================================================

def _make_cache(parquet_path, start_date, end_date, tmp_path):
    config = {
        "datasets": [{"name": "test", "path": str(parquet_path),
                       "time_col": "open_time", "symbol_col": "symbol",
                       "use_col": ["open", "high", "low", "close", "volume"]}],
        "universe": None,
        "backtest": {"start_date": start_date, "end_date": end_date,
                     "initial_capital": 1000000, "transaction_fee": 10},
    }
    config_path = tmp_path / "config.yaml"
    yaml.dump(config, open(config_path, "w"))
    cache = DataCache(str(config_path))
    cache.load()
    return cache


# =============================================================================
# Fixtures (same patterns as test_backtester.py)
# =============================================================================

@pytest.fixture
def basic_data_path(tmp_path) -> Path:
    """3-day, 2-symbol data for basic tests."""
    file_path = tmp_path / "basic_data.parquet"
    data = []
    symbols = ["A", "B"]
    dates = [
        pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2024-01-02", tz="UTC"),
        pd.Timestamp("2024-01-03", tz="UTC"),
    ]
    closes = {"A": [100, 110, 105], "B": [50, 45, 55]}

    for i, ts in enumerate(dates):
        ts_ms = int(ts.value // 1_000_000)
        for sym in symbols:
            close = closes[sym][i]
            data.append({
                "open_time": ts_ms, "symbol": sym,
                "open": close - 1, "high": close + 2,
                "low": close - 2, "close": close, "volume": 1000.0,
            })

    pd.DataFrame(data).to_parquet(file_path, index=False)
    return file_path


@pytest.fixture
def isos_data_path(tmp_path) -> Path:
    """6-day data for IS/OS tests (IS=1~3, OS=4~6)."""
    file_path = tmp_path / "isos_data.parquet"
    symbols = ["A", "B"]
    dates = [pd.Timestamp(f"2024-01-0{d}", tz="UTC") for d in range(1, 7)]
    closes_a = [100, 110, 105, 108, 115, 112]
    closes_b = [50, 45, 55, 52, 48, 60]
    data = []
    for i, ts in enumerate(dates):
        ts_ms = int(ts.value // 1_000_000)
        for sym, closes in [("A", closes_a), ("B", closes_b)]:
            c = closes[i]
            data.append({
                "open_time": ts_ms, "symbol": sym,
                "open": c - 1, "high": c + 2, "low": c - 2,
                "close": c, "volume": 1000.0,
            })
    pd.DataFrame(data).to_parquet(file_path, index=False)
    return file_path


@pytest.fixture
def five_symbol_data_path(tmp_path) -> Path:
    """3-day, 5-symbol data for long/short ratio tests.

    Symbols A, B, C have positive signals; D, E have negative signals.
    """
    file_path = tmp_path / "five_sym.parquet"
    symbols = ["A", "B", "C", "D", "E"]
    dates = [
        pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2024-01-02", tz="UTC"),
        pd.Timestamp("2024-01-03", tz="UTC"),
    ]
    base_closes = {"A": [100, 110, 105], "B": [50, 55, 52],
                   "C": [80, 85, 82], "D": [200, 190, 195],
                   "E": [150, 140, 145]}
    data = []
    for i, ts in enumerate(dates):
        ts_ms = int(ts.value // 1_000_000)
        for sym in symbols:
            c = base_closes[sym][i]
            data.append({
                "open_time": ts_ms, "symbol": sym,
                "open": c - 1, "high": c + 2, "low": c - 2,
                "close": c, "volume": 1000.0,
            })
    pd.DataFrame(data).to_parquet(file_path, index=False)
    return file_path


# =============================================================================
# TC-ADV-001: Long/short weight series populated from bt.run()
# =============================================================================

class TestBacktestResultLongShortFields:
    """TC-ADV-001: Verify long_weight_series and short_weight_series are populated."""

    def test_fields_populated_after_run(self, basic_data_path, tmp_path):
        """lw_series and sw_series should not be empty after run()."""
        cache = _make_cache(basic_data_path, "2024-01-01", "2024-01-03", tmp_path)
        alpha = MockAlpha()
        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000)
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        assert lw_s is not None
        assert sw_s is not None
        assert len(lw_s) == 3  # one entry per timestamp
        assert len(sw_s) == 3

    def test_fields_are_pd_series(self, basic_data_path, tmp_path):
        """The weight series should be pandas Series."""
        cache = _make_cache(basic_data_path, "2024-01-01", "2024-01-03", tmp_path)
        alpha = MockAlpha()
        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000)
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        assert isinstance(lw_s, pd.Series)
        assert isinstance(sw_s, pd.Series)


# =============================================================================
# TC-ADV-002: Long/short weight correctness
# =============================================================================

class TestLongShortWeightCorrectness:
    """TC-ADV-002: With known signals, verify long/short weight sums."""

    def test_mixed_signals(self, five_symbol_data_path, tmp_path):
        """3 positive + 2 negative signals: verify long and short weight sums.

        Alpha signals: A=+0.3, B=+0.2, C=+0.1, D=-0.2, E=-0.2
        abs_sum = 1.0
        Normalized weights: A=0.3, B=0.2, C=0.1, D=-0.2, E=-0.2
        Long sum = 0.3 + 0.2 + 0.1 = 0.6
        Short sum = |(-0.2)| + |(-0.2)| = 0.4
        """
        # Symbols sorted: A, B, C, D, E -> indices 0, 1, 2, 3, 4
        sig = np.array([0.3, 0.2, 0.1, -0.2, -0.2])
        alpha = MockAlpha(weights_map={
            0: sig, 1: sig, 2: sig,
        })

        cache = _make_cache(five_symbol_data_path, "2024-01-01", "2024-01-03", tmp_path)

        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000)
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        # Executed weights after normalization: same as input (abs_sum=1.0)
        # lw_s.iloc[0] is bar 1 (bar 0 is dropped)
        assert abs(lw_s.iloc[0] - 0.6) < 1e-9
        assert abs(sw_s.iloc[0] - 0.4) < 1e-9

    def test_all_positive_signals(self, basic_data_path, tmp_path):
        """All positive signals: long_weight should equal 1.0, short_weight should be 0.0."""
        # Symbols sorted: A, B -> indices 0, 1
        sig = np.array([0.6, 0.4])
        alpha = MockAlpha(weights_map={0: sig, 1: sig, 2: sig})

        cache = _make_cache(basic_data_path, "2024-01-01", "2024-01-03", tmp_path)
        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000)
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        assert abs(lw_s.iloc[0] - 1.0) < 1e-9
        assert abs(sw_s.iloc[0] - 0.0) < 1e-9


# =============================================================================
# TC-ADV-003: BacktestPeriodResult new fields populated
# =============================================================================

class TestBacktestPeriodResultFields:
    """TC-ADV-003: run_backtest_with_periods populates all new time series fields."""

    def test_new_fields_not_none(self, isos_data_path, tmp_path):
        """All new optional fields should be populated (not None)."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        cache = _make_cache(isos_data_path, settings.is_start, settings.os_end, tmp_path)
        result = run_backtest_with_periods(
            alpha=MockAlpha(),
            settings=settings, capital=1_000_000,
            data_cache=cache,
        )

        assert result.is_period.turnover_series is not None
        assert result.os_period.turnover_series is not None
        assert result.is_period.min_bet_series is not None
        assert result.os_period.min_bet_series is not None
        assert result.is_period.max_weight_series is not None
        assert result.os_period.max_weight_series is not None

    def test_time_series_lengths(self, isos_data_path, tmp_path):
        """IS/OS time series should have expected lengths (3 bars each)."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        cache = _make_cache(isos_data_path, settings.is_start, settings.os_end, tmp_path)
        result = run_backtest_with_periods(
            alpha=MockAlpha(),
            settings=settings, capital=1_000_000,
            data_cache=cache,
        )

        # IS: 3 bars → ret_series has 2 entries (first bar no return),
        #   turnover/min_bet have 3 entries (all bars including bar 0)
        # OS: 3 bars → 3 entries for all series
        assert len(result.is_period.ret_series) == 2
        assert len(result.is_period.turnover_series) == 3
        assert len(result.os_period.turnover_series) == 3
        assert len(result.is_period.min_bet_series) == 3
        assert len(result.os_period.min_bet_series) == 3


# =============================================================================
# TC-ADV-004: Long/short ratio calculation
# =============================================================================

class TestLongShortRatio:
    """TC-ADV-004: Verify is_long_ratio and is_short_ratio."""

    def test_ratios_sum_to_one(self, isos_data_path, tmp_path):
        """is_long_ratio + is_short_ratio should sum to ~1.0."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        cache = _make_cache(isos_data_path, settings.is_start, settings.os_end, tmp_path)
        result = run_backtest_with_periods(
            alpha=MockAlpha(),
            settings=settings, capital=1_000_000,
            data_cache=cache,
        )
        total = result.is_period.long_ratio + result.is_period.short_ratio
        assert abs(total - 1.0) < 1e-6

    def test_ratios_in_range(self, isos_data_path, tmp_path):
        """Both ratios should be in [0, 1]."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        cache = _make_cache(isos_data_path, settings.is_start, settings.os_end, tmp_path)
        result = run_backtest_with_periods(
            alpha=MockAlpha(),
            settings=settings, capital=1_000_000,
            data_cache=cache,
        )
        assert 0.0 <= result.is_period.long_ratio <= 1.0
        assert 0.0 <= result.is_period.short_ratio <= 1.0

    def test_all_long_ratio(self, isos_data_path, tmp_path):
        """With all-positive signals, is_long_ratio ~ 1.0, is_short_ratio ~ 0.0."""
        # MockAlpha default: all equal positive weights -> all long
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        cache = _make_cache(isos_data_path, settings.is_start, settings.os_end, tmp_path)
        result = run_backtest_with_periods(
            alpha=MockAlpha(),
            settings=settings, capital=1_000_000,
            data_cache=cache,
        )
        assert abs(result.is_period.long_ratio - 1.0) < 1e-6
        assert abs(result.is_period.short_ratio - 0.0) < 1e-6


# =============================================================================
# TC-ADV-005: AlphaResult fields in multi-alpha backtest
# =============================================================================

class TestAlphaResultMultiAlpha:
    """TC-ADV-005: run_multiple_backtest propagates new fields to AlphaResult."""

    def test_alpha_result_has_new_fields(self, isos_data_path, tmp_path):
        """Each AlphaResult should have the new detail view fields."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        alpha1 = MockAlpha()
        alpha1.name = "alpha_1"
        alpha2 = MockAlpha()
        alpha2.name = "alpha_2"

        cache = _make_cache(isos_data_path, settings.is_start, settings.os_end, tmp_path)
        result = run_multiple_backtest(
            alphas=[alpha1, alpha2],

            settings=settings, capital=1_000_000,
            data_cache=cache,
        )

        for ar in result.alpha_results:
            assert ar.is_period.turnover_series is not None
            assert ar.os_period.turnover_series is not None
            assert ar.is_period.min_bet_series is not None
            assert ar.os_period.min_bet_series is not None
            assert ar.is_period.max_weight_series is not None
            assert ar.os_period.max_weight_series is not None
            assert isinstance(ar.is_period.long_ratio, float)
            assert isinstance(ar.is_period.short_ratio, float)

    def test_alpha_result_ratios_valid(self, isos_data_path, tmp_path):
        """AlphaResult ratios should be in [0,1] and sum to ~1."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        alpha1 = MockAlpha()
        alpha1.name = "a1"

        cache = _make_cache(isos_data_path, settings.is_start, settings.os_end, tmp_path)
        result = run_multiple_backtest(
            alphas=[alpha1],

            settings=settings, capital=1_000_000,
            data_cache=cache,
        )

        ar = result.alpha_results[0]
        assert 0.0 <= ar.is_period.long_ratio <= 1.0
        assert 0.0 <= ar.is_period.short_ratio <= 1.0
        assert abs(ar.is_period.long_ratio + ar.is_period.short_ratio - 1.0) < 1e-6


# =============================================================================
# TC-ADV-007: _split_series_by_date preserves series on both sides
# =============================================================================

class TestSplitSeriesByDate:
    """TC-ADV-007: _split_series_by_date splits series correctly."""

    def test_split_preserves_long_short_series(self):
        """Both IS and OS sub-series should be split correctly."""
        dates = pd.to_datetime(
            ["2024-01-01", "2024-01-02", "2024-03-01", "2024-03-02"], utc=True
        )
        lw_series = pd.Series([0.6, 0.7, 0.55, 0.65], index=dates)
        sw_series = pd.Series([0.4, 0.3, 0.45, 0.35], index=dates)

        is_lw, os_lw = _split_series_by_date(lw_series, "2024-03-01")
        is_sw, os_sw = _split_series_by_date(sw_series, "2024-03-01")

        # IS side
        assert len(is_lw) == 2
        assert len(is_sw) == 2
        assert abs(is_lw.iloc[0] - 0.6) < 1e-9
        assert abs(is_sw.iloc[0] - 0.4) < 1e-9

        # OS side
        assert len(os_lw) == 2
        assert len(os_sw) == 2
        assert abs(os_lw.iloc[0] - 0.55) < 1e-9
        assert abs(os_sw.iloc[0] - 0.45) < 1e-9

    def test_split_empty_series(self):
        """Empty series should split into two empty series without error."""
        empty = pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
        is_part, os_part = _split_series_by_date(empty, "2024-03-01")
        assert len(is_part) == 0
        assert len(os_part) == 0


# =============================================================================
# TC-ADV-009: Edge case -- all-long signals
# =============================================================================

class TestEdgeCaseAllLong:
    """TC-ADV-009: All-long signals edge case."""

    def test_all_long_short_weight_zero(self, basic_data_path, tmp_path):
        """With all-positive signals, sw_series should be all zeros."""
        # Symbols sorted: A, B -> indices 0, 1
        # t_idx 0 = day1, t_idx 1 = day2
        alpha = MockAlpha(weights_map={
            0: np.array([0.7, 0.3]),
            1: np.array([0.5, 0.5]),
        })

        cache = _make_cache(basic_data_path, "2024-01-01", "2024-01-03", tmp_path)
        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000)
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        # All short weights should be zero
        for val in sw_s.values:
            assert abs(val) < 1e-9, f"Expected 0.0, got {val}"

        # All long weights should be 1.0 (gross=1 normalized)
        for val in lw_s.values:
            assert abs(val - 1.0) < 1e-9, f"Expected 1.0, got {val}"

    def test_all_long_ratio_one(self, isos_data_path, tmp_path):
        """All-long signals: is_long_ratio ~ 1.0."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        # Default MockAlpha returns equal positive weights
        cache = _make_cache(isos_data_path, settings.is_start, settings.os_end, tmp_path)
        result = run_backtest_with_periods(
            alpha=MockAlpha(),
            settings=settings, capital=1_000_000,
            data_cache=cache,
        )
        assert abs(result.is_period.long_ratio - 1.0) < 1e-6
        assert abs(result.is_period.short_ratio - 0.0) < 1e-6


# =============================================================================
# TC-ADV-009b: Edge case -- all-short signals
# =============================================================================

class TestEdgeCaseAllShort:
    """TC-ADV-009b: All-short (all-negative) signals edge case."""

    def test_all_short_long_weight_zero(self, basic_data_path, tmp_path):
        """With all-negative signals, lw_series should be all zeros."""
        # Symbols sorted: A, B -> indices 0, 1
        # t_idx 0 = day1, t_idx 1 = day2, t_idx 2 = day3
        alpha = MockAlpha(weights_map={
            0: np.array([-0.6, -0.4]),
            1: np.array([-0.5, -0.5]),
            2: np.array([-0.7, -0.3]),
        })

        cache = _make_cache(basic_data_path, "2024-01-01", "2024-01-03", tmp_path)
        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000)
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        # All long weights should be zero
        for val in lw_s.values:
            assert abs(val) < 1e-9, f"Expected 0.0, got {val}"

        # All short weights should be 1.0 (gross=1 normalized)
        for val in sw_s.values:
            assert abs(val - 1.0) < 1e-9, f"Expected 1.0, got {val}"

    def test_all_short_ratio_one(self, isos_data_path, tmp_path):
        """All-short signals: is_short_ratio ~ 1.0, is_long_ratio ~ 0.0."""
        # 6 days total: IS covers t_idx 0-2, OS covers t_idx 3-5
        alpha = MockAlpha(weights_map={
            0: np.array([-0.6, -0.4]),
            1: np.array([-0.5, -0.5]),
            2: np.array([-0.7, -0.3]),
            3: np.array([-0.6, -0.4]),
            4: np.array([-0.5, -0.5]),
            5: np.array([-0.7, -0.3]),
        })

        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        cache = _make_cache(isos_data_path, settings.is_start, settings.os_end, tmp_path)
        result = run_backtest_with_periods(
            alpha=alpha,
            settings=settings, capital=1_000_000,
            data_cache=cache,
        )
        assert abs(result.is_period.short_ratio - 1.0) < 1e-6
        assert abs(result.is_period.long_ratio - 0.0) < 1e-6


# =============================================================================
# TC-ADV-010: Edge case -- all-zero signals
# =============================================================================

class TestEdgeCaseAllZero:
    """TC-ADV-010: All-zero signals -> no division error, ratios = 0."""

    def test_all_zero_no_error(self, basic_data_path, tmp_path):
        """All-zero signals should not cause division errors."""
        # t_idx 0, 1, 2 for 3-day data
        alpha = MockAlpha(weights_map={
            0: np.array([0.0, 0.0]),
            1: np.array([0.0, 0.0]),
            2: np.array([0.0, 0.0]),
        })

        cache = _make_cache(basic_data_path, "2024-01-01", "2024-01-03", tmp_path)
        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000)
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        # Both series should be all zeros, no NaN
        for val in lw_s.values:
            assert val == 0.0
        for val in sw_s.values:
            assert val == 0.0

    def test_all_zero_ratio_zero(self, isos_data_path, tmp_path):
        """All-zero signals: both ratios should be 0.0 (not NaN)."""
        # 6 days total, t_idx 0-5
        alpha = MockAlpha(weights_map={
            0: np.array([0.0, 0.0]),
            1: np.array([0.0, 0.0]),
            2: np.array([0.0, 0.0]),
            3: np.array([0.0, 0.0]),
            4: np.array([0.0, 0.0]),
            5: np.array([0.0, 0.0]),
        })

        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )

        cache = _make_cache(isos_data_path, settings.is_start, settings.os_end, tmp_path)
        result = run_backtest_with_periods(
            alpha=alpha,
            settings=settings, capital=1_000_000,
            data_cache=cache,
        )

        assert result.is_period.long_ratio == 0.0
        assert result.is_period.short_ratio == 0.0
        # Ensure no NaN
        assert not np.isnan(result.is_period.long_ratio)
        assert not np.isnan(result.is_period.short_ratio)
