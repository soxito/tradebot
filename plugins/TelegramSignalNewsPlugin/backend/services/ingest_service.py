"""Telegram channel source management and ingestion orchestration."""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import and_, desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.TelegramSignalNewsPlugin.backend.config import TelegramPluginConfig
from plugins.TelegramSignalNewsPlugin.backend.models import (
    PollRunStatus,
    SourceKind,
    TelegramChannelPreset,
    TelegramChannelSource,
    TelegramIngestMessage,
    TelegramPollRun,
)
from plugins.TelegramSignalNewsPlugin.backend.schemas import (
    TelegramApplyPresetResponse,
    TelegramChannelPresetCreate,
    TelegramChannelPresetUpdate,
    TelegramChannelSourceCreate,
    TelegramChannelSourceUpdate,
    TelegramExtractionResult,
    TelegramPollRequest,
    TelegramPollResult,
)
from plugins.TelegramSignalNewsPlugin.backend.services.extractor import extract_message
from plugins.TelegramSignalNewsPlugin.backend.services.telegram_provider import TelegramProviderRegistry
from plugins.TelegramSignalNewsPlugin.backend.timezone_utils import now_utc_naive


def normalize_user_id(user_id: str | int | None) -> int:
    if user_id is None:
        return 0
    try:
        return int(str(user_id))
    except (TypeError, ValueError):
        return 0


def to_source_kind(value: str | SourceKind) -> SourceKind:
    if isinstance(value, SourceKind):
        return value
    return SourceKind.NEWS if str(value) == "news" else SourceKind.SIGNALS


def to_source_response(model: TelegramChannelSource) -> dict[str, Any]:
    return {
        "id": model.id,
        "user_id": model.user_id,
        "title": model.title,
        "channel_handle": model.channel_handle,
        "channel_id": model.channel_id,
        "source_kind": model.source_kind.value,
        "provider": model.provider,
        "is_enabled": model.is_enabled,
        "market_type": getattr(model, "market_type", "crypto"),
        "poll_interval_seconds": model.poll_interval_seconds,
        "include_keywords": model.include_keywords_json,
        "exclude_keywords": model.exclude_keywords_json,
        "language_hint": model.language_hint,
        "last_message_id": model.last_message_id,
        "last_polled_at": model.last_polled_at,
        "last_error": model.last_error,
        "created_at": model.created_at,
        "updated_at": model.updated_at,
    }


def to_preset_response(model: TelegramChannelPreset) -> dict[str, Any]:
    return {
        "id": model.id,
        "slug": model.slug,
        "name": model.name,
        "description": model.description,
        "source_kind": model.source_kind.value,
        "channels": model.channels_json or [],
        "is_public": model.is_public,
        "created_at": model.created_at,
        "updated_at": model.updated_at,
    }


async def list_sources(
    db: AsyncSession,
    user_id: int,
    source_kind: str | None,
) -> list[TelegramChannelSource]:
    q = select(TelegramChannelSource).where(TelegramChannelSource.user_id == user_id)
    if source_kind:
        q = q.where(TelegramChannelSource.source_kind == to_source_kind(source_kind))
    q = q.order_by(TelegramChannelSource.source_kind, TelegramChannelSource.title)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_source_for_user(
    db: AsyncSession,
    source_id: int,
    user_id: int,
) -> TelegramChannelSource | None:
    result = await db.execute(
        select(TelegramChannelSource).where(
            and_(
                TelegramChannelSource.id == source_id,
                TelegramChannelSource.user_id == user_id,
            )
        )
    )
    return result.scalar_one_or_none()


async def create_source(
    db: AsyncSession,
    payload: TelegramChannelSourceCreate,
    provider_registry: TelegramProviderRegistry,
) -> TelegramChannelSource:
    user_id = normalize_user_id(payload.user_id)
    source_kind = to_source_kind(payload.source_kind)
    channel_handle = normalize_handle(payload.channel_handle)

    existing = await db.execute(
        select(TelegramChannelSource).where(
            and_(
                TelegramChannelSource.user_id == user_id,
                TelegramChannelSource.channel_handle == channel_handle,
                TelegramChannelSource.source_kind == source_kind,
            )
        )
    )
    if existing.scalar_one_or_none():
        raise ValueError("Channel source already exists for this user and source kind")

    title = payload.title or channel_handle
    channel_id: str | None = None
    provider = payload.provider

    if payload.verify_on_create:
        info, used_provider = await provider_registry.resolve_channel(channel_handle, provider)
        title = payload.title or info.title
        channel_id = info.channel_id
        provider = used_provider

    model = TelegramChannelSource(
        user_id=user_id,
        title=title,
        channel_handle=channel_handle,
        channel_id=channel_id,
        source_kind=source_kind,
        provider=provider,
        is_enabled=True,
        market_type=getattr(payload, "market_type", "crypto"),
        poll_interval_seconds=payload.poll_interval_seconds,
        include_keywords_json=payload.include_keywords,
        exclude_keywords_json=payload.exclude_keywords,
        language_hint=payload.language_hint,
    )
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return model


