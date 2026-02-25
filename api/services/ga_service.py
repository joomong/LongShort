"""GA service: wires GA engine to backtest evaluation."""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Tuple

from api.models.schemas import BacktestSettings
from api.services.backtest_service import resolve_mode, run_backtest
from api.services.ga_engine import (
    GAConfig, GAResult, extract_search_space, run_ga,
)
from framework.exceptions import XmlCompileError
from api.xml_alpha_sources import compile_with_sources
from framework.backtester import (
    PeriodSettings, run_backtest as run_framework_backtest,
)
from framework.data import DataCache

logger = logging.getLogger(__name__)


def _make_evaluate_batch(
    settings: BacktestSettings,
    mode: str,
    data_cache: DataCache,
    n_jobs: int,
) -> Tuple[Callable[[List[str]], List[float]], Dict[str, dict]]:
    """Create the evaluate_batch callable for the GA engine.

    Returns (evaluate_batch, result_store) where result_store caches
    per-alpha backtest dicts keyed by xml_hash for streaming.
    """
    result_store: Dict[str, dict] = {}

    def evaluate_batch(xml_list: List[str]) -> List[float]:
        if not xml_list:
            return []

        # Compile all XMLs to Signals
        compiled = []
        xml_index_map = []  # compiled_idx -> original xml_list index
        for i, xml in enumerate(xml_list):
            try:
                pairs = compile_with_sources(xml)
                for signal, _src in pairs:
                    compiled.append(signal)
                    xml_index_map.append(i)
            except XmlCompileError:
                # Invalid XMLs are intentionally skipped and remain NaN fitness.
                continue
            except Exception as exc:
                logger.warning("Unexpected compile failure at batch index %d: %s", i, exc)
                continue

        if not compiled:
            return [float('nan')] * len(xml_list)

        period_settings = PeriodSettings(
            is_start=settings.isStart,
            is_end=settings.isEnd,
            os_start=settings.osStart,
            os_end=settings.osEnd,
        )

        effective_jobs = n_jobs if n_jobs > 0 else -1

        multi_result = run_framework_backtest(
            alphas=compiled,
            settings=period_settings,
            capital=data_cache.initial_capital,
            data_cache=data_cache,
            mode=mode,
            n_jobs=effective_jobs,
        )

        results = [float('nan')] * len(xml_list)
        for ar in multi_result.alpha_results:
            try:
                source_idx = getattr(ar, "source_idx", -1)
                if isinstance(source_idx, int) and source_idx >= 0:
                    compiled_idx = source_idx
                else:
                    compiled_idx = int(ar.name.split("_")[1])
                orig_idx = xml_index_map[compiled_idx]
                results[orig_idx] = ar.is_period.metrics.sharpe
                # Cache per-alpha dict for streaming
                from api.services.backtest_service import _serialize_alpha_result
                alpha_dict = _serialize_alpha_result(ar, data_cache.initial_capital)
                from api.services.ga_engine import xml_hash as _xml_hash
                h = _xml_hash(xml_list[orig_idx])
                result_store[h] = alpha_dict
            except (IndexError, ValueError):
                pass

        return results

    return evaluate_batch, result_store


def run_ga_search(
    xml_template: str,
    settings: BacktestSettings,
    data_cache: DataCache,
    ga_population: int = 50,
    n_jobs: int = 5,
    top_n: int = 10,
) -> Dict[str, Any]:
    """High-level GA search entry point."""
    mode = resolve_mode(settings.barInterval)
    space = extract_search_space(xml_template)
    config = GAConfig.from_frontend(ga_population, top_n=top_n)

    evaluate_batch, _result_store = _make_evaluate_batch(settings, mode, data_cache, n_jobs)
    ga_result = run_ga(space, config, evaluate_batch)

    return _build_response(ga_result, settings, mode, data_cache)


def _build_response(
    ga_result: GAResult,
    settings: BacktestSettings,
    mode: str,
    data_cache: DataCache,
) -> Dict[str, Any]:
    """Build the API response for GA results."""
    top_xmls = [ind.xml for ind in ga_result.best_individuals]

    compiled_with_xml = []
    for xml in top_xmls:
        try:
            pairs = compile_with_sources(xml)
            compiled_with_xml.extend(pairs)
        except XmlCompileError:
            continue
        except Exception as exc:
            logger.warning("Unexpected compile failure while building GA response: %s", exc)
            continue

    compiled = [c for c, _x in compiled_with_xml]
    compiled_xmls = [_x for _c, _x in compiled_with_xml]

    if not compiled:
        return {
            "combined": None,
            "alphas": [],
            "initialCapital": data_cache.initial_capital,
            "sortedBy": "sharpe",
            "ga": {
                "generationsRun": ga_result.generations_run,
                "totalEvaluations": ga_result.total_evaluations,
                "searchSpaceSize": ga_result.search_space_size,
            },
        }

    response = run_backtest(compiled, compiled_xmls, settings, mode, data_cache)

    response["ga"] = {
        "generationsRun": ga_result.generations_run,
        "totalEvaluations": ga_result.total_evaluations,
        "searchSpaceSize": ga_result.search_space_size,
    }
    response["sortedBy"] = "sharpe"
    return response
