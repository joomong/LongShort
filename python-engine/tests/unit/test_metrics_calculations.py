from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from framework.backtester import (
    _compute_metrics, metrics_to_api_dict, BARS_PER_YEAR, PeriodMetrics,
)


def _pandas_sharpe(r: pd.Series) -> float:
    """Match implementation: finite-only mean/std * sqrt(365), ddof=1."""
    r = r[np.isfinite(r)]
    if r.empty:
        return 0.0
    vol = r.std()
    return float((r.mean() / (vol + 1e-12)) * np.sqrt(BARS_PER_YEAR)) if vol > 0 else 0.0



def test_metrics_edge_cases_all_zero_or_singleton_api() -> None:
    # All zero -> sharpe 0, pnl 0, mdd 0
    r0 = pd.Series([0.0, 0.0, 0.0])
    out0 = metrics_to_api_dict(_compute_metrics(r0, pd.Series([0.0, 0.0, 0.0]), pd.Series([0.0, 0.0, 0.0]), pd.Series([0.0, 0.0, 0.0])))
    # all-zero returns => pnl/cagr/sharpe/mdd are all 0
    assert out0["pnl"] == 0.0
    assert out0["sharpe"] == 0.0
    assert out0["mdd"] == pytest.approx(0.0, abs=1e-8)  # epsilon in cummax causes tiny non-zero

    # Singleton -> pandas std = NaN -> vol>0 false -> sharpe 0
    r1 = pd.Series([0.01])
    is_m = _compute_metrics(r1, pd.Series([0.0]), pd.Series([0.0]), pd.Series([0.0]))
    out1 = metrics_to_api_dict(is_m)
    assert out1["sharpe"] == 0.0
    # pnl field is cumulative return (% units), not annualized cagr.
    assert math.isclose(out1["pnl"], is_m.pnl * 100.0, rel_tol=1e-12)


def test_compute_is_os_metrics_consistency() -> None:
    # Same returns -> PeriodMetrics.pnl should match for IS and OS
    r = pd.Series([0.01, -0.02, 0.03])
    to = pd.Series([0.1, 0.1, 0.1])
    mb = pd.Series([0.0, 0.0, 0.0])
    mw = pd.Series([0.0, 0.0, 0.0])
    is_m = _compute_metrics(r, to, mb, mw)
    os_m = _compute_metrics(r, to, mb, mw)

    equity = (1.0 + r.fillna(0.0)).cumprod()
    expected_total_return = float(equity.iloc[-1] - 1.0)

    assert math.isclose(is_m.pnl, expected_total_return, rel_tol=1e-12, abs_tol=1e-12)
    assert math.isclose(os_m.pnl, expected_total_return, rel_tol=1e-12, abs_tol=1e-12)
    assert math.isclose(is_m.sharpe, _pandas_sharpe(r), rel_tol=1e-12, abs_tol=1e-12)
    assert math.isclose(os_m.sharpe, _pandas_sharpe(r), rel_tol=1e-12, abs_tol=1e-12)
    # cagr should also match between IS and OS (same data)
    assert math.isclose(is_m.cagr, os_m.cagr, rel_tol=1e-12, abs_tol=1e-12)


def test_api_metrics_to_api_dict_units_and_nan_handling() -> None:
    # metrics_to_api_dict converts _compute_metrics output to percent units.
    r = pd.Series([0.10, np.nan, -0.05])  # NaN excluded from metric aggregation
    to = pd.Series([0.2, 0.2, 0.2])
    mb = pd.Series([0.0, 0.0, 0.0])
    mw = pd.Series([0.0, 0.0, 0.0])
    is_m = _compute_metrics(r, to, mb, mw)
    out = metrics_to_api_dict(is_m)

    # pnl field in API = cumulative return x 100
    expected_pnl = is_m.pnl * 100.0
    expected_cagr = is_m.cagr * 100.0
    assert math.isclose(out["pnl"], expected_pnl, rel_tol=1e-12, abs_tol=1e-12)

    # cagr field also present
    assert math.isclose(out["cagr"], expected_cagr, rel_tol=1e-12, abs_tol=1e-12)

    # mdd as percent (with epsilon in cummax)
    rr = r[np.isfinite(r)]
    equity = (1.0 + rr).cumprod()
    dd = equity / (equity.cummax() + 1e-12) - 1.0
    expected_mdd = float(dd.min())
    assert math.isclose(out["mdd"], expected_mdd * 100.0, rel_tol=1e-6, abs_tol=1e-12)

    # turnover is avg_turnover * 100
    assert math.isclose(out["turnover"], 0.2 * 100.0, rel_tol=1e-12)

    # sharpe matches the same formula (365)
    vol = float(rr.std())
    expected_sharpe = float((rr.mean() / (vol + 1e-12)) * np.sqrt(365.0)) if vol > 0 else 0.0
    assert math.isclose(out["sharpe"], expected_sharpe, rel_tol=1e-12, abs_tol=1e-12)


def test_metrics_to_api_dict_percent_vs_raw_fields() -> None:
    m = PeriodMetrics(
        pnl=0.1234,
        cagr=0.5678,
        sharpe=1.25,
        mdd=-0.2222,
        turnover=0.3333,
        min_bet=0.0456,
        max_weight=0.789,
        nonzero_position=12,
        ir=float("nan"),
        corr=0.4321,
    )
    out = metrics_to_api_dict(m)

    # Percent-scaled fields
    assert math.isclose(out["pnl"], 12.34, rel_tol=1e-12, abs_tol=1e-12)
    assert math.isclose(out["cagr"], 56.78, rel_tol=1e-12, abs_tol=1e-12)
    assert math.isclose(out["mdd"], -22.22, rel_tol=1e-12, abs_tol=1e-12)
    assert math.isclose(out["turnover"], 33.33, rel_tol=1e-12, abs_tol=1e-12)

    # Raw (non-scaled) fields
    assert math.isclose(out["sharpe"], 1.25, rel_tol=1e-12, abs_tol=1e-12)
    assert math.isclose(out["min_bet"], 0.0456, rel_tol=1e-12, abs_tol=1e-12)
    assert math.isclose(out["max_weight"], 0.789, rel_tol=1e-12, abs_tol=1e-12)
    assert out["nonzero_position"] == 12
    assert math.isclose(out["corr"], 0.4321, rel_tol=1e-12, abs_tol=1e-12)

    # NaN float fields are omitted
    assert "ir" not in out
