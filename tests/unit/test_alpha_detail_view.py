# tests/unit/test_alpha_detail_view.py
"""
Alpha Detail View feature tests.

Tests for the new long/short weight tracking, turnover/min_bet/max_weight
time series, and long/short ratio fields added to BacktestResult,
BacktestPeriodResult, AlphaResult, and the API layer.

Test ID convention:
- TC-ADV-*: Alpha Detail View tests
"""
import pytest
import pandas as pd
import numpy as np
from pathlib import Path
from unittest.mock import MagicMock, patch

from framework.backtester import (
    Backtester, BacktestResult, BacktestPeriodResult,
    PeriodMetrics, PeriodSettings,
    run_backtest_with_periods, AlphaResult, MultipleBacktestResult,
    run_multiple_backtest,
)
from framework.dataloader import DataLoader
from tests.conftest import MockAlpha, MockStrategy


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
# TC-ADV-001: BacktestResult long/short weight fields populated
# =============================================================================

class TestBacktestResultLongShortFields:
    """TC-ADV-001: Verify long_weight_series and short_weight_series are populated."""

    def test_fields_populated_after_run(self, basic_data_path, mock_strategy):
        """long_weight_series and short_weight_series should not be None after run()."""
        loader = DataLoader(
            file_path=str(basic_data_path),
            start_date="2024-01-01", end_date="2024-01-03",
            time_col="open_time", symbol_col="symbol",
        )
        alpha = MockAlpha()
        bt = Backtester(loader, alpha, mock_strategy, capital=1_000_000)
        result, _ = bt.run()

        assert result.long_weight_series is not None
        assert result.short_weight_series is not None
        assert len(result.long_weight_series) == 3  # one entry per timestamp
        assert len(result.short_weight_series) == 3

    def test_fields_are_pd_series(self, basic_data_path, mock_strategy):
        """The weight series should be pandas Series."""
        loader = DataLoader(
            file_path=str(basic_data_path),
            start_date="2024-01-01", end_date="2024-01-03",
            time_col="open_time", symbol_col="symbol",
        )
        alpha = MockAlpha()
        bt = Backtester(loader, alpha, mock_strategy, capital=1_000_000)
        result, _ = bt.run()

        assert isinstance(result.long_weight_series, pd.Series)
        assert isinstance(result.short_weight_series, pd.Series)


# =============================================================================
# TC-ADV-002: Long/short weight correctness
# =============================================================================

class TestLongShortWeightCorrectness:
    """TC-ADV-002: With known signals, verify long/short weight sums."""

    def test_mixed_signals(self, five_symbol_data_path, mock_strategy):
        """3 positive + 2 negative signals: verify long and short weight sums.

        Alpha signals: A=+0.3, B=+0.2, C=+0.1, D=-0.2, E=-0.2
        abs_sum = 1.0
        Normalized weights: A=0.3, B=0.2, C=0.1, D=-0.2, E=-0.2
        Long sum = 0.3 + 0.2 + 0.1 = 0.6
        Short sum = |(-0.2)| + |(-0.2)| = 0.4
        """
        day1 = pd.Timestamp("2024-01-01", tz="UTC")

        alpha = MockAlpha(weights_map={
            day1: {"A": 0.3, "B": 0.2, "C": 0.1, "D": -0.2, "E": -0.2},
        })

        loader = DataLoader(
            file_path=str(five_symbol_data_path),
            start_date="2024-01-01", end_date="2024-01-03",
            time_col="open_time", symbol_col="symbol",
        )

        bt = Backtester(loader, alpha, mock_strategy, capital=1_000_000)
        result, _ = bt.run()

        # Day1 executed weights after normalization: same as input (abs_sum=1.0)
        assert abs(result.long_weight_series.iloc[0] - 0.6) < 1e-9
        assert abs(result.short_weight_series.iloc[0] - 0.4) < 1e-9

    def test_all_positive_signals(self, basic_data_path, mock_strategy):
        """All positive signals: long_weight should equal 1.0, short_weight should be 0.0."""
        day1 = pd.Timestamp("2024-01-01", tz="UTC")
        alpha = MockAlpha(weights_map={
            day1: {"A": 0.6, "B": 0.4},
        })

        loader = DataLoader(
            file_path=str(basic_data_path),
            start_date="2024-01-01", end_date="2024-01-03",
            time_col="open_time", symbol_col="symbol",
        )
        bt = Backtester(loader, alpha, mock_strategy, capital=1_000_000)
        result, _ = bt.run()

        assert abs(result.long_weight_series.iloc[0] - 1.0) < 1e-9
        assert abs(result.short_weight_series.iloc[0] - 0.0) < 1e-9


