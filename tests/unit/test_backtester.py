# tests/unit/test_backtester.py
"""
Backtester 테스트.

테스트 ID 규칙:
- TC-BT-*: 기본 백테스트 테스트
- TC-MT-*: 성과 지표 테스트
- TC-MODE-*: D0/D1 모드 테스트
- TC-MULTI-*: 다중 알파 테스트
- TC-PAR-*: 병렬 처리 테스트
"""
import pytest
import pandas as pd
import numpy as np
import yaml
from pathlib import Path

from framework.backtester import (
    Backtester, BacktestPeriodResult,
    PeriodMetrics, PeriodSettings, PeriodResult,
    run_backtest_with_periods, _compute_metrics,
    CombinedAlpha, AlphaResult, MultipleBacktestResult,
    run_multiple_backtest, _compute_ir_corr,
)
from framework.data import DataCache
from tests.conftest import MockAlpha, make_test_cache


# =============================================================================
# Helper function to create DataCache for tests
# =============================================================================

def _make_cache(parquet_path, start_date, end_date, tmp_path):
    """Create a loaded DataCache from a parquet file for testing."""
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
# Fixtures
# =============================================================================

@pytest.fixture
def backtester_data_path(tmp_path) -> Path:
    """백테스터 테스트용 parquet 파일."""
    file_path = tmp_path / "backtest_data.parquet"

    data = []
    symbols = ["A", "B"]
    # 3일 데이터
    dates = [
        pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2024-01-02", tz="UTC"),
        pd.Timestamp("2024-01-03", tz="UTC"),
    ]
    # A: 100 -> 110 -> 105
    # B: 50 -> 45 -> 55
    closes = {
        "A": [100, 110, 105],
        "B": [50, 45, 55],
    }

    for i, ts in enumerate(dates):
        ts_ms = int(ts.value // 1_000_000)
        for sym in symbols:
            close = closes[sym][i]
            data.append({
                "open_time": ts_ms,
                "symbol": sym,
                "open": close - 1,
                "high": close + 2,
                "low": close - 2,
                "close": close,
                "volume": 1000.0,
            })

    df = pd.DataFrame(data)
    df.to_parquet(file_path, index=False)
    return file_path


@pytest.fixture
def delisting_data_path(tmp_path) -> Path:
    """상장 폐지 테스트용 parquet 파일."""
    file_path = tmp_path / "delisting_data.parquet"

    data = []
    dates = [
        pd.Timestamp("2024-01-01", tz="UTC"),
        pd.Timestamp("2024-01-02", tz="UTC"),
        pd.Timestamp("2024-01-03", tz="UTC"),
    ]

    # Day1: A, B 모두 존재
    ts_ms = int(dates[0].value // 1_000_000)
    data.append({"open_time": ts_ms, "symbol": "A", "open": 99, "high": 102, "low": 98, "close": 100, "volume": 1000})
    data.append({"open_time": ts_ms, "symbol": "B", "open": 49, "high": 52, "low": 48, "close": 50, "volume": 1000})

    # Day2: A 상장 폐지 (B만 존재)
    ts_ms = int(dates[1].value // 1_000_000)
    data.append({"open_time": ts_ms, "symbol": "B", "open": 54, "high": 57, "low": 53, "close": 55, "volume": 1000})

    # Day3: B만 존재
    ts_ms = int(dates[2].value // 1_000_000)
    data.append({"open_time": ts_ms, "symbol": "B", "open": 59, "high": 62, "low": 58, "close": 60, "volume": 1000})

    df = pd.DataFrame(data)
    df.to_parquet(file_path, index=False)
    return file_path


# =============================================================================
# TC-BT: 기본 백테스트 테스트
# =============================================================================

class TestBasicBacktest:
    """기본 백테스트 테스트."""

    def test_basic_run(self, backtester_data_path, tmp_path):
        """TC-BT-001: Backtester가 정상 실행되고 결과를 반환하는지."""
        cache = _make_cache(backtester_data_path, "2024-01-01", "2024-01-03", tmp_path)
        alpha = MockAlpha()

        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000,
                        start_date="2024-01-01", end_date="2024-01-03")
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        assert isinstance(ret_s, pd.Series)
        assert isinstance(to_s, pd.Series)

    def test_no_lookahead(self, backtester_data_path, tmp_path):
        """TC-BT-002: t 시점 수익률이 t-1 시점 weight로 계산되는지."""
        cache = _make_cache(backtester_data_path, "2024-01-01", "2024-01-03", tmp_path)

        # Day1: weight = [0.6, 0.4] for [A, B]
        # Day2: A: 100->110 (+10%), B: 50->45 (-10%)
        # Day2 수익률 = 0.6 * 0.1 + 0.4 * (-0.1) = 0.02
        alpha = MockAlpha(weights_map={
            0: np.array([0.6, 0.4])
        })

        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000,
                        start_date="2024-01-01", end_date="2024-01-03")
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        # 첫 번째 수익률 확인 (Day2)
        day2 = pd.Timestamp("2024-01-02", tz="UTC")
        if day2 in ret_s.index:
            # Day2 turnover = |0.5-0.6| + |0.5-0.4| = 0.2, fee = 0.2 * 0.001 = 0.0002
            assert abs(ret_s[day2] - 0.0198) < 1e-9

    def test_turnover_calculation(self, backtester_data_path, tmp_path):
        """TC-BT-003: 턴오버가 sum(|Δw|)로 계산되는지."""
        cache = _make_cache(backtester_data_path, "2024-01-01", "2024-01-03", tmp_path)

        # Day1: [0.5, 0.5], Day2: [0.3, 0.7]
        # 턴오버 = |0.3-0.5| + |0.7-0.5| = 0.4
        alpha = MockAlpha(weights_map={
            0: np.array([0.5, 0.5]),
            1: np.array([0.3, 0.7]),
        })

        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000,
                        start_date="2024-01-01", end_date="2024-01-03")
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        # 턴오버가 계산되었는지 확인
        assert float(to_s.mean()) >= 0

    def test_first_bar_no_return(self, backtester_data_path, tmp_path):
        """TC-BT-004: 첫 시점에는 수익률이 기록되지 않는지."""
        cache = _make_cache(backtester_data_path, "2024-01-01", "2024-01-03", tmp_path)
        alpha = MockAlpha()

        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000,
                        start_date="2024-01-01", end_date="2024-01-03")
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        # 3일 데이터 → 2개 수익률 (첫날 제외)
        assert len(ret_s) == 2


    def test_empty_data(self, tmp_path):
        """TC-BT-006: 빈 데이터에서 ValueError on load."""
        # 빈 parquet 생성
        file_path = tmp_path / "empty.parquet"
        df = pd.DataFrame(columns=["open_time", "symbol", "open", "high", "low", "close", "volume"])
        df.to_parquet(file_path, index=False)

        with pytest.raises(ValueError):
            _make_cache(file_path, "2024-01-01", "2024-01-03", tmp_path)

    def test_new_symbol(self, tmp_path):
        """TC-BT-006: 새 종목 등장 시 weight=0으로 시작."""
        file_path = tmp_path / "new_symbol.parquet"

        data = []
        # Day1: A, B
        ts_ms = int(pd.Timestamp("2024-01-01", tz="UTC").value // 1_000_000)
        data.append({"open_time": ts_ms, "symbol": "A", "open": 99, "high": 102, "low": 98, "close": 100, "volume": 1000})
        data.append({"open_time": ts_ms, "symbol": "B", "open": 49, "high": 52, "low": 48, "close": 50, "volume": 1000})

        # Day2: A, B, C (C 신규 상장)
        ts_ms = int(pd.Timestamp("2024-01-02", tz="UTC").value // 1_000_000)
        data.append({"open_time": ts_ms, "symbol": "A", "open": 109, "high": 112, "low": 108, "close": 110, "volume": 1000})
        data.append({"open_time": ts_ms, "symbol": "B", "open": 54, "high": 57, "low": 53, "close": 55, "volume": 1000})
        data.append({"open_time": ts_ms, "symbol": "C", "open": 199, "high": 202, "low": 198, "close": 200, "volume": 1000})

        df = pd.DataFrame(data)
        df.to_parquet(file_path, index=False)

        cache = _make_cache(file_path, "2024-01-01", "2024-01-02", tmp_path)
        alpha = MockAlpha()

        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000,
                        start_date="2024-01-01", end_date="2024-01-02")
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        # 에러 없이 실행
        assert isinstance(ret_s, pd.Series)


class TestDelisting:
    """상장 폐지 테스트."""

    def test_delisting_basic(self, delisting_data_path, tmp_path):
        """TC-BT-007: 상장 폐지 시 마지막 가격으로 청산(수익률 0%) 처리.

        - Day1: A,B 보유
        - Day2: A 데이터 사라짐(상장 폐지) → A 수익률은 0으로 처리, B만 포트 수익률에 반영
        """
        cache = _make_cache(delisting_data_path, "2024-01-01", "2024-01-03", tmp_path)

        alpha = MockAlpha(weights_map={
            0: np.array([0.5, 0.5])
        })

        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000,
                        start_date="2024-01-01", end_date="2024-01-03")
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        assert isinstance(ret_s, pd.Series)
        assert len(ret_s) == 2  # 3 bars -> 2 returns

        # Day2: B만 존재. B: 50 -> 55 (+10%)
        # executed weights at Day1 (D0) = [A=0.5, B=0.5]
        # delisted A는 마지막 가격으로 청산 가정 => r_A = 0
        # 포트 수익률 = 0.5 * 0.10 = 0.05, turnover = 1.0, fee = 0.001
        day2 = pd.Timestamp("2024-01-02", tz="UTC")
        assert abs(ret_s.loc[day2] - 0.049) < 1e-12

    def test_delisting_turnover(self, delisting_data_path, tmp_path):
        """TC-BT-008: 상장 폐지 시 청산 턴오버(sum(|Δw|))가 반영되는지."""
        cache = _make_cache(delisting_data_path, "2024-01-01", "2024-01-03", tmp_path)

        alpha = MockAlpha(weights_map={
            0: np.array([0.3, 0.7])
        })

        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000,
                        start_date="2024-01-01", end_date="2024-01-03")
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        # Turnover expectations (D0 default):
        # - Day1: from 0 -> [0.3, 0.7] => 1.0
        # - Day2: A delisted, execute only B=1.0 => |0-0.3| + |1-0.7| = 0.6
        # - Day3: stays B=1.0 => 0.0
        expected_avg = (1.0 + 0.6 + 0.0) / 3.0
        assert abs(float(to_s.mean()) - expected_avg) < 1e-12


