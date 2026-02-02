"""Tests for the unified /api/backtest/run endpoint and its helpers."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.services.backtest_service import resolve_mode, range_and_hist
from api.main import app
from framework.backtester import (
    BacktestPeriodResult,
    BacktestResult,
    PeriodMetrics,
    MultipleBacktestResult,
    AlphaResult,
    _compute_period_metrics,
    metrics_to_api_dict,
)


# ---------------------------------------------------------------------------
# Helpers: resolve_mode
# ---------------------------------------------------------------------------

class TestResolveMode:
    def test_d0_passthrough(self):
        assert resolve_mode("D0") == "D0"

    def test_d1_passthrough(self):
        assert resolve_mode("D1") == "D1"

    def test_1d_maps_to_d1(self):
        assert resolve_mode("1d") == "D1"

    def test_intraday_returns_d0(self):
        assert resolve_mode("intraday") == "D0"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown barInterval"):
            resolve_mode("unknown")


# ---------------------------------------------------------------------------
# Helpers: range_and_hist
# ---------------------------------------------------------------------------

class TestRangeAndHist:
    def test_empty_array(self):
        sig_range, dist = range_and_hist(np.array([]))
        assert sig_range == {"min": 0.0, "max": 0.0}
        assert len(dist) == 1000

    def test_normal_array(self):
        values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        sig_range, dist = range_and_hist(values, bins=5)
        assert sig_range["min"] == 1.0
        assert sig_range["max"] == 5.0
        assert len(dist) == 5
        assert sum(dist) == 5

    def test_all_nan(self):
        values = np.array([np.nan, np.nan, np.nan])
        sig_range, dist = range_and_hist(values)
        assert sig_range == {"min": 0.0, "max": 0.0}
        assert len(dist) == 1000


# ---------------------------------------------------------------------------
# Helpers: metrics_to_api_dict
# ---------------------------------------------------------------------------

class TestMetricsToApiDict:
    def test_empty_series(self):
        bt_result = BacktestResult(
            ret_series=pd.Series([], dtype=float),
            avg_turnover=0.0,
            initial_capital=1_000_000.0,
        )
        result = metrics_to_api_dict(_compute_period_metrics(bt_result))
        assert result["pnl"] == 0.0
        assert result["sharpe"] == 0.0
        assert result["mdd"] == 0.0
        assert result["turnover"] == 0.0
        assert result["min_bet"] == 0.0
        assert result["max_weight"] == 0.0

    def test_normal_series(self):
        bt_result = BacktestResult(
            ret_series=pd.Series([0.01, 0.02, -0.005, 0.03, -0.01]),
            avg_turnover=0.15,
            initial_capital=1_000_000.0,
            min_bet_series=pd.Series([10, 12, 11, 13, 9], dtype=float),
            max_weight_series=pd.Series([0.15, 0.20, 0.18, 0.22, 0.17], dtype=float),
        )
        result = metrics_to_api_dict(_compute_period_metrics(bt_result))

        # pnl = CAGR (annualised), not cumulative return
        assert result["pnl"] != 0.0
        assert isinstance(result["sharpe"], float)
        assert result["mdd"] <= 0.0
        assert result["turnover"] == pytest.approx(15.0)
        assert result["min_bet"] == pytest.approx(11.0)
        assert result["max_weight"] == pytest.approx(0.22)


# ---------------------------------------------------------------------------
# Integration tests via TestClient
# ---------------------------------------------------------------------------

SETTINGS_PAYLOAD = {
    "osStart": "2024-01-01",
    "osEnd": "2024-06-30",
    "isStart": "2023-01-01",
    "isEnd": "2023-12-31",
    "barInterval": "D1",
}


def _fake_backtest_result() -> BacktestResult:
    dates = pd.to_datetime(["2023-06-01", "2023-09-01", "2024-03-01"], utc=True)
    return BacktestResult(
        ret_series=pd.Series([0.01, -0.005, 0.02], index=dates, dtype=float),
        avg_turnover=0.10,
        initial_capital=1_000_000.0,
        turnover_series=pd.Series([0.1, 0.1, 0.1], index=dates, dtype=float),
        min_bet_series=pd.Series([10, 12, 11], index=dates, dtype=float),
        max_weight_series=pd.Series([0.15, 0.20, 0.18], index=dates, dtype=float),
    )


def _fake_viz_dict() -> dict:
    idx = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2024-01-01"), "BTCUSDT"),
            (pd.Timestamp("2024-01-01"), "ETHUSDT"),
        ],
        names=["date", "symbol"],
    )
    sym_df = pd.DataFrame({"signal": [1.0, -0.5]}, index=idx)
    return {"symbols": sym_df}


def _fake_multi_result() -> MultipleBacktestResult:
    is_metrics = PeriodMetrics(
        pnl=0.10, cagr=0.12, sharpe=1.5, mdd=-0.05,
        turnover=0.10, min_bet=0.02, max_weight=0.15,
    )
    os_metrics = PeriodMetrics(pnl=0.08, cagr=0.10, sharpe=1.2, mdd=0.0)
    ret = pd.Series([0.01, 0.02], dtype=float)

    ar = AlphaResult(
        name="alpha_0",
        is_metrics=is_metrics,
        os_metrics=os_metrics,
        is_ret_series=ret,
        os_ret_series=ret,
        rank=1,
    )

    combined = BacktestPeriodResult(
        is_metrics=is_metrics,
        os_metrics=os_metrics,
        is_ret_series=ret,
        os_ret_series=ret,
    )

    return MultipleBacktestResult(
        alpha_results=[ar],
        combined_result=combined,
        sorted_by="sharpe",
    )


def _mock_data_cache() -> MagicMock:
    """Create a mock DataCache for testing."""
    mock_cache = MagicMock()
    mock_cache.is_loaded = True
    mock_cache.get_timestamps.return_value = pd.DatetimeIndex(
        [pd.Timestamp("2023-01-01", tz="UTC"), pd.Timestamp("2024-06-30", tz="UTC")]
    )
    mock_cache.symbols = ["BTCUSDT", "ETHUSDT"]
    mock_cache.start_date = "2020-01-01"
    mock_cache.end_date = "2025-08-31"
    mock_cache.initial_capital = 1000000
    return mock_cache


@pytest.fixture(autouse=True)
def _set_data_cache_on_app():
    """Inject a mock DataCache into app.state for all tests in this module."""
    app.state.data_cache = _mock_data_cache()
    yield
    del app.state.data_cache


client = TestClient(app, raise_server_exceptions=False)


class TestSingleAlphaEndpoint:
    @patch("api.routers.backtest.compile_with_sources")
    @patch("api.services.backtest_service.Backtester")
    def test_single_alpha_response_shape(self, mock_bt_cls, mock_compile_with_sources):
        mock_signal = MagicMock()
        mock_compile_with_sources.return_value = [(mock_signal, '<data expr="close" delay="1"/>')]

        mock_bt = MagicMock()
        mock_bt.run.return_value = (_fake_backtest_result(), _fake_viz_dict())
        mock_bt_cls.return_value = mock_bt

        resp = client.post("/api/backtest/run", json={
            "xml": '<data expr="close" delay="1"/>',
            "settings": SETTINGS_PAYLOAD,
        })

        assert resp.status_code == 200
        body = resp.json()
        assert body["combined"] is None
        assert isinstance(body["alphas"], list)
        assert len(body["alphas"]) == 1
        assert "heatmap" in body
        assert "signalRange" in body
        assert "distribution" in body

        is_metrics = body["alphas"][0]["isMetrics"]
        assert "min_bet" in is_metrics
        assert "max_weight" in is_metrics


class TestDateValidation:
    """IS/OS date order validation in BacktestSettings."""

    def test_valid_dates_accepted(self):
        """IS before OS chronologically should pass."""
        resp = client.post("/api/backtest/run", json={
            "xml": '<data expr="close"/>',
            "settings": {
                "isStart": "2023-01-01", "isEnd": "2023-06-30",
                "osStart": "2023-07-01", "osEnd": "2023-12-31",
                "barInterval": "D1",
            },
        })
        # Should not be a 422 validation error (may be 200 or other)
        assert resp.status_code != 422

    def test_is_reversed_returns_422(self):
        """isStart > isEnd should fail validation."""
        resp = client.post("/api/backtest/run", json={
            "xml": '<data expr="close"/>',
            "settings": {
                "isStart": "2023-06-30", "isEnd": "2023-01-01",
                "osStart": "2024-01-01", "osEnd": "2024-06-30",
                "barInterval": "D1",
            },
        })
        assert resp.status_code == 422

    def test_os_reversed_returns_422(self):
        """osStart > osEnd should fail validation."""
        resp = client.post("/api/backtest/run", json={
            "xml": '<data expr="close"/>',
            "settings": {
                "isStart": "2023-01-01", "isEnd": "2023-06-30",
                "osStart": "2024-06-30", "osEnd": "2024-01-01",
                "barInterval": "D1",
            },
        })
        assert resp.status_code == 422

    def test_is_after_os_returns_422(self):
        """IS period after OS period (the original bug) should fail."""
        resp = client.post("/api/backtest/run", json={
            "xml": '<data expr="close"/>',
            "settings": {
                "isStart": "2023-07-01", "isEnd": "2023-12-31",
                "osStart": "2023-01-01", "osEnd": "2023-06-30",
                "barInterval": "D1",
            },
        })
        assert resp.status_code == 422

    def test_overlap_returns_422(self):
        """IS and OS overlap (isEnd == osStart) should fail."""
        resp = client.post("/api/backtest/run", json={
            "xml": '<data expr="close"/>',
            "settings": {
                "isStart": "2023-01-01", "isEnd": "2023-07-01",
                "osStart": "2023-07-01", "osEnd": "2023-12-31",
                "barInterval": "D1",
            },
        })
        assert resp.status_code == 422

    def test_bad_date_format_returns_422(self):
        """Non YYYY-MM-DD format should fail."""
        resp = client.post("/api/backtest/run", json={
            "xml": '<data expr="close"/>',
            "settings": {
                "isStart": "01/01/2023", "isEnd": "2023-06-30",
                "osStart": "2023-07-01", "osEnd": "2023-12-31",
                "barInterval": "D1",
            },
        })
        assert resp.status_code == 422

    def test_adjacent_dates_accepted(self):
        """IS ending day before OS starts should pass (no gap requirement)."""
        resp = client.post("/api/backtest/run", json={
            "xml": '<data expr="close"/>',
            "settings": {
                "isStart": "2023-01-01", "isEnd": "2023-06-30",
                "osStart": "2023-07-01", "osEnd": "2023-12-31",
                "barInterval": "D1",
            },
        })
        assert resp.status_code != 422


class TestEndpointErrors:
    def test_missing_xml_returns_422(self):
        resp = client.post("/api/backtest/run", json={
            "settings": SETTINGS_PAYLOAD,
        })
        # xml is now required (not Optional), so missing xml returns 422
        assert resp.status_code == 422

    def test_invalid_xml_returns_400(self):
        resp = client.post("/api/backtest/run", json={
            "xml": "<not_data/>",
            "settings": SETTINGS_PAYLOAD,
        })
        assert resp.status_code == 400

    def test_invalid_bar_interval_returns_422(self):
        settings = {**SETTINGS_PAYLOAD, "barInterval": "bogus"}
        resp = client.post("/api/backtest/run", json={
            "xml": '<data expr="close"/>',
            "settings": settings,
        })
        assert resp.status_code == 422
