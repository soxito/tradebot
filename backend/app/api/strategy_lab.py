"""Strategy Lab API routes.

MVP endpoints for managing strategy versions, execution runs, and promotions.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.database import (
    StrategyLabPromotion,
    StrategyLabPromotionTarget,
    StrategyLabRun,
    StrategyLabVersion,
    StrategyRunStatus,
)

router = APIRouter(prefix="/strategy-lab", tags=["strategy-lab"])


def _loads_json(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _version_to_dict(version: StrategyLabVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "name": version.name,
        "description": version.description,
        "timeframe": version.timeframe,
        "pairs": _loads_json(version.pairs, []),
        "indicators": _loads_json(version.indicators, []),
        "parameters": _loads_json(version.parameters, {}),
        "risk_constraints": _loads_json(version.risk_constraints, {}),
        "is_active": version.is_active,
        "created_by": version.created_by,
        "updated_by": version.updated_by,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "updated_at": version.updated_at.isoformat() if version.updated_at else None,
    }


def _run_to_dict(run: StrategyLabRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "version_id": run.version_id,
        "run_mode": run.run_mode,
        "status": run.status.value if run.status else None,
        "metrics": _loads_json(run.metrics, {}),
        "notes": run.notes,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_by": run.created_by,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }


def _promotion_to_dict(promotion: StrategyLabPromotion) -> dict[str, Any]:
    return {
        "id": promotion.id,
        "version_id": promotion.version_id,
        "target": promotion.target.value if promotion.target else None,
        "approved_by": promotion.approved_by,
        "reason": promotion.reason,
        "metadata": _loads_json(promotion.metadata_json, {}),
        "created_at": promotion.created_at.isoformat() if promotion.created_at else None,
    }


class StrategyVersionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = ""
    timeframe: str = Field(default="1h", max_length=20)
    pairs: list[str] = Field(default_factory=lambda: ["BTC/USDT"])
    indicators: list[dict[str, Any]] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    risk_constraints: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True
    created_by: Optional[str] = None


class StrategyVersionUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = None
    timeframe: Optional[str] = Field(default=None, max_length=20)
    pairs: Optional[list[str]] = None
    indicators: Optional[list[dict[str, Any]]] = None
    parameters: Optional[dict[str, Any]] = None
    risk_constraints: Optional[dict[str, Any]] = None
    is_active: Optional[bool] = None
    updated_by: Optional[str] = None


class StrategyRunCreate(BaseModel):
    run_mode: str = Field(default="simulation", max_length=30)
    metrics: dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None
    created_by: Optional[str] = None


class StrategyRunUpdate(BaseModel):
    status: Optional[StrategyRunStatus] = None
    metrics: Optional[dict[str, Any]] = None
    notes: Optional[str] = None


class StrategyPromotionCreate(BaseModel):
    target: StrategyLabPromotionTarget = StrategyLabPromotionTarget.SIMULATION
    approved_by: Optional[str] = None
    reason: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.get("/versions")
async def list_strategy_versions(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(StrategyLabVersion).order_by(desc(StrategyLabVersion.updated_at))
    )
    return [_version_to_dict(item) for item in result.scalars().all()]


@router.post("/versions")
async def create_strategy_version(
    payload: StrategyVersionCreate,
    db: AsyncSession = Depends(get_db),
):
    version = StrategyLabVersion(
        name=payload.name,
        description=payload.description,
        timeframe=payload.timeframe,
        pairs=json.dumps(payload.pairs),
        indicators=json.dumps(payload.indicators),
        parameters=json.dumps(payload.parameters),
        risk_constraints=json.dumps(payload.risk_constraints),
        is_active=payload.is_active,
        created_by=payload.created_by,
        updated_by=payload.created_by,
    )
    db.add(version)
    await db.commit()
    await db.refresh(version)
    return _version_to_dict(version)


@router.get("/versions/{version_id}")
async def get_strategy_version(version_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(StrategyLabVersion).where(StrategyLabVersion.id == version_id)
    )
    version = result.scalar_one_or_none()
    if not version:
        raise HTTPException(status_code=404, detail="Strategy version not found")
    return _version_to_dict(version)


@router.patch("/versions/{version_id}")
async def update_strategy_version(
    version_id: int,
    payload: StrategyVersionUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(StrategyLabVersion).where(StrategyLabVersion.id == version_id)
    )
    version = result.scalar_one_or_none()
    if not version:
        raise HTTPException(status_code=404, detail="Strategy version not found")

    updates = payload.model_dump(exclude_unset=True)

    if "name" in updates:
        version.name = updates["name"]
    if "description" in updates:
        version.description = updates["description"]
    if "timeframe" in updates:
        version.timeframe = updates["timeframe"]
    if "pairs" in updates:
        version.pairs = json.dumps(updates["pairs"])
    if "indicators" in updates:
        version.indicators = json.dumps(updates["indicators"])
    if "parameters" in updates:
        version.parameters = json.dumps(updates["parameters"])
    if "risk_constraints" in updates:
        version.risk_constraints = json.dumps(updates["risk_constraints"])
    if "is_active" in updates:
        version.is_active = updates["is_active"]
    if "updated_by" in updates:
        version.updated_by = updates["updated_by"]

    await db.commit()
    await db.refresh(version)
    return _version_to_dict(version)


@router.post("/versions/{version_id}/runs")
async def create_strategy_run(
    version_id: int,
    payload: StrategyRunCreate,
    db: AsyncSession = Depends(get_db),
):
    version_result = await db.execute(
        select(StrategyLabVersion).where(StrategyLabVersion.id == version_id)
    )
    if not version_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Strategy version not found")

    run = StrategyLabRun(
        version_id=version_id,
        run_mode=payload.run_mode,
        status=StrategyRunStatus.QUEUED,
        metrics=json.dumps(payload.metrics),
        notes=payload.notes,
        created_by=payload.created_by,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return _run_to_dict(run)


@router.get("/runs")
async def list_strategy_runs(
    version_id: Optional[int] = Query(default=None),
    status: Optional[StrategyRunStatus] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    query = select(StrategyLabRun)
    if version_id is not None:
        query = query.where(StrategyLabRun.version_id == version_id)
    if status is not None:
        query = query.where(StrategyLabRun.status == status)

    result = await db.execute(query.order_by(desc(StrategyLabRun.created_at)).limit(limit))
    return [_run_to_dict(item) for item in result.scalars().all()]


@router.patch("/runs/{run_id}")
async def update_strategy_run(
    run_id: int,
    payload: StrategyRunUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(StrategyLabRun).where(StrategyLabRun.id == run_id))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Strategy run not found")

    updates = payload.model_dump(exclude_unset=True)
    if "status" in updates:
        run.status = updates["status"]
        if updates["status"] in (StrategyRunStatus.COMPLETED, StrategyRunStatus.FAILED):
            run.finished_at = run.finished_at or run.updated_at or run.started_at
    if "metrics" in updates:
        run.metrics = json.dumps(updates["metrics"])
    if "notes" in updates:
        run.notes = updates["notes"]

    await db.commit()
    await db.refresh(run)
    return _run_to_dict(run)


@router.post("/versions/{version_id}/promotions")
async def promote_strategy_version(
    version_id: int,
    payload: StrategyPromotionCreate,
    db: AsyncSession = Depends(get_db),
):
    version_result = await db.execute(
        select(StrategyLabVersion).where(StrategyLabVersion.id == version_id)
    )
    if not version_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Strategy version not found")

    promotion = StrategyLabPromotion(
        version_id=version_id,
        target=payload.target,
        approved_by=payload.approved_by,
        reason=payload.reason,
        metadata_json=json.dumps(payload.metadata),
    )
    db.add(promotion)
    await db.commit()
    await db.refresh(promotion)

    return _promotion_to_dict(promotion)


@router.get("/promotions")
async def list_promotions(
    version_id: Optional[int] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    query = select(StrategyLabPromotion)
    if version_id is not None:
        query = query.where(StrategyLabPromotion.version_id == version_id)

    result = await db.execute(query.order_by(desc(StrategyLabPromotion.created_at)).limit(limit))
    return [_promotion_to_dict(item) for item in result.scalars().all()]
