"""GA search router -- POST /api/ga/run, POST /api/ga/run-stream."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from api.models.schemas import GARunRequest
from api.services.backtest_service import resolve_mode, run_backtest
from api.services.ga_engine import GAConfig, extract_search_space, run_ga
from api.services.ga_service import _make_evaluate_batch, _build_response, run_ga_search
from api.xml_alpha_sources import compile_with_sources
from framework.exceptions import XmlCompileError

router = APIRouter()


@router.post("/api/ga/run")
def run_ga_endpoint(req: GARunRequest, request: Request) -> Dict[str, Any]:
    """Run GA parameter search on an XML template."""
    data_cache = request.app.state.data_cache

    try:
        result = run_ga_search(
            xml_template=req.xml,
            settings=req.settings,
            data_cache=data_cache,
            ga_population=req.settings.gaPopulation,
            n_jobs=req.settings.nJobs,
            top_n=req.topN,
        )
    except XmlCompileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return result


@router.post("/api/ga/run-stream")
async def run_ga_stream_endpoint(req: GARunRequest, request: Request):
    """Stream GA progress as NDJSON."""
    data_cache = request.app.state.data_cache
    q: queue.Queue = queue.Queue()

    def worker():
        try:
            mode = resolve_mode(req.settings.barInterval)
            try:
                space = extract_search_space(req.xml)
            except XmlCompileError:
                # No variable dimensions → run regular backtest
                compiled_with_xml = compile_with_sources(req.xml)
                compiled = [c for c, _x in compiled_with_xml]
                compiled_xmls = [_x for _c, _x in compiled_with_xml]
                final = run_backtest(compiled, compiled_xmls, req.settings, mode, data_cache)
                q.put(json.dumps({"type": "complete", "data": final}) + "\n")
                return
            config = GAConfig.from_frontend(req.settings.gaPopulation, top_n=req.topN)
            evaluate_batch, result_store = _make_evaluate_batch(
                req.settings, mode, data_cache, req.settings.nJobs
            )

            def on_progress(event):
                if event["type"] == "generation_end":
                    top_inds = event.pop("topIndividuals", [])
                    partial_alphas = []
                    for ind in top_inds:
                        cached = result_store.get(ind.xml_hash)
                        if cached:
                            partial_alphas.append({**cached, "xml": ind.xml})
                    event["partialResult"] = {
                        "combined": None,
                        "alphas": partial_alphas,
                        "initialCapital": data_cache.initial_capital,
                        "sortedBy": "sharpe",
                        "heatmap": None,
                        "signalRange": None,
                        "distribution": None,
                    }
                q.put(json.dumps(event) + "\n")

            ga_result = run_ga(space, config, evaluate_batch, on_progress)
            final = _build_response(ga_result, req.settings, mode, data_cache)
            q.put(json.dumps({"type": "complete", "data": final}) + "\n")
        except Exception as e:
            q.put(json.dumps({"type": "error", "message": str(e)}) + "\n")
        finally:
            q.put(None)  # sentinel

    threading.Thread(target=worker, daemon=True).start()

    async def stream():
        while True:
            line = await asyncio.to_thread(q.get)
            if line is None:
                break
            yield line

    return StreamingResponse(stream(), media_type="application/x-ndjson")
