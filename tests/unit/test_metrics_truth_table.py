from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from framework.backtester import Backtester, _compute_metrics
from framework.data import DataCache
from tests.unit._metrics_oracle import (
    OracleMetrics,
    compute_metrics_from_series,
    simulate_backtest_series,
)


RTOL = 1e-12
ATOL = 1e-12


@dataclass(frozen=True)
class SeriesCase:
    ret: np.ndarray
    turnover: np.ndarray
    min_bet: np.ndarray
    max_weight: np.ndarray
    nonzero_count: np.ndarray
    expect_pnl_sign: int
    expect_mdd_nonpos: bool


def _assert_metrics_close(actual, expected: OracleMetrics, *, rtol: float = RTOL, atol: float = ATOL) -> None:
    assert math.isclose(actual.pnl, expected.pnl, rel_tol=rtol, abs_tol=atol)
    assert math.isclose(actual.cagr, expected.cagr, rel_tol=rtol, abs_tol=atol)
    assert math.isclose(actual.sharpe, expected.sharpe, rel_tol=rtol, abs_tol=atol)
    assert math.isclose(actual.mdd, expected.mdd, rel_tol=rtol, abs_tol=atol)
    assert math.isclose(actual.turnover, expected.turnover, rel_tol=rtol, abs_tol=atol)
    assert math.isclose(actual.min_bet, expected.min_bet, rel_tol=rtol, abs_tol=atol)
    assert math.isclose(actual.max_weight, expected.max_weight, rel_tol=rtol, abs_tol=atol)
    assert int(actual.nonzero_position) == int(expected.nonzero_position)


