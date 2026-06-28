"""Telegram Signal & News Plugin API router."""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from plugins.TelegramSignalNewsPlugin.backend.config import telegram_plugin_config, build_config_from_db
from plugins.TelegramSignalNewsPlugin.backend.schemas import (
    TelegramApplyPresetRequest,
    TelegramApplyPresetResponse,
    TelegramAuthCompleteRequest,
    TelegramAuthCompleteResponse,
    TelegramAuthStartRequest,
    TelegramAuthStartResponse,
    TelegramAuthStatusResponse,
    TelegramDiscoveredChannelResponse,
    TelegramMethodDescriptor,
    TelegramMethodsCatalogResponse,
    TelegramMethodsTestRequest,
    TelegramMethodsTestResponse,
    TelegramMethodsTestSummary,
    TelegramChannelPresetCreate,
    TelegramChannelPresetResponse,
    TelegramChannelPresetUpdate,
    TelegramChannelSourceCreate,
    TelegramChannelSourceResponse,
    TelegramSubscribedChannelsResponse,
    TelegramChannelSourceUpdate,
    TelegramIngestMessageResponse,
    TelegramPollRequest,
    TelegramPollResult,
    TelegramPreviewRequest,
    TelegramPluginSettingsUpdate,
    TelegramPluginSettingsResponse,
    TelegramTestConnectionResponse,
    TelegramTestProviderResult,
    TelegramStatusResponse,
    TelegramParsedSignalResponse,
    TelegramMonitorStatusResponse,
    TelegramSniperSettingsResponse,
    TelegramSniperSettingsUpdate,
    TelegramSniperTradeResponse,
)
from plugins.TelegramSignalNewsPlugin.backend.services.ingest_service import (
    apply_preset,
    build_status,
    create_preset,
    create_source,
    delete_preset,
    delete_source,
    get_preset,
    get_source_for_user,
    list_messages,
    list_presets,
    list_sources,
    normalize_user_id,
    preview_source_messages,
    run_poll,
    to_preset_response,
    to_source_response,
    update_preset,
    update_source,
)
from plugins.TelegramSignalNewsPlugin.backend.services.telegram_provider import TelegramProviderRegistry
from plugins.TelegramSignalNewsPlugin.backend.services.monitor_service import (
    signal_monitor,
    create_signals_from_messages,
    reconcile_active_signals_from_live_price,
)
from plugins.TelegramSignalNewsPlugin.backend.services.sniper_service import (
    get_or_create_settings as get_sniper_settings,
    run_sniper_cycle,
    execute_sniper_trade,
    execute_parsed_signal,
    get_signal_prices,
    analyze_signal_full,
    volume_monitor_snapshot,
    reanalyze_skipped_signals,
    process_volume_channel_message,
    auto_close_positions_for_signal,
)
from plugins.TelegramSignalNewsPlugin.backend.models import (
    TelegramPluginSettings,
    TelegramParsedSignal,
    SignalStatus,
    TelegramSniperSettings,
    TelegramSniperTrade,
    SniperTradeStatus,
    TelegramSubscribedCache,
)
from plugins.TelegramSignalNewsPlugin.backend.timezone_utils import now_utc_naive


router = APIRouter(prefix="/plugins/telegram", tags=["Telegram Signal & News"])
provider_registry = TelegramProviderRegistry(telegram_plugin_config)

# In-flight background refresh tasks for subscribed-channel cache (per user).
_subscribed_refresh_tasks: dict[int, asyncio.Task[None]] = {}


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def _ensure_monitor() -> None:
    """Idempotently start the background signal monitor on any request.

    The app uses a lifespan context, so router on_event startup handlers do not
    fire; instead we lazily start the monitor the first time any telegram
    endpoint is hit. Safe to call on every request.
    """
    try:
        signal_monitor.ensure_started(AsyncSessionLocal)
    except Exception:
        pass


# Auto-start the monitor on every request to this router (idempotent).
router.dependencies.append(Depends(_ensure_monitor))


async def _load_registry(db: AsyncSession) -> TelegramProviderRegistry:
    """Return a registry built from live DB credentials (merged over env vars)."""
    from sqlalchemy import select
    result = await db.execute(select(TelegramPluginSettings).limit(1))
    settings = result.scalars().first()
    live_cfg = build_config_from_db(settings)
    return TelegramProviderRegistry(live_cfg)


