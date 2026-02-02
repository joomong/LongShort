import logging
from pathlib import Path
from typing import List, Optional

import pandas as pd
import yaml

from .dataloader import DataLoader

logger = logging.getLogger(__name__)


class DataCache:
    """
    config.yaml에 등록된 여러 Parquet 파일을 메모리에 로드하여
    통합 캐시를 제공하는 클래스.

    모든 설정(datasets, columns, backtest, symbols)을 yaml에서 읽는다.
    """

    def __init__(self, config_path: str):
        config_file = Path(config_path)
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_file, "r") as f:
            config = yaml.safe_load(f)

        datasets = config.get("datasets", [])
        if not datasets:
            raise ValueError("datasets list is empty in config")

        self.dataset_paths = [d["path"] for d in datasets]

        # columns
        columns_cfg = config.get("columns", {})
        self.time_col: str = columns_cfg.get("time", "open_time")
        self.symbol_col: str = columns_cfg.get("symbol", "symbol")
        self.ohlcv_columns: List[str] = columns_cfg.get("ohlcv", ["open", "high", "low", "close", "volume"])

        # backtest
        backtest_cfg = config.get("backtest", {})
        self.start_date: str = backtest_cfg.get("start_date", "2020-01-01")
        self.end_date: str = backtest_cfg.get("end_date", "2025-08-31")
        self.initial_capital: int = backtest_cfg.get("initial_capital", 1000000)
        self.transaction_fee_rate: float = backtest_cfg.get("transaction_fee_rate", 0.0010)

        # symbols
        self.symbols: Optional[List[str]] = config.get("symbols", None)

        self._cache: Optional[pd.DataFrame] = None

    @property
    def is_loaded(self) -> bool:
        return self._cache is not None

    def load(self) -> None:
        """Load all datasets from config and merge into cache."""
        frames = []
        for path in self.dataset_paths:
            if not Path(path).exists():
                raise FileNotFoundError(f"Dataset file not found: {path}")

            loader = DataLoader(
                file_path=path,
                start_date=self.start_date,
                end_date=self.end_date,
                time_col=self.time_col,
                symbol_col=self.symbol_col,
                chunk_row_size=None,
                select_symbols=self.symbols,
            )

            slices = []
            for timestamp, data_slice in loader:
                # data_slice has instrument as index; re-add timestamp level
                df = data_slice.copy()
                df.index = pd.MultiIndex.from_product(
                    [[timestamp], data_slice.index],
                    names=["timestamp", "instrument"],
                )
                slices.append(df)

            if slices:
                frames.append(pd.concat(slices))

        if not frames:
            self._cache = pd.DataFrame()
            return

        # Merge multiple datasets by joining on (timestamp, instrument)
        merged = frames[0]
        for other in frames[1:]:
            merged = merged.join(other, how="outer")

        self._cache = merged.sort_index()
        logger.info(
            "DataCache loaded: %d timestamps, %d columns",
            len(self._cache.index.get_level_values(0).unique()),
            len(self._cache.columns),
        )

    def get_slice(self, timestamp: pd.Timestamp) -> pd.DataFrame:
        """Get merged data slice for a given timestamp. Returns DataFrame with instrument index."""
        if not self.is_loaded:
            raise RuntimeError("DataCache not loaded. Call load() first.")
        return self._cache.loc[timestamp]

    def get_timestamps(self) -> pd.DatetimeIndex:
        """Return sorted list of all unique timestamps."""
        if not self.is_loaded:
            raise RuntimeError("DataCache not loaded. Call load() first.")
        return self._cache.index.get_level_values(0).unique().sort_values()


class DataCacheIterator:
    """Iterate over DataCache slices filtered by date range.

    Compatible with the DataLoader iterator interface expected by Backtester.
    """

    def __init__(self, cache: DataCache, start_date: str | None = None, end_date: str | None = None):
        self.cache = cache
        self.start_date = start_date
        self.end_date = end_date

    def __iter__(self):
        for ts in self.cache.get_timestamps():
            if self.start_date and ts < pd.Timestamp(self.start_date, tz="UTC"):
                continue
            if self.end_date and ts > pd.Timestamp(self.end_date, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1):
                continue
            yield ts, self.cache.get_slice(ts)
