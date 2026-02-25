"""
DataCache + Backtester 통합 테스트.

Backtester와 DataCache 통합 테스트.

IT-DC-001: DataCache 이터레이터 래퍼로 Backtester 실행
IT-DC-002: DataCache 기반 백테스트 검증
IT-DC-003: 다중 소스 데이터로 Alpha 계산 (Price("close") + Price("funding_rate"))
IT-DC-004: DataCache 빈 데이터로 Backtester 실행 시 빈 결과
IT-DC-005: DataCache로 IS/OS 기간 분리 (수동 슬라이싱)
"""
import pytest
import pandas as pd
import numpy as np
import yaml

from framework.data import DataCache
from framework.backtester import Backtester
from framework.modules.sources import Price


def _extract_series(run_output):
    """Backtester.run() output -> return series with datetime index."""
    if len(run_output) in (8, 9):
        timestamps, ret_s, *_ = run_output
    elif len(run_output) == 7:
        ret_s, *_ = run_output
        if isinstance(ret_s, pd.Series):
            return ret_s
        raise ValueError("Legacy 7-tuple output requires ret_series as pd.Series")
    else:
        raise ValueError(f"Unexpected Backtester.run() output length: {len(run_output)}")

    if isinstance(ret_s, pd.Series):
        return ret_s

    ret_arr = np.asarray(ret_s, dtype=float)
    ts_arr = np.asarray(timestamps)
    if ts_arr.size == ret_arr.size + 1:
        ts_arr = ts_arr[1:]
    if ts_arr.size != ret_arr.size:
        raise ValueError(
            f"Timestamp/return length mismatch: timestamps={ts_arr.size}, returns={ret_arr.size}"
        )
    idx = pd.DatetimeIndex(ts_arr, tz="UTC")
    return pd.Series(ret_arr, index=idx, dtype=float)


# =============================================================================
# Helper: 데이터 생성 유틸
# =============================================================================


