"""Backtest router -- POST /api/backtest/run."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from api.models.schemas import BacktestRunRequest
from api.services.backtest_service import resolve_mode, run_backtest
from api.xml_alpha_sources import compile_with_sources
from api.xml_alpha import XmlCompileError

router = APIRouter()


@router.post("/api/backtest/run")
def run_backtest_endpoint(req: BacktestRunRequest, request: Request) -> Dict[str, Any]:
    try:
        mode = resolve_mode(req.settings.barInterval)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        compiled_with_xml = compile_with_sources(req.xml)
    except XmlCompileError as e:
        raise HTTPException(status_code=400, detail=str(e))

    data_cache = request.app.state.data_cache

    compiled = [c for (c, _x) in compiled_with_xml]
    compiled_xmls = [_x for (_c, _x) in compiled_with_xml]

    try:
        return run_backtest(compiled, compiled_xmls, req.settings, mode, data_cache)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