def _write_ohlcv_parquet(
    file_path: Path,
    dates: list[pd.Timestamp],
    close_by_symbol: dict[str, list[float | None]],
) -> None:
    rows: list[dict] = []
    symbols = sorted(close_by_symbol.keys())
    for t_idx, ts in enumerate(dates):
        ts_ms = int(ts.value // 1_000_000)
        for sym in symbols:
            c_raw = close_by_symbol[sym][t_idx]
            if c_raw is None or (isinstance(c_raw, float) and np.isnan(c_raw)):
                # Missing row means delisting/unlisted state.
                continue
            c = float(c_raw)
            rows.append(
                {
                    "open_time": ts_ms,
                    "symbol": sym,
                    "open": c,
                    "high": c,
                    "low": c,
                    "close": c,
                    "volume": 1000.0,
                }
            )
    pd.DataFrame(rows).to_parquet(file_path, index=False)


def _make_cache(
    parquet_path: Path,
    start_date: str,
    end_date: str,
    tmp_path: Path,
    *,
    fee_bps: int,
) -> DataCache:
    config = {
        "datasets": [
            {
                "name": "test",
                "path": str(parquet_path),
                "time_col": "open_time",
                "symbol_col": "symbol",
                "use_col": ["open", "high", "low", "close", "volume"],
            }
        ],
        "universe": None,
        "backtest": {
            "start_date": start_date,
            "end_date": end_date,
            "initial_capital": 1_000_000,
            "transaction_fee": fee_bps,
        },
    }
    config_path = tmp_path / f"config_{parquet_path.stem}.yaml"
    yaml.dump(config, open(config_path, "w"))
    cache = DataCache(str(config_path))
    cache.load()
    return cache


class SignalPathAlpha:
    def __init__(
        self,
        *,
        signal_map: dict[int, dict[str, float] | np.ndarray],
        default_signal: dict[str, float] | np.ndarray | None = None,
    ):
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


def _build_signals_2d(
    symbols: np.ndarray,
    n_bars: int,
    *,
    signal_map: dict[int, dict[str, float] | np.ndarray],
    default_signal: dict[str, float] | np.ndarray | None,
) -> np.ndarray:
    alpha = SignalPathAlpha(signal_map=signal_map, default_signal=default_signal)
    arr = {"__symbols__": symbols, "__n_symbols__": int(symbols.size)}
    out = np.zeros((n_bars, symbols.size), dtype=np.float64)
    for t_idx in range(n_bars):
        out[t_idx] = alpha.compute(t_idx, arr)
    return out


SERIES_CASES: dict[str, SeriesCase] = {
    "A_monotonic_up": SeriesCase(
        ret=np.array([0.01, 0.02, 0.015, 0.03], dtype=np.float64),
        turnover=np.array([0.10, 0.00, 0.20, 0.10], dtype=np.float64),
        min_bet=np.array([0.4, 0.4, 0.4, 0.4], dtype=np.float64),
        max_weight=np.array([0.6, 0.6, 0.6, 0.6], dtype=np.float64),
        nonzero_count=np.array([2, 2, 2, 2], dtype=np.float64),
        expect_pnl_sign=1,
        expect_mdd_nonpos=True,
    ),
    "B_monotonic_down": SeriesCase(
        ret=np.array([-0.01, -0.02, -0.005, -0.03], dtype=np.float64),
        turnover=np.array([0.10, 0.10, 0.10, 0.10], dtype=np.float64),
        min_bet=np.array([0.3, 0.3, 0.3, 0.3], dtype=np.float64),
        max_weight=np.array([0.5, 0.5, 0.5, 0.5], dtype=np.float64),
        nonzero_count=np.array([2, 2, 2, 2], dtype=np.float64),
        expect_pnl_sign=-1,
        expect_mdd_nonpos=True,
    ),
    "C_volatile_zero_terminal_pnl": SeriesCase(
        ret=np.array([0.10, -0.09090909090909091, 0.0], dtype=np.float64),
        turnover=np.array([0.20, 0.20, 0.20], dtype=np.float64),
        min_bet=np.array([0.5, 0.5, 0.5], dtype=np.float64),
        max_weight=np.array([0.7, 0.7, 0.7], dtype=np.float64),
        nonzero_count=np.array([3, 3, 3], dtype=np.float64),
        expect_pnl_sign=0,
        expect_mdd_nonpos=True,
    ),
    "D_all_zero_returns": SeriesCase(
        ret=np.array([0.0, 0.0, 0.0], dtype=np.float64),
        turnover=np.array([0.0, 0.0, 0.0], dtype=np.float64),
        min_bet=np.array([0.0, 0.0, 0.0], dtype=np.float64),
        max_weight=np.array([0.0, 0.0, 0.0], dtype=np.float64),
        nonzero_count=np.array([0, 0, 0], dtype=np.float64),
        expect_pnl_sign=0,
        expect_mdd_nonpos=True,
    ),
    "E_singleton_return": SeriesCase(
        ret=np.array([0.02], dtype=np.float64),
        turnover=np.array([0.1], dtype=np.float64),
        min_bet=np.array([0.2], dtype=np.float64),
        max_weight=np.array([0.4], dtype=np.float64),
        nonzero_count=np.array([1], dtype=np.float64),
        expect_pnl_sign=1,
        expect_mdd_nonpos=True,
    ),
    "F_return_with_nan_middle": SeriesCase(
        ret=np.array([0.08, np.nan, -0.04, 0.01], dtype=np.float64),
        turnover=np.array([0.2, np.nan, 0.1, 0.05], dtype=np.float64),
        min_bet=np.array([0.3, np.nan, 0.4, 0.2], dtype=np.float64),
        max_weight=np.array([0.5, np.nan, 0.6, 0.4], dtype=np.float64),
        nonzero_count=np.array([2, 0, 2, 1], dtype=np.float64),
        expect_pnl_sign=1,
        expect_mdd_nonpos=True,
    ),
}


@pytest.mark.parametrize("case_name", list(SERIES_CASES.keys()))
def test_metrics_truth_table_raw_series(case_name: str) -> None:
    case = SERIES_CASES[case_name]

    expected = compute_metrics_from_series(
        case.ret,
        case.turnover,
        case.min_bet,
        case.max_weight,
        nonzero_count_series=case.nonzero_count,
    )
    actual = _compute_metrics(
        case.ret,
        case.turnover,
        case.min_bet,
        case.max_weight,
        nz_series=case.nonzero_count,
    )
    _assert_metrics_close(actual, expected)

    if case.expect_pnl_sign > 0:
        assert actual.pnl > 0
    elif case.expect_pnl_sign < 0:
        assert actual.pnl < 0
    else:
        assert abs(actual.pnl) < 1e-10

    if case.expect_mdd_nonpos:
        assert actual.mdd <= 0.0

    if case_name == "D_all_zero_returns":
        assert actual.sharpe == 0.0
        assert abs(actual.mdd) < 1e-8
    if case_name == "E_singleton_return":
        assert actual.sharpe == 0.0


def test_metrics_truth_table_case_g_delisting_nan_close(tmp_path: Path) -> None:
    dates = [pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2024-01-02", tz="UTC"), pd.Timestamp("2024-01-03", tz="UTC")]
    close_by_symbol = {
        "A": [100.0, None, None],
        "B": [50.0, 55.0, 60.0],
    }
    parquet_path = tmp_path / "case_g_delisting.parquet"
    _write_ohlcv_parquet(parquet_path, dates, close_by_symbol)
    cache = _make_cache(parquet_path, "2024-01-01", "2024-01-03", tmp_path, fee_bps=0)

    signal_map = {}
    default_signal = {"A": 1.0, "B": 1.0}
    alpha = SignalPathAlpha(signal_map=signal_map, default_signal=default_signal)
    bt = Backtester(
        cache=cache,
        alpha_recipe=alpha,
        capital=1_000_000,
        mode="D0",
        start_date="2024-01-01",
        end_date="2024-01-03",
    )
    _, ret_s, to_s, mb_s, mw_s, lw_s, sw_s, nz_s, _ = bt.run()

    signals_2d = _build_signals_2d(cache.symbols, len(cache.timestamps), signal_map=signal_map, default_signal=default_signal)
    oracle = simulate_backtest_series(
        close_2d=cache.arrays["close"],
        signals_2d=signals_2d,
        fee_rate=cache.transaction_fee_rate,
        mode="D0",
    )

    np.testing.assert_allclose(ret_s, oracle.ret_series, rtol=RTOL, atol=ATOL)
    np.testing.assert_allclose(to_s, oracle.turnover, rtol=RTOL, atol=ATOL)
    np.testing.assert_allclose(mb_s, oracle.min_bet, rtol=RTOL, atol=ATOL)
    np.testing.assert_allclose(mw_s, oracle.max_weight, rtol=RTOL, atol=ATOL)
    np.testing.assert_allclose(nz_s, oracle.nonzero_count, rtol=RTOL, atol=ATOL)
    # Delisted symbol contributes zero return from day2 onward.
    assert ret_s[0] == pytest.approx(0.05, abs=ATOL)


def test_metrics_truth_table_case_h_zero_close_treated_as_invalid_return(tmp_path: Path) -> None:
    dates = [pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2024-01-02", tz="UTC"), pd.Timestamp("2024-01-03", tz="UTC")]
    close_by_symbol = {
        "A": [100.0, 0.0, 0.0],
        "B": [50.0, 55.0, 60.0],
    }
    parquet_path = tmp_path / "case_h_zero_close.parquet"
    _write_ohlcv_parquet(parquet_path, dates, close_by_symbol)
    cache = _make_cache(parquet_path, "2024-01-01", "2024-01-03", tmp_path, fee_bps=0)

    signal_map = {}
    default_signal = {"A": 1.0, "B": 1.0}
    alpha = SignalPathAlpha(signal_map=signal_map, default_signal=default_signal)
    bt = Backtester(
        cache=cache,
        alpha_recipe=alpha,
        capital=1_000_000,
        mode="D0",
        start_date="2024-01-01",
        end_date="2024-01-03",
    )
    _, ret_s, to_s, mb_s, mw_s, lw_s, sw_s, nz_s, _ = bt.run()

    signals_2d = _build_signals_2d(cache.symbols, len(cache.timestamps), signal_map=signal_map, default_signal=default_signal)
    oracle = simulate_backtest_series(
        close_2d=cache.arrays["close"],
        signals_2d=signals_2d,
        fee_rate=cache.transaction_fee_rate,
        mode="D0",
    )

    np.testing.assert_allclose(ret_s, oracle.ret_series, rtol=RTOL, atol=ATOL)
    np.testing.assert_allclose(to_s, oracle.turnover, rtol=RTOL, atol=ATOL)
    # close=0 row exists, so weights stay 0.5/0.5 and day2 turnover remains 0.
    assert to_s[1] == pytest.approx(0.0, abs=ATOL)
    assert ret_s[0] == pytest.approx(0.05, abs=ATOL)


def test_metrics_truth_table_case_i_new_listing_transition(tmp_path: Path) -> None:
    dates = [pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2024-01-02", tz="UTC"), pd.Timestamp("2024-01-03", tz="UTC")]
    close_by_symbol = {
        "A": [100.0, 110.0, 120.0],
        "B": [None, 50.0, 55.0],
    }
    parquet_path = tmp_path / "case_i_new_listing.parquet"
    _write_ohlcv_parquet(parquet_path, dates, close_by_symbol)
    cache = _make_cache(parquet_path, "2024-01-01", "2024-01-03", tmp_path, fee_bps=0)

    signal_map = {}
    default_signal = {"A": 1.0, "B": 1.0}
    alpha = SignalPathAlpha(signal_map=signal_map, default_signal=default_signal)
    bt = Backtester(
        cache=cache,
        alpha_recipe=alpha,
        capital=1_000_000,
        mode="D0",
        start_date="2024-01-01",
        end_date="2024-01-03",
    )
    _, ret_s, to_s, mb_s, mw_s, lw_s, sw_s, nz_s, _ = bt.run()

    signals_2d = _build_signals_2d(cache.symbols, len(cache.timestamps), signal_map=signal_map, default_signal=default_signal)
    oracle = simulate_backtest_series(
        close_2d=cache.arrays["close"],
        signals_2d=signals_2d,
        fee_rate=cache.transaction_fee_rate,
        mode="D0",
    )

    np.testing.assert_allclose(ret_s, oracle.ret_series, rtol=RTOL, atol=ATOL)
    np.testing.assert_allclose(to_s, oracle.turnover, rtol=RTOL, atol=ATOL)
    # Day2: only A existed on day1, so day2 return is full A return.
    assert ret_s[0] == pytest.approx(0.10, abs=ATOL)
    # Day2 turnover: [1,0] -> [0.5,0.5]
    assert to_s[1] == pytest.approx(1.0, abs=ATOL)


def test_metrics_truth_table_case_j_d0_d1_constant_signal_shift(tmp_path: Path) -> None:
    dates = [pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2024-01-02", tz="UTC"), pd.Timestamp("2024-01-03", tz="UTC"), pd.Timestamp("2024-01-04", tz="UTC"), pd.Timestamp("2024-01-05", tz="UTC")]
    close_by_symbol = {
        "A": [100.0, 110.0, 120.0, 90.0, 95.0],
        "B": [100.0, 100.0, 100.0, 100.0, 100.0],
    }
    parquet_path = tmp_path / "case_j_constant.parquet"
    _write_ohlcv_parquet(parquet_path, dates, close_by_symbol)
    cache = _make_cache(parquet_path, "2024-01-01", "2024-01-05", tmp_path, fee_bps=10)

    signal_map = {}
    default_signal = {"A": 1.0, "B": 0.0}
    signals_2d = _build_signals_2d(cache.symbols, len(cache.timestamps), signal_map=signal_map, default_signal=default_signal)

    d0 = Backtester(
        cache=cache,
        alpha_recipe=SignalPathAlpha(signal_map=signal_map, default_signal=default_signal),
        capital=1_000_000,
        mode="D0",
        start_date="2024-01-01",
        end_date="2024-01-05",
    ).run()
    d1 = Backtester(
        cache=cache,
        alpha_recipe=SignalPathAlpha(signal_map=signal_map, default_signal=default_signal),
        capital=1_000_000,
        mode="D1",
        start_date="2024-01-01",
        end_date="2024-01-05",
    ).run()

    _, d0_ret, d0_to, *_ = d0
    _, d1_ret, d1_to, *_ = d1

    oracle_d0 = simulate_backtest_series(
        close_2d=cache.arrays["close"],
        signals_2d=signals_2d,
        fee_rate=cache.transaction_fee_rate,
        mode="D0",
    )
    oracle_d1 = simulate_backtest_series(
        close_2d=cache.arrays["close"],
        signals_2d=signals_2d,
        fee_rate=cache.transaction_fee_rate,
        mode="D1",
    )

    np.testing.assert_allclose(d0_ret, oracle_d0.ret_series, rtol=RTOL, atol=ATOL)
    np.testing.assert_allclose(d1_ret, oracle_d1.ret_series, rtol=RTOL, atol=ATOL)
    np.testing.assert_allclose(d0_to, oracle_d0.turnover, rtol=RTOL, atol=ATOL)
    np.testing.assert_allclose(d1_to, oracle_d1.turnover, rtol=RTOL, atol=ATOL)

    assert d1_ret[0] == pytest.approx(-0.001, abs=ATOL)
    np.testing.assert_allclose(d0_ret[1:], d1_ret[1:], rtol=RTOL, atol=ATOL)


def test_metrics_truth_table_case_j_d0_d1_varying_signal_diverges(tmp_path: Path) -> None:
    dates = [pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2024-01-02", tz="UTC"), pd.Timestamp("2024-01-03", tz="UTC"), pd.Timestamp("2024-01-04", tz="UTC"), pd.Timestamp("2024-01-05", tz="UTC")]
    close_by_symbol = {
        "A": [100.0, 110.0, 100.0, 120.0, 100.0],
        "B": [100.0, 90.0, 110.0, 80.0, 120.0],
    }
    parquet_path = tmp_path / "case_j_varying.parquet"
    _write_ohlcv_parquet(parquet_path, dates, close_by_symbol)
    cache = _make_cache(parquet_path, "2024-01-01", "2024-01-05", tmp_path, fee_bps=10)

    signal_map = {
        0: {"A": 1.0, "B": 0.0},
        1: {"A": 0.0, "B": 1.0},
        2: {"A": 1.0, "B": 0.0},
        3: {"A": 0.0, "B": 1.0},
        4: {"A": 1.0, "B": 0.0},
    }
    default_signal = None
    signals_2d = _build_signals_2d(cache.symbols, len(cache.timestamps), signal_map=signal_map, default_signal=default_signal)

    d0 = Backtester(
        cache=cache,
        alpha_recipe=SignalPathAlpha(signal_map=signal_map, default_signal=default_signal),
        capital=1_000_000,
        mode="D0",
        start_date="2024-01-01",
        end_date="2024-01-05",
    ).run()
    d1 = Backtester(
        cache=cache,
        alpha_recipe=SignalPathAlpha(signal_map=signal_map, default_signal=default_signal),
        capital=1_000_000,
        mode="D1",
        start_date="2024-01-01",
        end_date="2024-01-05",
    ).run()

    _, d0_ret, *_ = d0
    _, d1_ret, *_ = d1

    oracle_d0 = simulate_backtest_series(
        close_2d=cache.arrays["close"],
        signals_2d=signals_2d,
        fee_rate=cache.transaction_fee_rate,
        mode="D0",
    )
    oracle_d1 = simulate_backtest_series(
        close_2d=cache.arrays["close"],
        signals_2d=signals_2d,
        fee_rate=cache.transaction_fee_rate,
        mode="D1",
    )

    np.testing.assert_allclose(d0_ret, oracle_d0.ret_series, rtol=RTOL, atol=ATOL)
    np.testing.assert_allclose(d1_ret, oracle_d1.ret_series, rtol=RTOL, atol=ATOL)
    assert not np.allclose(d0_ret, d1_ret, rtol=RTOL, atol=ATOL)