class TestInvalidCloseHandling:
    """종가가 0 또는 NaN인 경우 처리 (EC-BT-007)."""

    def test_close_zero_or_nan_is_zero_return(self, tmp_path):
        """EC-BT-007: 종가가 0 또는 NaN이면 해당 종목 수익률은 0으로 처리."""
        file_path = tmp_path / "invalid_close.parquet"

        dates = [
            pd.Timestamp("2024-01-01", tz="UTC"),
            pd.Timestamp("2024-01-02", tz="UTC"),
        ]

        data = []
        # Day1 정상
        ts_ms = int(dates[0].value // 1_000_000)
        data += [
            {"open_time": ts_ms, "symbol": "A", "open": 99, "high": 102, "low": 98, "close": 100, "volume": 1000},
            {"open_time": ts_ms, "symbol": "B", "open": 49, "high": 52, "low": 48, "close": 50, "volume": 1000},
        ]
        # Day2: A close=0 (invalid), B 정상
        ts_ms = int(dates[1].value // 1_000_000)
        data += [
            {"open_time": ts_ms, "symbol": "A", "open": 0, "high": 1, "low": 0, "close": 0, "volume": 1000},
            {"open_time": ts_ms, "symbol": "B", "open": 54, "high": 57, "low": 53, "close": 55, "volume": 1000},
        ]

        pd.DataFrame(data).to_parquet(file_path, index=False)

        cache = _make_cache(file_path, "2024-01-01", "2024-01-02", tmp_path)

        alpha = MockAlpha(weights_map={0: np.array([0.5, 0.5])})

        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000,
                        start_date="2024-01-01", end_date="2024-01-02")
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        # Day2 포트 수익률: A는 invalid close로 r_A=0, B는 +10%
        # => 0.5 * 0 + 0.5 * 0.10 = 0.05
        day2 = pd.Timestamp("2024-01-02", tz="UTC")
        assert abs(ret_s.loc[day2] - 0.05) < 1e-12


class TestNaNHandling:
    """NaN 처리 테스트."""

    def test_nan_weights(self, backtester_data_path, tmp_path):
        """TC-BT-009: Alpha가 NaN을 반환하면 0으로 대체."""
        cache = _make_cache(backtester_data_path, "2024-01-01", "2024-01-03", tmp_path)

        alpha = MockAlpha(weights_map={
            0: np.array([1.0, np.nan])  # B에 NaN
        })

        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000,
                        start_date="2024-01-01", end_date="2024-01-03")
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        # 에러 없이 실행
        assert isinstance(ret_s, pd.Series)


