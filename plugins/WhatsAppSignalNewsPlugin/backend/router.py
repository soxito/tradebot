"""WhatsApp Signal & News Plugin API router."""
from __future__ import annotations

import asyncio
import json
import hmac
import hashlib
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Header
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.exchanges.manager import exchange_manager, SupportedExchange
from plugins.WhatsAppSignalNewsPlugin.backend.config import (
    WhatsAppPluginConfig,
    build_config_from_db,
    whatsapp_plugin_config,
)
from plugins.WhatsAppSignalNewsPlugin.backend.schemas import (
    WhatsAppApplyPresetRequest,
    WhatsAppApplyPresetResponse,
    WhatsAppAuthStatusResponse,
    WhatsAppChannelKind,
    WhatsAppChannelPresetCreate,
    WhatsAppChannelPresetResponse,
    WhatsAppChannelPresetUpdate,
    WhatsAppChannelSourceCreate,
    WhatsAppChannelSourceResponse,
    WhatsAppChannelSourceUpdate,
    WhatsAppDiscoveredChatResponse,
    WhatsAppMessageResponse,
    WhatsAppMonitorStatusResponse,
    WhatsAppParsedSignalResponse,
    WhatsAppPluginSettingsResponse,
    WhatsAppPluginSettingsUpdate,
    WhatsAppPollRequest,
    WhatsAppPollResult,
    WhatsAppPreviewRequest,
    WhatsAppPricesResponse,
    WhatsAppQRResponse,
    WhatsAppSniperSettingsResponse,
    WhatsAppSniperSettingsUpdate,
    WhatsAppSniperTradeResponse,
    WhatsAppSourceType,
    WhatsAppSubscribedChatsResponse,
    WhatsAppTestConnectionResponse,
    WhatsAppTestProviderResult,
    WhatsAppSessionCreateRequest,
    WhatsAppSessionResponse,
)
from plugins.WhatsAppSignalNewsPlugin.backend.services.ingest_service import (
    create_channel_from_preset,
    discover_chats,
    ensure_default_session,
    get_session_status,
    process_webhook_payload,
    run_poll_all_channels,
)
from plugins.WhatsAppSignalNewsPlugin.backend.services.monitor_service import (
    reconcile_active_signals,
    signal_monitor,
)
from plugins.WhatsAppSignalNewsPlugin.backend.services.openwa_client import (
    OpenWAClient,
    OpenWAClientManager,
)
from plugins.WhatsAppSignalNewsPlugin.backend.services.sniper_service import (
    analyze_signal_full,
    auto_close_positions_for_signal,
    execute_parsed_signal,
    execute_sniper_trade,
    get_signal_prices,
    reanalyze_skipped_signals,
    run_sniper_cycle,
    sniper_service,
    volume_monitor_snapshot,
)
from plugins.WhatsAppSignalNewsPlugin.backend.models import (
    WhatsAppChannelPreset,
    WhatsAppChannelSource,
    WhatsAppMessage,
    WhatsAppParsedSignal,
    WhatsAppPluginSettings,
    WhatsAppSession,
    WhatsAppSniperSettings,
    WhatsAppSniperTrade,
    SignalStatus,
    SniperTradeStatus,
    WhatsAppChannelKind,
    WhatsAppSourceType,
)
from plugins.WhatsAppSignalNewsPlugin.backend.timezone_utils import now_utc_naive


router = APIRouter(prefix="/plugins/whatsapp", tags=["WhatsApp Signal & News"])
client_manager = OpenWAClientManager()


# ────────────────────────────────────────────────────────────────────
# Database Dependency
# ────────────────────────────────────────────────────────────────────

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def get_client(config: Optional[WhatsAppPluginConfig] = None) -> OpenWAClient:
    """Get OpenWA client instance."""
    return client_manager.get_client(config)


async def _load_config(db: AsyncSession) -> WhatsAppPluginConfig:
    """Load plugin config from DB (merged over env)."""
    result = await db.execute(select(WhatsAppPluginSettings).limit(1))
    settings = result.scalars().first()
    return build_config_from_db(settings)


# ────────────────────────────────────────────────────────────────────
# Plugin Settings
# ────────────────────────────────────────────────────────────────────