# =============================================================================
# TC-ADV-003: BacktestPeriodResult new fields populated
# =============================================================================

class TestBacktestPeriodResultFields:
    """TC-ADV-003: run_backtest_with_periods populates all new time series fields."""

    def test_new_fields_not_none(self, isos_data_path):
        """All new optional fields should be populated (not None)."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        result = run_backtest_with_periods(
            alpha=MockAlpha(), strategy=MockStrategy(),
            settings=settings, capital=1_000_000,
            data_loader_kwargs={
                "file_path": str(isos_data_path),
                "time_col": "open_time", "symbol_col": "symbol",
            },
        )

        assert result.is_turnover_series is not None
        assert result.os_turnover_series is not None
        assert result.is_min_bet_series is not None
        assert result.os_min_bet_series is not None
        assert result.is_max_weight_series is not None
        assert result.os_max_weight_series is not None

    def test_time_series_lengths(self, isos_data_path):
        """IS/OS time series should have expected lengths (3 bars each)."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        result = run_backtest_with_periods(
            alpha=MockAlpha(), strategy=MockStrategy(),
            settings=settings, capital=1_000_000,
            data_loader_kwargs={
                "file_path": str(isos_data_path),
                "time_col": "open_time", "symbol_col": "symbol",
            },
        )

        # IS: 3 bars, first bar has no return -> 2 time series entries
        # OS: 3 bars, transition bar has return (warm start) -> 3 time series entries
        assert len(result.is_turnover_series) == 2
        assert len(result.os_turnover_series) == 3
        assert len(result.is_min_bet_series) == 2
        assert len(result.os_min_bet_series) == 3


# =============================================================================
# TC-ADV-004: Long/short ratio calculation
# =============================================================================

class TestLongShortRatio:
    """TC-ADV-004: Verify is_long_ratio and is_short_ratio."""

    def test_ratios_sum_to_one(self, isos_data_path):
        """is_long_ratio + is_short_ratio should sum to ~1.0."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        result = run_backtest_with_periods(
            alpha=MockAlpha(), strategy=MockStrategy(),
            settings=settings, capital=1_000_000,
            data_loader_kwargs={
                "file_path": str(isos_data_path),
                "time_col": "open_time", "symbol_col": "symbol",
            },
        )
        total = result.is_long_ratio + result.is_short_ratio
        assert abs(total - 1.0) < 1e-6

    def test_ratios_in_range(self, isos_data_path):
        """Both ratios should be in [0, 1]."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        result = run_backtest_with_periods(
            alpha=MockAlpha(), strategy=MockStrategy(),
            settings=settings, capital=1_000_000,
            data_loader_kwargs={
                "file_path": str(isos_data_path),
                "time_col": "open_time", "symbol_col": "symbol",
            },
        )
        assert 0.0 <= result.is_long_ratio <= 1.0
        assert 0.0 <= result.is_short_ratio <= 1.0

    def test_all_long_ratio(self, isos_data_path):
        """With all-positive signals, is_long_ratio ~ 1.0, is_short_ratio ~ 0.0."""
        # MockAlpha default: all equal positive weights -> all long
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        result = run_backtest_with_periods(
            alpha=MockAlpha(), strategy=MockStrategy(),
            settings=settings, capital=1_000_000,
            data_loader_kwargs={
                "file_path": str(isos_data_path),
                "time_col": "open_time", "symbol_col": "symbol",
            },
        )
        assert abs(result.is_long_ratio - 1.0) < 1e-6
        assert abs(result.is_short_ratio - 0.0) < 1e-6


# =============================================================================
# TC-ADV-005: AlphaResult fields in multi-alpha backtest
# =============================================================================