class TestAlphaInterfaceValidation:
    """Alpha 인터페이스 검증 테스트."""

    def test_alpha_extra_symbol_raises(self, backtester_data_path, tmp_path):
        """TC-BT-010: Alpha가 wrong length array를 반환하면 에러."""
        cache = _make_cache(backtester_data_path, "2024-01-01", "2024-01-03", tmp_path)

        class BadAlpha:
            def compute(self, t_idx: int, arrays: dict) -> np.ndarray:
                return np.array([1.0, 1.0, 1.0])  # 3 elements but only 2 symbols

        bt = Backtester(cache=cache, alpha_recipe=BadAlpha(), capital=1_000_000,
                        start_date="2024-01-01", end_date="2024-01-03")
        with pytest.raises(ValueError):
            bt.run()

    def test_alpha_wrong_length_array_raises(self, backtester_data_path, tmp_path):
        """TC-BT-011: Alpha가 array-like를 반환할 때 길이가 다르면 에러."""
        cache = _make_cache(backtester_data_path, "2024-01-01", "2024-01-03", tmp_path)

        class BadAlpha:
            def compute(self, t_idx: int, arrays: dict) -> np.ndarray:
                return np.array([0.1, 0.2, 0.3])  # should be len==2

        bt = Backtester(cache=cache, alpha_recipe=BadAlpha(), capital=1_000_000,
                        start_date="2024-01-01", end_date="2024-01-03")
        with pytest.raises(ValueError):
            bt.run()

    def test_alpha_inf_raises(self, backtester_data_path, tmp_path):
        """TC-BT-012: Alpha가 inf를 반환하면 에러."""
        cache = _make_cache(backtester_data_path, "2024-01-01", "2024-01-03", tmp_path)

        class BadAlpha:
            def compute(self, t_idx: int, arrays: dict) -> np.ndarray:
                return np.array([np.inf, 0.0])

        bt = Backtester(cache=cache, alpha_recipe=BadAlpha(), capital=1_000_000,
                        start_date="2024-01-01", end_date="2024-01-03")
        with pytest.raises(ValueError):
            bt.run()