async def _refresh_subscribed_cache(user_id: int, provider_hint: str, limit: int) -> None:
    """Refresh subscribed-channel cache in the background."""
    from sqlalchemy import delete

    async with AsyncSessionLocal() as session:
        live_registry = await _load_registry(session)
        rows, used_provider = await live_registry.list_subscribed_channels(
            limit=limit,
            provider_hint=provider_hint,
        )

        now = now_utc_naive()
        await session.execute(
            delete(TelegramSubscribedCache).where(TelegramSubscribedCache.user_id == user_id)
        )
        for item in rows:
            session.add(
                TelegramSubscribedCache(
                    user_id=user_id,
                    title=item.title,
                    channel_handle=item.handle,
                    channel_id=item.channel_id,
                    provider=used_provider,
                    fetched_at=now,
                )
            )
        await session.commit()


def _schedule_subscribed_refresh(user_id: int, provider_hint: str, limit: int) -> None:
    """Schedule one background cache refresh per user (idempotent while in-flight)."""
    existing = _subscribed_refresh_tasks.get(user_id)
    if existing and not existing.done():
        return

    async def _runner() -> None:
        try:
            await _refresh_subscribed_cache(user_id=user_id, provider_hint=provider_hint, limit=limit)
        except Exception:
            # Best-effort refresh; endpoint callers continue using cache paths.
            pass
        finally:
            _subscribed_refresh_tasks.pop(user_id, None)

    _subscribed_refresh_tasks[user_id] = asyncio.create_task(_runner())


