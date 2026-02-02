"""
DataCache 엣지 케이스 테스트.

EC-DC-001: 중복 컬럼 (두 parquet에 같은 컬럼명이 있을 때)
EC-DC-002: 심볼 커버리지가 다른 경우 (parquet A: BTC+ETH, parquet B: BTC만)
EC-DC-003: get_slice / get_timestamps 호출 전에 load() 안 했을 때 RuntimeError
EC-DC-004: config.yaml에 datasets 키 자체가 없을 때
EC-DC-005: config.yaml이 유효하지 않은 YAML일 때
EC-DC-006: 빈 기간 (데이터 없는 날짜 범위)
EC-DC-007: 단일 데이터셋만 있는 config
EC-DC-008: parquet에 time_col/symbol_col이 없을 때
EC-DC-009: 날짜 범위가 일부만 겹치는 parquet들 (ohlcv: 1/1~1/5, funding: 1/3~1/7)
EC-DC-010: get_slice에 존재하지 않는 타임스탬프 전달 시
"""
import pytest
import pandas as pd
import yaml

import pyarrow.lib

from framework.data_cache import DataCache


# =============================================================================
# Helper: parquet 파일 생성 유틸
# =============================================================================

def _make_parquet(path, data):
    """딕셔너리 리스트 -> parquet 파일 생성."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(data).to_parquet(path, index=False)
    return path


def _make_config(tmp_path, paths, filename="config.yaml", start_date="2024-01-01", end_date="2024-01-03", time_col=None):
    """데이터셋 경로 리스트 -> config.yaml 생성."""
    config_path = tmp_path / filename
    config = {"datasets": [{"path": str(p)} for p in paths]}
    config["backtest"] = {"start_date": start_date, "end_date": end_date}
    if time_col is not None:
        config["columns"] = {"time": time_col}
    with open(config_path, "w") as f:
        yaml.dump(config, f)
    return config_path


def _ts_ms(date_str):
    """날짜 문자열 -> UTC ms epoch."""
    return int(pd.Timestamp(date_str, tz="UTC").value // 1_000_000)


# =============================================================================
# EC-DC-001: 중복 컬럼
# =============================================================================


class TestDuplicateColumns:
    """EC-DC-001: 두 parquet에 같은 컬럼명(close)이 있을 때."""

    def test_duplicate_column_raises_value_error(self, tmp_path):
        """중복 컬럼(close)이 있는 두 parquet을 join하면 ValueError가 발생한다."""
        dates = pd.date_range("2024-01-01", "2024-01-02", freq="1D", tz="UTC")
        symbols = ["BTCUSDT"]

        # parquet A: close = 100.0
        data_a = []
        for ts in dates:
            for sym in symbols:
                data_a.append({
                    "open_time": _ts_ms(str(ts.date())),
                    "symbol": sym,
                    "close": 100.0,
                    "volume": 500.0,
                })
        pq_a = _make_parquet(tmp_path / "a" / "data.parquet", data_a)

        # parquet B: close = 999.0 (다른 값, 동일 컬럼명)
        data_b = []
        for ts in dates:
            for sym in symbols:
                data_b.append({
                    "open_time": _ts_ms(str(ts.date())),
                    "symbol": sym,
                    "close": 999.0,
                })
        pq_b = _make_parquet(tmp_path / "b" / "data.parquet", data_b)

        config_path = _make_config(tmp_path, [pq_a, pq_b], end_date="2024-01-02")
        cache = DataCache(config_path=str(config_path))

        # pandas DataFrame.join은 lsuffix/rsuffix 없이 중복 컬럼 -> ValueError
        with pytest.raises(ValueError, match="columns overlap"):
            cache.load()


# =============================================================================
# EC-DC-002: 심볼 커버리지 불일치
# =============================================================================


class TestSymbolCoverageMismatch:
    """EC-DC-002: parquet 간 심볼 커버리지가 다른 경우."""

    def test_missing_symbol_gets_nan(self, tmp_path):
        """ohlcv: BTC+ETH, funding: BTC만 -> ETH의 funding_rate는 NaN."""
        dates = pd.date_range("2024-01-01", "2024-01-02", freq="1D", tz="UTC")

        # ohlcv: BTCUSDT, ETHUSDT
        ohlcv_data = []
        for ts in dates:
            for i, sym in enumerate(["BTCUSDT", "ETHUSDT"]):
                ohlcv_data.append({
                    "open_time": _ts_ms(str(ts.date())),
                    "symbol": sym,
                    "open": 100.0 * (i + 1),
                    "high": 105.0 * (i + 1),
                    "low": 95.0 * (i + 1),
                    "close": 102.0 * (i + 1),
                    "volume": 1000.0,
                })
        pq_ohlcv = _make_parquet(tmp_path / "ohlcv" / "data.parquet", ohlcv_data)

        # funding: BTCUSDT만
        funding_data = []
        for ts in dates:
            funding_data.append({
                "open_time": _ts_ms(str(ts.date())),
                "symbol": "BTCUSDT",
                "funding_rate": 0.0001,
            })
        pq_fund = _make_parquet(tmp_path / "funding" / "data.parquet", funding_data)

        config_path = _make_config(tmp_path, [pq_ohlcv, pq_fund], end_date="2024-01-02")
        cache = DataCache(config_path=str(config_path))
        cache.load()

        ts = pd.Timestamp("2024-01-01", tz="UTC")
        data_slice = cache.get_slice(ts)

        # BTCUSDT: close와 funding_rate 모두 존재
        assert pd.notna(data_slice.loc["BTCUSDT", "close"])
        assert pd.notna(data_slice.loc["BTCUSDT", "funding_rate"])

        # ETHUSDT: close 존재, funding_rate는 NaN
        assert pd.notna(data_slice.loc["ETHUSDT", "close"])
        assert pd.isna(data_slice.loc["ETHUSDT", "funding_rate"])


# =============================================================================
# EC-DC-003: load() 안 한 상태
# =============================================================================


class TestNotLoaded:
    """EC-DC-003: load() 호출 전 접근 시 RuntimeError."""

    @pytest.fixture
    def unloaded_cache(self, tmp_path):
        """load()를 호출하지 않은 DataCache."""
        data = [{
            "open_time": _ts_ms("2024-01-01"),
            "symbol": "BTCUSDT",
            "close": 100.0,
        }]
        pq = _make_parquet(tmp_path / "data" / "test.parquet", data)
        config_path = _make_config(tmp_path, [pq], end_date="2024-01-01")
        return DataCache(config_path=str(config_path))

    def test_is_loaded_false(self, unloaded_cache):
        """load() 전에는 is_loaded가 False."""
        assert unloaded_cache.is_loaded is False

    def test_get_slice_raises_runtime_error(self, unloaded_cache):
        """load() 전 get_slice 호출 시 RuntimeError."""
        with pytest.raises(RuntimeError, match="not loaded"):
            unloaded_cache.get_slice(pd.Timestamp("2024-01-01", tz="UTC"))

    def test_get_timestamps_raises_runtime_error(self, unloaded_cache):
        """load() 전 get_timestamps 호출 시 RuntimeError."""
        with pytest.raises(RuntimeError, match="not loaded"):
            unloaded_cache.get_timestamps()


# =============================================================================
# EC-DC-004: datasets 키 없음
# =============================================================================


class TestMissingDatasetsKey:
    """EC-DC-004: config.yaml에 datasets 키가 없을 때."""

    def test_empty_config_raises_value_error(self, tmp_path):
        """빈 config ({})일 때 ValueError."""
        config_path = tmp_path / "empty.yaml"
        with open(config_path, "w") as f:
            yaml.dump({}, f)

        with pytest.raises(ValueError, match="datasets"):
            DataCache(config_path=str(config_path))

    def test_no_datasets_key_raises_value_error(self, tmp_path):
        """datasets 키 없이 다른 키만 있을 때 ValueError."""
        config_path = tmp_path / "other.yaml"
        with open(config_path, "w") as f:
            yaml.dump({"other_key": "value"}, f)

        with pytest.raises(ValueError, match="datasets"):
            DataCache(config_path=str(config_path))


# =============================================================================
# EC-DC-005: 유효하지 않은 YAML
# =============================================================================


class TestInvalidYaml:
    """EC-DC-005: config.yaml이 유효하지 않은 YAML일 때."""

    def test_invalid_yaml_raises_error(self, tmp_path):
        """유효하지 않은 YAML 내용이면 에러 발생."""
        config_path = tmp_path / "invalid.yaml"
        with open(config_path, "w") as f:
            f.write("{{invalid yaml content: [}")

        with pytest.raises((yaml.YAMLError, yaml.scanner.ScannerError)):
            DataCache(config_path=str(config_path))


# =============================================================================
# EC-DC-006: 빈 기간
# =============================================================================


class TestEmptyPeriod:
    """EC-DC-006: 데이터 없는 날짜 범위."""

    def test_empty_cache_loads_successfully(self, tmp_path):
        """미래 날짜로 조회 시 load()는 성공하되 캐시는 비어있다."""
        data = [{
            "open_time": _ts_ms("2024-01-01"),
            "symbol": "BTCUSDT",
            "close": 100.0,
        }]
        pq = _make_parquet(tmp_path / "data" / "test.parquet", data)
        config_path = _make_config(tmp_path, [pq], start_date="2099-01-01", end_date="2099-12-31")

        cache = DataCache(config_path=str(config_path))
        cache.load()
        assert cache.is_loaded

    def test_empty_cache_timestamps(self, tmp_path):
        """빈 캐시의 get_timestamps()는 빈 인덱스를 반환한다."""
        data = [{
            "open_time": _ts_ms("2024-01-01"),
            "symbol": "BTCUSDT",
            "close": 100.0,
        }]
        pq = _make_parquet(tmp_path / "data" / "test.parquet", data)
        config_path = _make_config(tmp_path, [pq], start_date="2099-01-01", end_date="2099-12-31")

        cache = DataCache(config_path=str(config_path))
        cache.load()
        timestamps = cache.get_timestamps()
        assert len(timestamps) == 0

    def test_empty_cache_get_slice_raises(self, tmp_path):
        """빈 캐시에서 get_slice 호출 시 KeyError."""
        data = [{
            "open_time": _ts_ms("2024-01-01"),
            "symbol": "BTCUSDT",
            "close": 100.0,
        }]
        pq = _make_parquet(tmp_path / "data" / "test.parquet", data)
        config_path = _make_config(tmp_path, [pq], start_date="2099-01-01", end_date="2099-12-31")

        cache = DataCache(config_path=str(config_path))
        cache.load()

        with pytest.raises(KeyError):
            cache.get_slice(pd.Timestamp("2099-06-01", tz="UTC"))


# =============================================================================
# EC-DC-007: 단일 데이터셋
# =============================================================================


class TestSingleDataset:
    """EC-DC-007: config에 데이터셋이 1개만 있을 때."""

    def test_single_dataset_loads_and_slices(self, tmp_path):
        """단일 데이터셋도 정상 로드 및 슬라이스."""
        dates = pd.date_range("2024-01-01", "2024-01-03", freq="1D", tz="UTC")
        data = []
        for ts in dates:
            for sym in ["BTCUSDT", "ETHUSDT"]:
                data.append({
                    "open_time": _ts_ms(str(ts.date())),
                    "symbol": sym,
                    "open": 100.0,
                    "high": 105.0,
                    "low": 95.0,
                    "close": 102.0,
                    "volume": 1000.0,
                })
        pq = _make_parquet(tmp_path / "single" / "data.parquet", data)
        config_path = _make_config(tmp_path, [pq])

        cache = DataCache(config_path=str(config_path))
        cache.load()

        assert cache.is_loaded
        assert len(cache.get_timestamps()) == 3

        ts = pd.Timestamp("2024-01-02", tz="UTC")
        data_slice = cache.get_slice(ts)
        assert "close" in data_slice.columns
        assert set(data_slice.index) == {"BTCUSDT", "ETHUSDT"}


# =============================================================================
# EC-DC-008: 잘못된 컬럼명
# =============================================================================


class TestWrongColumnName:
    """EC-DC-008: parquet에 time_col이 없을 때."""

    def test_missing_time_col_raises_key_error(self, tmp_path):
        """parquet에 'timestamp' 컬럼이 있지만 time_col='open_time' -> KeyError."""
        data = [{
            "timestamp": _ts_ms("2024-01-01"),  # open_time이 아닌 timestamp
            "symbol": "BTCUSDT",
            "close": 100.0,
        }]
        pq = _make_parquet(tmp_path / "wrong_col" / "data.parquet", data)
        config_path = _make_config(tmp_path, [pq], end_date="2024-01-01", time_col="open_time")

        cache = DataCache(config_path=str(config_path))

        with pytest.raises((KeyError, pyarrow.lib.ArrowInvalid)):
            cache.load()


# =============================================================================
# EC-DC-009: 날짜 범위 부분 겹침
# =============================================================================


class TestPartialDateOverlap:
    """EC-DC-009: 날짜 범위가 일부만 겹치는 parquet들."""

    def test_partial_overlap_merge(self, tmp_path):
        """ohlcv: 1/1~1/5, funding: 1/3~1/7 -> 부분 겹침 처리."""
        # ohlcv: 2024-01-01 ~ 2024-01-05
        ohlcv_dates = pd.date_range("2024-01-01", "2024-01-05", freq="1D", tz="UTC")
        ohlcv_data = []
        for ts in ohlcv_dates:
            ohlcv_data.append({
                "open_time": _ts_ms(str(ts.date())),
                "symbol": "BTCUSDT",
                "open": 100.0,
                "high": 105.0,
                "low": 95.0,
                "close": 102.0,
                "volume": 1000.0,
            })
        pq_ohlcv = _make_parquet(tmp_path / "ohlcv" / "data.parquet", ohlcv_data)

        # funding: 2024-01-03 ~ 2024-01-07
        funding_dates = pd.date_range("2024-01-03", "2024-01-07", freq="1D", tz="UTC")
        funding_data = []
        for ts in funding_dates:
            funding_data.append({
                "open_time": _ts_ms(str(ts.date())),
                "symbol": "BTCUSDT",
                "funding_rate": 0.0001,
            })
        pq_fund = _make_parquet(tmp_path / "funding" / "data.parquet", funding_data)

        config_path = _make_config(tmp_path, [pq_ohlcv, pq_fund], end_date="2024-01-07")
        cache = DataCache(config_path=str(config_path))
        cache.load()

        timestamps = cache.get_timestamps()
        assert len(timestamps) == 7

        # Day 1-2: ohlcv 존재, funding NaN
        for day in ["2024-01-01", "2024-01-02"]:
            sl = cache.get_slice(pd.Timestamp(day, tz="UTC"))
            assert pd.notna(sl.loc["BTCUSDT", "close"])
            assert pd.isna(sl.loc["BTCUSDT", "funding_rate"])

        # Day 3-5: 둘 다 존재
        for day in ["2024-01-03", "2024-01-04", "2024-01-05"]:
            sl = cache.get_slice(pd.Timestamp(day, tz="UTC"))
            assert pd.notna(sl.loc["BTCUSDT", "close"])
            assert pd.notna(sl.loc["BTCUSDT", "funding_rate"])

        # Day 6-7: ohlcv NaN, funding 존재
        for day in ["2024-01-06", "2024-01-07"]:
            sl = cache.get_slice(pd.Timestamp(day, tz="UTC"))
            assert pd.isna(sl.loc["BTCUSDT", "close"])
            assert pd.notna(sl.loc["BTCUSDT", "funding_rate"])


# =============================================================================
# EC-DC-010: 존재하지 않는 타임스탬프
# =============================================================================


class TestNonexistentTimestamp:
    """EC-DC-010: get_slice에 존재하지 않는 타임스탬프 전달 시."""

    def test_nonexistent_timestamp_raises_key_error(self, tmp_path):
        """캐시에 없는 타임스탬프로 get_slice 호출 시 KeyError."""
        data = [{
            "open_time": _ts_ms("2024-01-01"),
            "symbol": "BTCUSDT",
            "close": 100.0,
        }]
        pq = _make_parquet(tmp_path / "data" / "test.parquet", data)
        config_path = _make_config(tmp_path, [pq], end_date="2024-01-01")

        cache = DataCache(config_path=str(config_path))
        cache.load()

        with pytest.raises(KeyError):
            cache.get_slice(pd.Timestamp("2099-01-01", tz="UTC"))


# =============================================================================
# EC-DC-011/012: 파일 없을 때 FileNotFoundError
# =============================================================================


class TestDataCacheFileNotFound:
    """EC-DC-011/012: 파일 없을 때 FileNotFoundError"""

    def test_nonexistent_config_path(self, tmp_path):
        """존재하지 않는 config 경로에 대해 FileNotFoundError"""
        with pytest.raises(FileNotFoundError):
            DataCache(config_path=str(tmp_path / "nonexistent.yaml"))

    def test_nonexistent_dataset_path(self, tmp_path):
        """config에 등록된 parquet이 없을 때 FileNotFoundError"""
        config_path = tmp_path / "config.yaml"
        config = {
            "datasets": [{"path": str(tmp_path / "missing.parquet")}],
            "backtest": {"start_date": "2024-01-01", "end_date": "2024-01-03"},
        }
        with open(config_path, "w") as f:
            yaml.dump(config, f)

        cache = DataCache(config_path=str(config_path))
        with pytest.raises(FileNotFoundError):
            cache.load()
