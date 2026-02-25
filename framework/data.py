# framework/data.py
"""
Data loading and caching layer for the LongShort framework.

This module provides:
- DatasetConfig: Configuration dataclass for individual datasets
- DataCache: In-memory cache layer that loads Parquet data via PyArrow,
             stores data as numpy arrays for fast vectorised access.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import yaml

logger = logging.getLogger(__name__)

__all__ = ['DatasetConfig', 'DataCache']


# =============================================================================
# DatasetConfig - Dataset configuration
# =============================================================================

@dataclass
class DatasetConfig:
    name: str
    path: str
    time_col: str
    symbol_col: str
    use_col: List[str] = field(default_factory=list)


# =============================================================================
# DataCache - In-memory caching layer (numpy arrays)
# =============================================================================

class DataCache:
    """
    config.yaml에 등록된 여러 Parquet 파일을 메모리에 로드하여
    numpy 배열 기반 캐시를 제공하는 클래스.

    After load():
      - self.timestamps: ndarray (T,) dtype=datetime64[ns, UTC]
      - self.symbols: ndarray (N,) dtype=object (str)
      - self.sym_index: {str: int} dict
      - self._ts_index: {pd.Timestamp: int} dict
      - self.arrays: {col_name: ndarray(T, N) float64} dict
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

        # Parse dataset configs with strict validation
        self.dataset_configs: List[DatasetConfig] = []
        for ds_raw in datasets:
            name = ds_raw.get("name")
            path = ds_raw.get("path")
            time_col = ds_raw.get("time_col")
            symbol_col = ds_raw.get("symbol_col")
            use_col = ds_raw.get("use_col", [])

            if not name or not path or not time_col or not symbol_col or not use_col:
                missing = [k for k, v in {"name": name, "path": path, "time_col": time_col, "symbol_col": symbol_col, "use_col": use_col}.items() if not v]
                raise ValueError(f"Dataset config missing required fields: {missing}")

            self.dataset_configs.append(
                DatasetConfig(
                    name=name,
                    path=path,
                    time_col=time_col,
                    symbol_col=symbol_col,
                    use_col=use_col,
                )
            )

        # Parse universe (global symbol filter)
        universe_val = config.get("universe")
        if universe_val is None or universe_val == ["all"] or universe_val == "all":
            self._universe: Optional[List[str]] = None
        else:
            self._universe: Optional[List[str]] = universe_val

        # backtest - strict validation
        backtest_cfg = config.get("backtest")
        if not backtest_cfg:
            raise ValueError("Config missing 'backtest' section")

        start_date = backtest_cfg.get("start_date")
        end_date = backtest_cfg.get("end_date")
        initial_capital = backtest_cfg.get("initial_capital")
        fee_bps = backtest_cfg.get("transaction_fee")

        if (
            start_date in (None, "")
            or end_date in (None, "")
            or initial_capital is None
            or fee_bps is None
        ):
            missing = []
            if start_date in (None, ""):
                missing.append("start_date")
            if end_date in (None, ""):
                missing.append("end_date")
            if initial_capital is None:
                missing.append("initial_capital")
            if fee_bps is None:
                missing.append("transaction_fee")
            raise ValueError(f"Backtest config missing required fields: {missing}")

        self.start_date: str = start_date
        self.end_date: str = end_date
        self.initial_capital: int = initial_capital
        self.transaction_fee_rate: float = fee_bps * 0.0001

        # Numpy array storage (populated by load())
        self.timestamps: Optional[np.ndarray] = None   # (T,) datetime64[ns, UTC]
        self.symbols: Optional[np.ndarray] = None       # (N,) object
        self.sym_index: Optional[Dict[str, int]] = None
        self._ts_index: Optional[Dict] = None           # {pd.Timestamp: int}
        self.arrays: Optional[Dict[str, np.ndarray]] = None  # {col: (T,N) float64}


    @property
    def _loaded(self) -> bool:
        return self.arrays is not None

    def load(self) -> None:
        """Load all datasets from config using PyArrow directly and merge into cache."""
        frames = []
        for ds_config in self.dataset_configs:
            if not Path(ds_config.path).exists():
                raise FileNotFoundError(f"Dataset file not found: {ds_config.path}")

            # PyArrow filter
            _start = pd.Timestamp(self.start_date, tz="UTC")
            _end = pd.Timestamp(self.end_date, tz="UTC")
            start_ms = _start.value // 1_000_000
            end_ms = (_end + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)).value // 1_000_000

            time_filter = (ds.field(ds_config.time_col) >= start_ms) & (
                ds.field(ds_config.time_col) <= end_ms)

            if self._universe:
                filter_expr = time_filter & ds.field(ds_config.symbol_col).isin(self._universe)
            else:
                filter_expr = time_filter

            # Column selection
            columns = None
            if ds_config.use_col:
                columns = list({ds_config.time_col, ds_config.symbol_col} | set(ds_config.use_col))

            # Load via PyArrow
            dataset = ds.dataset(Path(ds_config.path), format="parquet")

            # Validate use_col against parquet schema before scanning
            if ds_config.use_col:
                schema_names = set(dataset.schema.names)
                missing = [c for c in ds_config.use_col if c not in schema_names]
                if missing:
                    raise ValueError(
                        f"Dataset '{ds_config.name}' missing columns: {missing}. "
                        f"Available: {sorted(schema_names)}"
                    )

            table = dataset.scanner(filter=filter_expr, columns=columns).to_table()
            df = table.to_pandas()

            if df.empty:
                raise ValueError(
                    f"Dataset '{ds_config.name}' has no data for range "
                    f"{self.start_date} ~ {self.end_date}"
                )

            # Standardize column names
            df = df.rename(columns={ds_config.time_col: "timestamp", ds_config.symbol_col: "instrument"})
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            df = df.sort_values(["timestamp", "instrument"]).set_index(["timestamp", "instrument"])

            frames.append(df)

        # Merge multiple datasets by joining on (timestamp, instrument)
        merged = frames[0]
        for other in frames[1:]:
            merged = merged.join(other, how="outer")

        # Validate that all use_col columns exist
        all_use_cols = {col for d in self.dataset_configs for col in d.use_col}
        missing_cols = [col for col in all_use_cols if col not in merged.columns]
        if missing_cols:
            raise ValueError(
                f"Missing required columns in loaded data: {missing_cols}. "
                f"Available columns: {list(merged.columns)}"
            )

        merged = merged.sort_index()

        # --- Convert to numpy arrays ---
        ts_unique = merged.index.get_level_values(0).unique().sort_values()
        sym_unique = sorted(merged.index.get_level_values(1).unique().tolist())

        self.timestamps = ts_unique.values  # numpy datetime64
        self.symbols = np.array(sym_unique, dtype=object)
        self.sym_index = {s: i for i, s in enumerate(sym_unique)}
        self._ts_index = {ts: i for i, ts in enumerate(ts_unique)}

        T = len(ts_unique)
        N = len(sym_unique)
        cols = sorted(merged.columns.tolist())

        self.arrays = {}
        for col in cols:
            series = merged[col]
            if series.index.has_duplicates:
                # Preserve legacy behavior by taking the last duplicate.
                series = series.groupby(level=[0, 1]).last()

            arr_df = series.unstack(level=1).reindex(index=ts_unique, columns=sym_unique)
            self.arrays[col] = arr_df.to_numpy(dtype=np.float64)

        logger.info(
            "DataCache loaded: %d timestamps, %d symbols, %d columns (numpy)",
            T, N, len(cols),
        )

    def get_column_array(self, col: str) -> np.ndarray:
        """Get (T, N) array for a column. Raises KeyError if not found."""
        if not self._loaded:
            raise RuntimeError("DataCache not loaded. Call load() first.")
        return self.arrays[col]

    def get_timestamp_index(self, ts) -> int:
        """Get integer index for a timestamp. Raises KeyError if not found."""
        if not self._loaded:
            raise RuntimeError("DataCache not loaded. Call load() first.")
        return self._ts_index[ts]

    def count_timestamps_in_range(self, start_date: str, end_date: str) -> int:
        """Count timestamps within a date range. Used by parallel module for probe."""
        if not self._loaded:
            raise RuntimeError("DataCache not loaded. Call load() first.")
        _start = pd.Timestamp(start_date, tz="UTC")
        _end = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
        ts = pd.DatetimeIndex(self.timestamps)
        if ts.tz is None and len(ts) > 0:
            ts = ts.tz_localize("UTC")
        return int(((ts >= _start) & (ts <= _end)).sum())
