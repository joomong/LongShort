"""Data router -- GET /api/symbols, GET /api/data-range."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Request

from api.models.schemas import DataRangeResponse

router = APIRouter()


@router.get("/api/symbols")
def get_symbols(request: Request) -> List[str]:
    cache = request.app.state.data_cache
    return cache.symbols or []


@router.get("/api/data-range")
def get_data_range(request: Request) -> DataRangeResponse:
    cache = request.app.state.data_cache
    timestamps = cache.get_timestamps()
    min_date = str(timestamps[0].date()) if len(timestamps) > 0 else cache.start_date
    max_date = str(timestamps[-1].date()) if len(timestamps) > 0 else cache.end_date
    return DataRangeResponse(
        minDate=min_date,
        maxDate=max_date,
        transactionFeeRate=cache.transaction_fee_rate,
    )