@router.get("/settings", response_model=WhatsAppPluginSettingsResponse)
async def get_plugin_settings(db: AsyncSession = Depends(get_db)):
    """Get plugin settings (safe - no secrets)."""
    result = await db.execute(select(WhatsAppPluginSettings).limit(1))
    s = result.scalars().first()
    if not s:
        return WhatsAppPluginSettingsResponse()

    return WhatsAppPluginSettingsResponse(
        openwa_base_url=s.openwa_base_url,
        openwa_api_key_set=bool(s.openwa_api_key),
        default_session_name=s.default_session_name,
        webhook_secret_set=bool(s.webhook_secret),
        poll_interval_seconds=s.poll_interval_seconds,
        session_health_check_seconds=s.session_health_check_seconds,
        enable_llm_fallback=s.enable_llm_fallback,
        llm_model=s.llm_model,
        max_messages_per_poll=s.max_messages_per_poll,
        message_dedupe_ttl_hours=s.message_dedupe_ttl_hours,
        sniper_enabled_default=s.sniper_enabled_default,
        sniper_mode_default=s.sniper_mode_default,
        sniper_position_size_usdt_default=s.sniper_position_size_usdt_default,
        sniper_max_positions_default=s.sniper_max_positions_default,
        sniper_min_confidence_default=s.sniper_min_confidence_default,
        sniper_min_risk_reward_default=s.sniper_min_risk_reward_default,
    )


