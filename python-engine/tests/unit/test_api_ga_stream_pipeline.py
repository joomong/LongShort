"""Pipeline tests for /api/ga/run-stream event contract and payload shape."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.services.ga_engine import GAResult, Individual, xml_hash
from framework.exceptions import XmlCompileError


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
    cache.symbols = np.array(["AAA"])
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


def _parse_ndjson(text: str) -> list[dict]:
    lines = [line for line in text.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


@patch("api.routers.ga.run_backtest")
@patch("api.routers.ga.compile_with_sources")
@patch("api.routers.ga.extract_search_space")
def test_ga_stream_pipeline_no_variable_xml_fallback(
    mock_extract_search_space,
    mock_compile_with_sources,
    mock_run_backtest,
):
    mock_extract_search_space.side_effect = XmlCompileError(
        "No variable dimensions in XML template -- nothing to search"
    )
    mock_compile_with_sources.return_value = [(MagicMock(), '<data expr="close" delay="1"/>')]
    mock_run_backtest.return_value = SINGLE_FIXTURE

    response = client.post(
        "/api/ga/run-stream",
        json={
            "xml": '<data expr="close" delay="1"/>',
            "settings": SETTINGS_PAYLOAD,
            "topN": 2,
        },
    )

    assert response.status_code == 200
    events = _parse_ndjson(response.text)
    assert [e["type"] for e in events] == ["complete"]
    assert events[0]["data"] == SINGLE_FIXTURE


@patch("api.routers.ga._build_response")
@patch("api.routers.ga.run_ga")
@patch("api.routers.ga._make_evaluate_batch")
@patch("api.routers.ga.extract_search_space")
def test_ga_stream_pipeline_event_order_and_payload_contract(
    mock_extract_search_space,
    mock_make_evaluate_batch,
    mock_run_ga,
    mock_build_response,
):
    mock_extract_search_space.return_value = MagicMock()

    xml_1 = '<data expr="close" delay="1"/>'
    xml_2 = '<data expr="open" delay="1"/>'

    ind_1 = Individual(genome=[0], xml=xml_1, fitness=1.1, xml_hash=xml_hash(xml_1))
    ind_2 = Individual(genome=[1], xml=xml_2, fitness=0.9, xml_hash=xml_hash(xml_2))

    fixture_alphas = MULTI_FIXTURE.get("alphas", [])
    alpha_1_payload = next((a for a in fixture_alphas if a.get("name") == "alpha_1"), None)
    alpha_2_payload = next((a for a in fixture_alphas if a.get("name") == "alpha_2"), None)
    if alpha_1_payload is None and len(fixture_alphas) > 0:
        alpha_1_payload = fixture_alphas[0]
    if alpha_2_payload is None and len(fixture_alphas) > 1:
        alpha_2_payload = fixture_alphas[1]
    assert alpha_1_payload is not None
    assert alpha_2_payload is not None

    result_store = {
        ind_1.xml_hash: alpha_1_payload,
        ind_2.xml_hash: alpha_2_payload,
    }
    mock_make_evaluate_batch.return_value = (lambda _xml_list: [1.0], result_store)

    def _run_ga_side_effect(space, config, evaluate_batch, on_progress):
        on_progress(
            {
                "type": "progress",
                "generation": 1,
                "totalGenerations": 5,
                "evaluated": 4,
                "populationSize": 10,
            }
        )
        on_progress(
            {
                "type": "generation_end",
                "generation": 1,
                "totalGenerations": 5,
                "evaluated": 10,
                "populationSize": 10,
                "topIndividuals": [ind_1, ind_2],
            }
        )
        return GAResult(
            best_individuals=[ind_1, ind_2],
            generations_run=1,
            total_evaluations=2,
            search_space_size=2,
        )

    mock_run_ga.side_effect = _run_ga_side_effect
    mock_build_response.return_value = MULTI_FIXTURE

    response = client.post(
        "/api/ga/run-stream",
        json={
            "xml": '<data expr="close|open" delay="1"/>',
            "settings": SETTINGS_PAYLOAD,
            "topN": 2,
        },
    )

    assert response.status_code == 200
    events = _parse_ndjson(response.text)
    assert [e["type"] for e in events] == ["progress", "generation_end", "complete"]

    progress = events[0]
    assert progress["generation"] == 1
    assert progress["totalGenerations"] == 5
    assert progress["populationSize"] == 10

    generation_end = events[1]
    assert "partialResult" in generation_end
    partial = generation_end["partialResult"]
    assert partial["combined"] is None
    assert partial["sortedBy"] == "sharpe"
    assert partial["initialCapital"] == 100000.0
    assert partial["heatmap"] is None
    assert partial["signalRange"] is None
    assert partial["distribution"] is None
    assert len(partial["alphas"]) == 2

    for alpha in partial["alphas"]:
        assert isinstance(alpha["name"], str)
        assert isinstance(alpha["isMetrics"], dict)
        assert isinstance(alpha["osMetrics"], dict)
        assert isinstance(alpha["isRetSeries"], dict)
        assert isinstance(alpha["osRetSeries"], dict)
        assert "pnl" in alpha["isMetrics"]
        assert "sharpe" in alpha["isMetrics"]
        assert "mdd" in alpha["isMetrics"]

    complete = events[2]
    assert complete["data"] == MULTI_FIXTURE
