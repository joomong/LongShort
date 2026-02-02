"""Tests for the GA API endpoint."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _make_test_client():
    """Create a TestClient with mocked data_cache (skip lifespan)."""
    from api.main import app

    # Override lifespan to avoid loading real data
    app.state.data_cache = MagicMock()

    return TestClient(app, raise_server_exceptions=False)


VALID_SETTINGS = {
    "isStart": "2023-01-01",
    "isEnd": "2023-06-30",
    "osStart": "2023-07-01",
    "osEnd": "2023-12-31",
    "barInterval": "D1",
    "nJobs": 1,
    "gaPopulation": 10,
}


class TestGAEndpoint:
    @pytest.fixture(autouse=True)
    def setup_client(self):
        self.client = _make_test_client()

    @patch("api.routers.ga.run_ga_search")
    def test_400_no_variable_dimensions(self, mock_search):
        """Fixed XML returns 400."""
        from framework.exceptions import XmlCompileError

        mock_search.side_effect = XmlCompileError(
            "No variable dimensions in XML template -- nothing to search"
        )

        resp = self.client.post(
            "/api/ga/run",
            json={
                "xml": '<data expr="close" delay="1"/>',
                "settings": VALID_SETTINGS,
                "topN": 5,
            },
        )
        assert resp.status_code == 400
        assert "No variable dimensions" in resp.json()["detail"]

    @patch("api.routers.ga.run_ga_search")
    def test_200_valid_request(self, mock_search):
        """Returns 200 with correct response structure when search succeeds."""
        mock_search.return_value = {
            "combined": None,
            "alphas": [],
            "ga": {
                "generationsRun": 1,
                "totalEvaluations": 4,
                "searchSpaceSize": 4,
            },
            "sortedBy": "sharpe",
        }

        resp = self.client.post(
            "/api/ga/run",
            json={
                "xml": '<data expr="close|open" delay="1"/>',
                "settings": VALID_SETTINGS,
                "topN": 5,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "ga" in data
        assert "alphas" in data

    @patch("api.routers.ga.run_ga_search")
    def test_400_invalid_xml(self, mock_search):
        """Returns 400 for invalid XML template."""
        from framework.exceptions import XmlCompileError

        mock_search.side_effect = XmlCompileError("Invalid XML: not well-formed")

        resp = self.client.post(
            "/api/ga/run",
            json={
                "xml": "<<<not valid xml>>>",
                "settings": VALID_SETTINGS,
                "topN": 5,
            },
        )
        assert resp.status_code == 400
        assert "Invalid XML" in resp.json()["detail"]

    @patch("api.routers.ga.run_ga_search")
    def test_400_value_error(self, mock_search):
        """Returns 400 for ValueError from service."""
        mock_search.side_effect = ValueError("Unknown barInterval")

        resp = self.client.post(
            "/api/ga/run",
            json={
                "xml": '<data expr="close|open" delay="1"/>',
                "settings": VALID_SETTINGS,
                "topN": 5,
            },
        )
        assert resp.status_code == 400
        assert "barInterval" in resp.json()["detail"]

    def test_422_missing_required_fields(self):
        """Returns 422 when required fields are missing."""
        resp = self.client.post(
            "/api/ga/run",
            json={"xml": '<data expr="close|open" delay="1"/>'},
        )
        assert resp.status_code == 422

    def test_422_topn_out_of_range(self):
        """Returns 422 when topN is out of allowed range."""
        resp = self.client.post(
            "/api/ga/run",
            json={
                "xml": '<data expr="close|open" delay="1"/>',
                "settings": VALID_SETTINGS,
                "topN": 0,
            },
        )
        assert resp.status_code == 422
