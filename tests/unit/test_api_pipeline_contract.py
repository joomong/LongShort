"""End-to-end contract checks for /api/backtest/run (compute -> API payload)."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import app
from framework.backtester import (
    AlphaResult,
    BacktestPeriodResult,
    MultipleBacktestResult,
    PeriodMetrics,
    PeriodResult,
)
from tests.unit._metrics_oracle import compute_metrics_from_series, to_api_units


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "pipeline"
SINGLE_FIXTURE = json.loads((FIXTURE_DIR / "single_alpha_full_payload.json").read_text())
MULTI_FIXTURE = json.loads((FIXTURE_DIR / "multi_alpha_full_payload.json").read_text())

SETTINGS_PAYLOAD = {
    "osStart": "2024-01-05",
    "osEnd": "2024-01-07",
    "isStart": "2024-01-02",
    "isEnd": "2024-01-04",
    "barInterval": "D1",
    "nJobs": 2,
    "gaPopulation": 10,
}


def _mock_data_cache() -> MagicMock:
    cache = MagicMock()
    cache.arrays = {"close": np.array([[100.0]], dtype=np.float64)}
    cache.timestamps = pd.DatetimeIndex(
        [pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2024-01-07", tz="UTC")]
    )
    cache.symbols = np.array(["AAA"])  # only needed for basic validation
    cache.initial_capital = 100000.0
    cache.transaction_fee_rate = 0.001
    cache.start_date = "2024-01-01"
    cache.end_date = "2024-01-07"
    return cache


@pytest.fixture(autouse=True)
def _set_data_cache_on_app():
    app.state.data_cache = _mock_data_cache()
    yield
    del app.state.data_cache


client = TestClient(app, raise_server_exceptions=False)


def _ts(dates: list[str]) -> np.ndarray:
    return pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in dates]).values


def _period_from_arrays(
    *,
    dates: list[str],
    ret: list[float],
    turnover: list[float],
    min_bet: list[float],
    max_weight: list[float],
    nonzero: list[float],
    long_ratio: float,
    short_ratio: float,
    ir: float = float("nan"),
    corr: float = float("nan"),
) -> PeriodResult:
    ret_arr = np.asarray(ret, dtype=np.float64)
    to_arr = np.asarray(turnover, dtype=np.float64)
    mb_arr = np.asarray(min_bet, dtype=np.float64)
    mw_arr = np.asarray(max_weight, dtype=np.float64)
    nz_arr = np.asarray(nonzero, dtype=np.float64)

    m = compute_metrics_from_series(
        ret_arr,
        to_arr,
        mb_arr,
        mw_arr,
        nonzero_count_series=nz_arr,
    )

    metrics = PeriodMetrics(
        pnl=m.pnl,
        cagr=m.cagr,
        sharpe=m.sharpe,
        mdd=m.mdd,
        turnover=m.turnover,
        min_bet=m.min_bet,
        max_weight=m.max_weight,
        nonzero_position=m.nonzero_position,
        ir=ir,
        corr=corr,
    )

    return PeriodResult(
        metrics=metrics,
        timestamps=_ts(dates),
        ret_series=ret_arr,
        turnover_series=to_arr,
        min_bet_series=mb_arr,
        max_weight_series=mw_arr,
        nonzero_position_series=nz_arr.astype(np.int16),
        long_ratio=long_ratio,
        short_ratio=short_ratio,
    )


def _single_periods() -> tuple[PeriodResult, PeriodResult]:
    is_period = _period_from_arrays(
        dates=["2024-01-02", "2024-01-03", "2024-01-04"],
        ret=[0.01, -0.02, 0.03],
        turnover=[0.5, 0.2, 0.1],
        min_bet=[0.3, 0.4, 0.35],
        max_weight=[0.6, 0.55, 0.58],
        nonzero=[2, 2, 3],
        long_ratio=0.6,
        short_ratio=0.4,
        ir=0.0,
        corr=1.0,
    )
    os_period = _period_from_arrays(
        dates=["2024-01-05", "2024-01-06", "2024-01-07"],
        ret=[0.005, float("nan"), -0.01],
        turnover=[0.15, float("nan"), 0.05],
        min_bet=[0.25, float("nan"), 0.2],
        max_weight=[0.45, float("nan"), 0.35],
        nonzero=[2, 0, 2],
        long_ratio=0.6,
        short_ratio=0.4,
        ir=0.0,
        corr=1.0,
    )
    return is_period, os_period


def _alpha1_periods() -> tuple[PeriodResult, PeriodResult]:
    is_period = _period_from_arrays(
        dates=["2024-01-02", "2024-01-03", "2024-01-04"],
        ret=[0.012, -0.018, 0.028],
        turnover=[0.45, 0.22, 0.11],
        min_bet=[0.31, 0.39, 0.36],
        max_weight=[0.59, 0.54, 0.57],
        nonzero=[2, 2, 3],
        long_ratio=0.62,
        short_ratio=0.38,
        ir=0.11,
        corr=0.42,
    )
    os_period = _period_from_arrays(
        dates=["2024-01-05", "2024-01-06", "2024-01-07"],
        ret=[0.006, float("nan"), -0.009],
        turnover=[0.16, float("nan"), 0.04],
        min_bet=[0.26, float("nan"), 0.19],
        max_weight=[0.44, float("nan"), 0.34],
        nonzero=[2, 0, 2],
        long_ratio=0.62,
        short_ratio=0.38,
        ir=-0.21,
        corr=0.33,
    )
    return is_period, os_period


def _alpha2_periods() -> tuple[PeriodResult, PeriodResult]:
    is_period = _period_from_arrays(
        dates=["2024-01-02", "2024-01-03", "2024-01-04"],
        ret=[-0.01, 0.005, 0.0],
        turnover=[0.4, 0.3, 0.2],
        min_bet=[0.2, 0.25, 0.22],
        max_weight=[0.5, 0.45, 0.4],
        nonzero=[2, 2, 2],
        long_ratio=0.55,
        short_ratio=0.45,
        ir=-0.44,
        corr=-0.12,
    )
    os_period = _period_from_arrays(
        dates=["2024-01-05", "2024-01-06", "2024-01-07"],
        ret=[0.002, 0.004, -0.003],
        turnover=[0.2, 0.1, 0.1],
        min_bet=[0.2, 0.2, 0.2],
        max_weight=[0.4, 0.35, 0.3],
        nonzero=[1, 2, 2],
        long_ratio=0.55,
        short_ratio=0.45,
        ir=0.55,
        corr=0.19,
    )
    return is_period, os_period


def _build_single_framework_result() -> MultipleBacktestResult:
    is_period, os_period = _single_periods()
    combined_is = replace(
        is_period,
        metrics=replace(is_period.metrics, ir=float("nan"), corr=float("nan")),
    )
    combined_os = replace(
        os_period,
        metrics=replace(os_period.metrics, ir=float("nan"), corr=float("nan")),
    )
    alpha = AlphaResult(
        name="alpha_0",
        rank=1,
        source_idx=0,
        is_period=is_period,
        os_period=os_period,
    )
    combined = BacktestPeriodResult(is_period=combined_is, os_period=combined_os)
    return MultipleBacktestResult(
        alpha_results=[alpha],
        combined_result=combined,
        sorted_by="sharpe",
        viz_bundle=None,
    )


def _build_multi_framework_result() -> MultipleBacktestResult:
    combined_alpha_is, combined_alpha_os = _single_periods()
    combined_is = replace(
        combined_alpha_is,
        metrics=replace(combined_alpha_is.metrics, ir=float("nan"), corr=float("nan")),
    )
    combined_os = replace(
        combined_alpha_os,
        metrics=replace(combined_alpha_os.metrics, ir=float("nan"), corr=float("nan")),
    )
    a1_is, a1_os = _alpha1_periods()
    a2_is, a2_os = _alpha2_periods()

    combined = BacktestPeriodResult(is_period=combined_is, os_period=combined_os)
    alpha_1 = AlphaResult(
        name="alpha_1",
        rank=1,
        source_idx=0,
        is_period=a1_is,
        os_period=a1_os,
    )
    alpha_2 = AlphaResult(
        name="alpha_2",
        rank=2,
        source_idx=1,
        is_period=a2_is,
        os_period=a2_os,
    )

    return MultipleBacktestResult(
        alpha_results=[alpha_1, alpha_2],
        combined_result=combined,
        sorted_by="sharpe",
        viz_bundle=None,
    )


def _assert_metric_dict_close(actual: dict, expected: dict, *, atol: float = 1e-12, rtol: float = 1e-12) -> None:
    for key, expected_val in expected.items():
        assert key in actual
        actual_val = actual[key]
        if isinstance(expected_val, (int, float)) and not isinstance(expected_val, bool):
            assert math.isclose(float(actual_val), float(expected_val), rel_tol=rtol, abs_tol=atol)
        else:
            assert actual_val == expected_val


def _assert_series_keys_ordered(series: dict[str, float | None], expected_dates: list[str]) -> None:
    assert list(series.keys()) == expected_dates


def _assert_payload_close(actual, expected, *, atol: float = 1e-12, rtol: float = 1e-12) -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict)
        assert set(actual.keys()) == set(expected.keys())
        for key in expected:
            _assert_payload_close(actual[key], expected[key], atol=atol, rtol=rtol)
        return

    if isinstance(expected, list):
        assert isinstance(actual, list)
        assert len(actual) == len(expected)
        for a_item, e_item in zip(actual, expected):
            _assert_payload_close(a_item, e_item, atol=atol, rtol=rtol)
        return

    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        assert isinstance(actual, (int, float))
        assert math.isclose(float(actual), float(expected), rel_tol=rtol, abs_tol=atol)
        return

    assert actual == expected


@patch("api.routers.backtest.compile_with_sources")
@patch("api.services.backtest_service.run_framework_backtest")
def test_api_backtest_pipeline_single_contract_and_oracle(
    mock_run_framework,
    mock_compile_with_sources,
):
    mock_compile_with_sources.return_value = [
        (MagicMock(), '<data expr="close" delay="1"/>')
    ]
    mock_run_framework.return_value = _build_single_framework_result()

    response = client.post(
        "/api/backtest/run",
        json={
            "xml": '<data expr="close" delay="1"/>',
            "settings": SETTINGS_PAYLOAD,
        },
    )

    assert response.status_code == 200
    body = response.json()
    _assert_payload_close(body, SINGLE_FIXTURE)

    expected_is = to_api_units(
        compute_metrics_from_series(
            np.array([0.01, -0.02, 0.03], dtype=np.float64),
            np.array([0.5, 0.2, 0.1], dtype=np.float64),
            np.array([0.3, 0.4, 0.35], dtype=np.float64),
            np.array([0.6, 0.55, 0.58], dtype=np.float64),
            nonzero_count_series=np.array([2, 2, 3], dtype=np.float64),
        )
    )
    expected_os = to_api_units(
        compute_metrics_from_series(
            np.array([0.005, float("nan"), -0.01], dtype=np.float64),
            np.array([0.15, float("nan"), 0.05], dtype=np.float64),
            np.array([0.25, float("nan"), 0.2], dtype=np.float64),
            np.array([0.45, float("nan"), 0.35], dtype=np.float64),
            nonzero_count_series=np.array([2, 0, 2], dtype=np.float64),
        )
    )

    _assert_metric_dict_close(body["alphas"][0]["isMetrics"], {**expected_is, "ir": 0.0, "corr": 1.0})
    _assert_metric_dict_close(body["alphas"][0]["osMetrics"], {**expected_os, "ir": 0.0, "corr": 1.0})

    _assert_series_keys_ordered(body["alphas"][0]["isRetSeries"], ["2024-01-02", "2024-01-03", "2024-01-04"])
    _assert_series_keys_ordered(body["alphas"][0]["osRetSeries"], ["2024-01-05", "2024-01-06", "2024-01-07"])
    assert body["alphas"][0]["osRetSeries"]["2024-01-06"] is None
    assert body["alphas"][0]["osEquitySeries"]["2024-01-06"] is None
    assert body["alphas"][0]["osTurnoverSeries"]["2024-01-06"] is None
    assert body["alphas"][0]["longRatio"] == pytest.approx(0.6, abs=1e-12)
    assert body["alphas"][0]["shortRatio"] == pytest.approx(0.4, abs=1e-12)


@patch("api.routers.backtest.compile_with_sources")
@patch("api.services.backtest_service.run_framework_backtest")
def test_api_backtest_pipeline_multi_contract_and_oracle(
    mock_run_framework,
    mock_compile_with_sources,
):
    mock_compile_with_sources.return_value = [
        (MagicMock(), '<data expr="close" delay="1"/>'),
        (MagicMock(), '<data expr="open" delay="1"/>'),
    ]
    mock_run_framework.return_value = _build_multi_framework_result()

    response = client.post(
        "/api/backtest/run",
        json={
            "xml": '<data expr="close" delay="1"/>\n<data expr="open" delay="1"/>',
            "settings": SETTINGS_PAYLOAD,
        },
    )

    assert response.status_code == 200
    body = response.json()
    _assert_payload_close(body, MULTI_FIXTURE)

    # all expected metric fields should be present for each alpha entry
    metric_fields = {
        "pnl",
        "cagr",
        "sharpe",
        "mdd",
        "turnover",
        "min_bet",
        "max_weight",
        "nonzero_position",
    }
    for alpha in body["alphas"]:
        assert metric_fields.issubset(set(alpha["isMetrics"].keys()))
        assert metric_fields.issubset(set(alpha["osMetrics"].keys()))

    # Combined row keeps IR/Corr omitted (NaN fields dropped by metrics_to_api_dict)
    assert "ir" not in body["alphas"][0]["isMetrics"]
    assert "corr" not in body["alphas"][0]["isMetrics"]

    # date-key order + null handling should be stable
    for alpha in body["alphas"]:
        _assert_series_keys_ordered(alpha["isRetSeries"], ["2024-01-02", "2024-01-03", "2024-01-04"])
        _assert_series_keys_ordered(alpha["osRetSeries"], ["2024-01-05", "2024-01-06", "2024-01-07"])

    assert body["alphas"][0]["osRetSeries"]["2024-01-06"] is None
    assert body["alphas"][1]["osRetSeries"]["2024-01-06"] is None
