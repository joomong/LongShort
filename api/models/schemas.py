"""Pydantic request/response models for the LongShort API."""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal

from pydantic import BaseModel, Field, model_validator

VALID_NAME_PATTERN = r"^[A-Za-z0-9_]+$"


class BacktestSettings(BaseModel):
    osStart: str
    osEnd: str
    isStart: str
    isEnd: str
    barInterval: Literal['D0', 'D1', 'intraday'] = 'D1'
    nJobs: int = 5
    gaPopulation: int = 50

    @model_validator(mode="after")
    def validate_date_order(self) -> "BacktestSettings":
        fmt = "%Y-%m-%d"
        try:
            is_start = datetime.strptime(self.isStart, fmt)
            is_end = datetime.strptime(self.isEnd, fmt)
            os_start = datetime.strptime(self.osStart, fmt)
            os_end = datetime.strptime(self.osEnd, fmt)
        except ValueError as e:
            raise ValueError(f"Date format must be YYYY-MM-DD: {e}")

        if is_start > is_end:
            raise ValueError(
                f"isStart ({self.isStart}) must be <= isEnd ({self.isEnd})"
            )
        if os_start > os_end:
            raise ValueError(
                f"osStart ({self.osStart}) must be <= osEnd ({self.osEnd})"
            )
        if is_end >= os_start:
            raise ValueError(
                f"IS period must end before OS period starts: "
                f"isEnd ({self.isEnd}) >= osStart ({self.osStart})"
            )
        return self


class BacktestRunRequest(BaseModel):
    xml: str
    settings: BacktestSettings


# ---------------------------------------------------------------------------
# Data range
# ---------------------------------------------------------------------------

class DataRangeResponse(BaseModel):
    minDate: str
    maxDate: str
    transactionFeeRate: float = 0.0010


# ---------------------------------------------------------------------------
# Alpha CRUD
# ---------------------------------------------------------------------------

class SaveAlphaRequest(BaseModel):
    name: str = Field(..., pattern=VALID_NAME_PATTERN)
    xml: str


class SaveAlphaResponse(BaseModel):
    success: bool
    name: str


class AlphaInfo(BaseModel):
    name: str
    createdAt: str


class AlphaListResponse(BaseModel):
    alphas: List[AlphaInfo]


class AlphaDetailResponse(BaseModel):
    name: str
    xml: str
    createdAt: str


# ---------------------------------------------------------------------------
# GA
# ---------------------------------------------------------------------------

class GARunRequest(BaseModel):
    xml: str
    settings: BacktestSettings
    topN: int = Field(default=10, ge=1, le=100)
