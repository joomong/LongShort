"""Data router -- GET /api/symbols, GET /api/data-range."""

from __future__ import annotations

from typing import List

import pandas as pd
from fastapi import APIRouter, Request

from api.models.schemas import DataRangeResponse

router = APIRouter()


@router.get("/api/symbols")
def get_symbols(request: Request) -> List[str]:
    cache = request.app.state.data_cache
    if cache.symbols is not None:
        return cache.symbols.tolist()
    return []


@router.get("/api/data-range")
def get_data_range(request: Request) -> DataRangeResponse:
    cache = request.app.state.data_cache
    timestamps = cache.timestamps
    if timestamps is not None and len(timestamps) > 0:
        min_date = str(pd.Timestamp(timestamps[0]).date())
        max_date = str(pd.Timestamp(timestamps[-1]).date())
    else:
        min_date = cache.start_date
        max_date = cache.end_date
    return DataRangeResponse(
        minDate=min_date,
        maxDate=max_date,
        transactionFeeRate=cache.transaction_fee_rate,
    )
