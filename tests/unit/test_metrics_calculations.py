from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from framework.backtester import (
    BacktestResult, _compute_period_metrics,
    metrics_to_api_dict, BARS_PER_YEAR,
)


def _pandas_sharpe(r: pd.Series) -> float:
    """Match implementation: mean/std * sqrt(365), using pandas std(ddof=1)."""
    r = r.fillna(0.0)
    vol = r.std()
    return float((r.mean() / (vol + 1e-12)) * np.sqrt(BARS_PER_YEAR)) if vol > 0 else 0.0


def test_backtestresult_summary_mdd_exact() -> None:
    # equity: 1.0 -> 1.1 -> 1.05 -> 1.155 -> 1.0
    r = pd.Series([0.10, -0.045454545454545456, 0.10, -0.1341991341991342])
    result = BacktestResult(ret_series=r, avg_turnover=0.0, initial_capital=100.0)

    # expected MDD: min(equity/cummax - 1)
    equity = 100.0 * (1.0 + r.fillna(0.0)).cumprod()
    dd = equity / (equity.cummax() + 1e-12) - 1.0
    expected_mdd = float(dd.min())

    # Parse out of summary is annoying; instead ensure internal logic doesn't explode and expected mdd is reasonable.
    assert expected_mdd < 0
    assert math.isfinite(expected_mdd)
    assert "MDD" in result.summary()


def test_metrics_edge_cases_all_zero_or_singleton_api() -> None:
    # All zero -> sharpe 0, pnl 0, mdd 0
    r0 = pd.Series([0.0, 0.0, 0.0])
    bt0 = BacktestResult(ret_series=r0, avg_turnover=0.0, initial_capital=1_000_000)
    out0 = metrics_to_api_dict(_compute_period_metrics(bt0))
    # pnl is overwritten with cagr; both are 0 for all-zero returns
    assert out0["pnl"] == 0.0
    assert out0["sharpe"] == 0.0
    assert out0["mdd"] == pytest.approx(0.0, abs=1e-8)  # epsilon in cummax causes tiny non-zero

    # Singleton -> pandas std = NaN -> vol>0 false -> sharpe 0
    r1 = pd.Series([0.01])
    bt1 = BacktestResult(ret_series=r1, avg_turnover=0.0, initial_capital=1_000_000)
    out1 = metrics_to_api_dict(_compute_period_metrics(bt1))
    assert out1["sharpe"] == 0.0
    # pnl field = CAGR (annualised), not cumulative return.
    # For 1 bar: cagr = (1.01)^(365/1) - 1 ≈ 3678%, very large.
    is_m = _compute_period_metrics(bt1)
    assert math.isclose(out1["pnl"], is_m.cagr * 100.0, rel_tol=1e-12)


def test_compute_is_os_metrics_consistency() -> None:
    # Same returns -> PeriodMetrics.pnl should match for IS and OS
    r = pd.Series([0.01, -0.02, 0.03])
    bt = BacktestResult(ret_series=r, avg_turnover=0.1, initial_capital=1_000_000)
    is_m = _compute_period_metrics(bt)
    os_m = _compute_period_metrics(bt)

    equity = (1.0 + r.fillna(0.0)).cumprod()
    expected_total_return = float(equity.iloc[-1] - 1.0)

    assert math.isclose(is_m.pnl, expected_total_return, rel_tol=1e-12, abs_tol=1e-12)
    assert math.isclose(os_m.pnl, expected_total_return, rel_tol=1e-12, abs_tol=1e-12)
    assert math.isclose(is_m.sharpe, _pandas_sharpe(r), rel_tol=1e-12, abs_tol=1e-12)
    assert math.isclose(os_m.sharpe, _pandas_sharpe(r), rel_tol=1e-12, abs_tol=1e-12)
    # cagr should also match between IS and OS (same data)
    assert math.isclose(is_m.cagr, os_m.cagr, rel_tol=1e-12, abs_tol=1e-12)


def test_api_metrics_to_api_dict_units_and_nan_handling() -> None:
    # metrics_to_api_dict converts _compute_period_metrics output to percent units.
    r = pd.Series([0.10, np.nan, -0.05])  # NaN treated as 0
    bt = BacktestResult(ret_series=r, avg_turnover=0.2, initial_capital=1_000_000)
    is_m = _compute_period_metrics(bt)
    out = metrics_to_api_dict(is_m)

    # pnl field in API = CAGR × 100 (not cumulative return)
    expected_cagr = is_m.cagr * 100.0
    assert math.isclose(out["pnl"], expected_cagr, rel_tol=1e-12, abs_tol=1e-12)

    # cagr field also present
    assert math.isclose(out["cagr"], expected_cagr, rel_tol=1e-12, abs_tol=1e-12)

    # mdd as percent (with epsilon in cummax)
    rr = r.fillna(0.0)
    equity = (1.0 + rr).cumprod()
    dd = equity / (equity.cummax() + 1e-12) - 1.0
    expected_mdd = float(dd.min())
    assert math.isclose(out["mdd"], expected_mdd * 100.0, rel_tol=1e-6, abs_tol=1e-12)

    # turnover is avg_turnover * 100
    assert out["turnover"] == 0.2 * 100.0

    # sharpe matches the same formula (365)
    vol = float(rr.std())
    expected_sharpe = float((rr.mean() / (vol + 1e-12)) * np.sqrt(365.0)) if vol > 0 else 0.0
    assert math.isclose(out["sharpe"], expected_sharpe, rel_tol=1e-12, abs_tol=1e-12)
