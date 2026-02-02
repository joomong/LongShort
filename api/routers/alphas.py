"""Alphas router -- CRUD for saved alpha XML files."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from api.models.schemas import (
    AlphaDetailResponse,
    AlphaListResponse,
    SaveAlphaRequest,
    SaveAlphaResponse,
)
from api.services.alpha_service import get_alpha, list_alphas, save_alpha
from api.xml_alpha import XmlCompileError

router = APIRouter()


@router.post("/api/alphas")
def create_alpha(req: SaveAlphaRequest) -> SaveAlphaResponse:
    try:
        save_alpha(req.name, req.xml)
    except XmlCompileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return SaveAlphaResponse(success=True, name=req.name)


@router.get("/api/alphas")
def list_saved_alphas() -> AlphaListResponse:
    return AlphaListResponse(alphas=list_alphas())


@router.get("/api/alphas/{name}")
def get_saved_alpha(name: str) -> AlphaDetailResponse:
    alpha = get_alpha(name)
    if alpha is None:
        raise HTTPException(status_code=404, detail=f"Alpha '{name}' not found")
    return alpha