class TestAlphaResultMultiAlpha:
    """TC-ADV-005: run_multiple_backtest propagates new fields to AlphaResult."""

    def test_alpha_result_has_new_fields(self, isos_data_path):
        """Each AlphaResult should have the new detail view fields."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        alpha1 = MockAlpha()
        alpha1.name = "alpha_1"
        alpha2 = MockAlpha()
        alpha2.name = "alpha_2"

        result = run_multiple_backtest(
            alphas=[alpha1, alpha2],
            strategy=MockStrategy(),
            settings=settings, capital=1_000_000,
            data_loader_kwargs={
                "file_path": str(isos_data_path),
                "time_col": "open_time", "symbol_col": "symbol",
            },
        )

        for ar in result.alpha_results:
            assert ar.is_turnover_series is not None
            assert ar.os_turnover_series is not None
            assert ar.is_min_bet_series is not None
            assert ar.os_min_bet_series is not None
            assert ar.is_max_weight_series is not None
            assert ar.os_max_weight_series is not None
            assert isinstance(ar.is_long_ratio, float)
            assert isinstance(ar.is_short_ratio, float)

    def test_alpha_result_ratios_valid(self, isos_data_path):
        """AlphaResult ratios should be in [0,1] and sum to ~1."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        alpha1 = MockAlpha()
        alpha1.name = "a1"

        result = run_multiple_backtest(
            alphas=[alpha1],
            strategy=MockStrategy(),
            settings=settings, capital=1_000_000,
            data_loader_kwargs={
                "file_path": str(isos_data_path),
                "time_col": "open_time", "symbol_col": "symbol",
            },
        )

        ar = result.alpha_results[0]
        assert 0.0 <= ar.is_long_ratio <= 1.0
        assert 0.0 <= ar.is_short_ratio <= 1.0
        assert abs(ar.is_long_ratio + ar.is_short_ratio - 1.0) < 1e-6


# =============================================================================
# TC-ADV-006: to_dict() serialization
# =============================================================================