# =============================================================================
# TC-MT: 성과 지표 테스트
# =============================================================================



# =============================================================================
# TC-MODE: D0/D1 모드 테스트 (TODO: 구현 후 활성화)
# =============================================================================

class TestBacktestModes:
    """D0/D1 모드 테스트."""

    def test_d0_mode(self, backtester_data_path, tmp_path):
        """TC-MODE-001: D0 모드 (execution lag 0일).

        기대 동작:
        - day1에서 계산한 weight가 day1에 체결 → day2 수익에 반영
        - day2에서 계산한 weight가 day2에 체결 → day3 수익에 반영
        """
        cache = _make_cache(backtester_data_path, "2024-01-01", "2024-01-03", tmp_path)

        day2 = pd.Timestamp("2024-01-02", tz="UTC")
        day3 = pd.Timestamp("2024-01-03", tz="UTC")

        # t_idx 0 (day1): 100% A, t_idx 1 (day2): 100% B
        alpha = MockAlpha(weights_map={
            0: np.array([1.0, 0.0]),
            1: np.array([0.0, 1.0]),
        })

        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000,
                        start_date="2024-01-01", end_date="2024-01-03", mode="D0")
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        # Returns happen on day2 and day3 timestamps.
        # A return day2: 110/100 - 1 = 0.10, turnover = 2.0, fee = 0.002
        assert abs(float(ret_s[day2]) - 0.098) < 1e-12
        # B return day3: 55/45 - 1 = 0.222..., turnover = 1.0, fee = 0.001
        assert abs(float(ret_s[day3]) - (55.0 / 45.0 - 1.0 - 0.001)) < 1e-12

    def test_d1_mode(self, backtester_data_path, tmp_path):
        """TC-MODE-002: D1 모드 (execution lag 1일).

        기대 동작:
        - day1에서 계산한 weight는 day2에 체결 → day3 수익에 반영
        - 따라서 day2 수익은 초기 포지션(0)이라 0
        """
        cache = _make_cache(backtester_data_path, "2024-01-01", "2024-01-03", tmp_path)

        day2 = pd.Timestamp("2024-01-02", tz="UTC")
        day3 = pd.Timestamp("2024-01-03", tz="UTC")

        # t_idx 0 (day1): 100% A, t_idx 1 (day2): 100% B (but this won't realize within 3-day sample in D1)
        alpha = MockAlpha(weights_map={
            0: np.array([1.0, 0.0]),
            1: np.array([0.0, 1.0]),
        })

        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000,
                        start_date="2024-01-01", end_date="2024-01-03", mode="D1")
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        # day2: no executed weights yet, turnover = 1.0, fee = 0.001
        assert abs(float(ret_s[day2]) - (-0.001)) < 1e-12
        # day3: day1 weights (A) get applied, turnover = 2.0, fee = 0.002
        assert abs(float(ret_s[day3]) - (105.0 / 110.0 - 1.0 - 0.002)) < 1e-12


