from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from framework.backtester import PeriodSettings, run_backtest
from framework.data import DataCache


RTOL_DEFAULT = 1e-12
ATOL_DEFAULT = 1e-12
RTOL_COMPOUND = 1e-9


def _write_dataset(parquet_path: Path) -> None:
    dates = pd.date_range("2024-01-01", periods=9, freq="D", tz="UTC")
    close_by_symbol = {
        "A": [100.0, 102.0, 101.0, 103.0, 104.0, 105.0, 106.0, 105.0, 107.0],
        "B": [50.0, 49.0, 51.0, 52.0, 53.0, 52.0, 54.0, 55.0, 56.0],
        "C": [30.0, 31.0, None, 33.0, 0.0, 34.0, 35.0, None, 36.0],
    }

    rows: list[dict] = []
    for i, ts in enumerate(dates):
        ts_ms = int(ts.value // 1_000_000)
        for sym in sorted(close_by_symbol.keys()):
            c = close_by_symbol[sym][i]
            if c is None:
                continue
            close = float(c)
            rows.append(
                {
                    "open_time": ts_ms,
                    "symbol": sym,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1000.0,
                }
            )
    pd.DataFrame(rows).to_parquet(parquet_path, index=False)


def _make_cache(parquet_path: Path, tmp_path: Path) -> DataCache:
    config = {
        "datasets": [
            {
                "name": "parallel_consistency",
                "path": str(parquet_path),
                "time_col": "open_time",
                "symbol_col": "symbol",
                "use_col": ["open", "high", "low", "close", "volume"],
            }
        ],
        "universe": None,
        "backtest": {
            "start_date": "2024-01-01",
            "end_date": "2024-01-09",
            "initial_capital": 1_000_000,
            "transaction_fee": 10,
        },
    }
    config_path = tmp_path / "config_parallel_consistency.yaml"
    yaml.dump(config, open(config_path, "w"))
    cache = DataCache(str(config_path))
    cache.load()
    return cache


class PathAlpha:
    def __init__(
        self,
        *,
        name: str,
        signal_map: dict[int, dict[str, float] | np.ndarray],
        default_signal: dict[str, float] | np.ndarray | None = None,
    ):
        self.name = name
        self.signal_map = signal_map
        self.default_signal = default_signal

    def _vector_from_obj(self, obj, symbols: np.ndarray) -> np.ndarray:
        if isinstance(obj, np.ndarray):
            return np.asarray(obj, dtype=np.float64)
        return np.array([float(obj.get(str(sym), 0.0)) for sym in symbols], dtype=np.float64)

    def compute(self, t_idx: int, arrays: dict) -> np.ndarray:
        symbols = arrays["__symbols__"]
        if t_idx in self.signal_map:
            return self._vector_from_obj(self.signal_map[t_idx], symbols)
        if self.default_signal is not None:
            return self._vector_from_obj(self.default_signal, symbols)
        return np.zeros(arrays["__n_symbols__"], dtype=np.float64)


def _assert_float_close(a: float, b: float, *, rtol: float, atol: float) -> None:
    if np.isnan(a) and np.isnan(b):
        return
    assert math.isclose(a, b, rel_tol=rtol, abs_tol=atol)


def _assert_metrics_equal(seq_metrics, par_metrics) -> None:
    _assert_float_close(seq_metrics.pnl, par_metrics.pnl, rtol=RTOL_COMPOUND, atol=ATOL_DEFAULT)
    _assert_float_close(seq_metrics.cagr, par_metrics.cagr, rtol=RTOL_COMPOUND, atol=ATOL_DEFAULT)
    _assert_float_close(seq_metrics.mdd, par_metrics.mdd, rtol=RTOL_COMPOUND, atol=ATOL_DEFAULT)
    _assert_float_close(seq_metrics.sharpe, par_metrics.sharpe, rtol=RTOL_DEFAULT, atol=ATOL_DEFAULT)
    _assert_float_close(seq_metrics.turnover, par_metrics.turnover, rtol=RTOL_DEFAULT, atol=ATOL_DEFAULT)
    _assert_float_close(seq_metrics.min_bet, par_metrics.min_bet, rtol=RTOL_DEFAULT, atol=ATOL_DEFAULT)
    _assert_float_close(seq_metrics.max_weight, par_metrics.max_weight, rtol=RTOL_DEFAULT, atol=ATOL_DEFAULT)
    _assert_float_close(seq_metrics.ir, par_metrics.ir, rtol=RTOL_DEFAULT, atol=ATOL_DEFAULT)
    _assert_float_close(seq_metrics.corr, par_metrics.corr, rtol=RTOL_DEFAULT, atol=ATOL_DEFAULT)
    assert int(seq_metrics.nonzero_position) == int(par_metrics.nonzero_position)


def _assert_period_equal(seq_period, par_period) -> None:
    _assert_metrics_equal(seq_period.metrics, par_period.metrics)
    _assert_float_close(seq_period.long_ratio, par_period.long_ratio, rtol=RTOL_DEFAULT, atol=ATOL_DEFAULT)
    _assert_float_close(seq_period.short_ratio, par_period.short_ratio, rtol=RTOL_DEFAULT, atol=ATOL_DEFAULT)
    np.testing.assert_allclose(
        np.asarray(seq_period.ret_series, dtype=np.float64),
        np.asarray(par_period.ret_series, dtype=np.float64),
        rtol=RTOL_DEFAULT,
        atol=ATOL_DEFAULT,
        equal_nan=True,
    )
    np.testing.assert_allclose(
        np.asarray(seq_period.nonzero_position_series, dtype=np.float64),
        np.asarray(par_period.nonzero_position_series, dtype=np.float64),
        rtol=RTOL_DEFAULT,
        atol=ATOL_DEFAULT,
        equal_nan=True,
    )


def test_parallel_consistency_metrics_and_returns_match(tmp_path: Path) -> None:
    parquet_path = tmp_path / "parallel_consistency.parquet"
    _write_dataset(parquet_path)
    cache = _make_cache(parquet_path, tmp_path)

    settings = PeriodSettings(
        is_start="2024-01-01",
        is_end="2024-01-05",
        os_start="2024-01-06",
        os_end="2024-01-09",
    )

    alphas = [
        PathAlpha(
            name="alpha_long_short",
            signal_map={},
            default_signal={"A": 1.0, "B": -1.0, "C": 0.5},
        ),
        PathAlpha(
            name="alpha_alternating",
            signal_map={
                0: {"A": 1.0, "B": 0.0, "C": -0.5},
                1: {"A": 0.0, "B": 1.0, "C": np.nan},
                2: {"A": -1.0, "B": 1.0, "C": 0.0},
                3: {"A": 1.0, "B": -1.0, "C": 1.0},
                4: {"A": 0.0, "B": -1.0, "C": 1.0},
                5: {"A": 1.0, "B": 0.0, "C": 1.0},
                6: {"A": -1.0, "B": 1.0, "C": -1.0},
                7: {"A": 0.0, "B": 1.0, "C": 1.0},
                8: {"A": 1.0, "B": -1.0, "C": 0.0},
            },
        ),
        PathAlpha(
            name="alpha_focus_c",
            signal_map={},
            default_signal={"A": 0.0, "B": 0.0, "C": 1.0},
        ),
    ]

    seq_result = run_backtest(
        alphas=alphas,
        settings=settings,
        capital=1_000_000,
        data_cache=cache,
        mode="D0",
        n_jobs=1,
    )
    par_result = run_backtest(
        alphas=alphas,
        settings=settings,
        capital=1_000_000,
        data_cache=cache,
        mode="D0",
        n_jobs=2,
    )

    assert seq_result.sorted_by == "sharpe"
    assert par_result.sorted_by == "sharpe"

    _assert_period_equal(seq_result.combined_result.is_period, par_result.combined_result.is_period)
    _assert_period_equal(seq_result.combined_result.os_period, par_result.combined_result.os_period)

    seq_map = {ar.name: ar for ar in seq_result.alpha_results}
    par_map = {ar.name: ar for ar in par_result.alpha_results}
    assert set(seq_map.keys()) == set(par_map.keys())

    for name in sorted(seq_map.keys()):
        _assert_period_equal(seq_map[name].is_period, par_map[name].is_period)
        _assert_period_equal(seq_map[name].os_period, par_map[name].os_period)