def _ts_ms(date_str):
    """날짜 문자열 -> UTC ms epoch."""
    return int(pd.Timestamp(date_str, tz="UTC").value // 1_000_000)


def _make_parquet(path, data):
    """딕셔너리 리스트 -> parquet 파일 생성."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(data).to_parquet(path, index=False)
    return path


def _make_config(tmp_path, paths, filename="config.yaml", start_date="2024-01-01", end_date="2024-01-03", use_cols=None):
    """데이터셋 경로 리스트 -> config.yaml 생성."""
    if use_cols is None:
        use_cols = [["close"]] * len(paths)
    config_path = tmp_path / filename
    config = {
        "datasets": [
            {
                "name": f"dataset_{i}",
                "path": str(p),
                "time_col": "open_time",
                "symbol_col": "symbol",
                "use_col": use_cols[i],
            }
            for i, p in enumerate(paths)
        ],
        "universe": None,
    }
    config["backtest"] = {"start_date": start_date, "end_date": end_date,
                          "initial_capital": 1000000, "transaction_fee": 10}
    with open(config_path, "w") as f:
        yaml.dump(config, f)
    return config_path


def _build_ohlcv_data(dates, symbols, base_close=100.0):
    """OHLCV 데이터 생성. close 값은 날짜/심볼에 따라 약간 변동."""
    data = []
    for day_idx, ts in enumerate(dates):
        for sym_idx, sym in enumerate(symbols):
            base = base_close * (sym_idx + 1)
            close = base + day_idx * 1.0  # 날짜별로 약간 증가
            data.append({
                "open_time": _ts_ms(str(ts.date())),
                "symbol": sym,
                "open": close - 1.0,
                "high": close + 5.0,
                "low": close - 3.0,
                "close": close,
                "volume": 1000.0 * (sym_idx + 1),
            })
    return data


def _build_funding_data(dates, symbols, base_rate=0.0001):
    """Funding rate 데이터 생성."""
    data = []
    for day_idx, ts in enumerate(dates):
        for sym_idx, sym in enumerate(symbols):
            data.append({
                "open_time": _ts_ms(str(ts.date())),
                "symbol": sym,
                "funding_rate": base_rate * (sym_idx + 1),
            })
    return data


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def ohlcv_3day_2sym(tmp_path):
    """3일, 2 심볼 OHLCV parquet."""
    dates = pd.date_range("2024-01-01", "2024-01-03", freq="1D", tz="UTC")
    symbols = ["BTCUSDT", "ETHUSDT"]
    data = _build_ohlcv_data(dates, symbols)
    return _make_parquet(tmp_path / "ohlcv" / "data.parquet", data)


@pytest.fixture
def funding_3day_2sym(tmp_path):
    """3일, 2 심볼 funding parquet."""
    dates = pd.date_range("2024-01-01", "2024-01-03", freq="1D", tz="UTC")
    symbols = ["BTCUSDT", "ETHUSDT"]
    data = _build_funding_data(dates, symbols)
    return _make_parquet(tmp_path / "funding" / "data.parquet", data)


@pytest.fixture
def ohlcv_10day_3sym(tmp_path):
    """10일, 3 심볼 OHLCV parquet."""
    dates = pd.date_range("2024-01-01", "2024-01-10", freq="1D", tz="UTC")
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    data = _build_ohlcv_data(dates, symbols)
    return _make_parquet(tmp_path / "ext_ohlcv" / "data.parquet", data)


@pytest.fixture
def config_ohlcv_only(tmp_path, ohlcv_3day_2sym):
    """OHLCV만 있는 config."""
    return _make_config(tmp_path, [ohlcv_3day_2sym], "config_ohlcv.yaml")


@pytest.fixture
def config_ohlcv_funding(tmp_path, ohlcv_3day_2sym, funding_3day_2sym):
    """OHLCV + funding config."""
    return _make_config(tmp_path, [ohlcv_3day_2sym, funding_3day_2sym], "config_both.yaml",
                        use_cols=[["close"], ["funding_rate"]])


@pytest.fixture
def config_10day(tmp_path, ohlcv_10day_3sym):
    """10일 OHLCV config."""
    return _make_config(tmp_path, [ohlcv_10day_3sym], "config_10day.yaml", end_date="2024-01-10")


# =============================================================================
# IT-DC-001: 기본 Backtester 실행
# =============================================================================


class TestDataCacheBasicBacktest:
    """IT-DC-001: DataCache 이터레이터 래퍼로 Backtester 실행."""

    def test_backtester_runs_with_cache(self, config_ohlcv_only):
        """DataCache를 통해 Backtester가 정상 실행된다 (numpy path)."""
        cache = DataCache(config_path=str(config_ohlcv_only))
        cache.load()

        alpha = Price("close")

        bt = Backtester(
            cache=cache,
            alpha_recipe=alpha,
            capital=10000.0,
            collect_viz=False,
        )
        ret_s = _extract_series(bt.run())

        assert isinstance(ret_s, pd.Series)
        # 3일 데이터지만 첫 바에는 수익이 없으므로 ret_series는 2개 이상
        assert len(ret_s) >= 2

    def test_backtester_result_has_valid_values(self, config_ohlcv_only):
        """결과의 ret_series 값이 유한한 숫자다."""
        cache = DataCache(config_path=str(config_ohlcv_only))
        cache.load()

        alpha = Price("close")

        bt = Backtester(
            cache=cache,
            alpha_recipe=alpha,
            capital=10000.0,
            collect_viz=False,
        )
        ret_s = _extract_series(bt.run())

        assert all(np.isfinite(ret_s.values))


# =============================================================================
# IT-DC-003: 다중 소스 Alpha
# =============================================================================


class TestMultiSourceAlpha:
    """IT-DC-003: 다중 소스 데이터로 Alpha 계산."""

    def test_multi_source_alpha_runs(self, config_ohlcv_funding):
        """Price('close') - Price('funding_rate') * 1000 알파가 정상 작동한다."""
        cache = DataCache(config_path=str(config_ohlcv_funding))
        cache.load()

        alpha = Price("close") - Price("funding_rate") * 1000

        bt = Backtester(
            cache=cache,
            alpha_recipe=alpha,
            capital=10000.0,
            collect_viz=False,
        )
        ret_s = _extract_series(bt.run())

        assert isinstance(ret_s, pd.Series)
        assert len(ret_s) >= 2

    def test_multi_source_alpha_uses_funding_rate(self, config_ohlcv_funding):
        """다중 소스 알파의 시그널이 단일 소스와 다르다."""
        cache = DataCache(config_path=str(config_ohlcv_funding))
        cache.load()

        # 다중 소스 알파
        multi_alpha = Price("close") - Price("funding_rate") * 1000

        # 첫 타임스탬프에서 numpy 시그널 비교
        t_idx = 0
        arrays = cache.arrays
        arrays["__n_symbols__"] = len(cache.symbols)

        multi_signal = multi_alpha.compute(t_idx, arrays)
        close_signal = Price("close").compute(t_idx, arrays)

        # funding_rate가 0이 아니므로 두 시그널은 달라야 함
        assert not np.allclose(multi_signal, close_signal)


# =============================================================================
# IT-DC-004: 빈 데이터
# =============================================================================


class TestEmptyDataBacktest:
    """IT-DC-004: DataCache 빈 데이터로 Backtester 실행."""

    def test_empty_cache_raises_on_load(self, tmp_path):
        """빈 DataCache (범위 밖 데이터)로 load() 시 ValueError."""
        data = [{
            "open_time": _ts_ms("2024-01-01"),
            "symbol": "BTCUSDT",
            "close": 100.0,
        }]
        pq = _make_parquet(tmp_path / "data" / "test.parquet", data)
        config_path = _make_config(tmp_path, [pq], start_date="2099-01-01", end_date="2099-12-31")

        cache = DataCache(config_path=str(config_path))
        with pytest.raises(ValueError, match="no data"):
            cache.load()


# =============================================================================
# IT-DC-005: 수동 IS/OS 분리
# =============================================================================


class TestManualISOSSplit:
    """IT-DC-005: DataCache로 IS/OS 기간 수동 분리."""

    def test_is_os_split_produces_valid_results(self, config_10day):
        """10일 데이터를 5일 IS + 5일 OS로 분리하여 각각 유효한 결과를 얻는다."""
        cache = DataCache(config_path=str(config_10day))
        cache.load()

        all_ts = pd.DatetimeIndex(cache.timestamps)
        assert len(all_ts) == 10

        alpha = Price("close")

        # IS: day 1-5
        bt_is = Backtester(
            cache=cache,
            alpha_recipe=alpha,
            capital=10000.0,
            collect_viz=False,
            start_date="2024-01-01",
            end_date="2024-01-05",
        )
        ret_is = _extract_series(bt_is.run())

        # OS: day 6-10
        bt_os = Backtester(
            cache=cache,
            alpha_recipe=alpha,
            capital=10000.0,
            collect_viz=False,
            start_date="2024-01-06",
            end_date="2024-01-10",
        )
        ret_os = _extract_series(bt_os.run())

        # 두 결과 모두 유효
        assert isinstance(ret_is, pd.Series)
        assert isinstance(ret_os, pd.Series)
        assert len(ret_is) >= 1
        assert len(ret_os) >= 1

    def test_is_os_timestamps_dont_overlap(self, config_10day):
        """IS와 OS 기간의 타임스탬프가 겹치지 않는다."""
        cache = DataCache(config_path=str(config_10day))
        cache.load()

        alpha = Price("close")

        bt_is = Backtester(
            cache=cache,
            alpha_recipe=alpha,
            capital=10000.0,
            collect_viz=False,
            start_date="2024-01-01",
            end_date="2024-01-05",
        )
        ret_is = _extract_series(bt_is.run())

        bt_os = Backtester(
            cache=cache,
            alpha_recipe=alpha,
            capital=10000.0,
            collect_viz=False,
            start_date="2024-01-06",
            end_date="2024-01-10",
        )
        ret_os = _extract_series(bt_os.run())

        # 타임스탬프 겹침 없음
        is_index = set(ret_is.index)
        os_index = set(ret_os.index)
        assert len(is_index & os_index) == 0

    def test_is_os_combined_equals_full(self, config_10day):
        """IS + OS를 합치면 전체 기간과 같은 수의 리턴 포인트."""
        cache = DataCache(config_path=str(config_10day))
        cache.load()

        alpha = Price("close")

        # 전체 실행
        bt_full = Backtester(
            cache=cache,
            alpha_recipe=alpha,
            capital=10000.0,
            collect_viz=False,
        )
        ret_full = _extract_series(bt_full.run())

        # IS + OS 분리 실행
        bt_is = Backtester(
            cache=cache,
            alpha_recipe=alpha,
            capital=10000.0,
            collect_viz=False,
            start_date="2024-01-01",
            end_date="2024-01-05",
        )
        ret_is = _extract_series(bt_is.run())

        bt_os = Backtester(
            cache=cache,
            alpha_recipe=alpha,
            capital=10000.0,
            collect_viz=False,
            start_date="2024-01-06",
            end_date="2024-01-10",
        )
        ret_os = _extract_series(bt_os.run())

        # IS ret_series 길이 + OS ret_series 길이 = 전체 길이
        # 참고: IS 첫 바와 OS 첫 바 모두 previous_close가 없어서 수익이 없으므로
        # 분리 실행 시 1개 포인트가 줄어든다. 이는 예상 동작.
        total_split = len(ret_is) + len(ret_os)
        total_full = len(ret_full)
        # split에서는 OS 첫 바에 대해 이전 close가 없으므로 1개 적을 수 있음
        assert total_split == total_full - 1 or total_split == total_full