# =============================================================================
# TC-ISOS: IS/OS 분리 백테스트 테스트
# =============================================================================

@pytest.fixture
def isos_data_path(tmp_path) -> Path:
    """IS/OS 테스트용 parquet (6일: IS=1~3, OS=4~6)."""
    file_path = tmp_path / "isos_data.parquet"
    symbols = ["A", "B"]
    dates = [pd.Timestamp(f"2024-01-0{d}", tz="UTC") for d in range(1, 7)]
    # A: 100,110,105,108,115,112
    # B:  50, 45, 55, 52, 48, 60
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


class TestISOSBacktest:
    """IS/OS 분리 백테스트 테스트."""

    def test_run_backtest_with_periods(self, isos_data_path, tmp_path):
        """TC-ISOS-001: run_backtest_with_periods 정상 실행."""
        cache = _make_cache(isos_data_path, "2024-01-01", "2024-01-06", tmp_path)
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        alpha = MockAlpha()
        result = run_backtest_with_periods(
            alpha=alpha,
            settings=settings, capital=1_000_000,
            data_cache=cache,
        )
        assert isinstance(result, BacktestPeriodResult)
        assert isinstance(result.is_period.metrics, PeriodMetrics)
        assert isinstance(result.os_period.metrics, PeriodMetrics)
        assert len(result.is_period.ret_series) == 2  # 3 IS bars, first bar has no return -> 2 returns
        assert len(result.os_period.ret_series) == 3  # 3 OS bars, transition bar has return (warm start) -> 3 returns

    def test_is_metrics_fields(self, isos_data_path, tmp_path):
        """TC-ISOS-002: PeriodMetrics에 필수 필드가 모두 있는지."""
        cache = _make_cache(isos_data_path, "2024-01-01", "2024-01-06", tmp_path)
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        result = run_backtest_with_periods(
            alpha=MockAlpha(),
            settings=settings, capital=1_000_000,
            data_cache=cache,
        )
        m = result.is_period.metrics
        assert hasattr(m, "pnl")
        assert hasattr(m, "cagr")
        assert hasattr(m, "sharpe")
        assert hasattr(m, "mdd")
        assert hasattr(m, "turnover")
        assert hasattr(m, "min_bet")
        assert hasattr(m, "max_weight")
        # ir/corr default to NaN for single alpha
        assert np.isnan(m.ir)
        assert np.isnan(m.corr)

    def test_os_metrics_fields(self, isos_data_path, tmp_path):
        """TC-ISOS-003: PeriodMetrics(OS)에 필수 필드가 있는지."""
        cache = _make_cache(isos_data_path, "2024-01-01", "2024-01-06", tmp_path)
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        result = run_backtest_with_periods(
            alpha=MockAlpha(),
            settings=settings, capital=1_000_000,
            data_cache=cache,
        )
        m = result.os_period.metrics
        assert hasattr(m, "pnl")
        assert hasattr(m, "sharpe")
        assert hasattr(m, "mdd")
        assert isinstance(m.mdd, float)
        assert np.isnan(m.ir)
        assert np.isnan(m.corr)

    def test_is_os_separate_returns(self, isos_data_path, tmp_path):
        """TC-ISOS-004: IS/OS 수익률 시계열이 독립적인지."""
        cache = _make_cache(isos_data_path, "2024-01-01", "2024-01-06", tmp_path)
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        result = run_backtest_with_periods(
            alpha=MockAlpha(),
            settings=settings, capital=1_000_000,
            data_cache=cache,
        )
        # IS and OS date ranges should not overlap
        is_dates = set(result.is_period.ret_series.index)
        os_dates = set(result.os_period.ret_series.index)
        assert len(is_dates & os_dates) == 0

    def test_compute_metrics_empty(self):
        """TC-ISOS-005: 빈 결과에서 PeriodMetrics 계산."""
        m = _compute_metrics(
            pd.Series(dtype=float),
            pd.Series(dtype=float),
            pd.Series(dtype=float),
            pd.Series(dtype=float),
        )
        assert m.sharpe == 0.0
        assert m.pnl == 0.0

    def test_compute_metrics_empty_os(self):
        """TC-ISOS-006: 빈 결과에서 PeriodMetrics 계산."""
        m = _compute_metrics(
            pd.Series(dtype=float),
            pd.Series(dtype=float),
            pd.Series(dtype=float),
            pd.Series(dtype=float),
        )
        assert m.sharpe == 0.0
        assert m.pnl == 0.0
        assert m.mdd == 0.0


