"""OBLIVION MASTER v5 — global engine settings.

``GET /oblivion/settings`` returns the current values, the defaults and the
field spec the popup renders from; ``PUT`` merges a partial patch (validated
against the spec); ``POST /reset`` restores the defaults. Every consumer of
the engine — room agents, MT5 bots, Kronos, Telegram, chart overlays — reads
the same cached row, so a save here changes all of them at once.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.services import oblivion_settings

router = APIRouter(prefix="/oblivion", tags=["oblivion"])


class SettingsPatch(BaseModel):
    settings: Dict[str, Any] = Field(default_factory=dict)


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    return await oblivion_settings.load(db)


@router.put("/settings")
async def put_settings(patch: SettingsPatch, db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    try:
        return await oblivion_settings.update(db, patch.settings)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/settings/reset")
async def reset_settings(db: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    return await oblivion_settings.reset(db)


@router.get("/settings/version")
async def settings_version() -> Dict[str, Any]:
    """Cheap poll target for charts: bumps on every save."""
    return {"version": oblivion_settings.current_version()}
