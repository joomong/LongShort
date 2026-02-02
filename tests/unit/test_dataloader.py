# tests/unit/test_dataloader.py
"""
DataLoader 단위 테스트.

테스트 케이스 참조: simulator_plan.md Section 1.8

TC-DL-001: 기본 로딩
TC-DL-002: 타임스탬프 형식
TC-DL-003: data_slice 구조
TC-DL-004: 심볼 필터링
TC-DL-005: 날짜 범위 inclusive
TC-DL-006: 파일 없음 에러
TC-DL-007: 빈 기간
"""
import pytest
import pandas as pd
from pathlib import Path

from framework.dataloader import DataLoader


class TestDataLoaderBasicIteration:
    """TC-DL-001: DataLoader가 정상적으로 이터레이션되는지"""

    def test_iteration_returns_correct_count(self, sample_parquet_path):
        """3일치 데이터를 로딩하면 3개의 결과가 반환되어야 함"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
        )

        results = list(loader)

        assert len(results) == 3, f"Expected 3 results, got {len(results)}"

    def test_iteration_returns_tuples(self, sample_parquet_path):
        """각 이터레이션이 (timestamp, DataFrame) 튜플을 반환하는지"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
        )

        for timestamp, data_slice in loader:
            assert isinstance(timestamp, pd.Timestamp), f"timestamp should be pd.Timestamp, got {type(timestamp)}"
            assert isinstance(data_slice, pd.DataFrame), f"data_slice should be pd.DataFrame, got {type(data_slice)}"

    def test_iterator_protocol(self, sample_parquet_path):
        """__iter__와 __next__ 프로토콜이 올바르게 동작하는지"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-01",  # 1일만
            time_col="open_time",
            symbol_col="symbol",
        )

        iterator = iter(loader)
        timestamp, data_slice = next(iterator)

        assert timestamp is not None
        assert data_slice is not None

        # 더 이상 데이터가 없으면 StopIteration
        with pytest.raises(StopIteration):
            next(iterator)


class TestDataLoaderTimestampFormat:
    """TC-DL-002: 반환되는 timestamp가 UTC tz-aware인지"""

    def test_timestamp_is_utc_aware(self, sample_parquet_path):
        """timestamp가 UTC timezone을 가지는지"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
        )

        timestamp, _ = next(iter(loader))

        assert timestamp.tzinfo is not None, "timestamp should be timezone-aware"
        assert str(timestamp.tzinfo) == "UTC", f"Expected UTC, got {timestamp.tzinfo}"

    def test_timestamps_are_sorted(self, sample_parquet_path):
        """timestamp가 오름차순으로 정렬되어 반환되는지"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
        )

        timestamps = [ts for ts, _ in loader]

        for i in range(1, len(timestamps)):
            assert timestamps[i] > timestamps[i - 1], "Timestamps should be sorted ascending"


class TestDataLoaderSliceStructure:
    """TC-DL-003: data_slice가 올바른 구조인지"""

    def test_slice_has_instrument_index(self, sample_parquet_path):
        """data_slice의 index가 instrument(symbol)인지"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
        )

        _, data_slice = next(iter(loader))

        # index.name이 "instrument"이거나 index 값이 symbol 문자열이어야 함
        assert data_slice.index.name == "instrument" or all(
            isinstance(idx, str) for idx in data_slice.index
        ), "data_slice index should be instrument symbols"

    def test_slice_has_ohlcv_columns(self, sample_parquet_path):
        """data_slice가 OHLCV 컬럼을 포함하는지"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
        )

        _, data_slice = next(iter(loader))

        expected_columns = ["open", "high", "low", "close", "volume"]
        for col in expected_columns:
            assert col in data_slice.columns, f"Missing column: {col}"

    def test_slice_contains_correct_symbols(self, sample_parquet_path):
        """data_slice가 올바른 심볼들을 포함하는지"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
        )

        _, data_slice = next(iter(loader))

        expected_symbols = {"BTCUSDT", "ETHUSDT", "XRPUSDT"}
        actual_symbols = set(data_slice.index)

        assert actual_symbols == expected_symbols, f"Expected {expected_symbols}, got {actual_symbols}"