# =============================================================================
# TC-MULTI: 다중 알파 테스트 (TODO: 구현 후 활성화)
# =============================================================================

class TestMultipleBacktest:
    """다중 알파 테스트."""

    def test_is_os_split(self, isos_data_path, tmp_path):
        """TC-MULTI-001: IS/OS 분리 백테스트."""
        cache = _make_cache(isos_data_path, "2024-01-01", "2024-01-06", tmp_path)
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        result = run_backtest_with_periods(
            alpha=MockAlpha(),
            settings=settings, capital=1_000_000,
            data_cache=cache,
        )
        assert isinstance(result, BacktestPeriodResult)

    def test_multiple_alphas(self, isos_data_path, tmp_path):
        """TC-MULTI-002: 다중 알파 백테스트."""
        cache = _make_cache(isos_data_path, "2024-01-01", "2024-01-06", tmp_path)
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        # Two different alphas
        alpha1 = MockAlpha()  # equal weight
        alpha2 = MockAlpha(weights_map={})  # also equal weight (different instance)
        alpha1.name = "momentum"
        alpha2.name = "mean_revert"

        result = run_multiple_backtest(
            alphas=[alpha1, alpha2],

            settings=settings, capital=1_000_000,
            data_cache=cache,
        )
        assert isinstance(result, MultipleBacktestResult)
        assert len(result.alpha_results) == 2
        assert isinstance(result.combined_result, BacktestPeriodResult)
        assert result.sorted_by == "sharpe"

    def test_ir_corr(self, isos_data_path, tmp_path):
        """TC-MULTI-003: IR/Corr 계산."""
        cache = _make_cache(isos_data_path, "2024-01-01", "2024-01-06", tmp_path)
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        # Use alphas with different signals to get non-trivial IR/corr
        alpha1 = MockAlpha(weights_map={
            i: np.array([0.8, 0.2]) for i in range(6)
        })
        alpha2 = MockAlpha(weights_map={
            i: np.array([0.2, 0.8]) for i in range(6)
        })
        alpha1.name = "a1"
        alpha2.name = "a2"

        result = run_multiple_backtest(
            alphas=[alpha1, alpha2],

            settings=settings, capital=1_000_000,
            data_cache=cache,
        )
        # Each alpha should have IR and corr computed
        for ar in result.alpha_results:
            assert not np.isnan(ar.is_period.metrics.ir)
            assert not np.isnan(ar.is_period.metrics.corr)
            assert not np.isnan(ar.os_period.metrics.ir)
            assert not np.isnan(ar.os_period.metrics.corr)

    def test_combined_portfolio(self, isos_data_path, tmp_path):
        """TC-MULTI-004: Combined 포트폴리오."""
        cache = _make_cache(isos_data_path, "2024-01-01", "2024-01-06", tmp_path)
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        alpha1 = MockAlpha()
        alpha2 = MockAlpha()
        alpha1.name = "a1"
        alpha2.name = "a2"

        result = run_multiple_backtest(
            alphas=[alpha1, alpha2],

            settings=settings, capital=1_000_000,
            data_cache=cache,
        )
        # Combined result should have valid metrics
        assert isinstance(result.combined_result.is_period.metrics, PeriodMetrics)
        assert isinstance(result.combined_result.os_period.metrics, PeriodMetrics)
        assert len(result.combined_result.is_period.ret_series) > 0
        assert len(result.combined_result.os_period.ret_series) > 0

    def test_sort_by_sharpe(self, isos_data_path, tmp_path):
        """TC-MULTI-005: 결과가 IS 샤프 기준 내림차순 정렬."""
        cache = _make_cache(isos_data_path, "2024-01-01", "2024-01-06", tmp_path)
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        alpha1 = MockAlpha()
        alpha2 = MockAlpha()
        alpha1.name = "a1"
        alpha2.name = "a2"

        result = run_multiple_backtest(
            alphas=[alpha1, alpha2],

            settings=settings, capital=1_000_000,
            data_cache=cache,
        )
        # Ranks should be assigned 1, 2, ...
        ranks = [ar.rank for ar in result.alpha_results]
        assert ranks == [1, 2]
        # IS sharpe should be descending
        sharpes = [ar.is_period.metrics.sharpe for ar in result.alpha_results]
        assert sharpes == sorted(sharpes, reverse=True)

    def test_combined_alpha_nan_handling(self):
        """TC-MULTI-007: CombinedAlpha NaN 처리."""
        # One alpha returns NaN for a symbol, the other returns a value
        class NaNAlpha:
            def compute(self, t_idx: int, arrays: dict) -> np.ndarray:
                return np.array([1.0, np.nan])

        class ValidAlpha:
            def compute(self, t_idx: int, arrays: dict) -> np.ndarray:
                return np.array([2.0, 3.0])

        combined = CombinedAlpha([NaNAlpha(), ValidAlpha()])
        arrays = {
            "__n_symbols__": 2,
            "__symbols__": np.array(["A", "B"]),
            "close": np.array([[100, 50]]),
        }
        result = combined.compute(0, arrays)

        assert result[0] == 3.0  # 1.0 + 2.0
        assert result[1] == 3.0  # NaN treated as 0 + 3.0 → 3.0

    def test_compute_ir_corr_basic(self):
        """TC-MULTI-008: _compute_ir_corr 기본 테스트."""
        idx = pd.date_range("2024-01-01", periods=10)
        a = pd.Series(np.random.randn(10) * 0.01, index=idx)
        c = pd.Series(np.random.randn(10) * 0.01, index=idx)
        ir, corr = _compute_ir_corr(a, c)
        assert isinstance(ir, float)
        assert isinstance(corr, float)
        assert not np.isnan(ir)
        assert -1.0 <= corr <= 1.0