async def update_source(
    db: AsyncSession,
    model: TelegramChannelSource,
    payload: TelegramChannelSourceUpdate,
) -> TelegramChannelSource:
    updates = payload.model_dump(exclude_unset=True)
    if "source_kind" in updates:
        updates["source_kind"] = to_source_kind(updates["source_kind"])
    if "include_keywords" in updates:
        updates["include_keywords_json"] = updates.pop("include_keywords")
    if "exclude_keywords" in updates:
        updates["exclude_keywords_json"] = updates.pop("exclude_keywords")

    for key, value in updates.items():
        setattr(model, key, value)

    await db.commit()
    await db.refresh(model)
    return model


async def delete_source(
    db: AsyncSession,
    model: TelegramChannelSource,
) -> None:
    await db.delete(model)
    await db.commit()


async def list_presets(
    db: AsyncSession,
    source_kind: str | None,
) -> list[TelegramChannelPreset]:
    q = select(TelegramChannelPreset)
    if source_kind:
        q = q.where(TelegramChannelPreset.source_kind == to_source_kind(source_kind))
    q = q.order_by(TelegramChannelPreset.name)
    result = await db.execute(q)
    return list(result.scalars().all())


async def get_preset(db: AsyncSession, preset_id: int) -> TelegramChannelPreset | None:
    return await db.get(TelegramChannelPreset, preset_id)


async def create_preset(
    db: AsyncSession,
    payload: TelegramChannelPresetCreate,
) -> TelegramChannelPreset:
    model = TelegramChannelPreset(
        slug=payload.slug,
        name=payload.name,
        description=payload.description,
        source_kind=to_source_kind(payload.source_kind),
        channels_json=[normalize_handle(item) for item in payload.channels],
        is_public=payload.is_public,
    )
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return model


async def update_preset(
    db: AsyncSession,
    model: TelegramChannelPreset,
    payload: TelegramChannelPresetUpdate,
) -> TelegramChannelPreset:
    updates = payload.model_dump(exclude_unset=True)
    if "source_kind" in updates:
        updates["source_kind"] = to_source_kind(updates["source_kind"])
    if "channels" in updates:
        updates["channels_json"] = [normalize_handle(item) for item in updates.pop("channels")]

    for key, value in updates.items():
        setattr(model, key, value)

    await db.commit()
    await db.refresh(model)
    return model


async def delete_preset(db: AsyncSession, model: TelegramChannelPreset) -> None:
    await db.delete(model)
    await db.commit()


async def apply_preset(
    db: AsyncSession,
    preset: TelegramChannelPreset,
    user_id: int,
    provider_registry: TelegramProviderRegistry,
    overwrite_existing: bool,
    verify_on_create: bool,
) -> TelegramApplyPresetResponse:
    created_count = 0
    skipped_count = 0

    for raw_handle in preset.channels_json or []:
        handle = normalize_handle(raw_handle)
        existing = await db.execute(
            select(TelegramChannelSource).where(
                and_(
                    TelegramChannelSource.user_id == user_id,
                    TelegramChannelSource.channel_handle == handle,
                    TelegramChannelSource.source_kind == preset.source_kind,
                )
            )
        )
        model = existing.scalar_one_or_none()
        if model and not overwrite_existing:
            skipped_count += 1
            continue

        title = handle
        channel_id = None
        provider = "auto"
        if verify_on_create:
            info, used_provider = await provider_registry.resolve_channel(handle, "auto")
            title = info.title
            channel_id = info.channel_id
            provider = used_provider

        if model:
            model.title = title
            model.channel_id = channel_id
            model.provider = provider
            model.is_enabled = True
        else:
            db.add(
                TelegramChannelSource(
                    user_id=user_id,
                    title=title,
                    channel_handle=handle,
                    channel_id=channel_id,
                    source_kind=preset.source_kind,
                    provider=provider,
                    is_enabled=True,
                    poll_interval_seconds=300,
                )
            )
            created_count += 1

    await db.commit()

    return TelegramApplyPresetResponse(
        preset_id=preset.id,
        created_count=created_count,
        skipped_count=skipped_count,
    )


