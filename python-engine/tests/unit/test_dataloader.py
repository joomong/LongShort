# tests/unit/test_dataloader.py
"""
DataCache 단위 테스트 (numpy interface).

테스트 케이스 참조: simulator_plan.md Section 1.8

TC-DL-001: 기본 로딩
TC-DL-002: 타임스탬프 형식
TC-DL-003: arrays 구조
TC-DL-004: 심볼 필터링
TC-DL-005: 날짜 범위 inclusive
TC-DL-006: 파일 없음 에러
TC-DL-007: 빈 기간
"""
import pytest
import numpy as np
import pandas as pd
from pathlib import Path

from tests.conftest import make_test_cache


class TestDataCacheBasicLoading:
    """TC-DL-001: DataCache가 정상적으로 로딩되는지"""

    def test_load_returns_correct_timestamp_count(self, sample_parquet_path, tmp_path):
        """3일치 데이터를 로딩하면 3개의 타임스탬프가 반환되어야 함"""
        cache = make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path)
        assert len(cache.timestamps) == 3, f"Expected 3 timestamps, got {len(cache.timestamps)}"

    def test_load_returns_correct_symbol_count(self, sample_parquet_path, tmp_path):
        """3개 심볼을 로딩하면 3개의 심볼이 반환되어야 함"""
        cache = make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path)
        assert len(cache.symbols) == 3, f"Expected 3 symbols, got {len(cache.symbols)}"

    def test_arrays_populated(self, sample_parquet_path, tmp_path):
        """load() 후 arrays dict가 채워져야 함"""
        cache = make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path)
        assert cache.arrays is not None
        assert len(cache.arrays) > 0


class TestDataCacheTimestampFormat:
    """TC-DL-002: 타임스탬프가 올바른 형식인지"""

    def test_timestamps_are_numpy_datetime(self, sample_parquet_path, tmp_path):
        """timestamps가 numpy datetime64 배열인지"""
        cache = make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path)
        assert isinstance(cache.timestamps, np.ndarray)
        assert np.issubdtype(cache.timestamps.dtype, np.datetime64)

    def test_timestamps_are_sorted(self, sample_parquet_path, tmp_path):
        """timestamps가 오름차순으로 정렬되어 있는지"""
        cache = make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path)
        for i in range(1, len(cache.timestamps)):
            assert cache.timestamps[i] > cache.timestamps[i - 1], "Timestamps should be sorted ascending"


class TestDataCacheArrayStructure:
    """TC-DL-003: arrays가 올바른 구조인지"""

    def test_arrays_shape(self, sample_parquet_path, tmp_path):
        """각 array가 (T, N) shape인지"""
        cache = make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path)
        T = len(cache.timestamps)
        N = len(cache.symbols)
        for col, arr in cache.arrays.items():
            assert arr.shape == (T, N), f"Array '{col}' shape {arr.shape} != ({T}, {N})"

    def test_arrays_has_ohlcv(self, sample_parquet_path, tmp_path):
        """arrays가 OHLCV 컬럼을 포함하는지"""
        cache = make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path)
        expected_columns = ["open", "high", "low", "close", "volume"]
        for col in expected_columns:
            assert col in cache.arrays, f"Missing column: {col}"

    def test_symbols_correct(self, sample_parquet_path, tmp_path):
        """symbols가 올바른 심볼들을 포함하는지"""
        cache = make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path)
        expected_symbols = {"BTCUSDT", "ETHUSDT", "XRPUSDT"}
        actual_symbols = set(cache.symbols.tolist())
        assert actual_symbols == expected_symbols, f"Expected {expected_symbols}, got {actual_symbols}"

    def test_sym_index_matches_symbols(self, sample_parquet_path, tmp_path):
        """sym_index가 symbols와 일치하는지"""
        cache = make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path)
        for sym, idx in cache.sym_index.items():
            assert cache.symbols[idx] == sym


class TestDataCacheSymbolFilter:
    """TC-DL-004: universe가 올바르게 필터링되는지"""

    def test_filter_single_symbol(self, sample_parquet_path, tmp_path):
        """단일 심볼 필터링"""
        cache = make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path,
                                 symbols=["BTCUSDT"])
        assert set(cache.symbols.tolist()) == {"BTCUSDT"}

    def test_filter_multiple_symbols(self, sample_parquet_path, tmp_path):
        """다중 심볼 필터링"""
        cache = make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path,
                                 symbols=["BTCUSDT", "ETHUSDT"])
        assert set(cache.symbols.tolist()) == {"BTCUSDT", "ETHUSDT"}

    def test_filter_none_returns_all(self, sample_parquet_path, tmp_path):
        """universe=None이면 전체 심볼 반환"""
        cache = make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path,
                                 symbols=None)
        assert len(cache.symbols) == 3, "Should return all 3 symbols"

    def test_filter_empty_list_returns_all(self, sample_parquet_path, tmp_path):
        """universe=[]이면 전체 심볼 반환"""
        cache = make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path,
                                 symbols=[])
        assert len(cache.symbols) == 3, "Empty list should return all symbols"

    def test_filter_nonexistent_symbol_raises_on_load(self, sample_parquet_path, tmp_path):
        """존재하지 않는 심볼 필터링 시 load()에서 ValueError"""
        with pytest.raises(ValueError, match="no data"):
            make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path,
                            symbols=["NONEXISTENT"])