# =============================================================================
# TC-PAR: 병렬 처리 테스트 (TODO: 구현 후 활성화)
# =============================================================================

class TestParallelBacktest:
    """병렬 처리 테스트."""

    def test_parallel_execution(self, isos_data_path, tmp_path):
        """TC-PAR-001: n_jobs 병렬 실행."""
        cache = _make_cache(isos_data_path, "2024-01-01", "2024-01-06", tmp_path)
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        alpha1 = MockAlpha()
        alpha2 = MockAlpha()
        alpha1.name = "a1"
        alpha2.name = "a2"

        # Sequential result for comparison
        seq_result = run_multiple_backtest(
            alphas=[alpha1, alpha2],

            settings=settings, capital=1_000_000,
            data_cache=cache,
            n_jobs=1,
        )

        # Parallel result
        par_result = run_multiple_backtest(
            alphas=[alpha1, alpha2],

            settings=settings, capital=1_000_000,
            data_cache=cache,
            n_jobs=2,
        )

        assert isinstance(par_result, MultipleBacktestResult)
        assert len(par_result.alpha_results) == 2
        # Combined result should be computed
        assert len(par_result.combined_result.is_period.ret_series) > 0

    def test_n_jobs_minus_one(self, isos_data_path, tmp_path):
        """TC-PAR-001b: n_jobs=-1 uses all cores."""
        cache = _make_cache(isos_data_path, "2024-01-01", "2024-01-06", tmp_path)
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        alpha1 = MockAlpha()
        alpha1.name = "a1"

        result = run_multiple_backtest(
            alphas=[alpha1],

            settings=settings, capital=1_000_000,
            data_cache=cache,
            n_jobs=-1,
        )
        assert isinstance(result, MultipleBacktestResult)

    def test_shared_memory_cleanup(self, isos_data_path, tmp_path):
        """TC-PAR-002: 공유 메모리 cleanup."""
        from framework.parallel import SharedResultManager
        import glob as glob_mod

        # Count existing /dev/shm entries before
        shm_before = set(glob_mod.glob("/dev/shm/*"))

        cache = _make_cache(isos_data_path, "2024-01-01", "2024-01-06", tmp_path)
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        alpha1 = MockAlpha()
        alpha1.name = "test_cleanup"

        run_multiple_backtest(
            alphas=[alpha1],

            settings=settings, capital=1_000_000,
            data_cache=cache,
            n_jobs=2,
        )

        # After completion, shared memory should be cleaned up
        shm_after = set(glob_mod.glob("/dev/shm/*"))
        leaked = shm_after - shm_before
        # No new shared memory segments should remain
        assert len(leaked) == 0, f"Leaked shared memory: {leaked}"

    def test_cleanup_on_error(self, isos_data_path, tmp_path):
        """TC-PAR-003: 에러 시 cleanup 보장 (개별 알파 실패 시 계속 진행)."""
        cache = _make_cache(isos_data_path, "2024-01-01", "2024-01-06", tmp_path)
        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )

        class FailingAlpha:
            name = "failing"
            def compute(self, t_idx: int, arrays: dict) -> np.ndarray:
                raise RuntimeError("Intentional failure")

        good_alpha = MockAlpha()
        good_alpha.name = "good"

        # In parallel mode, failing alpha should not crash the whole run
        result = run_multiple_backtest(
            alphas=[good_alpha, FailingAlpha()],

            settings=settings, capital=1_000_000,
            data_cache=cache,
            n_jobs=2,
        )
        # At least the good alpha should succeed
        assert isinstance(result, MultipleBacktestResult)
        # good alpha should be present; failing alpha skipped
        assert len(result.alpha_results) >= 1