async def run_poll(
    db: AsyncSession,
    request: TelegramPollRequest,
    provider_registry: TelegramProviderRegistry,
    cfg: TelegramPluginConfig,
) -> TelegramPollResult:
    user_id = normalize_user_id(request.user_id)
    q = select(TelegramChannelSource).where(
        and_(
            TelegramChannelSource.user_id == user_id,
            TelegramChannelSource.is_enabled.is_(True),
        )
    )

    if request.channel_source_ids:
        q = q.where(TelegramChannelSource.id.in_(request.channel_source_ids))

    channels_result = await db.execute(q.order_by(TelegramChannelSource.id))
    channels = list(channels_result.scalars().all())

    run = TelegramPollRun(
        user_id=user_id,
        status=PollRunStatus.SUCCESS,
        channels_scanned=0,
        messages_read=0,
        messages_saved=0,
        errors_json=[],
        started_at=now_utc_naive(),
    )
    db.add(run)
    await db.flush()

    errors: list[dict[str, Any]] = []
    messages_saved = 0
    messages_read = 0
    channels_scanned = 0

    for channel in channels:
        limit = min(request.limit_per_channel, max(channel.poll_interval_seconds // 3, 1), cfg.poll_limit)
        limit = max(1, limit)

        try:
            rows, used_provider = await provider_registry.fetch_recent_messages(
                channel_ref=channel.channel_handle,
                limit=limit,
                min_message_id=channel.last_message_id,
                provider_hint=channel.provider,
            )
            channel.provider = used_provider
            channels_scanned += 1
            messages_read += len(rows)
        except Exception as exc:
            channel.last_error = str(exc)
            channel.last_polled_at = now_utc_naive()
            errors.append({"channel_id": channel.id, "error": str(exc)})
            continue

        for row in rows:
            dedupe_hash = compute_dedupe_hash(channel.id, row.message_id, row.text)

            extraction = await extract_message(
                text=row.text,
                source_kind=channel.source_kind.value,
                cfg=cfg,
            )

            insert_stmt = (
                pg_insert(TelegramIngestMessage)
                .values(
                    channel_source_id=channel.id,
                    source_kind=channel.source_kind,
                    telegram_message_id=row.message_id,
                    posted_at=row.posted_at,
                    author_name=row.author_name,
                    raw_text=row.text,
                    normalized_text=row.text.strip(),
                    extraction_json=extraction.model_dump(),
                    symbols_json=extraction.symbols,
                    confidence=extraction.confidence,
                    dedupe_hash=dedupe_hash,
                )
                .on_conflict_do_nothing()
                .returning(TelegramIngestMessage.id)
            )
            inserted_id = (await db.execute(insert_stmt)).scalar_one_or_none()
            if inserted_id is not None:
                messages_saved += 1
                channel.last_message_id = max_message_id(channel.last_message_id, row.message_id)

        channel.last_error = None
        channel.last_polled_at = now_utc_naive()

    if errors and messages_saved:
        run.status = PollRunStatus.PARTIAL
    elif errors and not messages_saved:
        run.status = PollRunStatus.FAILED
    else:
        run.status = PollRunStatus.SUCCESS

    run.channels_scanned = channels_scanned
    run.messages_read = messages_read
    run.messages_saved = messages_saved
    run.errors_json = errors
    run.completed_at = now_utc_naive()

    await db.commit()

    return TelegramPollResult(
        poll_run_id=run.id,
        status=run.status.value,
        channels_scanned=channels_scanned,
        messages_read=messages_read,
        messages_saved=messages_saved,
        errors=errors,
    )


async def preview_source_messages(
    source: TelegramChannelSource,
    limit: int,
    provider_registry: TelegramProviderRegistry,
    cfg: TelegramPluginConfig,
) -> list[dict[str, Any]]:
    rows, _ = await provider_registry.fetch_recent_messages(
        channel_ref=source.channel_handle,
        limit=limit,
        min_message_id=None,
        provider_hint=source.provider,
    )

    preview: list[dict[str, Any]] = []
    for row in rows:
        extraction = await extract_message(
            text=row.text,
            source_kind=source.source_kind.value,
            cfg=cfg,
        )
        preview.append(
            {
                "id": 0,
                "channel_source_id": source.id,
                "source_kind": source.source_kind.value,
                "telegram_message_id": row.message_id,
                "posted_at": row.posted_at,
                "author_name": row.author_name,
                "raw_text": row.text,
                "extraction_json": extraction.model_dump(),
                "symbols_json": extraction.symbols,
                "confidence": extraction.confidence,
                "created_at": now_utc_naive(),
            }
        )
    return preview


async def list_messages(
    db: AsyncSession,
    user_id: int,
    source_kind: str | None,
    channel_source_id: int | None,
    limit: int,
    per_channel: bool = True,
) -> list[TelegramIngestMessage]:
    base_filters = [TelegramChannelSource.user_id == user_id]
    if source_kind:
        base_filters.append(TelegramIngestMessage.source_kind == to_source_kind(source_kind))
    if channel_source_id is not None:
        base_filters.append(TelegramIngestMessage.channel_source_id == channel_source_id)

    # When no specific channel is selected and per_channel is on, return up to
    # `limit` of the most-recent messages FROM EACH channel (not a global cap).
    if per_channel and channel_source_id is None:
        chan_q = (
            select(TelegramChannelSource.id)
            .where(TelegramChannelSource.user_id == user_id)
        )
        if source_kind:
            chan_q = chan_q.where(
                TelegramChannelSource.source_kind == to_source_kind(source_kind)
            )
        chan_rows = await db.execute(chan_q)
        channel_ids = [row[0] for row in chan_rows.all()]

        collected: list[TelegramIngestMessage] = []
        for cid in channel_ids:
            per_q = (
                select(TelegramIngestMessage)
                .where(
                    TelegramIngestMessage.channel_source_id == cid,
                    *(
                        [TelegramIngestMessage.source_kind == to_source_kind(source_kind)]
                        if source_kind
                        else []
                    ),
                )
                .order_by(desc(TelegramIngestMessage.created_at))
                .limit(limit)
            )
            res = await db.execute(per_q)
            collected.extend(res.scalars().all())

        collected.sort(key=lambda m: m.created_at or datetime.min, reverse=True)
        return collected

    q = (
        select(TelegramIngestMessage)
        .join(
            TelegramChannelSource,
            TelegramIngestMessage.channel_source_id == TelegramChannelSource.id,
        )
        .where(*base_filters)
        .order_by(desc(TelegramIngestMessage.created_at))
        .limit(limit)
    )
    result = await db.execute(q)
    return list(result.scalars().all())


async def build_status(
    db: AsyncSession,
    provider_registry: TelegramProviderRegistry,
) -> dict[str, Any]:
    channels_total = await db.scalar(select(func.count(TelegramChannelSource.id))) or 0
    channels_enabled = await db.scalar(
        select(func.count(TelegramChannelSource.id)).where(TelegramChannelSource.is_enabled.is_(True))
    ) or 0
    messages_total = await db.scalar(select(func.count(TelegramIngestMessage.id))) or 0

    return {
        "plugin": "telegram",
        "version": "1.0.0",
        "providers": provider_registry.status(),
        "channels_total": int(channels_total),
        "channels_enabled": int(channels_enabled),
        "messages_total": int(messages_total),
    }


def normalize_handle(raw: str) -> str:
    value = (raw or "").strip()
    if value.startswith("https://t.me/"):
        value = value.removeprefix("https://t.me/")
    if value.startswith("t.me/"):
        value = value.removeprefix("t.me/")
    if value.lstrip("-").isdigit():
        return value
    if not value.startswith("@"):
        value = f"@{value}"
    return value


def max_message_id(left: str | None, right: str | None) -> str | None:
    if left is None:
        return right
    if right is None:
        return left
    try:
        return str(max(int(left), int(right)))
    except ValueError:
        return max(left, right)


def compute_dedupe_hash(channel_source_id: int, message_id: str, text: str) -> str:
    key = f"{channel_source_id}:{message_id}:{text.strip()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