class TestDataCacheDateRange:
    """TC-DL-005: end_date가 inclusive인지"""

    def test_end_date_inclusive(self, sample_parquet_path, tmp_path):
        """end_date 날짜의 데이터가 포함되는지"""
        cache = make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path)
        ts = pd.DatetimeIndex(cache.timestamps)
        if ts.tz is None:
            ts = ts.tz_localize("UTC")
        last_ts = ts[-1]
        assert last_ts.date() == pd.Timestamp("2024-01-03", tz="UTC").date()

    def test_single_day_range(self, sample_parquet_path, tmp_path):
        """start_date == end_date인 경우 해당 날짜 데이터만 반환"""
        cache = make_test_cache(sample_parquet_path, "2024-01-02", "2024-01-02", tmp_path)
        assert len(cache.timestamps) == 1, "Should return exactly 1 day of data"

    def test_date_range_filtering(self, extended_parquet_path, tmp_path):
        """날짜 범위 필터링이 정확한지"""
        cache = make_test_cache(extended_parquet_path, "2024-01-03", "2024-01-07", tmp_path)
        assert len(cache.timestamps) == 5, f"Expected 5 days (01-03 to 01-07), got {len(cache.timestamps)}"


class TestDataCacheFileNotFound:
    """TC-DL-006: 존재하지 않는 파일에 대해 에러 발생"""

    def test_raises_file_not_found_error(self, tmp_path):
        """FileNotFoundError 발생 확인"""
        bad_path = "/nonexistent/path/to/file.parquet"
        with pytest.raises(FileNotFoundError):
            make_test_cache(bad_path, "2024-01-01", "2024-01-03", tmp_path)

    def test_error_message_contains_path(self, tmp_path):
        """에러 메시지에 경로가 포함되는지"""
        bad_path = "/nonexistent/path/to/file.parquet"
        with pytest.raises(FileNotFoundError) as exc_info:
            make_test_cache(bad_path, "2024-01-01", "2024-01-03", tmp_path)
        assert bad_path in str(exc_info.value)


class TestDataCacheEmptyPeriod:
    """TC-DL-007: 데이터 없는 기간에 대해 ValueError"""

    def test_future_date_raises_on_load(self, sample_parquet_path, tmp_path):
        """미래 날짜 범위에 대해 load()에서 ValueError"""
        with pytest.raises(ValueError, match="no data"):
            make_test_cache(sample_parquet_path, "2099-01-01", "2099-12-31", tmp_path)

    def test_past_date_raises_on_load(self, sample_parquet_path, tmp_path):
        """과거 날짜 범위(데이터 이전)에 대해 load()에서 ValueError"""
        with pytest.raises(ValueError, match="no data"):
            make_test_cache(sample_parquet_path, "2020-01-01", "2020-12-31", tmp_path)


class TestDataCacheSingleTimestamp:
    """EC-DL-005: 단일 타임스탬프만 존재할 때"""

    def test_single_timestamp(self, single_timestamp_parquet_path, tmp_path):
        """단일 타임스탬프에서 정상 로딩"""
        cache = make_test_cache(single_timestamp_parquet_path, "2024-01-01", "2024-01-01", tmp_path)
        assert len(cache.timestamps) == 1
        assert len(cache.symbols) == 2  # BTCUSDT, ETHUSDT


class TestDataCacheSelectColumns:
    """select_columns 파라미터 테스트"""

    def test_select_specific_columns(self, sample_parquet_path, tmp_path):
        """특정 컬럼만 로딩"""
        cache = make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path,
                                 use_col=["close", "volume"])
        assert "close" in cache.arrays
        assert "volume" in cache.arrays

    def test_select_columns_none_returns_all(self, sample_parquet_path, tmp_path):
        """use_col=None이면 전체 컬럼 반환"""
        cache = make_test_cache(sample_parquet_path, "2024-01-01", "2024-01-03", tmp_path,
                                 use_col=None)
        expected_columns = ["open", "high", "low", "close", "volume"]
        for col in expected_columns:
            assert col in cache.arrays
