"""LongShort API -- FastAPI application entry point.

This module creates the FastAPI app, configures CORS, and registers routers.
All business logic lives in api.services and Pydantic models in api.models.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from framework.data import DataCache

from api.routers import alphas, backtest, data, ga

# ---------------------------------------------------------------------------
# Lifespan: load DataCache at startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: load DataCache
    cache = DataCache(config_path="config.yaml")
    cache.load()
    app.state.data_cache = cache
    yield
    # Shutdown: nothing to clean up

# ---------------------------------------------------------------------------
# App creation
# ---------------------------------------------------------------------------

app = FastAPI(title="LongShort API", version="0.1.0", lifespan=lifespan)

# Allow local frontend dev servers (localhost/127.0.0.1 on any port).
extra_origins = [
    o.strip()
    for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",")
    if o.strip()
]
allow_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    *extra_origins,
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------

app.include_router(data.router)
app.include_router(backtest.router)
app.include_router(alphas.router)
app.include_router(ga.router)