@router.get("/settings", response_model=TelegramPluginSettingsResponse)
async def get_plugin_settings(db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    result = await db.execute(select(TelegramPluginSettings).limit(1))
    settings = result.scalars().first()
    if settings is None:
        return TelegramPluginSettingsResponse()
    return TelegramPluginSettingsResponse(
        api_id=settings.api_id,
        api_hash_set=bool(settings.api_hash),
        phone_number=settings.phone_number,
        bot_token_set=bool(settings.bot_token),
        mcp_chat_id=settings.mcp_chat_id,
        label=settings.label,
    )


@router.put("/settings", response_model=TelegramPluginSettingsResponse)
async def update_plugin_settings(
    payload: TelegramPluginSettingsUpdate,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import select
    result = await db.execute(select(TelegramPluginSettings).limit(1))
    settings = result.scalars().first()
    if settings is None:
        settings = TelegramPluginSettings()
        db.add(settings)

    if payload.api_id is not None:
        settings.api_id = payload.api_id
    if payload.api_hash is not None:
        settings.api_hash = payload.api_hash
    if payload.phone_number is not None:
        settings.phone_number = payload.phone_number
    if payload.bot_token is not None:
        settings.bot_token = payload.bot_token
    if payload.mcp_chat_id is not None:
        settings.mcp_chat_id = payload.mcp_chat_id
    if payload.label is not None:
        settings.label = payload.label

    await db.commit()
    await db.refresh(settings)
    return TelegramPluginSettingsResponse(
        api_id=settings.api_id,
        api_hash_set=bool(settings.api_hash),
        phone_number=settings.phone_number,
        bot_token_set=bool(settings.bot_token),
        mcp_chat_id=settings.mcp_chat_id,
        label=settings.label,
    )


@router.post("/test-connection", response_model=TelegramTestConnectionResponse)
async def test_connection(db: AsyncSession = Depends(get_db)):
    """Test all configured Telegram providers and return per-provider results."""
    live_registry = await _load_registry(db)
    raw = await live_registry.test_connection()
    results = [TelegramTestProviderResult(**r) for r in raw]
    return TelegramTestConnectionResponse(
        results=results,
        any_ok=any(r.ok for r in results),
    )


# ── Telethon account authentication ──────────────────────────────────────────

@router.get("/auth/status", response_model=TelegramAuthStatusResponse)
async def auth_status(db: AsyncSession = Depends(get_db)):
    """Check whether the Telethon session is authenticated."""
    live_registry = await _load_registry(db)
    telethon = live_registry._get_telethon_provider()
    if telethon is None or not telethon.is_available():
        return TelegramAuthStatusResponse(authenticated=False)
    try:
        info = await telethon.get_account_info()
        if info is None:
            return TelegramAuthStatusResponse(authenticated=False)
        return TelegramAuthStatusResponse(
            authenticated=True,
            phone_number=info.get("phone"),
            username=info.get("username"),
            first_name=info.get("first_name"),
        )
    except Exception:
        return TelegramAuthStatusResponse(authenticated=False)


@router.post("/auth/start", response_model=TelegramAuthStartResponse)
async def auth_start(
    payload: TelegramAuthStartRequest,
    db: AsyncSession = Depends(get_db),
):
    """Send a login code to the phone number. Returns phone_code_hash."""
    live_registry = await _load_registry(db)
    telethon = live_registry._get_telethon_provider()
    if telethon is None or not telethon.is_available():
        raise HTTPException(status_code=400, detail="Telethon provider is not configured (API ID + API Hash required).")
    try:
        phone_code_hash = await telethon.start_auth(payload.phone_number)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TelegramAuthStartResponse(
        phone_code_hash=phone_code_hash,
        message=f"Code sent to {payload.phone_number}. Enter the code you received.",
    )


@router.post("/auth/complete", response_model=TelegramAuthCompleteResponse)
async def auth_complete(
    payload: TelegramAuthCompleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Verify the OTP code (and optional 2FA password) to complete authentication."""
    live_registry = await _load_registry(db)
    telethon = live_registry._get_telethon_provider()
    if telethon is None or not telethon.is_available():
        raise HTTPException(status_code=400, detail="Telethon provider is not configured.")
    try:
        account = await telethon.complete_auth(
            phone_number=payload.phone_number,
            phone_code_hash=payload.phone_code_hash,
            code=payload.code,
            password=payload.password,
        )
        name = account.get("first_name") or account.get("username") or payload.phone_number
        return TelegramAuthCompleteResponse(
            success=True,
            message=f"Authenticated as {name}.",
            account=account,
        )
    except RuntimeError as exc:
        if "2FA_REQUIRED" in str(exc):
            return TelegramAuthCompleteResponse(
                success=False,
                requires_2fa=True,
                message="Two-factor authentication password required.",
            )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/auth/disconnect")
async def auth_disconnect(db: AsyncSession = Depends(get_db)):
    """Log out and remove the Telethon session."""
    live_registry = await _load_registry(db)
    telethon = live_registry._get_telethon_provider()
    if telethon is None:
        return {"success": True, "message": "No Telethon provider configured."}
    try:
        await telethon.disconnect()
        return {"success": True, "message": "Disconnected and session removed."}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/status", response_model=TelegramStatusResponse)
async def plugin_status(db: AsyncSession = Depends(get_db)):
    live_registry = await _load_registry(db)
    return await build_status(db, live_registry)


@router.get("/discovery/subscribed", response_model=TelegramSubscribedChannelsResponse)
async def discover_subscribed_channels(
    provider: str = Query(default="auto", pattern=r"^(auto|telethon|bot_api|telegram_mcp)$"),
    limit: int = Query(default=100, ge=1, le=500),
    refresh: bool = Query(default=False, description="Force a live re-fetch from Telegram"),
    db: AsyncSession = Depends(get_db),
):
    """Return subscribed channels from the DB cache for speed.

    The cache is refreshed from Telegram at most once every 30 minutes (or when
    ?refresh=true). This keeps the /telegram page fast and avoids hammering the
    Telethon session (which caused "database is locked").
    """
    from datetime import timedelta
    from sqlalchemy import select, delete

    CACHE_TTL = timedelta(minutes=30)
    FAST_LIVE_TIMEOUT_SECONDS = 1.0
    FORCED_REFRESH_TIMEOUT_SECONDS = 8.0
    user_id = normalize_user_id(None)

    # 1) Read cache + freshness
    cache_rows = (
        await db.execute(
            select(TelegramSubscribedCache)
            .where(TelegramSubscribedCache.user_id == user_id)
            .order_by(TelegramSubscribedCache.title.asc())
        )
    ).scalars().all()

    newest = max((r.fetched_at for r in cache_rows), default=None)
    is_fresh = newest is not None and (now_utc_naive() - newest) < CACHE_TTL

    # 2) Serve from cache when fresh and not forced
    if cache_rows and is_fresh and not refresh:
        channels = [
            TelegramDiscoveredChannelResponse(
                title=r.title,
                channel_handle=r.channel_handle,
                channel_id=r.channel_id,
                provider=r.provider,
            )
            for r in cache_rows[:limit]
        ]
        return TelegramSubscribedChannelsResponse(
            provider="cache",
            total_subscribed=len(channels),
            channels=channels,
        )

    # 2b) Serve stale cache immediately and refresh in background.
    if cache_rows and not is_fresh and not refresh:
        _schedule_subscribed_refresh(user_id=user_id, provider_hint=provider, limit=limit)
        channels = [
            TelegramDiscoveredChannelResponse(
                title=r.title,
                channel_handle=r.channel_handle,
                channel_id=r.channel_id,
                provider=r.provider,
            )
            for r in cache_rows[:limit]
        ]
        return TelegramSubscribedChannelsResponse(
            provider="cache (stale)",
            total_subscribed=len(channels),
            channels=channels,
        )

    # 2c) Empty cache: try a short live fetch once; if still slow, warm in background.
    if not cache_rows and not refresh:
        live_registry = await _load_registry(db)
        try:
            rows, used_provider = await asyncio.wait_for(
                live_registry.list_subscribed_channels(limit=limit, provider_hint=provider),
                timeout=FAST_LIVE_TIMEOUT_SECONDS,
            )
        except Exception:
            _schedule_subscribed_refresh(user_id=user_id, provider_hint=provider, limit=limit)
            return TelegramSubscribedChannelsResponse(
                provider="cache (warming)",
                total_subscribed=0,
                channels=[],
            )

        now = now_utc_naive()
        await db.execute(delete(TelegramSubscribedCache).where(TelegramSubscribedCache.user_id == user_id))
        for item in rows:
            db.add(
                TelegramSubscribedCache(
                    user_id=user_id,
                    title=item.title,
                    channel_handle=item.handle,
                    channel_id=item.channel_id,
                    provider=used_provider,
                    fetched_at=now,
                )
            )
        await db.commit()

        channels = [
            TelegramDiscoveredChannelResponse(
                title=item.title,
                channel_handle=item.handle,
                channel_id=item.channel_id,
                provider=used_provider,
            )
            for item in rows
        ]
        return TelegramSubscribedChannelsResponse(
            provider=used_provider,
            total_subscribed=len(channels),
            channels=channels,
        )

    # 3) Refresh from Telegram (cache stale / empty / forced)
    live_registry = await _load_registry(db)
    try:
        rows, used_provider = await asyncio.wait_for(
            live_registry.list_subscribed_channels(
                limit=limit,
                provider_hint=provider,
            ),
            timeout=FORCED_REFRESH_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        # On failure, fall back to whatever cache we have rather than erroring
        if cache_rows:
            channels = [
                TelegramDiscoveredChannelResponse(
                    title=r.title,
                    channel_handle=r.channel_handle,
                    channel_id=r.channel_id,
                    provider=r.provider,
                )
                for r in cache_rows[:limit]
            ]
            return TelegramSubscribedChannelsResponse(
                provider="cache (stale)",
                total_subscribed=len(channels),
                channels=channels,
            )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 4) Replace cache atomically
    now = now_utc_naive()
    await db.execute(delete(TelegramSubscribedCache).where(TelegramSubscribedCache.user_id == user_id))
    for item in rows:
        db.add(
            TelegramSubscribedCache(
                user_id=user_id,
                title=item.title,
                channel_handle=item.handle,
                channel_id=item.channel_id,
                provider=used_provider,
                fetched_at=now,
            )
        )
    await db.commit()

    channels = [
        TelegramDiscoveredChannelResponse(
            title=item.title,
            channel_handle=item.handle,
            channel_id=item.channel_id,
            provider=used_provider,
        )
        for item in rows
    ]
    return TelegramSubscribedChannelsResponse(
        provider=used_provider,
        total_subscribed=len(channels),
        channels=channels,
    )


@router.get("/methods/catalog", response_model=TelegramMethodsCatalogResponse)
async def get_methods_catalog(
    refresh: bool = Query(default=False),
    limit: int | None = Query(default=None, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
):
    live_registry = await _load_registry(db)
    try:
        payload = await live_registry.build_core_methods_catalog(refresh=refresh, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    methods = [TelegramMethodDescriptor(**item) for item in payload.get("methods", [])]
    return TelegramMethodsCatalogResponse(
        source_url=payload["source_url"],
        total_methods=payload["total_methods"],
        fetched_at=payload["fetched_at"],
        methods=methods,
    )


@router.post("/methods/test", response_model=TelegramMethodsTestResponse)
async def test_methods(
    payload: TelegramMethodsTestRequest,
    db: AsyncSession = Depends(get_db),
):
    live_registry = await _load_registry(db)
    try:
        result = await live_registry.test_core_methods(
            provider_hint=payload.provider,
            refresh=payload.refresh,
            limit=payload.limit,
            mode=payload.mode,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return TelegramMethodsTestResponse(
        source_url=result["source_url"],
        provider=result["provider"],
        mode=result["mode"],
        readonly_allowlist=result.get("readonly_allowlist", []),
        summary=TelegramMethodsTestSummary(**result["summary"]),
        results=result["results"],
    )


@router.get("/channels", response_model=list[TelegramChannelSourceResponse])
async def get_channels(
    user_id: str = "0",
    source_kind: Optional[str] = Query(default=None, pattern=r"^(signals|news)$"),
    db: AsyncSession = Depends(get_db),
):
    user = normalize_user_id(user_id)
    rows = await list_sources(db=db, user_id=user, source_kind=source_kind)
    return [to_source_response(item) for item in rows]


@router.post("/channels", response_model=TelegramChannelSourceResponse)
async def post_channel(
    payload: TelegramChannelSourceCreate,
    db: AsyncSession = Depends(get_db),
):
    live_registry = await _load_registry(db)
    try:
        model = await create_source(
            db=db,
            payload=payload,
            provider_registry=live_registry,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return to_source_response(model)


@router.patch("/channels/{source_id}", response_model=TelegramChannelSourceResponse)
async def patch_channel(
    source_id: int,
    payload: TelegramChannelSourceUpdate,
    user_id: str = "0",
    db: AsyncSession = Depends(get_db),
):
    user = normalize_user_id(user_id)
    model = await get_source_for_user(db, source_id, user)
    if model is None:
        raise HTTPException(status_code=404, detail="Channel source not found")

    updated = await update_source(db, model, payload)
    return to_source_response(updated)


@router.delete("/channels/{source_id}")
async def remove_channel(
    source_id: int,
    user_id: str = "0",
    db: AsyncSession = Depends(get_db),
):
    user = normalize_user_id(user_id)
    model = await get_source_for_user(db, source_id, user)
    if model is None:
        raise HTTPException(status_code=404, detail="Channel source not found")

    await delete_source(db, model)
    return {"deleted": True}


@router.post("/channels/{source_id}/preview", response_model=list[TelegramIngestMessageResponse])
async def preview_channel(
    source_id: int,
    payload: TelegramPreviewRequest,
    user_id: str = "0",
    db: AsyncSession = Depends(get_db),
):
    user = normalize_user_id(user_id)
    model = await get_source_for_user(db, source_id, user)
    if model is None:
        raise HTTPException(status_code=404, detail="Channel source not found")

    live_registry = await _load_registry(db)
    try:
        rows = await preview_source_messages(
            source=model,
            limit=payload.limit,
            provider_registry=live_registry,
            cfg=live_registry._cfg,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return rows


@router.post("/poll", response_model=TelegramPollResult)
async def trigger_poll(
    payload: TelegramPollRequest,
    db: AsyncSession = Depends(get_db),
):
    live_registry = await _load_registry(db)
    try:
        return await run_poll(
            db=db,
            request=payload,
            provider_registry=live_registry,
            cfg=live_registry._cfg,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── Parsed signals & background monitor ──────────────────────────────────────

def _signal_to_response(model: TelegramParsedSignal) -> TelegramParsedSignalResponse:
    return TelegramParsedSignalResponse(
        id=model.id,
        channel_source_id=model.channel_source_id,
        channel_title=model.channel_title,
        telegram_message_id=model.telegram_message_id,
        symbol=model.symbol,
        direction=model.direction,
        leverage=model.leverage,
        entry=model.entry,
        entry_raw=model.entry_raw,
        stop_loss=model.stop_loss,
        stop_loss_raw=model.stop_loss_raw,
        trailing_sl=getattr(model, "trailing_sl", None),
        tp_reached_count=getattr(model, "tp_reached_count", 0),
        market_type=getattr(model, "market_type", "crypto"),
        take_profits=model.take_profits_json or [],
        status=model.status.value if hasattr(model.status, "value") else str(model.status),
        confidence=model.confidence,
        raw_text=model.raw_text,
        posted_at=model.posted_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


@router.get("/signals", response_model=list[TelegramParsedSignalResponse])
async def list_signals(
    status: Optional[str] = Query(
        default=None, pattern=r"^(active|filled|tp_hit|sl_hit|closed)$"
    ),
    market_type: Optional[str] = Query(default=None, pattern=r"^(crypto|forex)$"),
    channel_source_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import select, desc

    q = select(TelegramParsedSignal)
    if status:
        q = q.where(TelegramParsedSignal.status == SignalStatus(status))
    if market_type:
        q = q.where(TelegramParsedSignal.market_type == market_type)
    if channel_source_id is not None:
        q = q.where(TelegramParsedSignal.channel_source_id == channel_source_id)
    q = q.order_by(desc(TelegramParsedSignal.created_at)).limit(limit)

    result = await db.execute(q)
    return [_signal_to_response(row) for row in result.scalars().all()]


@router.post("/signals/rebuild")
async def rebuild_signals(db: AsyncSession = Depends(get_db)):
    """Re-scan stored messages and (re)create signals. Idempotent."""
    stats = await create_signals_from_messages(db, limit=500)
    return stats


@router.get("/monitor/status", response_model=TelegramMonitorStatusResponse)
async def monitor_status():
    return TelegramMonitorStatusResponse(**signal_monitor.status())


@router.post("/monitor/start", response_model=TelegramMonitorStatusResponse)
async def monitor_start():
    signal_monitor.ensure_started(AsyncSessionLocal)
    return TelegramMonitorStatusResponse(**signal_monitor.status())


@router.post("/monitor/stop", response_model=TelegramMonitorStatusResponse)
async def monitor_stop():
    signal_monitor.stop()
    return TelegramMonitorStatusResponse(**signal_monitor.status())


# ── Sniper auto-trade engine ─────────────────────────────────────────────────

def _sniper_settings_to_response(s: TelegramSniperSettings) -> TelegramSniperSettingsResponse:
    return TelegramSniperSettingsResponse(
        enabled=s.enabled,
        mode=s.mode,
        trade_type=s.trade_type,
        position_size_usdt=s.position_size_usdt,
        max_positions=s.max_positions,
        max_positions_sandbox=getattr(s, "max_positions_sandbox", s.max_positions),
        max_positions_live=getattr(s, "max_positions_live", 3),
        leverage=s.leverage,
        margin_mode=s.margin_mode,
        sniper_offset_pct=s.sniper_offset_pct,
        min_confidence=s.min_confidence,
        min_risk_reward=s.min_risk_reward,
        pending_ttl_minutes=s.pending_ttl_minutes,
        reanalyze=s.reanalyze,
        execute_sandbox=getattr(s, "execute_sandbox", True),
        execute_live=getattr(s, "execute_live", False),
        require_ai_confirmation=getattr(s, "require_ai_confirmation", True),
        execute_immediately=getattr(s, "execute_immediately", True),
        skipped_reanalyze_minutes=getattr(s, "skipped_reanalyze_minutes", 15),
        tp_trail_pct=getattr(s, "tp_trail_pct", 1.5),
        volume_channel_id=getattr(s, "volume_channel_id", None),
        allowed_channel_ids=s.allowed_channel_ids_json,
    )


@router.get("/sniper/settings", response_model=TelegramSniperSettingsResponse)
async def get_sniper_settings_endpoint(db: AsyncSession = Depends(get_db)):
    s = await get_sniper_settings(db)
    return _sniper_settings_to_response(s)


@router.put("/sniper/settings", response_model=TelegramSniperSettingsResponse)
async def update_sniper_settings_endpoint(
    payload: TelegramSniperSettingsUpdate,
    db: AsyncSession = Depends(get_db),
):
    s = await get_sniper_settings(db)
    data = payload.model_dump(exclude_unset=True)
    field_map = {
        "enabled": "enabled",
        "trade_type": "trade_type",
        "position_size_usdt": "position_size_usdt",
        "max_positions": "max_positions",
        "max_positions_sandbox": "max_positions_sandbox",
        "max_positions_live": "max_positions_live",
        "leverage": "leverage",
        "margin_mode": "margin_mode",
        "sniper_offset_pct": "sniper_offset_pct",
        "min_confidence": "min_confidence",
        "min_risk_reward": "min_risk_reward",
        "pending_ttl_minutes": "pending_ttl_minutes",
        "reanalyze": "reanalyze",
        "execute_sandbox": "execute_sandbox",
        "execute_live": "execute_live",
        "require_ai_confirmation": "require_ai_confirmation",
        "execute_immediately": "execute_immediately",
        "skipped_reanalyze_minutes": "skipped_reanalyze_minutes",
        "tp_trail_pct": "tp_trail_pct",
    }
    for key, attr in field_map.items():
        if key in data:
            setattr(s, attr, data[key])
    if "allowed_channel_ids" in data:
        s.allowed_channel_ids_json = data["allowed_channel_ids"]
    if "volume_channel_id" in data:
        setattr(s, "volume_channel_id", data["volume_channel_id"])
    await db.commit()
    await db.refresh(s)
    return _sniper_settings_to_response(s)


@router.get("/sniper/trades", response_model=list[TelegramSniperTradeResponse])
async def list_sniper_trades(
    status: Optional[str] = Query(
        default=None, pattern=r"^(pending|placed|skipped|missed|failed)$"
    ),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import select, desc

    q = select(TelegramSniperTrade)
    if status:
        q = q.where(TelegramSniperTrade.status == SniperTradeStatus(status))
    q = q.order_by(desc(TelegramSniperTrade.created_at)).limit(limit)
    result = await db.execute(q)
    return list(result.scalars().all())


@router.post("/sniper/run")
async def sniper_run_now(db: AsyncSession = Depends(get_db)):
    """Manually trigger one sniper cycle (re-analyse + fill)."""
    return await run_sniper_cycle(db)


@router.post("/sniper/trades/{trade_id}/execute")
async def execute_sniper_trade_endpoint(
    trade_id: int,
    mode: str = Query(default="sandbox", pattern=r"^(sandbox|live|both)$"),
    force: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
):
    """Manually execute a pending telegram-signal trade on sandbox and/or live.

    ``force=true`` overrides the AI-agent / volume confirmation gate.
    """
    return await execute_sniper_trade(db, trade_id, mode=mode, force=force)


@router.post("/signals/{signal_id}/execute")
async def execute_parsed_signal_endpoint(
    signal_id: int,
    mode: str = Query(default="sandbox", pattern=r"^(sandbox|live|both)$"),
    force: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
):
    """Execute a parsed telegram signal directly from the Active Signals tab.

    Places the order at the current market price on the requested target(s) and
    surfaces it on /trading.
    """
    return await execute_parsed_signal(db, signal_id, mode=mode, force=force)


@router.get("/prices")
async def get_prices_endpoint(
    symbols: str = Query(..., description="Comma-separated symbols, e.g. BTC/USDT,ETH/USDT"),
    db: AsyncSession = Depends(get_db),
):
    """Current live prices for active-signal symbols (for the UI)."""
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    prices = await get_signal_prices(db, syms)
    return {"prices": prices}


@router.post("/signals/{signal_id}/analyze")
async def analyze_signal_endpoint(signal_id: int, db: AsyncSession = Depends(get_db)):
    """Full AI + volume + sniper-entry analysis of a signal, with a decision."""
    return await analyze_signal_full(db, signal_id)


@router.get("/signals/volume-monitor")
async def volume_monitor_endpoint(limit: int = Query(default=25, ge=1, le=60), db: AsyncSession = Depends(get_db)):
    """Live volume read for all active signals (Volume Monitor tab)."""
    return await volume_monitor_snapshot(db, limit=limit)


@router.post("/signals/reanalyze-skipped")
async def reanalyze_skipped_endpoint(db: AsyncSession = Depends(get_db)):
    """Manually trigger the skipped-signal re-analysis cycle."""
    return await reanalyze_skipped_signals(db)


@router.post("/signals/volume-alert")
async def volume_alert_endpoint(
    body: dict = None,
    db: AsyncSession = Depends(get_db),
):
    """Process a volume-alert message text (e.g. from a webhook or manual trigger)."""
    text = (body or {}).get("text", "")
    if not text:
        raise HTTPException(status_code=422, detail="'text' field required")
    return await process_volume_channel_message(db, text)


@router.post("/signals/process-outcome")
async def process_outcome_endpoint(
    body: dict = None,
    db: AsyncSession = Depends(get_db),
):
    """Manually process a signal outcome / close message (for testing or webhooks).

    Parses the text for outcomes (TP hit, SL, closed, opposite-direction) and
    applies the status update to matching active signals — same logic as the
    monitor loop. Useful for manually triggering a close or opposite-direction
    reversal outside the 5-minute poll window.
    """
    from plugins.TelegramSignalNewsPlugin.backend.services.signal_parser import parse_outcome
    from sqlalchemy import select, desc

    text = (body or {}).get("text", "")
    channel_id: int | None = (body or {}).get("channel_id")
    if not text:
        raise HTTPException(status_code=422, detail="'text' field required")

    outcome = parse_outcome(text)
    if outcome is None:
        return {"ok": False, "message": "No outcome detected in text"}

    status_map = {
        "tp_hit": SignalStatus.TP_HIT,
        "sl_hit": SignalStatus.SL_HIT,
        "filled": SignalStatus.FILLED,
        "closed": SignalStatus.CLOSED,
        "opposite_direction": SignalStatus.CLOSED,
    }
    new_status = status_map.get(outcome.kind)
    if new_status is None:
        return {"ok": False, "message": f"Unknown outcome kind: {outcome.kind}"}

    q = (
        select(TelegramParsedSignal)
        .where(
            TelegramParsedSignal.symbol.ilike(f"%{outcome.symbol.replace('USDT','')}%"),
            TelegramParsedSignal.status.in_(
                [SignalStatus.ACTIVE, SignalStatus.FILLED, SignalStatus.TP_HIT]
            ),
        )
        .order_by(desc(TelegramParsedSignal.created_at))
        .limit(1)
    )
    if channel_id:
        q = q.where(TelegramParsedSignal.channel_source_id == channel_id)
    sig = (await db.execute(q)).scalar_one_or_none()

    if sig is None:
        return {"ok": False, "message": f"No active signal found for {outcome.symbol}"}

    sig.status = new_status
    sig.updated_at = now_utc_naive()

    cancelled_trades = 0
    auto_close_result: dict = {}
    if outcome.kind == "opposite_direction":
        # Cancel PENDING sniper trades
        sniper_q = await db.execute(
            select(TelegramSniperTrade).where(
                TelegramSniperTrade.signal_id == sig.id,
                TelegramSniperTrade.status == SniperTradeStatus.PENDING,
            )
        )
        for st in sniper_q.scalars().all():
            st.status = SniperTradeStatus.SKIPPED
            st.reason = (
                f"Cancelled — opposite direction signal: "
                f"{outcome.detail or 'direction reversed'}"
            )
            st.updated_at = now_utc_naive()
            cancelled_trades += 1
        await db.commit()
        # Auto-close PLACED (open) positions at market — protects from loss even
        # when the user is offline. Closes both sandbox and live positions.
        auto_close_result = await auto_close_positions_for_signal(
            db, sig.id,
            reason="Opposite direction detected — auto-closed by system",
        )
    else:
        await db.commit()

    sandbox_closed = len((auto_close_result.get("sandbox_closed") or []))
    live_closed = len((auto_close_result.get("live_closed") or []))
    close_errors = auto_close_result.get("errors") or []
    return {
        "ok": True,
        "outcome_kind": outcome.kind,
        "symbol": outcome.symbol,
        "signal_id": sig.id,
        "old_direction": sig.direction,
        "new_status": new_status.value,
        "cancelled_pending_trades": cancelled_trades,
        "auto_closed_sandbox": sandbox_closed,
        "auto_closed_live": live_closed,
        "close_errors": close_errors,
        "message": (
            f"Signal {sig.id} ({sig.symbol} {sig.direction}) closed — "
            f"{cancelled_trades} pending cancelled, "
            f"{sandbox_closed} sandbox + {live_closed} live position(s) closed at market."
            + (f" ⚠ errors: {'; '.join(str(e) for e in close_errors)}" if close_errors else "")
            if outcome.kind == "opposite_direction"
            else f"Signal {sig.id} updated to {new_status.value}"
        ),
    }


@router.get("/messages", response_model=list[TelegramIngestMessageResponse])
async def get_messages(
    user_id: str = "0",
    source_kind: Optional[str] = Query(default=None, pattern=r"^(signals|news)$"),
    channel_source_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    per_channel: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
):
    user = normalize_user_id(user_id)
    rows = await list_messages(
        db=db,
        user_id=user,
        source_kind=source_kind,
        channel_source_id=channel_source_id,
        limit=limit,
        per_channel=per_channel,
    )
    return rows


@router.get("/presets", response_model=list[TelegramChannelPresetResponse])
async def get_presets(
    source_kind: Optional[str] = Query(default=None, pattern=r"^(signals|news)$"),
    db: AsyncSession = Depends(get_db),
):
    rows = await list_presets(db, source_kind)
    return [to_preset_response(item) for item in rows]


@router.post("/presets", response_model=TelegramChannelPresetResponse)
async def post_preset(
    payload: TelegramChannelPresetCreate,
    db: AsyncSession = Depends(get_db),
):
    try:
        model = await create_preset(db, payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return to_preset_response(model)


@router.patch("/presets/{preset_id}", response_model=TelegramChannelPresetResponse)
async def patch_preset(
    preset_id: int,
    payload: TelegramChannelPresetUpdate,
    db: AsyncSession = Depends(get_db),
):
    model = await get_preset(db, preset_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Preset not found")

    updated = await update_preset(db, model, payload)
    return to_preset_response(updated)


@router.delete("/presets/{preset_id}")
async def remove_preset(
    preset_id: int,
    db: AsyncSession = Depends(get_db),
):
    model = await get_preset(db, preset_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Preset not found")

    await delete_preset(db, model)
    return {"deleted": True}


@router.post("/presets/{preset_id}/apply", response_model=TelegramApplyPresetResponse)
async def apply_channel_preset(
    preset_id: int,
    payload: TelegramApplyPresetRequest,
    db: AsyncSession = Depends(get_db),
):
    preset = await get_preset(db, preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail="Preset not found")

    try:
        return await apply_preset(
            db=db,
            preset=preset,
            user_id=normalize_user_id(payload.user_id),
            provider_registry=provider_registry,
            overwrite_existing=payload.overwrite_existing,
            verify_on_create=payload.verify_on_create,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
