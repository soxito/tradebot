"""
Ngrok Tunnel API Routes

Provides start/stop/status/config endpoints for managing ngrok tunnels.
OAuth (Google) is always enforced and cannot be disabled.
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.models.database import NgrokConfig
from app.services.ngrok_service import ngrok_service

router = APIRouter(prefix="/ngrok", tags=["ngrok"])


# ── Request / Response schemas ─────────────────────────────────────────────────

class NgrokConfigUpdate(BaseModel):
    authtoken_override: Optional[str] = None
    backend_addr_override: Optional[str] = None
    frontend_addr_override: Optional[str] = None
    enable_on_start: Optional[bool] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _get_or_create_config(db: AsyncSession) -> NgrokConfig:
    row = (await db.execute(select(NgrokConfig).where(NgrokConfig.id == 1))).scalar_one_or_none()
    if row is None:
        row = NgrokConfig(id=1)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


def _effective_config(row: NgrokConfig) -> dict:
    """Merge DB overrides on top of env defaults."""
    authtoken = row.authtoken_override or settings.NGROK_AUTHTOKEN
    backend_addr = row.backend_addr_override or settings.NGROK_BACKEND_ADDR
    frontend_addr = row.frontend_addr_override or settings.NGROK_FRONTEND_ADDR
    enable_on_start = row.enable_on_start if row.enable_on_start is not None else settings.NGROK_AUTO_START
    return {
        "authtoken": authtoken,
        "backend_addr": backend_addr,
        "frontend_addr": frontend_addr,
        "enable_on_start": enable_on_start,
        "oauth_provider": settings.NGROK_OAUTH_PROVIDER,
        "oauth_enforced": True,
        "sources": {
            "authtoken": "db" if row.authtoken_override else "env",
            "backend_addr": "db" if row.backend_addr_override else "env",
            "frontend_addr": "db" if row.frontend_addr_override else "env",
            "enable_on_start": "db" if row.enable_on_start is not None else "env",
        },
    }


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/status")
async def get_status(db: AsyncSession = Depends(get_db)):
    """Return ngrok runtime state, tunnel URLs, and effective config."""
    row = await _get_or_create_config(db)
    eff = _effective_config(row)
    runtime = ngrok_service.status()
    return {**runtime, "config": eff}


@router.post("/start")
async def start_tunnels(db: AsyncSession = Depends(get_db)):
    """Start backend and frontend ngrok tunnels with Google OAuth enforced."""
    row = await _get_or_create_config(db)
    eff = _effective_config(row)

    if not eff["authtoken"]:
        raise HTTPException(
            status_code=400,
            detail="NGROK_AUTHTOKEN is not configured. Set it in .env or save it via /ngrok/config.",
        )

    current = ngrok_service.status()
    if current["state"] == "running":
        raise HTTPException(status_code=409, detail="ngrok tunnels are already running")

    try:
        result = await ngrok_service.start(
            authtoken=eff["authtoken"],
            backend_addr=eff["backend_addr"],
            frontend_addr=eff["frontend_addr"],
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return result


@router.post("/stop")
async def stop_tunnels():
    """Stop all active ngrok tunnels."""
    current = ngrok_service.status()
    if current["state"] not in ("running", "error"):
        raise HTTPException(status_code=409, detail="ngrok tunnels are not running")
    return await ngrok_service.stop()


@router.post("/restart")
async def restart_tunnels(db: AsyncSession = Depends(get_db)):
    """Stop then re-start ngrok tunnels (picks up latest config)."""
    await ngrok_service.stop()
    return await start_tunnels(db)


@router.get("/config")
async def get_config(db: AsyncSession = Depends(get_db)):
    """Return the effective merged config (env defaults + DB overrides)."""
    row = await _get_or_create_config(db)
    return _effective_config(row)


@router.patch("/config")
async def update_config(
    payload: NgrokConfigUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Persist DB-level overrides for ngrok config. OAuth provider cannot be changed."""
    row = await _get_or_create_config(db)

    if payload.authtoken_override is not None:
        row.authtoken_override = payload.authtoken_override or None
    if payload.backend_addr_override is not None:
        row.backend_addr_override = payload.backend_addr_override or None
    if payload.frontend_addr_override is not None:
        row.frontend_addr_override = payload.frontend_addr_override or None
    if payload.enable_on_start is not None:
        row.enable_on_start = payload.enable_on_start

    await db.commit()
    await db.refresh(row)
    return _effective_config(row)