class TestToDictSerialization:
    """TC-ADV-006: MultipleBacktestResult.to_dict() includes new fields."""

    def test_to_dict_has_new_fields(self, isos_data_path):
        """Each alpha entry in to_dict() should contain the new fields."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        alpha1 = MockAlpha()
        alpha1.name = "test_alpha"

        result = run_multiple_backtest(
            alphas=[alpha1],
            strategy=MockStrategy(),
            settings=settings, capital=1_000_000,
            data_loader_kwargs={
                "file_path": str(isos_data_path),
                "time_col": "open_time", "symbol_col": "symbol",
            },
        )

        d = result.to_dict()
        alpha_entry = d["alphas"][0]

        # Check all new fields present
        assert "isTurnoverSeries" in alpha_entry
        assert "osTurnoverSeries" in alpha_entry
        assert "isMinBetSeries" in alpha_entry
        assert "osMinBetSeries" in alpha_entry
        assert "isMaxWeightSeries" in alpha_entry
        assert "osMaxWeightSeries" in alpha_entry
        assert "longRatio" in alpha_entry
        assert "shortRatio" in alpha_entry

    def test_to_dict_series_values_are_dicts(self, isos_data_path):
        """Serialized time series should be dicts of {str: float}."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        alpha1 = MockAlpha()
        alpha1.name = "test_alpha"

        result = run_multiple_backtest(
            alphas=[alpha1],
            strategy=MockStrategy(),
            settings=settings, capital=1_000_000,
            data_loader_kwargs={
                "file_path": str(isos_data_path),
                "time_col": "open_time", "symbol_col": "symbol",
            },
        )

        d = result.to_dict()
        alpha_entry = d["alphas"][0]

        # Time series fields should be dicts (not None, since we have data)
        assert isinstance(alpha_entry["isTurnoverSeries"], dict)
        assert isinstance(alpha_entry["osTurnoverSeries"], dict)
        assert len(alpha_entry["isTurnoverSeries"]) > 0
        assert len(alpha_entry["osTurnoverSeries"]) > 0

    def test_to_dict_ratio_values(self, isos_data_path):
        """longRatio and shortRatio should be floats in [0, 1]."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        alpha1 = MockAlpha()
        alpha1.name = "test_alpha"

        result = run_multiple_backtest(
            alphas=[alpha1],
            strategy=MockStrategy(),
            settings=settings, capital=1_000_000,
            data_loader_kwargs={
                "file_path": str(isos_data_path),
                "time_col": "open_time", "symbol_col": "symbol",
            },
        )

        d = result.to_dict()
        alpha_entry = d["alphas"][0]

        assert isinstance(alpha_entry["longRatio"], float)
        assert isinstance(alpha_entry["shortRatio"], float)
        assert 0.0 <= alpha_entry["longRatio"] <= 1.0
        assert 0.0 <= alpha_entry["shortRatio"] <= 1.0


# =============================================================================
# TC-ADV-007: API _split_result_by_date preserves long/short weight series
# =============================================================================

class TestSplitResultByDate:
    """TC-ADV-007: _split_result_by_date preserves new fields on both sides."""

    def test_split_preserves_long_short_series(self):
        """Both IS and OS sub-results should have long/short weight series."""
        from framework.backtester import _split_result_by_date

        dates = pd.to_datetime(
            ["2024-01-01", "2024-01-02", "2024-03-01", "2024-03-02"], utc=True
        )
        result = BacktestResult(
            ret_series=pd.Series([0.01, -0.005, 0.02, 0.01], index=dates),
            avg_turnover=0.10,
            initial_capital=1_000_000,
            turnover_series=pd.Series([0.1, 0.15, 0.12, 0.08], index=dates),
            min_bet_series=pd.Series([10, 12, 11, 9], index=dates),
            max_weight_series=pd.Series([0.15, 0.20, 0.18, 0.14], index=dates),
            long_weight_series=pd.Series([0.6, 0.7, 0.55, 0.65], index=dates),
            short_weight_series=pd.Series([0.4, 0.3, 0.45, 0.35], index=dates),
        )

        is_result, os_result = _split_result_by_date(result, "2024-03-01")

        # IS side
        assert is_result.long_weight_series is not None
        assert is_result.short_weight_series is not None
        assert len(is_result.long_weight_series) == 2
        assert len(is_result.short_weight_series) == 2
        assert abs(is_result.long_weight_series.iloc[0] - 0.6) < 1e-9
        assert abs(is_result.short_weight_series.iloc[0] - 0.4) < 1e-9

        # OS side
        assert os_result.long_weight_series is not None
        assert os_result.short_weight_series is not None
        assert len(os_result.long_weight_series) == 2
        assert len(os_result.short_weight_series) == 2
        assert abs(os_result.long_weight_series.iloc[0] - 0.55) < 1e-9
        assert abs(os_result.short_weight_series.iloc[0] - 0.45) < 1e-9

    def test_split_with_none_series(self):
        """If long/short series are None, split should handle gracefully."""
        from framework.backtester import _split_result_by_date

        dates = pd.to_datetime(["2024-01-01", "2024-03-01"], utc=True)
        result = BacktestResult(
            ret_series=pd.Series([0.01, 0.02], index=dates),
            avg_turnover=0.10,
            initial_capital=1_000_000,
            long_weight_series=None,
            short_weight_series=None,
        )

        is_result, os_result = _split_result_by_date(result, "2024-03-01")
        assert is_result.long_weight_series is None
        assert os_result.long_weight_series is None
        assert is_result.short_weight_series is None
        assert os_result.short_weight_series is None


# =============================================================================
# TC-ADV-008: API run_single_alpha response includes new fields
# =============================================================================

class TestRunSingleAlphaResponse:
    """TC-ADV-008: run_single_alpha response dict includes new fields."""

    def test_response_has_new_fields(self):
        """The single-alpha response should include all new detail view fields."""
        from api.services.backtest_service import run_single_alpha
        from framework.backtester import _split_result_by_date

        # We test the structure by mocking the Backtester and DataCache
        dates = pd.to_datetime(
            ["2023-06-01", "2023-09-01", "2024-03-01", "2024-05-01"], utc=True
        )
        fake_result = BacktestResult(
            ret_series=pd.Series([0.01, -0.005, 0.02, 0.01], index=dates),
            avg_turnover=0.10,
            initial_capital=1_000_000,
            turnover_series=pd.Series([0.1, 0.15, 0.12, 0.08], index=dates),
            min_bet_series=pd.Series([10, 12, 11, 9], index=dates),
            max_weight_series=pd.Series([0.15, 0.20, 0.18, 0.14], index=dates),
            long_weight_series=pd.Series([0.6, 0.7, 0.55, 0.65], index=dates),
            short_weight_series=pd.Series([0.4, 0.3, 0.45, 0.35], index=dates),
        )

        idx = pd.MultiIndex.from_tuples(
            [(pd.Timestamp("2024-01-01"), "SYM1")],
            names=["date", "symbol"],
        )
        fake_viz = {
            "symbols": pd.DataFrame({"signal": [1.0]}, index=idx),
        }

        mock_data_cache = MagicMock()
        mock_data_cache.get_timestamps.return_value = pd.DatetimeIndex(
            [pd.Timestamp("2023-01-01", tz="UTC"), pd.Timestamp("2024-12-31", tz="UTC")]
        )
        mock_data_cache.initial_capital = 1_000_000

        mock_settings = MagicMock()
        mock_settings.isStart = "2023-01-01"
        mock_settings.isEnd = "2023-12-31"
        mock_settings.osStart = "2024-01-01"
        mock_settings.osEnd = "2024-06-30"

        mock_signal = MagicMock()

        with patch("api.services.backtest_service.Backtester") as mock_bt_cls, \
             patch("api.services.backtest_service._validate_data_cache"):
            mock_bt_instance = MagicMock()
            mock_bt_instance.run.return_value = (fake_result, fake_viz)
            mock_bt_cls.return_value = mock_bt_instance

            response = run_single_alpha(mock_signal, mock_settings, "D0", mock_data_cache)

        alpha_entry = response["alphas"][0]
        assert "isTurnoverSeries" in alpha_entry
        assert "osTurnoverSeries" in alpha_entry
        assert "isMinBetSeries" in alpha_entry
        assert "osMinBetSeries" in alpha_entry
        assert "isMaxWeightSeries" in alpha_entry
        assert "osMaxWeightSeries" in alpha_entry
        assert "longRatio" in alpha_entry
        assert "shortRatio" in alpha_entry

        # Verify values are reasonable
        assert isinstance(alpha_entry["longRatio"], float)
        assert isinstance(alpha_entry["shortRatio"], float)
        assert 0.0 <= alpha_entry["longRatio"] <= 1.0
        assert 0.0 <= alpha_entry["shortRatio"] <= 1.0


# =============================================================================
# TC-ADV-009: Edge case -- all-long signals
# =============================================================================

class TestEdgeCaseAllLong:
    """TC-ADV-009: All-long signals edge case."""

    def test_all_long_short_weight_zero(self, basic_data_path, mock_strategy):
        """With all-positive signals, short_weight_series should be all zeros."""
        day1 = pd.Timestamp("2024-01-01", tz="UTC")
        day2 = pd.Timestamp("2024-01-02", tz="UTC")
        # Both symbols get positive weights on all days
        alpha = MockAlpha(weights_map={
            day1: {"A": 0.7, "B": 0.3},
            day2: {"A": 0.5, "B": 0.5},
        })

        loader = DataLoader(
            file_path=str(basic_data_path),
            start_date="2024-01-01", end_date="2024-01-03",
            time_col="open_time", symbol_col="symbol",
        )
        bt = Backtester(loader, alpha, mock_strategy, capital=1_000_000)
        result, _ = bt.run()

        # All short weights should be zero
        for val in result.short_weight_series.values:
            assert abs(val) < 1e-9, f"Expected 0.0, got {val}"

        # All long weights should be 1.0 (gross=1 normalized)
        for val in result.long_weight_series.values:
            assert abs(val - 1.0) < 1e-9, f"Expected 1.0, got {val}"

    def test_all_long_ratio_one(self, isos_data_path):
        """All-long signals: is_long_ratio ~ 1.0."""
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        # Default MockAlpha returns equal positive weights
        result = run_backtest_with_periods(
            alpha=MockAlpha(), strategy=MockStrategy(),
            settings=settings, capital=1_000_000,
            data_loader_kwargs={
                "file_path": str(isos_data_path),
                "time_col": "open_time", "symbol_col": "symbol",
            },
        )
        assert abs(result.is_long_ratio - 1.0) < 1e-6
        assert abs(result.is_short_ratio - 0.0) < 1e-6


# =============================================================================
# TC-ADV-009b: Edge case -- all-short signals
# =============================================================================

class TestEdgeCaseAllShort:
    """TC-ADV-009b: All-short (all-negative) signals edge case."""

    def test_all_short_long_weight_zero(self, basic_data_path, mock_strategy):
        """With all-negative signals, long_weight_series should be all zeros."""
        day1 = pd.Timestamp("2024-01-01", tz="UTC")
        day2 = pd.Timestamp("2024-01-02", tz="UTC")
        day3 = pd.Timestamp("2024-01-03", tz="UTC")
        alpha = MockAlpha(weights_map={
            day1: {"A": -0.6, "B": -0.4},
            day2: {"A": -0.5, "B": -0.5},
            day3: {"A": -0.7, "B": -0.3},
        })

        loader = DataLoader(
            file_path=str(basic_data_path),
            start_date="2024-01-01", end_date="2024-01-03",
            time_col="open_time", symbol_col="symbol",
        )
        bt = Backtester(loader, alpha, mock_strategy, capital=1_000_000)
        result, _ = bt.run()

        # All long weights should be zero
        for val in result.long_weight_series.values:
            assert abs(val) < 1e-9, f"Expected 0.0, got {val}"

        # All short weights should be 1.0 (gross=1 normalized)
        for val in result.short_weight_series.values:
            assert abs(val - 1.0) < 1e-9, f"Expected 1.0, got {val}"

    def test_all_short_ratio_one(self, isos_data_path):
        """All-short signals: is_short_ratio ~ 1.0, is_long_ratio ~ 0.0."""
        day1 = pd.Timestamp("2024-01-01", tz="UTC")
        day2 = pd.Timestamp("2024-01-02", tz="UTC")
        day3 = pd.Timestamp("2024-01-03", tz="UTC")
        alpha = MockAlpha(weights_map={
            day1: {"A": -0.6, "B": -0.4},
            day2: {"A": -0.5, "B": -0.5},
            day3: {"A": -0.7, "B": -0.3},
        })

        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        result = run_backtest_with_periods(
            alpha=alpha, strategy=MockStrategy(),
            settings=settings, capital=1_000_000,
            data_loader_kwargs={
                "file_path": str(isos_data_path),
                "time_col": "open_time", "symbol_col": "symbol",
            },
        )
        assert abs(result.is_short_ratio - 1.0) < 1e-6
        assert abs(result.is_long_ratio - 0.0) < 1e-6


# =============================================================================
# TC-ADV-010: Edge case -- all-zero signals
# =============================================================================

class TestEdgeCaseAllZero:
    """TC-ADV-010: All-zero signals -> no division error, ratios = 0."""

    def test_all_zero_no_error(self, basic_data_path, mock_strategy):
        """All-zero signals should not cause division errors."""
        day1 = pd.Timestamp("2024-01-01", tz="UTC")
        day2 = pd.Timestamp("2024-01-02", tz="UTC")
        day3 = pd.Timestamp("2024-01-03", tz="UTC")
        alpha = MockAlpha(weights_map={
            day1: {"A": 0.0, "B": 0.0},
            day2: {"A": 0.0, "B": 0.0},
            day3: {"A": 0.0, "B": 0.0},
        })

        loader = DataLoader(
            file_path=str(basic_data_path),
            start_date="2024-01-01", end_date="2024-01-03",
            time_col="open_time", symbol_col="symbol",
        )
        bt = Backtester(loader, alpha, mock_strategy, capital=1_000_000)
        result, _ = bt.run()

        # Both series should be all zeros, no NaN
        for val in result.long_weight_series.values:
            assert val == 0.0
        for val in result.short_weight_series.values:
            assert val == 0.0

    def test_all_zero_ratio_zero(self, isos_data_path):
        """All-zero signals: both ratios should be 0.0 (not NaN)."""
        day1 = pd.Timestamp("2024-01-01", tz="UTC")
        day2 = pd.Timestamp("2024-01-02", tz="UTC")
        day3 = pd.Timestamp("2024-01-03", tz="UTC")
        alpha = MockAlpha(weights_map={
            day1: {"A": 0.0, "B": 0.0},
            day2: {"A": 0.0, "B": 0.0},
            day3: {"A": 0.0, "B": 0.0},
        })

        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )

        result = run_backtest_with_periods(
            alpha=alpha, strategy=MockStrategy(),
            settings=settings, capital=1_000_000,
            data_loader_kwargs={
                "file_path": str(isos_data_path),
                "time_col": "open_time", "symbol_col": "symbol",
            },
        )

        assert result.is_long_ratio == 0.0
        assert result.is_short_ratio == 0.0
        # Ensure no NaN
        assert not np.isnan(result.is_long_ratio)
        assert not np.isnan(result.is_short_ratio)
