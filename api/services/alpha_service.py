"""Business logic for alpha CRUD operations."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from api.models.schemas import AlphaDetailResponse, AlphaInfo, VALID_NAME_PATTERN
from api.xml_alpha import compile, XmlCompileError

SAVED_ALPHAS_DIR = Path(__file__).resolve().parent.parent.parent / "saved_alphas"

_VALID_NAME_RE = re.compile(VALID_NAME_PATTERN)


def _ensure_dir() -> None:
    """Create the saved_alphas directory if it doesn't exist."""
    SAVED_ALPHAS_DIR.mkdir(parents=True, exist_ok=True)


def save_alpha(name: str, xml: str) -> None:
    """Save alpha XML to disk after validating it compiles.

    Raises XmlCompileError if the XML is invalid.
    """
    # Validate that XML compiles successfully
    compile(xml)

    _ensure_dir()
    path = SAVED_ALPHAS_DIR / f"{name}.xml"
    path.write_text(xml, encoding="utf-8")


def list_alphas() -> List[AlphaInfo]:
    """List all saved alphas sorted by name."""
    _ensure_dir()
    alphas: List[AlphaInfo] = []
    for f in sorted(SAVED_ALPHAS_DIR.glob("*.xml")):
        mtime = os.path.getmtime(f)
        created_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        alphas.append(AlphaInfo(name=f.stem, createdAt=created_at))
    return alphas


def get_alpha(name: str) -> Optional[AlphaDetailResponse]:
    """Get a specific alpha by name. Returns None if not found."""
    if not _VALID_NAME_RE.fullmatch(name):
        return None
    path = SAVED_ALPHAS_DIR / f"{name}.xml"
    if not path.is_file():
        return None
    xml = path.read_text(encoding="utf-8")
    mtime = os.path.getmtime(path)
    created_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    return AlphaDetailResponse(name=name, xml=xml, createdAt=created_at)