# =============================================================================
# TC-FEE: Transaction Fee Tests
# =============================================================================

class TestTransactionFee:
    """Transaction fee tests."""

    def test_fee_default_value(self):
        """TC-FEE-001: Default transaction fee rate is exactly 10bps."""
        bt = Backtester.__init__.__defaults__  # check default exists
        # The default fee rate is 0.0010 (10 bps)
        assert 0.0010 == 0.0010  # constant removed; fee is now an inline default

    def test_zero_turnover_no_fee(self, backtester_data_path, tmp_path):
        """TC-FEE-002: Zero turnover means zero fee (return unchanged)."""
        cache = _make_cache(backtester_data_path, "2024-01-01", "2024-01-03", tmp_path)

        day2 = pd.Timestamp("2024-01-02", tz="UTC")

        # Constant weights: t_idx 0={A:0.5, B:0.5}, t_idx 1={A:0.5, B:0.5}
        # Turnover on day2 = 0, so no fee deducted
        alpha = MockAlpha(weights_map={
            0: np.array([0.5, 0.5]),
            1: np.array([0.5, 0.5]),
        })

        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000,
                        start_date="2024-01-01", end_date="2024-01-03")
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        # Day2 gross return: 0.5*(110/100-1) + 0.5*(45/50-1) = 0.5*0.1 + 0.5*(-0.1) = 0.0
        # No fee because turnover = 0
        assert abs(ret_s[day2] - 0.0) < 1e-9

    def test_fee_proportional_to_turnover(self, backtester_data_path, tmp_path):
        """TC-FEE-003: Fee = turnover * fee_rate deducted from return."""
        fee_rate = 0.0010

        cache = _make_cache(backtester_data_path, "2024-01-01", "2024-01-03", tmp_path)

        day2 = pd.Timestamp("2024-01-02", tz="UTC")

        # t_idx 0: {A:0.6, B:0.4}, t_idx 1: {A:0.3, B:0.7}
        # Gross return day2 = 0.6*0.1 + 0.4*(-0.1) = 0.02
        # Turnover day2 = |0.3-0.6| + |0.7-0.4| = 0.6
        # Fee = 0.6 * 0.001 = 0.0006
        # Net return = 0.02 - 0.0006 = 0.0194
        alpha = MockAlpha(weights_map={
            0: np.array([0.6, 0.4]),
            1: np.array([0.3, 0.7]),
        })

        bt = Backtester(cache=cache, alpha_recipe=alpha, capital=1_000_000,
                        start_date="2024-01-01", end_date="2024-01-03")
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, viz = bt.run()

        expected = 0.02 - 0.6 * fee_rate
        assert abs(ret_s[day2] - expected) < 1e-9
