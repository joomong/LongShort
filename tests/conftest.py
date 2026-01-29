# tests/conftest.py
"""
DataLoader 테스트를 위한 pytest fixtures.
"""
import pytest
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pathlib import Path

# 테스트 fixture 디렉토리
FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def sample_parquet_path(tmp_path_factory) -> Path:
    """
    테스트용 소형 parquet 파일 생성.

    데이터 구조:
    - 3일간 데이터 (2024-01-01 ~ 2024-01-03)
    - 3개 심볼 (BTCUSDT, ETHUSDT, XRPUSDT)
    - 컬럼: open_time(ms), symbol, open, high, low, close, volume
    """
    tmp_dir = tmp_path_factory.mktemp("data")
    file_path = tmp_dir / "test_data.parquet"

    # 테스트 데이터 생성
    data = []
    symbols = ["BTCUSDT", "ETHUSDT", "XRPUSDT"]
    dates = pd.date_range("2024-01-01", "2024-01-03", freq="1D", tz="UTC")

    for ts in dates:
        ts_ms = int(ts.value // 1_000_000)  # ms epoch
        for i, sym in enumerate(symbols):
            base_price = 100 * (i + 1)  # BTCUSDT=100, ETHUSDT=200, XRPUSDT=300
            data.append({
                "open_time": ts_ms,
                "symbol": sym,
                "open": base_price + 1.0,
                "high": base_price + 5.0,
                "low": base_price - 2.0,
                "close": base_price + 3.0,
                "volume": 1000.0 * (i + 1),
            })

    df = pd.DataFrame(data)
    df.to_parquet(file_path, index=False)

    return file_path


@pytest.fixture(scope="session")
def extended_parquet_path(tmp_path_factory) -> Path:
    """
    더 많은 날짜와 심볼을 포함한 테스트용 parquet 파일.

    데이터 구조:
    - 10일간 데이터 (2024-01-01 ~ 2024-01-10)
    - 5개 심볼
    """
    tmp_dir = tmp_path_factory.mktemp("data_extended")
    file_path = tmp_dir / "test_data_extended.parquet"

    data = []
    symbols = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "BNBUSDT"]
    dates = pd.date_range("2024-01-01", "2024-01-10", freq="1D", tz="UTC")

    for ts in dates:
        ts_ms = int(ts.value // 1_000_000)
        for i, sym in enumerate(symbols):
            base_price = 100 * (i + 1)
            # 날짜에 따라 가격 변동 추가
            day_offset = (ts - dates[0]).days
            data.append({
                "open_time": ts_ms,
                "symbol": sym,
                "open": base_price + day_offset * 0.5,
                "high": base_price + day_offset * 0.5 + 5.0,
                "low": base_price + day_offset * 0.5 - 2.0,
                "close": base_price + day_offset * 0.5 + 3.0,
                "volume": 1000.0 * (i + 1) + day_offset * 100,
            })

    df = pd.DataFrame(data)
    df.to_parquet(file_path, index=False)

    return file_path


@pytest.fixture(scope="session")
def single_timestamp_parquet_path(tmp_path_factory) -> Path:
    """
    단일 타임스탬프만 가진 테스트용 parquet 파일.
    """
    tmp_dir = tmp_path_factory.mktemp("data_single")
    file_path = tmp_dir / "test_data_single.parquet"

    ts = pd.Timestamp("2024-01-01", tz="UTC")
    ts_ms = int(ts.value // 1_000_000)

    data = [
        {"open_time": ts_ms, "symbol": "BTCUSDT", "open": 100, "high": 105, "low": 98, "close": 103, "volume": 1000},
        {"open_time": ts_ms, "symbol": "ETHUSDT", "open": 200, "high": 210, "low": 195, "close": 205, "volume": 2000},
    ]

    df = pd.DataFrame(data)
    df.to_parquet(file_path, index=False)

    return file_path


@pytest.fixture(scope="session")
def real_parquet_path() -> Path:
    """
    실제 데이터 파일 경로 (존재하는 경우에만 사용).
    """
    path = Path(__file__).parent.parent / "Data" / "um_1d_all.parquet"
    if not path.exists():
        pytest.skip(f"Real data file not found: {path}")
    return path


@pytest.fixture
def dataloader_params(sample_parquet_path):
    """
    DataLoader 기본 파라미터.
    """
    return {
        "file_path": str(sample_parquet_path),
        "start_date": "2024-01-01",
        "end_date": "2024-01-03",
        "time_col": "open_time",
        "symbol_col": "symbol",
    }