@router.put("/settings", response_model=WhatsAppPluginSettingsResponse)
async def update_plugin_settings(
    payload: WhatsAppPluginSettingsUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update plugin settings."""
    result = await db.execute(select(WhatsAppPluginSettings).limit(1))
    s = result.scalars().first()
    if not s:
        s = WhatsAppPluginSettings()
        db.add(s)

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if hasattr(s, key):
            setattr(s, key, value)

    s.updated_at = now_utc_naive()
    await db.commit()
    await db.refresh(s)

    return WhatsAppPluginSettingsResponse(
        openwa_base_url=s.openwa_base_url,
        openwa_api_key_set=bool(s.openwa_api_key),
        default_session_name=s.default_session_name,
        webhook_secret_set=bool(s.webhook_secret),
        poll_interval_seconds=s.poll_interval_seconds,
        session_health_check_seconds=s.session_health_check_seconds,
        enable_llm_fallback=s.enable_llm_fallback,
        llm_model=s.llm_model,
        max_messages_per_poll=s.max_messages_per_poll,
        message_dedupe_ttl_hours=s.message_dedupe_ttl_hours,
        sniper_enabled_default=s.sniper_enabled_default,
        sniper_mode_default=s.sniper_mode_default,
        sniper_position_size_usdt_default=s.sniper_position_size_usdt_default,
        sniper_max_positions_default=s.sniper_max_positions_default,
        sniper_min_confidence_default=s.sniper_min_confidence_default,
        sniper_min_risk_reward_default=s.sniper_min_risk_reward_default,
    )


# ────────────────────────────────────────────────────────────────────
# Connection Test
# ────────────────────────────────────────────────────────────────────

@router.post("/test-connection", response_model=WhatsAppTestConnectionResponse)
async def test_connection(
    db: AsyncSession = Depends(get_db),
    client: OpenWAClient = Depends(get_client),
):
    """Test connection to OpenWA Gateway."""
    config = await _load_config(db)
    client = get_client(config)

    results = []
    try:
        health = await client.health_check()
        results.append(WhatsAppTestProviderResult(
            provider="openwa_gateway",
            ok=health.get("status") == "healthy",
            details=health,
        ))
    except Exception as e:
        results.append(WhatsAppTestProviderResult(
            provider="openwa_gateway",
            ok=False,
            error=str(e),
        ))

    # Test exchange connector
    connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
    if connector:
        try:
            ticker = await connector.get_ticker("BTC/USDT")
            results.append(WhatsAppTestProviderResult(
                provider="bitget",
                ok=bool(ticker),
                details={"last_price": ticker.get("last")} if ticker else None,
            ))
        except Exception as e:
            results.append(WhatsAppTestProviderResult(
                provider="bitget",
                ok=False,
                error=str(e),
            ))

    return WhatsAppTestConnectionResponse(
        results=results,
        any_ok=any(r.ok for r in results),
    )


# ────────────────────────────────────────────────────────────────────
# Session Management
# ────────────────────────────────────────────────────────────────────

@router.post("/session/create", response_model=WhatsAppSessionResponse)
async def create_session(
    payload: WhatsAppSessionCreateRequest,
    db: AsyncSession = Depends(get_db),
    client: OpenWAClient = Depends(get_client),
):
    """Create a new WhatsApp session."""
    config = await _load_config(db)
    client = get_client(config)

    try:
        result = await client.create_session(payload.name)
        session_id = result.get("id") or result.get("sessionId")

        # Store in DB
        session = WhatsAppSession(
            session_id=session_id,
            name=payload.name,
            is_default=True,
            status="connecting",
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)

        # Start session
        await client.start_session(session_id)

        return WhatsAppSessionResponse(
            id=session.session_id,
            name=session.name,
            status=session.status,
            phone=None,
            profile_name=None,
            profile_pic=None,
            qr_code=None,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/session/{session_id}/start")
async def start_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    client: OpenWAClient = Depends(get_client),
):
    """Start a WhatsApp session."""
    config = await _load_config(db)
    client = get_client(config)

    try:
        await client.start_session(session_id)

        # Update DB
        await db.execute(
            update(WhatsAppSession)
            .where(WhatsAppSession.session_id == session_id)
            .values(status="connecting", updated_at=now_utc_naive())
        )
        await db.commit()

        return {"success": True, "message": "Session started"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/session/{session_id}/qr", response_model=WhatsAppQRResponse)
async def get_qr_code(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    client: OpenWAClient = Depends(get_client),
):
    """Get QR code for session authentication."""
    config = await _load_config(db)
    client = get_client(config)

    try:
        result = await client.get_qr_code(session_id)
        qr_code = result.get("qr") or result.get("qrCode") or result.get("code")
        qr_data = result.get("qrData") or result.get("code")

        # Update DB with QR
        await db.execute(
            update(WhatsAppSession)
            .where(WhatsAppSession.session_id == session_id)
            .values(qr_code=qr_code, status="qr_ready", updated_at=now_utc_naive())
        )
        await db.commit()

        return WhatsAppQRResponse(
            session_id=session_id,
            qr_code=qr_code or "",
            qr_data=qr_data,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/session/{session_id}/status", response_model=WhatsAppAuthStatusResponse)
async def session_status(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    client: OpenWAClient = Depends(get_client),
):
    """Get session authentication status."""
    config = await _load_config(db)
    client = get_client(config)

    status = await get_session_status(db, client, session_id)

    db_status = status.get("db", {})
    openwa_status = status.get("openwa", {})

    authenticated = db_status.get("status") in ("ready", "authenticated", "connected") or \
                    openwa_status.get("status") in ("ready", "authenticated", "connected")

    return WhatsAppAuthStatusResponse(
        authenticated=authenticated,
        session_id=session_id,
        phone=db_status.get("phone") or openwa_status.get("phone"),
        name=db_status.get("platform") or openwa_status.get("name"),
        status=db_status.get("status") or openwa_status.get("status", "unknown"),
    )


@router.post("/session/{session_id}/stop")
async def stop_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    client: OpenWAClient = Depends(get_client),
):
    """Stop a WhatsApp session."""
    config = await _load_config(db)
    client = get_client(config)

    try:
        await client.stop_session(session_id)

        await db.execute(
            update(WhatsAppSession)
            .where(WhatsAppSession.session_id == session_id)
            .values(status="disconnected", updated_at=now_utc_naive())
        )
        await db.commit()

        return {"success": True, "message": "Session stopped"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/session/{session_id}")
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    client: OpenWAClient = Depends(get_client),
):
    """Delete a WhatsApp session."""
    config = await _load_config(db)
    client = get_client(config)

    try:
        await client.delete_session(session_id)

        await db.execute(
            delete(WhatsAppSession).where(WhatsAppSession.session_id == session_id)
        )
        await db.commit()

        return {"success": True, "message": "Session deleted"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ────────────────────────────────────────────────────────────────────
# Channel Discovery
# ────────────────────────────────────────────────────────────────────

@router.get("/chats", response_model=List[WhatsAppDiscoveredChatResponse])
async def list_chats(
    session_id: Optional[str] = None,
    only_groups: bool = True,
    db: AsyncSession = Depends(get_db),
    client: OpenWAClient = Depends(get_client),
):
    """Discover available chats/groups from WhatsApp."""
    config = await _load_config(db)
    client = get_client(config)

    if not session_id:
        # Use default session
        result = await db.execute(
            select(WhatsAppSession).where(WhatsAppSession.is_default == True)
        )
        session = result.scalar_one_or_none()
        if session:
            session_id = session.session_id

    if not session_id:
        raise HTTPException(status_code=400, detail="No session specified")

    chats = await discover_chats(db, session_id, client, only_groups=only_groups)
    return [
        WhatsAppDiscoveredChatResponse(
            id=chat["id"],
            name=chat["name"],
            type=chat["type"],
            participant_count=chat.get("participant_count"),
            is_read_only=chat.get("is_read_only", False),
            last_message_at=chat.get("last_message_at"),
        )
        for chat in chats
    ]


@router.get("/subscribed", response_model=WhatsAppSubscribedChatsResponse)
async def get_subscribed_chats(db: AsyncSession = Depends(get_db)):
    """Get list of subscribed/monitored channels."""
    result = await db.execute(
        select(WhatsAppChannelSource).where(WhatsAppChannelSource.enabled == True)
    )
    channels = result.scalars().all()

    return WhatsAppSubscribedChatsResponse(
        provider="database",
        total_subscribed=len(channels),
        chats=[
            WhatsAppDiscoveredChatResponse(
                id=c.chat_id,
                name=c.name,
                type=c.source_type,
                participant_count=None,
                is_read_only=False,
                last_message_at=c.last_message_timestamp,
            )
            for c in channels
        ],
    )


# ────────────────────────────────────────────────────────────────────
# Channel Sources (Monitored Channels)
# ────────────────────────────────────────────────────────────────────

@router.get("/channels", response_model=List[WhatsAppChannelSourceResponse])
async def get_channels(
    kind: Optional[str] = Query(None, pattern=r"^(signals|news|volume_alerts)$"),
    db: AsyncSession = Depends(get_db),
):
    """Get configured channel sources."""
    query = select(WhatsAppChannelSource)
    if kind:
        query = query.where(WhatsAppChannelSource.kind == kind)
    query = query.order_by(WhatsAppChannelSource.created_at.desc())

    result = await db.execute(query)
    channels = result.scalars().all()

    return [
        WhatsAppChannelSourceResponse(
            id=c.id,
            name=c.name,
            kind=c.kind,
            source_type=c.source_type,
            chat_id=c.chat_id,
            session_id=c.session_id,
            is_active=c.enabled,
            description=None,
            parse_signals=c.parse_signals,
            signal_format=None,
            default_leverage=None,
            default_tp_levels=None,
            default_sl_pct=None,
            last_message_at=c.last_message_timestamp,
            message_count=0,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in channels
    ]


@router.post("/channels", response_model=WhatsAppChannelSourceResponse)
async def create_channel(
    payload: WhatsAppChannelSourceCreate,
    db: AsyncSession = Depends(get_db),
):
    """Add a new channel to monitor."""
    # Check if already exists
    existing = await db.execute(
        select(WhatsAppChannelSource).where(
            WhatsAppChannelSource.chat_id == payload.chat_id,
            WhatsAppChannelSource.session_id == (payload.session_id or "default"),
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Channel already monitored")

    channel = WhatsAppChannelSource(
        name=payload.name,
        kind=payload.kind,
        source_type=payload.source_type,
        chat_id=payload.chat_id,
        session_id=payload.session_id or "default",
        enabled=payload.is_active,
        parse_signals=payload.parse_signals,
    )
    db.add(channel)
    await db.commit()
    await db.refresh(channel)

    return WhatsAppChannelSourceResponse(
        id=channel.id,
        name=channel.name,
        kind=channel.kind,
        source_type=channel.source_type,
        chat_id=channel.chat_id,
        session_id=channel.session_id,
        is_active=channel.enabled,
        description=None,
        parse_signals=channel.parse_signals,
        signal_format=None,
        default_leverage=None,
        default_tp_levels=None,
        default_sl_pct=None,
        last_message_at=channel.last_message_timestamp,
        message_count=0,
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


@router.patch("/channels/{channel_id}", response_model=WhatsAppChannelSourceResponse)
async def update_channel(
    channel_id: int,
    payload: WhatsAppChannelSourceUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update channel source."""
    result = await db.execute(
        select(WhatsAppChannelSource).where(WhatsAppChannelSource.id == channel_id)
    )
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if hasattr(channel, key):
            setattr(channel, key, value)

    channel.updated_at = now_utc_naive()
    await db.commit()
    await db.refresh(channel)

    return WhatsAppChannelSourceResponse(
        id=channel.id,
        name=channel.name,
        kind=channel.kind,
        source_type=channel.source_type,
        chat_id=channel.chat_id,
        session_id=channel.session_id,
        is_active=channel.enabled,
        description=None,
        parse_signals=channel.parse_signals,
        signal_format=None,
        default_leverage=None,
        default_tp_levels=None,
        default_sl_pct=None,
        last_message_at=channel.last_message_timestamp,
        message_count=0,
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )


@router.delete("/channels/{channel_id}")
async def delete_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Remove a monitored channel."""
    result = await db.execute(
        select(WhatsAppChannelSource).where(WhatsAppChannelSource.id == channel_id)
    )
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    await db.delete(channel)
    await db.commit()
    return {"deleted": True}


@router.post("/channels/{channel_id}/preview", response_model=List[WhatsAppMessageResponse])
async def preview_channel(
    channel_id: int,
    payload: WhatsAppPreviewRequest,
    db: AsyncSession = Depends(get_db),
):
    """Preview recent messages from a channel."""
    result = await db.execute(
        select(WhatsAppChannelSource).where(WhatsAppChannelSource.id == channel_id)
    )
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Get messages from DB
    query = select(WhatsAppMessage).where(
        WhatsAppMessage.channel_source_id == channel_id
    ).order_by(WhatsAppMessage.received_at.desc()).limit(payload.limit)

    result = await db.execute(query)
    messages = result.scalars().all()

    return [
        WhatsAppMessageResponse(
            id=m.id,
            channel_source_id=m.channel_source_id,
            channel_name=channel.name,
            whatsapp_message_id=m.message_id,
            from_number=m.sender_id,
            from_name=m.sender_name,
            text=m.text,
            media_type=m.media_type,
            media_url=m.media_url,
            timestamp=m.whatsapp_timestamp,
            is_processed=m.processed,
            parsed_signal_id=m.parsed_signal_id,
            created_at=m.received_at,
        )
        for m in messages
    ]


# ────────────────────────────────────────────────────────────────────
# Polling
# ────────────────────────────────────────────────────────────────────

@router.post("/poll", response_model=WhatsAppPollResult)
async def trigger_poll(
    payload: WhatsAppPollRequest,
    db: AsyncSession = Depends(get_db),
    client: OpenWAClient = Depends(get_client),
):
    """Manually trigger message polling."""
    config = await _load_config(db)
    client = get_client(config)

    try:
        stats = await run_poll_all_channels(db, config, client)
        return WhatsAppPollResult(
            polled_sources=stats.get("channels_polled", 0),
            new_messages=stats.get("total_messages", 0),
            new_signals=stats.get("total_signals", 0),
            errors=stats.get("errors", []),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ────────────────────────────────────────────────────────────────────
# Parsed Signals
# ────────────────────────────────────────────────────────────────────

@router.get("/signals", response_model=List[WhatsAppParsedSignalResponse])
async def get_signals(
    status: Optional[str] = Query(None, pattern=r"^(active|filled|tp_hit|sl_hit|closed|expired|cancelled)$"),
    symbol: Optional[str] = None,
    channel_id: Optional[int] = None,
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Get parsed signals."""
    query = select(WhatsAppParsedSignal)
    if status:
        query = query.where(WhatsAppParsedSignal.status == status)
    if symbol:
        query = query.where(WhatsAppParsedSignal.symbol.ilike(f"%{symbol}%"))
    if channel_id:
        query = query.where(WhatsAppParsedSignal.channel_source_id == channel_id)
    query = query.order_by(WhatsAppParsedSignal.posted_at.desc()).limit(limit)

    result = await db.execute(query)
    signals = result.scalars().all()

    return [
        WhatsAppParsedSignalResponse(
            id=s.id,
            channel_source_id=s.channel_source_id,
            channel_title=s.channel_source.name if s.channel_source else "Unknown",
            whatsapp_message_id=s.whatsapp_message_id,
            symbol=s.symbol,
            direction=s.direction,
            leverage=s.leverage,
            entry=s.entry,
            entry_raw=s.entry_raw,
            stop_loss=s.stop_loss,
            stop_loss_raw=s.stop_loss_raw,
            trailing_sl=s.trailing_sl,
            tp_reached_count=s.tp_reached_count,
            market_type=s.market_type,
            take_profits=s.take_profits if isinstance(s.take_profits, list) else [],
            status=s.status,
            confidence=s.confidence,
            raw_text=s.raw_text,
            posted_at=s.posted_at,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in signals
    ]


@router.post("/signals/reconcile")
async def reconcile_signals(db: AsyncSession = Depends(get_db)):
    """Manually trigger signal reconciliation with live prices."""
    stats = await reconcile_active_signals(db)
    return {"success": True, **stats}


# ────────────────────────────────────────────────────────────────────
# Monitor
# ────────────────────────────────────────────────────────────────────

@router.get("/monitor/status", response_model=WhatsAppMonitorStatusResponse)
async def monitor_status():
    """Get signal monitor status."""
    return WhatsAppMonitorStatusResponse(**signal_monitor.status())


@router.post("/monitor/start")
async def monitor_start():
    """Start signal monitor."""
    signal_monitor.ensure_started(AsyncSessionLocal)
    return WhatsAppMonitorStatusResponse(**signal_monitor.status())


@router.post("/monitor/stop")
async def monitor_stop():
    """Stop signal monitor."""
    signal_monitor.stop()
    return WhatsAppMonitorStatusResponse(**signal_monitor.status())


# ────────────────────────────────────────────────────────────────────
# Sniper Settings
# ────────────────────────────────────────────────────────────────────

@router.get("/sniper/settings", response_model=WhatsAppSniperSettingsResponse)
async def get_sniper_settings(db: AsyncSession = Depends(get_db)):
    """Get sniper auto-trade settings."""
    result = await db.execute(select(WhatsAppSniperSettings).limit(1))
    s = result.scalars().first()
    if not s:
        s = WhatsAppSniperSettings()
        db.add(s)
        await db.commit()
        await db.refresh(s)

    return WhatsAppSniperSettingsResponse(
        enabled=s.enabled,
        mode=s.mode,
        trade_type=s.trade_type,
        position_size_usdt=s.position_size_usdt,
        max_positions=s.max_positions,
        max_positions_sandbox=s.max_positions_sandbox,
        max_positions_live=s.max_positions_live,
        leverage=s.leverage,
        margin_mode=s.margin_mode,
        sniper_offset_pct=s.sniper_offset_pct,
        min_confidence=s.min_confidence,
        min_risk_reward=s.min_risk_reward,
        pending_ttl_minutes=s.pending_ttl_minutes,
        reanalyze=s.reanalyze,
        execute_sandbox=s.execute_sandbox,
        execute_live=s.execute_live,
        require_ai_confirmation=s.require_ai_confirmation,
        execute_immediately=s.execute_immediately,
        skipped_reanalyze_minutes=s.skipped_reanalyze_minutes,
        tp_trail_pct=s.tp_trail_pct,
        volume_channel_id=s.volume_channel_id,
        allowed_channel_ids=s.allowed_channel_ids or [],
    )


@router.put("/sniper/settings", response_model=WhatsAppSniperSettingsResponse)
async def update_sniper_settings(
    payload: WhatsAppSniperSettingsUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update sniper settings."""
    result = await db.execute(select(WhatsAppSniperSettings).limit(1))
    s = result.scalars().first()
    if not s:
        s = WhatsAppSniperSettings()
        db.add(s)

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if hasattr(s, key):
            setattr(s, key, value)

    s.updated_at = now_utc_naive()
    await db.commit()
    await db.refresh(s)

    return WhatsAppSniperSettingsResponse(
        enabled=s.enabled,
        mode=s.mode,
        trade_type=s.trade_type,
        position_size_usdt=s.position_size_usdt,
        max_positions=s.max_positions,
        max_positions_sandbox=s.max_positions_sandbox,
        max_positions_live=s.max_positions_live,
        leverage=s.leverage,
        margin_mode=s.margin_mode,
        sniper_offset_pct=s.sniper_offset_pct,
        min_confidence=s.min_confidence,
        min_risk_reward=s.min_risk_reward,
        pending_ttl_minutes=s.pending_ttl_minutes,
        reanalyze=s.reanalyze,
        execute_sandbox=s.execute_sandbox,
        execute_live=s.execute_live,
        require_ai_confirmation=s.require_ai_confirmation,
        execute_immediately=s.execute_immediately,
        skipped_reanalyze_minutes=s.skipped_reanalyze_minutes,
        tp_trail_pct=s.tp_trail_pct,
        volume_channel_id=s.volume_channel_id,
        allowed_channel_ids=s.allowed_channel_ids or [],
    )


# ────────────────────────────────────────────────────────────────────
# Sniper Trades
# ────────────────────────────────────────────────────────────────────

@router.get("/sniper/trades", response_model=List[WhatsAppSniperTradeResponse])
async def get_sniper_trades(
    status: Optional[str] = Query(None, pattern=r"^(pending|placed|filled|skipped|failed|cancelled)$"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Get sniper trades."""
    query = select(WhatsAppSniperTrade)
    if status:
        query = query.where(WhatsAppSniperTrade.status == status)
    query = query.order_by(WhatsAppSniperTrade.created_at.desc()).limit(limit)

    result = await db.execute(query)
    trades = result.scalars().all()

    return [
        WhatsAppSniperTradeResponse(
            id=t.id,
            signal_id=t.signal_id,
            symbol=t.symbol,
            direction=t.direction,
            entry_price=t.entry_price,
            stop_loss=t.stop_loss,
            take_profits=t.take_profits if isinstance(t.take_profits, list) else [],
            position_size_usdt=t.position_size_usdt,
            leverage=t.leverage,
            margin_mode=t.margin_mode,
            status=t.status,
            order_id=t.order_id,
            filled_price=t.filled_price,
            filled_at=t.filled_at,
            pnl_usdt=t.pnl_usdt,
            pnl_pct=t.pnl_pct,
            reason=t.reason,
            mode=t.mode,
            created_at=t.created_at,
            updated_at=t.updated_at,
        )
        for t in trades
    ]


@router.post("/sniper/run")
async def sniper_run_now(db: AsyncSession = Depends(get_db)):
    """Manually run one sniper cycle."""
    return await run_sniper_cycle(db)


@router.post("/sniper/trades/{trade_id}/execute")
async def execute_sniper_trade_endpoint(
    trade_id: int,
    mode: str = Query("sandbox", pattern=r"^(sandbox|live|both)$"),
    force: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Manually execute a pending sniper trade."""
    return await execute_sniper_trade(db, trade_id, mode=mode, force=force)


@router.post("/signals/{signal_id}/execute")
async def execute_signal_endpoint(
    signal_id: int,
    mode: str = Query("sandbox", pattern=r"^(sandbox|live|both)$"),
    force: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    """Execute a parsed signal as a sniper trade."""
    return await execute_parsed_signal(db, signal_id, mode=mode, force=force)


# ────────────────────────────────────────────────────────────────────
# Prices & Analysis
# ────────────────────────────────────────────────────────────────────

@router.get("/prices", response_model=WhatsAppPricesResponse)
async def get_prices(
    symbols: str = Query(..., description="Comma-separated symbols"),
    db: AsyncSession = Depends(get_db),
):
    """Get live prices for symbols."""
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    prices = await get_signal_prices(db, syms)
    return WhatsAppPricesResponse(prices=prices)


@router.post("/signals/{signal_id}/analyze", response_model=dict)
async def analyze_signal(
    signal_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Full analysis of a signal."""
    return await analyze_signal_full(db, signal_id)


@router.get("/signals/volume-monitor", response_model=dict)
async def volume_monitor(
    limit: int = Query(25, ge=1, le=60),
    db: AsyncSession = Depends(get_db),
):
    """Volume monitor snapshot for active signals."""
    return await volume_monitor_snapshot(db, limit=limit)


@router.post("/signals/reanalyze-skipped")
async def reanalyze_skipped(db: AsyncSession = Depends(get_db)):
    """Re-analyze skipped signals."""
    return await reanalyze_skipped_signals(db)


@router.post("/signals/process-outcome")
async def process_outcome(
    body: dict = None,
    db: AsyncSession = Depends(get_db),
):
    """Process signal outcome from message text."""
    # This would be implemented similarly to Telegram plugin
    return {"ok": True, "message": "Not fully implemented yet"}


# ────────────────────────────────────────────────────────────────────
# Channel Presets
# ────────────────────────────────────────────────────────────────────

@router.get("/presets", response_model=List[WhatsAppChannelPresetResponse])
async def get_presets(
    kind: Optional[str] = Query(None, pattern=r"^(signals|news|volume_alerts)$"),
    db: AsyncSession = Depends(get_db),
):
    """Get channel presets."""
    query = select(WhatsAppChannelPreset)
    if kind:
        query = query.where(WhatsAppChannelPreset.kind == kind)
    query = query.where(WhatsAppChannelPreset.is_active == True)

    result = await db.execute(query)
    presets = result.scalars().all()

    return [
        WhatsAppChannelPresetResponse(
            id=p.id,
            name=p.name,
            kind=p.kind,
            description=p.description,
            chat_ids=p.chat_ids if isinstance(p.chat_ids, list) else [],
            default_settings=p.default_config or {},
            created_at=p.created_at,
            updated_at=p.updated_at,
        )
        for p in presets
    ]


@router.post("/presets", response_model=WhatsAppChannelPresetResponse)
async def create_preset(
    payload: WhatsAppChannelPresetCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new channel preset."""
    preset = WhatsAppChannelPreset(
        name=payload.name,
        kind=payload.kind,
        description=payload.description,
        chat_ids=payload.chat_ids,
        default_config=payload.default_settings,
    )
    db.add(preset)
    await db.commit()
    await db.refresh(preset)

    return WhatsAppChannelPresetResponse(
        id=preset.id,
        name=preset.name,
        kind=preset.kind,
        description=preset.description,
        chat_ids=preset.chat_ids if isinstance(preset.chat_ids, list) else [],
        default_settings=preset.default_config or {},
        created_at=preset.created_at,
        updated_at=preset.updated_at,
    )


@router.patch("/presets/{preset_id}", response_model=WhatsAppChannelPresetResponse)
async def update_preset(
    preset_id: int,
    payload: WhatsAppChannelPresetUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a channel preset."""
    result = await db.execute(
        select(WhatsAppChannelPreset).where(WhatsAppChannelPreset.id == preset_id)
    )
    preset = result.scalar_one_or_none()
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if hasattr(preset, key):
            setattr(preset, key, value)

    preset.updated_at = now_utc_naive()
    await db.commit()
    await db.refresh(preset)

    return WhatsAppChannelPresetResponse(
        id=preset.id,
        name=preset.name,
        kind=preset.kind,
        description=preset.description,
        chat_ids=preset.chat_ids if isinstance(preset.chat_ids, list) else [],
        default_settings=preset.default_config or {},
        created_at=preset.created_at,
        updated_at=preset.updated_at,
    )


@router.delete("/presets/{preset_id}")
async def delete_preset(
    preset_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a channel preset."""
    result = await db.execute(
        select(WhatsAppChannelPreset).where(WhatsAppChannelPreset.id == preset_id)
    )
    preset = result.scalar_one_or_none()
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    await db.delete(preset)
    await db.commit()
    return {"deleted": True}


@router.post("/presets/{preset_id}/apply", response_model=WhatsAppApplyPresetResponse)
async def apply_preset(
    preset_id: int,
    payload: WhatsAppApplyPresetRequest,
    db: AsyncSession = Depends(get_db),
):
    """Apply a preset to create channel sources."""
    result = await db.execute(
        select(WhatsAppChannelPreset).where(WhatsAppChannelPreset.id == preset_id)
    )
    preset = result.scalar_one_or_none()
    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    # Get default session
    session_result = await db.execute(
        select(WhatsAppSession).where(WhatsAppSession.is_default == True)
    )
    session = session_result.scalar_one_or_none()

    created = await create_channel_from_preset(
        db, preset, session.session_id if session else "default",
        user_id=0
    )

    return WhatsAppApplyPresetResponse(
        created=len(created),
        updated=0,
        errors=[],
    )


# ────────────────────────────────────────────────────────────────────
# Webhook Receiver
# ────────────────────────────────────────────────────────────────────

@router.post("/webhook/{session_id}")
async def webhook_receiver(
    session_id: str,
    request: Request,
    x_openwa_signature: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Receive webhook from OpenWA Gateway."""
    config = await _load_config(db)

    # Verify webhook signature
    body = await request.body()
    if config.webhook_secret and x_openwa_signature:
        expected = hmac.new(
            config.webhook_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, x_openwa_signature):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Add session_id to payload if not present
    if "sessionId" not in payload and "session_id" not in payload:
        payload["sessionId"] = session_id

    # Process webhook
    stats = await process_webhook_payload(db, payload, config)

    return {
        "ok": True,
        "processed": stats.get("processed", 0),
        "new_messages": stats.get("new_messages", 0),
        "new_signals": stats.get("new_signals", 0),
        "errors": stats.get("errors", []),
    }


# ────────────────────────────────────────────────────────────────────
# Messages
# ────────────────────────────────────────────────────────────────────

@router.get("/messages", response_model=List[WhatsAppMessageResponse])
async def get_messages(
    channel_id: Optional[int] = None,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Get raw messages."""
    query = select(WhatsAppMessage)
    if channel_id:
        query = query.where(WhatsAppMessage.channel_source_id == channel_id)
    query = query.order_by(WhatsAppMessage.received_at.desc()).limit(limit)

    result = await db.execute(query)
    messages = result.scalars().all()

    return [
        WhatsAppMessageResponse(
            id=m.id,
            channel_source_id=m.channel_source_id,
            channel_name=m.channel_source.name if m.channel_source else "Unknown",
            whatsapp_message_id=m.message_id,
            from_number=m.sender_id,
            from_name=m.sender_name,
            text=m.text,
            media_type=m.media_type,
            media_url=m.media_url,
            timestamp=m.whatsapp_timestamp,
            is_processed=m.processed,
            parsed_signal_id=m.parsed_signal_id,
            created_at=m.received_at,
        )
        for m in messages
    ]