"""Monitoring and alerts endpoints."""
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from app.core.config import settings
from app.monitoring.alerts import AlertService
from app.monitoring.metrics import metrics_payload


router = APIRouter(prefix="/monitoring", tags=["monitoring"])


class TestAlertRequest(BaseModel):
    title: str = "TradeBot test alert"
    message: str = "Monitoring pipeline check"
    level: str = "WARNING"


@router.get("/status")
async def monitoring_status():
    return {
        "alerts_enabled": settings.ALERTS_ENABLED,
        "prometheus_enabled": settings.PROMETHEUS_ENABLED,
        "telegram_configured": bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID),
        "discord_configured": bool(settings.DISCORD_WEBHOOK_URL),
        "log_level": settings.LOG_LEVEL,
        "log_json": settings.LOG_JSON,
        "log_file_path": settings.LOG_FILE_PATH,
    }


@router.get("/metrics")
async def prometheus_metrics():
    if not settings.PROMETHEUS_ENABLED:
        raise HTTPException(status_code=404, detail="Prometheus metrics disabled")
    payload, content_type = metrics_payload()
    return Response(content=payload, media_type=content_type)


@router.post("/test-alert")
async def test_alert(req: TestAlertRequest):
    result = await AlertService.notify(req.title, req.message, req.level)
    return {"sent": result}
