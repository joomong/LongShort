# tests/unit/test_data_cache.py
"""
DataCache 단위 테스트.

DataCache는 config.yaml에 등록된 여러 Parquet 파일을
앱 시작 시 로드하여 메모리에 캐시하는 레이어이다.
Alpha는 DataCache에서 컬럼명으로 데이터를 참조한다.

TC-DC-001: config.yaml에서 데이터셋 경로 목록 로딩
TC-DC-002: 여러 Parquet 파일을 DataLoader로 로드하여 통합
TC-DC-003: 캐시된 데이터에서 컬럼명으로 참조 (close, volume 등)
TC-DC-004: 서로 다른 파퀘의 컬럼이 합쳐지는지 (ohlcv + funding)
TC-DC-005: 타임스탬프 기준 정렬 및 심볼 매칭
TC-DC-006: 존재하지 않는 컬럼 참조 시 에러
TC-DC-007: config.yaml 파일이 없을 때 에러
TC-DC-008: config.yaml에 등록된 파퀘가 없을 때 에러
"""
import pytest
import pandas as pd
import yaml


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def ohlcv_parquet(tmp_path):
    """OHLCV 테스트용 parquet 생성."""
    file_path = tmp_path / "ohlcv" / "um_1d.parquet"
    file_path.parent.mkdir(parents=True)

    symbols = ["BTCUSDT", "ETHUSDT"]
    dates = pd.date_range("2024-01-01", "2024-01-03", freq="1D", tz="UTC")
    data = []
    for ts in dates:
        ts_ms = int(ts.value // 1_000_000)
        for i, sym in enumerate(symbols):
            base = 100 * (i + 1)
            data.append({
                "open_time": ts_ms,
                "symbol": sym,
                "open": base + 1.0,
                "high": base + 5.0,
                "low": base - 2.0,
                "close": base + 3.0,
                "volume": 1000.0 * (i + 1),
            })
    pd.DataFrame(data).to_parquet(file_path, index=False)
    return file_path


@pytest.fixture
def funding_parquet(tmp_path):
    """Funding rate 테스트용 parquet 생성."""
    file_path = tmp_path / "funding" / "um_1d.parquet"
    file_path.parent.mkdir(parents=True)

    symbols = ["BTCUSDT", "ETHUSDT"]
    dates = pd.date_range("2024-01-01", "2024-01-03", freq="1D", tz="UTC")
    data = []
    for ts in dates:
        ts_ms = int(ts.value // 1_000_000)
        for i, sym in enumerate(symbols):
            data.append({
                "open_time": ts_ms,
                "symbol": sym,
                "funding_rate": 0.0001 * (i + 1),
            })
    pd.DataFrame(data).to_parquet(file_path, index=False)
    return file_path


@pytest.fixture
def config_yaml(tmp_path, ohlcv_parquet, funding_parquet):
    """config.yaml 생성."""
    config_path = tmp_path / "config.yaml"
    config = {
        "datasets": [
            {"path": str(ohlcv_parquet)},
            {"path": str(funding_parquet)},
        ],
        "backtest": {
            "start_date": "2024-01-01",
            "end_date": "2024-01-03",
        },
    }
    with open(config_path, "w") as f:
        yaml.dump(config, f)
    return config_path


# =============================================================================
# TC-DC-001: config.yaml에서 데이터셋 경로 목록 로딩
# =============================================================================


class TestDataCacheConfigLoading:
    """TC-DC-001: config.yaml에서 데이터셋 경로 목록 로딩"""

    def test_loads_dataset_paths_from_config(self, config_yaml):
        """config.yaml에서 데이터셋 경로 목록을 읽을 수 있는지"""
        from framework.data_cache import DataCache

        cache = DataCache(config_path=str(config_yaml))
        assert len(cache.dataset_paths) == 2

    def test_config_not_found_raises_error(self, tmp_path):
        """config.yaml이 없을 때 FileNotFoundError"""
        from framework.data_cache import DataCache

        with pytest.raises(FileNotFoundError):
            DataCache(config_path=str(tmp_path / "nonexistent.yaml"))

    def test_config_empty_datasets_raises_error(self, tmp_path):
        """config.yaml에 datasets가 비어있을 때 에러"""
        from framework.data_cache import DataCache

        config_path = tmp_path / "empty_config.yaml"
        with open(config_path, "w") as f:
            yaml.dump({"datasets": []}, f)

        with pytest.raises(ValueError):
            DataCache(config_path=str(config_path))


# =============================================================================
# TC-DC-002: 여러 Parquet 파일을 DataLoader로 로드하여 통합
# =============================================================================


class TestDataCacheMultipleParquetLoading:
    """TC-DC-002: 여러 Parquet 파일을 DataLoader로 로드하여 통합"""

    def test_loads_multiple_parquets(self, config_yaml):
        """여러 parquet 파일을 로드하여 메모리에 캐시하는지"""
        from framework.data_cache import DataCache

        cache = DataCache(config_path=str(config_yaml))
        cache.load()

        # 캐시에 데이터가 존재해야 함
        assert cache.is_loaded

    def test_parquet_not_found_raises_error(self, tmp_path):
        """등록된 parquet 파일이 없을 때 에러"""
        from framework.data_cache import DataCache

        config_path = tmp_path / "bad_config.yaml"
        config = {
            "datasets": [
                {"path": str(tmp_path / "nonexistent.parquet")},
            ],
            "backtest": {
                "start_date": "2024-01-01",
                "end_date": "2024-01-03",
            },
        }
        with open(config_path, "w") as f:
            yaml.dump(config, f)

        cache = DataCache(config_path=str(config_path))
        with pytest.raises(FileNotFoundError):
            cache.load()


# =============================================================================
# TC-DC-003: 캐시된 데이터에서 컬럼명으로 참조
# =============================================================================


class TestDataCacheColumnAccess:
    """TC-DC-003: 캐시된 데이터에서 컬럼명으로 참조"""

    def test_access_ohlcv_columns(self, config_yaml):
        """OHLCV 컬럼 (close, volume 등) 참조 가능한지"""
        from framework.data_cache import DataCache

        cache = DataCache(config_path=str(config_yaml))
        cache.load()

        ts = pd.Timestamp("2024-01-01", tz="UTC")
        data_slice = cache.get_slice(ts)

        assert "close" in data_slice.columns
        assert "volume" in data_slice.columns

    def test_access_funding_column(self, config_yaml):
        """funding_rate 컬럼 참조 가능한지"""
        from framework.data_cache import DataCache

        cache = DataCache(config_path=str(config_yaml))
        cache.load()

        ts = pd.Timestamp("2024-01-01", tz="UTC")
        data_slice = cache.get_slice(ts)

        assert "funding_rate" in data_slice.columns

    def test_nonexistent_column_raises_error(self, config_yaml):
        """존재하지 않는 컬럼 참조 시 KeyError"""
        from framework.data_cache import DataCache

        cache = DataCache(config_path=str(config_yaml))
        cache.load()

        ts = pd.Timestamp("2024-01-01", tz="UTC")
        data_slice = cache.get_slice(ts)

        with pytest.raises(KeyError):
            _ = data_slice["nonexistent_column"]


# =============================================================================
# TC-DC-004: 서로 다른 파퀘의 컬럼이 합쳐지는지
# =============================================================================


class TestDataCacheMergedData:
    """TC-DC-004: 서로 다른 파퀘의 컬럼이 합쳐지는지"""

    def test_merged_columns_from_different_parquets(self, config_yaml):
        """OHLCV + funding 컬럼이 하나의 data_slice에 합쳐지는지"""
        from framework.data_cache import DataCache

        cache = DataCache(config_path=str(config_yaml))
        cache.load()

        ts = pd.Timestamp("2024-01-01", tz="UTC")
        data_slice = cache.get_slice(ts)

        # OHLCV 컬럼
        assert "open" in data_slice.columns
        assert "close" in data_slice.columns
        # funding 컬럼
        assert "funding_rate" in data_slice.columns

    def test_symbols_aligned_after_merge(self, config_yaml):
        """합친 후 심볼이 올바르게 정렬되는지"""
        from framework.data_cache import DataCache

        cache = DataCache(config_path=str(config_yaml))
        cache.load()

        ts = pd.Timestamp("2024-01-01", tz="UTC")
        data_slice = cache.get_slice(ts)

        assert set(data_slice.index) == {"BTCUSDT", "ETHUSDT"}
        # 각 심볼의 close와 funding_rate가 같은 행에 존재
        for sym in ["BTCUSDT", "ETHUSDT"]:
            row = data_slice.loc[sym]
            assert pd.notna(row["close"])
            assert pd.notna(row["funding_rate"])


# =============================================================================
# TC-DC-005: 타임스탬프 기준 정렬 및 이터레이션
# =============================================================================


class TestDataCacheTimestampIteration:
    """TC-DC-005: 타임스탬프 기준 정렬 및 이터레이션"""

    def test_timestamps_sorted(self, config_yaml):
        """캐시된 타임스탬프가 정렬되어 있는지"""
        from framework.data_cache import DataCache

        cache = DataCache(config_path=str(config_yaml))
        cache.load()

        timestamps = cache.get_timestamps()
        for i in range(1, len(timestamps)):
            assert timestamps[i] > timestamps[i - 1]

    def test_iteration_count(self, config_yaml):
        """3일 데이터 -> 3번 이터레이션"""
        from framework.data_cache import DataCache

        cache = DataCache(config_path=str(config_yaml))
        cache.load()

        timestamps = cache.get_timestamps()
        assert len(timestamps) == 3


# =============================================================================
# TC-DC-010: DataCacheIterator
# =============================================================================


class TestDataCacheIterator:
    """TC-DC-010: DataCacheIterator 테스트."""

    def test_iterates_all_timestamps(self, config_yaml):
        """전체 기간 이터레이션."""
        from framework.data_cache import DataCache, DataCacheIterator

        cache = DataCache(config_path=str(config_yaml))
        cache.load()

        it = DataCacheIterator(cache)
        slices = list(it)
        assert len(slices) == 3

    def test_date_range_filtering(self, config_yaml):
        """날짜 범위 필터링."""
        from framework.data_cache import DataCache, DataCacheIterator

        cache = DataCache(config_path=str(config_yaml))
        cache.load()

        it = DataCacheIterator(cache, start_date="2024-01-02", end_date="2024-01-02")
        slices = list(it)
        assert len(slices) == 1

    def test_yields_timestamp_and_dataframe(self, config_yaml):
        """(timestamp, DataFrame) 튜플 반환."""
        from framework.data_cache import DataCache, DataCacheIterator

        cache = DataCache(config_path=str(config_yaml))
        cache.load()

        it = DataCacheIterator(cache, start_date="2024-01-01", end_date="2024-01-01")
        ts, df = next(iter(it))
        assert isinstance(ts, pd.Timestamp)
        assert isinstance(df, pd.DataFrame)
        assert "close" in df.columns


# =============================================================================
# TC-DC-020: DataCache 기반 Backtester 연동
# =============================================================================


class _MockAlpha:
    """테스트용 Mock Alpha."""
    def compute(self, timestamp, data_slice) -> pd.Series:
        n = len(data_slice)
        return pd.Series(1.0 / n, index=data_slice.index)


class _MockStrategy:
    """테스트용 Mock Strategy."""
    def generate_weights(self, raw_signals, **kwargs) -> pd.Series:
        import numpy as np
        abs_sum = np.nansum(np.abs(raw_signals))
        if abs_sum == 0:
            return raw_signals * 0
        return (raw_signals / abs_sum).fillna(0)


@pytest.fixture
def ohlcv_6day_parquet(tmp_path):
    """IS/OS 테스트용 OHLCV parquet (6일)."""
    file_path = tmp_path / "ohlcv_6d.parquet"
    symbols = ["BTCUSDT", "ETHUSDT"]
    dates = pd.date_range("2024-01-01", "2024-01-06", freq="1D", tz="UTC")
    closes_btc = [100, 110, 105, 108, 115, 112]
    closes_eth = [50, 45, 55, 52, 48, 60]
    data = []
    for i, ts in enumerate(dates):
        ts_ms = int(ts.value // 1_000_000)
        for sym, closes in [("BTCUSDT", closes_btc), ("ETHUSDT", closes_eth)]:
            c = closes[i]
            data.append({
                "open_time": ts_ms, "symbol": sym,
                "open": c - 1, "high": c + 2, "low": c - 2,
                "close": c, "volume": 1000.0,
            })
    pd.DataFrame(data).to_parquet(file_path, index=False)
    return file_path


@pytest.fixture
def cache_6day(tmp_path, ohlcv_6day_parquet):
    """6일 DataCache fixture."""
    from framework.data_cache import DataCache

    config_path = tmp_path / "config_6d.yaml"
    config = {
        "datasets": [{"path": str(ohlcv_6day_parquet)}],
        "backtest": {"start_date": "2024-01-01", "end_date": "2024-01-06"},
    }
    with open(config_path, "w") as f:
        yaml.dump(config, f)

    cache = DataCache(config_path=str(config_path))
    cache.load()
    return cache


class TestDataCacheBacktestIntegration:
    """TC-DC-020: DataCache 기반 Backtester IS/OS 연동 테스트."""

    def test_run_backtest_with_periods_using_cache(self, cache_6day):
        """TC-DC-021: run_backtest_with_periods()가 data_cache로 정상 동작."""
        from framework.backtester import (
            run_backtest_with_periods, PeriodSettings,
            BacktestPeriodResult, PeriodMetrics,
        )

        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        result = run_backtest_with_periods(
            alpha=_MockAlpha(), strategy=_MockStrategy(),
            settings=settings, capital=1_000_000,
            data_cache=cache_6day,
        )
        assert isinstance(result, BacktestPeriodResult)
        assert isinstance(result.is_metrics, PeriodMetrics)
        assert isinstance(result.os_metrics, PeriodMetrics)
        assert len(result.is_ret_series) == 2  # 3 IS bars, first bar has no return -> 2 returns
        assert len(result.os_ret_series) == 3  # 3 OS bars, transition bar has return (warm start) -> 3 returns

    def test_cache_and_loader_produce_same_metrics(self, cache_6day, ohlcv_6day_parquet):
        """TC-DC-022: DataCache 경로와 DataLoader 경로가 동일 결과."""
        from framework.backtester import run_backtest_with_periods, PeriodSettings

        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        alpha = _MockAlpha()
        strategy = _MockStrategy()

        # DataCache path
        cache_result = run_backtest_with_periods(
            alpha=alpha, strategy=strategy,
            settings=settings, capital=1_000_000,
            data_cache=cache_6day,
        )

        # DataLoader path
        loader_result = run_backtest_with_periods(
            alpha=alpha, strategy=strategy,
            settings=settings, capital=1_000_000,
            data_loader_kwargs={
                "file_path": str(ohlcv_6day_parquet),
                "time_col": "open_time",
                "symbol_col": "symbol",
            },
        )

        assert abs(cache_result.is_metrics.pnl - loader_result.is_metrics.pnl) < 1e-10
        assert abs(cache_result.is_metrics.sharpe - loader_result.is_metrics.sharpe) < 1e-10
        assert abs(cache_result.os_metrics.pnl - loader_result.os_metrics.pnl) < 1e-10
        assert abs(cache_result.os_metrics.sharpe - loader_result.os_metrics.sharpe) < 1e-10

    def test_run_multiple_backtest_with_cache(self, cache_6day):
        """TC-DC-023: run_multiple_backtest()가 data_cache로 정상 동작."""
        from framework.backtester import (
            run_multiple_backtest, PeriodSettings, MultipleBacktestResult,
        )

        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        alpha1 = _MockAlpha()
        alpha2 = _MockAlpha()
        alpha1.name = "a1"
        alpha2.name = "a2"

        result = run_multiple_backtest(
            alphas=[alpha1, alpha2],
            strategy=_MockStrategy(),
            settings=settings, capital=1_000_000,
            data_cache=cache_6day,
        )
        assert isinstance(result, MultipleBacktestResult)
        assert len(result.alpha_results) == 2
        assert result.sorted_by == "sharpe"

    def test_no_data_source_raises(self):
        """TC-DC-024: data_loader_kwargs도 data_cache도 없으면 ValueError."""
        from framework.backtester import run_backtest_with_periods, PeriodSettings

        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        with pytest.raises(ValueError, match="Either"):
            run_backtest_with_periods(
                alpha=_MockAlpha(), strategy=_MockStrategy(),
                settings=settings, capital=1_000_000,
            )

    def test_os_mdd_computed_with_cache(self, cache_6day):
        """TC-DC-025: DataCache 경로에서도 OS MDD가 계산되는지."""
        from framework.backtester import run_backtest_with_periods, PeriodSettings

        settings = PeriodSettings(
            is_start="2024-01-01", is_end="2024-01-03",
            os_start="2024-01-04", os_end="2024-01-06",
        )
        result = run_backtest_with_periods(
            alpha=_MockAlpha(), strategy=_MockStrategy(),
            settings=settings, capital=1_000_000,
            data_cache=cache_6day,
        )
        assert hasattr(result.os_metrics, "mdd")
        assert isinstance(result.os_metrics.mdd, float)