class TestDataLoaderSymbolFilter:
    """TC-DL-004: select_symbols가 올바르게 필터링되는지"""

    def test_filter_single_symbol(self, sample_parquet_path):
        """단일 심볼 필터링"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
            select_symbols=["BTCUSDT"],
        )

        _, data_slice = next(iter(loader))

        assert set(data_slice.index) == {"BTCUSDT"}, f"Expected only BTCUSDT, got {set(data_slice.index)}"

    def test_filter_multiple_symbols(self, sample_parquet_path):
        """다중 심볼 필터링"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
            select_symbols=["BTCUSDT", "ETHUSDT"],
        )

        _, data_slice = next(iter(loader))

        assert set(data_slice.index) == {"BTCUSDT", "ETHUSDT"}

    def test_filter_none_returns_all(self, sample_parquet_path):
        """select_symbols=None이면 전체 심볼 반환"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
            select_symbols=None,
        )

        _, data_slice = next(iter(loader))

        assert len(data_slice.index) == 3, "Should return all 3 symbols"

    def test_filter_empty_list_returns_all(self, sample_parquet_path):
        """select_symbols=[]이면 전체 심볼 반환"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
            select_symbols=[],
        )

        _, data_slice = next(iter(loader))

        assert len(data_slice.index) == 3, "Empty list should return all symbols"

    def test_filter_nonexistent_symbol_returns_empty(self, sample_parquet_path):
        """존재하지 않는 심볼 필터링 시 빈 결과"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
            select_symbols=["NONEXISTENT"],
        )

        results = list(loader)

        assert len(results) == 0, "Nonexistent symbol should return empty results"


class TestDataLoaderDateRange:
    """TC-DL-005: end_date가 inclusive인지"""

    def test_end_date_inclusive(self, sample_parquet_path):
        """end_date 날짜의 데이터가 포함되는지"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
        )

        results = list(loader)
        timestamps = [ts for ts, _ in results]

        # 마지막 타임스탬프가 2024-01-03을 포함해야 함
        last_ts = timestamps[-1]
        assert last_ts.date() == pd.Timestamp("2024-01-03", tz="UTC").date()

    def test_single_day_range(self, sample_parquet_path):
        """start_date == end_date인 경우 해당 날짜 데이터만 반환"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-02",
            end_date="2024-01-02",
            time_col="open_time",
            symbol_col="symbol",
        )

        results = list(loader)

        assert len(results) == 1, "Should return exactly 1 day of data"
        ts, _ = results[0]
        assert ts.date() == pd.Timestamp("2024-01-02", tz="UTC").date()

    def test_date_range_filtering(self, extended_parquet_path):
        """날짜 범위 필터링이 정확한지"""
        loader = DataLoader(
            file_path=str(extended_parquet_path),
            start_date="2024-01-03",
            end_date="2024-01-07",
            time_col="open_time",
            symbol_col="symbol",
        )

        results = list(loader)

        assert len(results) == 5, f"Expected 5 days (01-03 to 01-07), got {len(results)}"

        timestamps = [ts for ts, _ in results]
        assert timestamps[0].date() == pd.Timestamp("2024-01-03", tz="UTC").date()
        assert timestamps[-1].date() == pd.Timestamp("2024-01-07", tz="UTC").date()


class TestDataLoaderFileNotFound:
    """TC-DL-006: 존재하지 않는 파일에 대해 에러 발생"""

    def test_raises_file_not_found_error(self):
        """FileNotFoundError 발생 확인"""
        with pytest.raises(FileNotFoundError):
            DataLoader(
                file_path="/nonexistent/path/to/file.parquet",
                start_date="2024-01-01",
                end_date="2024-01-03",
                time_col="open_time",
                symbol_col="symbol",
            )

    def test_error_message_contains_path(self):
        """에러 메시지에 경로가 포함되는지"""
        bad_path = "/nonexistent/path/to/file.parquet"
        with pytest.raises(FileNotFoundError) as exc_info:
            DataLoader(
                file_path=bad_path,
                start_date="2024-01-01",
                end_date="2024-01-03",
                time_col="open_time",
                symbol_col="symbol",
            )

        assert bad_path in str(exc_info.value)


class TestDataLoaderEmptyPeriod:
    """TC-DL-007: 데이터 없는 기간에 대해 빈 결과"""

    def test_future_date_returns_empty(self, sample_parquet_path):
        """미래 날짜 범위에 대해 빈 결과"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2099-01-01",
            end_date="2099-12-31",
            time_col="open_time",
            symbol_col="symbol",
        )

        results = list(loader)

        assert len(results) == 0, "Future dates should return empty results"

    def test_past_date_returns_empty(self, sample_parquet_path):
        """과거 날짜 범위(데이터 이전)에 대해 빈 결과"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2020-01-01",
            end_date="2020-12-31",
            time_col="open_time",
            symbol_col="symbol",
        )

        results = list(loader)

        assert len(results) == 0, "Dates before data should return empty results"


class TestDataLoaderSingleTimestamp:
    """EC-DL-005: 단일 타임스탬프만 존재할 때"""

    def test_single_timestamp_iteration(self, single_timestamp_parquet_path):
        """단일 타임스탬프에서 정상 1회 이터레이션"""
        loader = DataLoader(
            file_path=str(single_timestamp_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-01",
            time_col="open_time",
            symbol_col="symbol",
        )

        results = list(loader)

        assert len(results) == 1, "Should return exactly 1 result"

        ts, data_slice = results[0]
        assert ts.date() == pd.Timestamp("2024-01-01", tz="UTC").date()
        assert len(data_slice) == 2  # BTCUSDT, ETHUSDT


class TestDataLoaderSelectColumns:
    """select_columns 파라미터 테스트"""

    def test_select_specific_columns(self, sample_parquet_path):
        """특정 컬럼만 로딩"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
            select_columns=["close", "volume"],
        )

        _, data_slice = next(iter(loader))

        # close, volume은 있어야 함
        assert "close" in data_slice.columns
        assert "volume" in data_slice.columns

    def test_select_columns_none_returns_all(self, sample_parquet_path):
        """select_columns=None이면 전체 컬럼 반환"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
            select_columns=None,
        )

        _, data_slice = next(iter(loader))

        expected_columns = ["open", "high", "low", "close", "volume"]
        for col in expected_columns:
            assert col in data_slice.columns


class TestDataLoaderChunkingMode:
    """청킹 모드 테스트"""

    def test_chunking_produces_same_results(self, extended_parquet_path):
        """청킹 모드와 전체 로딩 모드가 동일한 결과를 내는지"""
        # 전체 로딩
        loader_full = DataLoader(
            file_path=str(extended_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-10",
            time_col="open_time",
            symbol_col="symbol",
            chunk_row_size=None,
        )
        results_full = list(loader_full)

        # 청킹 (작은 청크 사이즈로)
        loader_chunked = DataLoader(
            file_path=str(extended_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-10",
            time_col="open_time",
            symbol_col="symbol",
            chunk_row_size=10,  # 작은 청크
        )
        results_chunked = list(loader_chunked)

        # 동일한 수의 타임스탬프
        assert len(results_full) == len(results_chunked), (
            f"Full: {len(results_full)}, Chunked: {len(results_chunked)}"
        )

        # 각 타임스탬프와 데이터 비교
        for (ts_full, df_full), (ts_chunk, df_chunk) in zip(results_full, results_chunked):
            assert ts_full == ts_chunk, f"Timestamps differ: {ts_full} vs {ts_chunk}"
            assert set(df_full.index) == set(df_chunk.index), "Symbols differ"

    def test_chunking_preserves_complete_timestamps(self, extended_parquet_path):
        """청킹 모드에서 타임스탬프 단위로 완전한 슬라이스가 보장되는지"""
        loader = DataLoader(
            file_path=str(extended_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-10",
            time_col="open_time",
            symbol_col="symbol",
            chunk_row_size=3,  # 의도적으로 작은 청크
        )

        for ts, data_slice in loader:
            # 각 타임스탬프에 모든 심볼이 포함되어야 함 (5개)
            assert len(data_slice) == 5, f"Expected 5 symbols at {ts}, got {len(data_slice)}"


class TestDataLoaderMultiIndex:
    """REQ-DL-003: 출력 DataFrame이 (timestamp, instrument) MultiIndex인지"""

    def test_full_dataframe_has_multiindex(self, sample_parquet_path):
        """전체 로딩 시 내부 DataFrame이 MultiIndex인지"""
        loader = DataLoader(
            file_path=str(sample_parquet_path),
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
        )

        # 내부 _full_data 확인
        assert isinstance(loader._full_data.index, pd.MultiIndex)
        assert loader._full_data.index.names == ["timestamp", "instrument"]


class TestDataLoaderRealData:
    """실제 데이터 파일을 사용한 통합 테스트 (선택적)"""

    @pytest.mark.skipif(
        not Path("/home/jgo/qv-project/LongShort/Data/ohlcv/um_1d.parquet").exists(),
        reason="Real data file not found",
    )
    def test_real_data_basic_iteration(self):
        """실제 데이터로 기본 이터레이션 테스트"""
        loader = DataLoader(
            file_path="/home/jgo/qv-project/LongShort/Data/ohlcv/um_1d.parquet",
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
        )

        results = list(loader)

        assert len(results) == 3
        for ts, data_slice in results:
            assert isinstance(ts, pd.Timestamp)
            assert ts.tzinfo is not None
            assert len(data_slice) > 0

    @pytest.mark.skipif(
        not Path("/home/jgo/qv-project/LongShort/Data/ohlcv/um_1d.parquet").exists(),
        reason="Real data file not found",
    )
    def test_real_data_symbol_filter(self):
        """실제 데이터로 심볼 필터링 테스트"""
        loader = DataLoader(
            file_path="/home/jgo/qv-project/LongShort/Data/ohlcv/um_1d.parquet",
            start_date="2024-01-01",
            end_date="2024-01-03",
            time_col="open_time",
            symbol_col="symbol",
            select_symbols=["BTCUSDT", "ETHUSDT"],
        )

        for ts, data_slice in loader:
            symbols = set(data_slice.index)
            assert symbols <= {"BTCUSDT", "ETHUSDT"}, f"Unexpected symbols: {symbols}"
