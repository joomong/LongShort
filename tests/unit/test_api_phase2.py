"""Tests for Phase 2 API endpoints: data-range and alpha CRUD."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.xml_alpha import XmlCompileError


def _mock_data_cache() -> MagicMock:
    """Create a mock DataCache for testing."""
    mock_cache = MagicMock()
    mock_cache.arrays = {"close": np.array([[1.0, 2.0]])}
    mock_cache.timestamps = pd.DatetimeIndex(
        [pd.Timestamp("2020-01-01", tz="UTC"), pd.Timestamp("2025-08-31", tz="UTC")]
    )
    mock_cache.symbols = np.array(["BTCUSDT", "ETHUSDT"])
    mock_cache.start_date = "2020-01-01"
    mock_cache.end_date = "2025-08-31"
    mock_cache.initial_capital = 1000000
    mock_cache.transaction_fee_rate = 0.001
    return mock_cache


@pytest.fixture(autouse=True)
def _set_data_cache_on_app():
    """Inject a mock DataCache into app.state for all tests in this module."""
    app.state.data_cache = _mock_data_cache()
    yield
    del app.state.data_cache


client = TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# GET /api/data-range
# ---------------------------------------------------------------------------

def test_data_range_returns_200():
    resp = client.get("/api/data-range")
    assert resp.status_code == 200
    body = resp.json()
    assert "minDate" in body
    assert "maxDate" in body
    assert "intervals" not in body


def test_data_range_uses_cache_timestamps():
    resp = client.get("/api/data-range")
    assert resp.status_code == 200
    body = resp.json()
    assert body["minDate"] == "2020-01-01"
    assert body["maxDate"] == "2025-08-31"


# ---------------------------------------------------------------------------
# POST /api/alphas
# ---------------------------------------------------------------------------

def test_save_alpha_valid(tmp_path):
    with (
        patch("api.services.alpha_service.SAVED_ALPHAS_DIR", tmp_path),
        patch("api.services.alpha_service.compile") as mock_compile,
    ):
        mock_compile.return_value = MagicMock()
        resp = client.post("/api/alphas", json={"name": "my_alpha1", "xml": '<data expr="close"/>'})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["name"] == "my_alpha1"
    assert (tmp_path / "my_alpha1.xml").read_text() == '<data expr="close"/>'


def test_save_alpha_invalid_xml(tmp_path):
    with (
        patch("api.services.alpha_service.SAVED_ALPHAS_DIR", tmp_path),
        patch("api.services.alpha_service.compile") as mock_compile,
    ):
        mock_compile.side_effect = XmlCompileError("bad xml")
        resp = client.post("/api/alphas", json={"name": "bad_alpha", "xml": "<notvalid>"})
    assert resp.status_code == 400
    assert "bad xml" in resp.json()["detail"]


def test_save_alpha_invalid_name_returns_422():
    resp = client.post("/api/alphas", json={"name": "../etc/passwd", "xml": '<data expr="close"/>'})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/alphas (list)
# ---------------------------------------------------------------------------

def test_list_alphas_empty(tmp_path):
    with patch("api.services.alpha_service.SAVED_ALPHAS_DIR", tmp_path):
        resp = client.get("/api/alphas")
    assert resp.status_code == 200
    assert resp.json()["alphas"] == []


def test_list_alphas_with_files(tmp_path):
    (tmp_path / "alpha1.xml").write_text('<data expr="close"/>')
    (tmp_path / "alpha2.xml").write_text('<data expr="open"/>')
    with patch("api.services.alpha_service.SAVED_ALPHAS_DIR", tmp_path):
        resp = client.get("/api/alphas")
    assert resp.status_code == 200
    names = [a["name"] for a in resp.json()["alphas"]]
    assert "alpha1" in names
    assert "alpha2" in names


# ---------------------------------------------------------------------------
# GET /api/alphas/{name}
# ---------------------------------------------------------------------------

def test_get_alpha_existing(tmp_path):
    (tmp_path / "test_alpha.xml").write_text('<data expr="close" delay="1"/>')
    with patch("api.services.alpha_service.SAVED_ALPHAS_DIR", tmp_path):
        resp = client.get("/api/alphas/test_alpha")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "test_alpha"
    assert body["xml"] == '<data expr="close" delay="1"/>'
    assert "createdAt" in body


def test_get_alpha_not_found(tmp_path):
    with patch("api.services.alpha_service.SAVED_ALPHAS_DIR", tmp_path):
        resp = client.get("/api/alphas/nonexistent")
    assert resp.status_code == 404


def test_get_alpha_path_traversal(tmp_path):
    with patch("api.services.alpha_service.SAVED_ALPHAS_DIR", tmp_path):
        resp = client.get("/api/alphas/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code == 404
