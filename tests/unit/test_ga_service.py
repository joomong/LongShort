"""Tests for GA service integration with backtest."""
from unittest.mock import MagicMock, patch

import pytest

from api.services.ga_service import run_ga_search, _make_evaluate_batch
from framework.exceptions import XmlCompileError


class TestEvaluateBatch:
    """Tests for _make_evaluate_batch callable."""

    @patch("api.services.ga_service.compile_with_sources")
    @patch("api.services.ga_service.run_multiple_backtest")
    def test_maps_results_to_correct_indices(self, mock_rmb, mock_compile):
        """IS Sharpe values correctly mapped back to xml_list indices."""
        from api.services.ga_engine import Individual

        # Mock compile_with_sources: each XML produces one (signal, source) pair
        mock_signal = MagicMock()
        mock_compile.side_effect = lambda xml: [(mock_signal, xml)]

        # Mock run_multiple_backtest result
        mock_ar0 = MagicMock()
        mock_ar0.name = "alpha_0"
        mock_ar0.is_metrics.sharpe = 1.5

        mock_ar1 = MagicMock()
        mock_ar1.name = "alpha_1"
        mock_ar1.is_metrics.sharpe = 2.3

        mock_result = MagicMock()
        mock_result.alpha_results = [mock_ar0, mock_ar1]
        mock_rmb.return_value = mock_result

        settings = MagicMock()
        settings.isStart = "2023-01-01"
        settings.isEnd = "2023-06-30"
        settings.osStart = "2023-07-01"
        settings.osEnd = "2023-12-31"
        data_cache = MagicMock()
        data_cache.initial_capital = 1000000

        eval_fn, _store = _make_evaluate_batch(settings, "D1", data_cache, n_jobs=1)
        results = eval_fn(["<xml_a/>", "<xml_b/>"])

        assert results[0] == 1.5
        assert results[1] == 2.3

    def test_empty_list_returns_empty(self):
        """evaluate_batch([]) returns []."""
        settings = MagicMock()
        data_cache = MagicMock()
        eval_fn, _store = _make_evaluate_batch(settings, "D1", data_cache, n_jobs=1)
        assert eval_fn([]) == []

    @patch("api.services.ga_service.compile_with_sources")
    def test_handles_compile_errors_gracefully(self, mock_compile):
        """Invalid XMLs in batch get NaN, valid ones are unaffected."""
        mock_compile.side_effect = Exception("compile error")

        settings = MagicMock()
        settings.isStart = "2023-01-01"
        settings.isEnd = "2023-06-30"
        settings.osStart = "2023-07-01"
        settings.osEnd = "2023-12-31"
        data_cache = MagicMock()
        data_cache.initial_capital = 1000000

        eval_fn, _store = _make_evaluate_batch(settings, "D1", data_cache, n_jobs=1)
        results = eval_fn(["<bad_xml/>", "<also_bad/>"])

        import math
        assert all(math.isnan(r) for r in results)


class TestRunGASearch:
    """Tests for run_ga_search high-level function."""

    @patch("api.services.ga_service._make_evaluate_batch")
    @patch("api.services.ga_service.run_ga")
    @patch("api.services.ga_service._build_response")
    def test_run_ga_search_response_shape(
        self, mock_build, mock_run_ga, mock_make_eval
    ):
        """Response has 'combined', 'alphas', 'ga' keys."""
        from api.services.ga_engine import GAResult, Individual

        # Setup mock evaluate_batch
        mock_make_eval.return_value = (lambda xml_list: [1.0] * len(xml_list), {})

        # Setup mock run_ga result
        mock_run_ga.return_value = GAResult(
            best_individuals=[
                Individual(genome=[0], xml='<data expr="close" delay="1"/>', fitness=1.0)
            ],
            generations_run=5,
            total_evaluations=50,
            search_space_size=10,
        )

        # Setup mock _build_response
        mock_build.return_value = {
            "combined": {"isMetrics": {}},
            "alphas": [{"name": "alpha_0", "xml": '<data expr="close" delay="1"/>'}],
            "ga": {
                "generationsRun": 5,
                "totalEvaluations": 50,
                "searchSpaceSize": 10,
            },
        }

        settings = MagicMock()
        settings.barInterval = "D1"
        data_cache = MagicMock()

        result = run_ga_search(
            xml_template='<data expr="close|open" delay="1"/>',
            settings=settings,
            data_cache=data_cache,
            ga_population=10,
            top_n=5,
        )

        assert "combined" in result
        assert "alphas" in result
        assert "ga" in result

    def test_no_variable_dimensions_raises(self):
        """Fixed XML template raises XmlCompileError."""
        settings = MagicMock()
        settings.barInterval = "D1"
        data_cache = MagicMock()

        with pytest.raises(XmlCompileError, match="No variable dimensions"):
            run_ga_search(
                xml_template='<data expr="close" delay="1"/>',
                settings=settings,
                data_cache=data_cache,
            )
