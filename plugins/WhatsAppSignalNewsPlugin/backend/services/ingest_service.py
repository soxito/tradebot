"""WhatsApp Ingest Service.

Handles webhook processing, message storage, deduplication,
and signal extraction from WhatsApp messages.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.exchanges.manager import exchange_manager, SupportedExchange
from plugins.WhatsAppSignalNewsPlugin.backend.config import (
    WhatsAppPluginConfig,
    build_config_from_db,
    whatsapp_plugin_config,
)
from plugins.WhatsAppSignalNewsPlugin.backend.models import (
    WhatsAppChannelPreset,
    WhatsAppChannelSource,
    WhatsAppMessage,
    WhatsAppParsedSignal,
    WhatsAppPluginSettings,
    WhatsAppSession,
    SignalStatus,
    WhatsAppChannelKind,
)
from plugins.WhatsAppSignalNewsPlugin.backend.services.openwa_client import OpenWAClient, OpenWAClientManager
from plugins.WhatsAppSignalNewsPlugin.backend.services.signal_parser import ParsedSignal, parse_signal, parse_outcome
from plugins.WhatsAppSignalNewsPlugin.backend.timezone_utils import now_utc_naive


# ────────────────────────────────────────────────────────────────────
# Webhook Verification
# ────────────────────────────────────────────────────────────────────

def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify HMAC signature from OpenWA webhook."""
    if not secret:
        return True  # No secret configured, skip verification
    try:
        expected = hmac.new(
            secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception as e:
        logger.warning(f"Webhook signature verification error: {e}")
        return False


# ────────────────────────────────────────────────────────────────────
# Message Deduplication
# ────────────────────────────────────────────────────────────────────

async def _is_duplicate_message(
    db: AsyncSession,
    session_id: str,
    message_id: str,
    ttl_hours: int = 24,
) -> bool:
    """Check if message was already processed (deduplication)."""
    cutoff = now_utc_naive() - timedelta(hours=ttl_hours)
    result = await db.execute(
        select(WhatsAppMessage.id)
        .where(WhatsAppMessage.session_id == session_id)
        .where(WhatsAppMessage.message_id == message_id)
        .where(WhatsAppMessage.received_at >= cutoff)
    )
    return result.scalar_one_or_none() is not None


async def _mark_channel_last_message(
    db: AsyncSession,
    channel_source_id: int,
    message_id: str,
    timestamp: datetime,
):
    """Update channel's last message tracking."""
    await db.execute(
        update(WhatsAppChannelSource)
        .where(WhatsAppChannelSource.id == channel_source_id)
        .values(
            last_message_id=message_id,
            last_message_timestamp=timestamp,
            updated_at=now_utc_naive(),
        )
    )


# ────────────────────────────────────────────────────────────────────
# Message Processing
# ────────────────────────────────────────────────────────────────────

async def process_webhook_payload(
    db: AsyncSession,
    payload: Dict[str, Any],
    config: WhatsAppPluginConfig,
) -> Dict[str, Any]:
    """Process incoming webhook payload from OpenWA.

    Returns dict with processing stats.
    """
    stats = {
        "processed": 0,
        "new_messages": 0,
        "new_signals": 0,
        "errors": [],
        "skipped_duplicates": 0,
    }

    event_type = payload.get("event")
    session_id = payload.get("sessionId") or payload.get("session_id")

    if not session_id:
        stats["errors"].append("Missing sessionId in payload")
        return stats

    # Handle different event types
    if event_type == "message":
        return await _process_message_event(db, payload, config, stats)
    elif event_type == "message_ack":
        return await _process_ack_event(db, payload, stats)
    elif event_type in ("qr", "session_status"):
        return await _process_session_event(db, payload, config, stats)
    else:
        logger.debug(f"Unhandled webhook event: {event_type}")
        stats["processed"] += 1
        return stats


async def _process_message_event(
    db: AsyncSession,
    payload: Dict[str, Any],
    config: WhatsAppPluginConfig,
    stats: Dict[str, Any],
) -> Dict[str, Any]:
    """Process incoming message event."""
    message_data = payload.get("data", {})
    message_id = message_data.get("id") or message_data.get("messageId")

    if not message_id:
        stats["errors"].append("Missing message ID")
        return stats

    session_id = payload.get("sessionId") or payload.get("session_id")
    chat_id = message_data.get("chatId") or message_data.get("from")
    from_me = message_data.get("fromMe", False)
    sender_id = message_data.get("senderId") or message_data.get("author")
    sender_name = message_data.get("senderName") or message_data.get("notifyName")
    text = message_data.get("body") or message_data.get("text") or ""
    message_type = message_data.get("type", "text")
    media_url = message_data.get("mediaUrl") or message_data.get("url")
    media_type = message_data.get("mediaType") or message_data.get("mimetype")
    timestamp_ms = message_data.get("timestamp") or message_data.get("t")
    whatsapp_timestamp = (
        datetime.fromtimestamp(timestamp_ms / 1000) if timestamp_ms else now_utc_naive()
    )

    # Deduplication check
    if await _is_duplicate_message(db, session_id, message_id, config.message_dedupe_ttl_hours):
        stats["skipped_duplicates"] += 1
        return stats

    # Find matching channel source
    channel_source = await _find_channel_source(db, session_id, chat_id)
    if not channel_source:
        # Not a monitored channel, store as orphan or ignore
        logger.debug(f"Message from unmonitored chat: {chat_id}")
        stats["processed"] += 1
        return stats

    # Store message
    msg = WhatsAppMessage(
        channel_source_id=channel_source.id,
        session_id=session_id,
        message_id=message_id,
        from_me=from_me,
        sender_id=sender_id,
        sender_name=sender_name,
        text=text[:10000] if text else None,  # Truncate long messages
        message_type=message_type,
        media_url=media_url,
        media_type=media_type,
        whatsapp_timestamp=whatsapp_timestamp,
        received_at=now_utc_naive(),
        processed=False,
        raw_payload=message_data,
    )
    db.add(msg)
    await db.flush()

    # Update channel last message
    await _mark_channel_last_message(db, channel_source.id, message_id, whatsapp_timestamp)

    stats["new_messages"] += 1
    stats["processed"] += 1

    # Parse signal if channel is for signals
    if channel_source.kind in (WhatsAppChannelKind.SIGNALS.value, WhatsAppChannelKind.VOLUME_ALERTS.value):
        if text and len(text.strip()) > 10:
            signal = await _extract_and_store_signal(
                db, channel_source, msg, text, config
            )
            if signal:
                stats["new_signals"] += 1
                msg.parsed_signal_id = signal.id
                msg.processed = True
            else:
                msg.processed = True  # Mark processed even if no signal found
        else:
            msg.processed = True
    else:
        msg.processed = True  # News channels don't parse signals

    # Check for outcome updates (TP hit, SL hit, etc.)
    if text:
        outcome = parse_outcome(text)
        if outcome:
            await _process_signal_outcome(db, channel_source, outcome, text)

    return stats


async def _process_ack_event(
    db: AsyncSession,
    payload: Dict[str, Any],
    stats: Dict[str, Any],
) -> Dict[str, Any]:
    """Process message acknowledgment (delivered/read)."""
    # Could update message status if needed
    stats["processed"] += 1
    return stats


async def _process_session_event(
    db: AsyncSession,
    payload: Dict[str, Any],
    config: WhatsAppPluginConfig,
    stats: Dict[str, Any],
) -> Dict[str, Any]:
    """Process session status or QR code updates."""
    session_id = payload.get("sessionId") or payload.get("session_id")
    event_type = payload.get("event")
    data = payload.get("data", {})

    if not session_id:
        return stats

    # Update session in DB
    result = await db.execute(
        select(WhatsAppSession).where(WhatsAppSession.session_id == session_id)
    )
    session = result.scalar_one_or_none()

    if session:
        if event_type == "qr":
            session.qr_code = data.get("qr") or data.get("qrCode")
            session.status = "qr_ready"
        elif event_type == "session_status":
            status = data.get("status") or data.get("state")
            if status:
                session.status = status
            if data.get("phone"):
                session.phone_number = data.get("phone")
            if data.get("name") or data.get("pushname"):
                session.platform = data.get("name") or data.get("pushname")
            if status in ("ready", "authenticated", "connected"):
                session.last_connected_at = now_utc_naive()

        session.updated_at = now_utc_naive()

    stats["processed"] += 1
    return stats


async def _find_channel_source(
    db: AsyncSession,
    session_id: str,
    chat_id: str,
) -> Optional[WhatsAppChannelSource]:
    """Find channel source matching session and chat."""
    result = await db.execute(
        select(WhatsAppChannelSource)
        .where(WhatsAppChannelSource.session_id == session_id)
        .where(WhatsAppChannelSource.chat_id == chat_id)
        .where(WhatsAppChannelSource.enabled == True)
    )
    return result.scalar_one_or_none()


async def _extract_and_store_signal(
    db: AsyncSession,
    channel_source: WhatsAppChannelSource,
    message: WhatsAppMessage,
    text: str,
    config: WhatsAppPluginConfig,
) -> Optional[WhatsAppParsedSignal]:
    """Extract signal from message text and store it."""
    try:
        parsed = parse_signal(text, channel_source.name)
        if not parsed:
            return None

        # Check if signal already exists for this message
        existing = await db.execute(
            select(WhatsAppParsedSignal).where(
                WhatsAppParsedSignal.whatsapp_message_id == message.message_id
            )
        )
        if existing.scalar_one_or_none():
            return None

        # Apply channel-specific defaults
        if channel_source.default_leverage and not parsed.leverage:
            parsed.leverage = channel_source.default_leverage
        if channel_source.default_tp_levels and not parsed.take_profits:
            parsed.take_profits = channel_source.default_tp_levels
        if channel_source.default_sl_pct and not parsed.stop_loss and parsed.entry:
            if parsed.direction.value in ("buy", "long"):
                parsed.stop_loss = parsed.entry * (1 - channel_source.default_sl_pct / 100)
                parsed.stop_loss_raw = f"{parsed.stop_loss:.4f}"
            else:
                parsed.stop_loss = parsed.entry * (1 + channel_source.default_sl_pct / 100)
                parsed.stop_loss_raw = f"{parsed.stop_loss:.4f}"

        signal = WhatsAppParsedSignal(
            channel_source_id=channel_source.id,
            message_id=message.id,
            whatsapp_message_id=message.message_id,
            symbol=parsed.symbol,
            direction=parsed.direction.value,
            leverage=parsed.leverage,
            entry=parsed.entry,
            entry_raw=parsed.entry_raw,
            stop_loss=parsed.stop_loss,
            stop_loss_raw=parsed.stop_loss_raw,
            trailing_sl=parsed.trailing_sl,
            take_profits=parsed.take_profits,
            market_type=parsed.market_type.value,
            confidence=parsed.confidence,
            status=SignalStatus.ACTIVE.value,
            raw_text=parsed.raw_text,
            posted_at=message.whatsapp_timestamp,
            extraction_method="regex",
        )
        db.add(signal)
        await db.flush()

        logger.info(
            f"New signal from WhatsApp: {signal.symbol} {signal.direction} "
            f"(conf={signal.confidence:.0%}) from {channel_source.name}"
        )
        return signal

    except Exception as e:
        logger.error(f"Failed to extract signal: {e}")
        return None


async def _process_signal_outcome(
    db: AsyncSession,
    channel_source: WhatsAppChannelSource,
    outcome: Any,
    raw_text: str,
):
    """Process signal outcome (TP hit, SL hit, closed, etc.)."""
    try:
        # Find active signal for this symbol
        result = await db.execute(
            select(WhatsAppParsedSignal)
            .where(WhatsAppParsedSignal.channel_source_id == channel_source.id)
            .where(WhatsAppParsedSignal.symbol.ilike(f"%{outcome.symbol.replace('USDT', '')}%"))
            .where(WhatsAppParsedSignal.status.in_([
                SignalStatus.ACTIVE.value,
                SignalStatus.FILLED.value,
                SignalStatus.TP_HIT.value,
            ]))
            .order_by(WhatsAppParsedSignal.posted_at.desc())
        )
        signal = result.scalar_one_or_none()

        if not signal:
            return

        status_map = {
            "tp_hit": SignalStatus.TP_HIT,
            "sl_hit": SignalStatus.SL_HIT,
            "filled": SignalStatus.FILLED,
            "closed": SignalStatus.CLOSED,
            "opposite_direction": SignalStatus.CLOSED,
        }
        new_status = status_map.get(outcome.kind)
        if not new_status:
            return

        signal.status = new_status.value
        signal.updated_at = now_utc_naive()

        if outcome.kind == "tp_hit" and outcome.tp_number:
            signal.tp_reached_count = max(signal.tp_reached_count, outcome.tp_number)

        logger.info(
            f"Signal {signal.id} ({signal.id} updated to {new_status.value} "
            f"from WhatsApp message in {channel_source.name}"
        )

    except Exception as e:
        logger.warning(f"Failed to process signal outcome: {e}")


# ────────────────────────────────────────────────────────────────────
# Manual Polling (fallback for when webhooks not available)
# ────────────────────────────────────────────────────────────────────

async def poll_channel_messages(
    db: AsyncSession,
    channel_source: WhatsAppChannelSource,
    client: OpenWAClient,
    limit: int = 50,
) -> Dict[str, Any]:
    """Manually poll messages from a channel (fallback polling)."""
    stats = {"new_messages": 0, "new_signals": 0, "errors": []}

    try:
        messages = await client.get_messages(
            channel_source.session_id,
            channel_source.chat_id,
            limit=limit,
        )

        for msg_data in messages:
            # Check if we already have this message
            message_id = msg_data.get("id") or msg_data.get("messageId")
            if not message_id:
                continue

            existing = await db.execute(
                select(WhatsAppMessage.id).where(
                    WhatsAppMessage.session_id == channel_source.session_id,
                    WhatsAppMessage.message_id == message_id,
                )
            )
            if existing.scalar_one_or_none():
                continue

            # Process similar to webhook
            text = msg_data.get("body") or msg_data.get("text") or ""
            timestamp_ms = msg_data.get("timestamp") or msg_data.get("t")
            whatsapp_timestamp = (
                datetime.fromtimestamp(timestamp_ms / 1000) if timestamp_ms else now_utc_naive()
            )

            msg = WhatsAppMessage(
                channel_source_id=channel_source.id,
                session_id=channel_source.session_id,
                message_id=message_id,
                from_me=msg_data.get("fromMe", False),
                sender_id=msg_data.get("senderId") or msg_data.get("author"),
                sender_name=msg_data.get("senderName") or msg_data.get("notifyName"),
                text=text[:10000] if text else None,
                message_type=msg_data.get("type", "text"),
                media_url=msg_data.get("mediaUrl") or msg_data.get("url"),
                media_type=msg_data.get("mediaType") or msg_data.get("mimetype"),
                whatsapp_timestamp=whatsapp_timestamp,
                received_at=now_utc_naive(),
                processed=False,
                raw_payload=msg_data,
            )
            db.add(msg)
            await db.flush()

            await _mark_channel_last_message(db, channel_source.id, message_id, whatsapp_timestamp)
            stats["new_messages"] += 1

            if text and len(text.strip()) > 10 and channel_source.kind == WhatsAppChannelKind.SIGNALS.value:
                signal = await _extract_and_store_signal(db, channel_source, msg, text, whatsapp_plugin_config)
                if signal:
                    stats["new_signals"] += 1
                    msg.parsed_signal_id = signal.id
                    msg.processed = True
                else:
                    msg.processed = True
            else:
                msg.processed = True

        await db.commit()

    except Exception as e:
        stats["errors"].append(str(e))
        logger.error(f"Poll error for {channel_source.name}: {e}")

    return stats


async def run_poll_all_channels(
    db: AsyncSession,
    config: WhatsAppPluginConfig,
    client: Optional[OpenWAClient] = None,
) -> Dict[str, Any]:
    """Run polling for all enabled channels."""
    if client is None:
        client = OpenWAClientManager.get_client(config)

    stats = {"total_messages": 0, "total_signals": 0, "channels_polled": 0, "errors": []}

    result = await db.execute(
        select(WhatsAppChannelSource).where(WhatsAppChannelSource.enabled == True)
    )
    channels = result.scalars().all()

    for channel in channels:
        stats["channels_polled"] += 1
        try:
            channel_stats = await poll_channel_messages(db, channel, client, config.max_messages_per_poll)
            stats["total_messages"] += channel_stats["new_messages"]
            stats["total_signals"] += channel_stats["new_signals"]
            stats["errors"].extend(channel_stats["errors"])
        except Exception as e:
            stats["errors"].append(f"{channel.name}: {e}")

    return stats


# ────────────────────────────────────────────────────────────────────
# Channel Discovery & Management
# ────────────────────────────────────────────────────────────────────

async def discover_chats(
    db: AsyncSession,
    session_id: str,
    client: OpenWAClient,
    only_groups: bool = False,
) -> List[Dict[str, Any]]:
    """Discover chats/groups from a WhatsApp session."""
    try:
        chats = await client.list_chats(session_id, limit=200, only_groups=only_groups)
        return [
            {
                "id": chat.get("id") or chat.get("chatId"),
                "name": chat.get("name") or chat.get("contact", {}).get("name") or chat.get("pushname"),
                "type": chat.get("type") or ("group" if chat.get("isGroup") else "contact"),
                "participant_count": chat.get("participantsCount") or len(chat.get("participants", [])),
                "is_read_only": chat.get("readOnly", False),
                "last_message_at": (
                    datetime.fromtimestamp(chat.get("lastMessage", {}).get("t", 0) / 1000)
                    if chat.get("lastMessage", {}).get("t")
                    else None
                ),
            }
            for chat in chats
            if chat.get("id") or chat.get("chatId")
        ]
    except Exception as e:
        logger.error(f"Failed to discover chats: {e}")
        return []


async def create_channel_from_preset(
    db: AsyncSession,
    preset: WhatsAppChannelPreset,
    session_id: str,
    user_id: int = 0,
) -> List[WhatsAppChannelSource]:
    """Create channel sources from a preset."""
    created = []
    for chat_id in preset.chat_ids:
        existing = await db.execute(
            select(WhatsAppChannelSource).where(
                WhatsAppChannelSource.session_id == session_id,
                WhatsAppChannelSource.chat_id == chat_id,
                WhatsAppChannelSource.user_id == user_id,
            )
        )
        if existing.scalar_one_or_none():
            continue

        channel = WhatsAppChannelSource(
            user_id=user_id,
            name=f"{preset.name} - {chat_id}",
            kind=preset.kind,
            source_type="group" if "@g.us" in chat_id else "contact",
            chat_id=chat_id,
            session_id=session_id,
            enabled=True,
            parse_signals=(preset.kind == WhatsAppChannelKind.SIGNALS.value),
            **preset.default_config,
        )
        db.add(channel)
        created.append(channel)

    if created:
        await db.flush()

    return created


# ────────────────────────────────────────────────────────────────────
# Session Management Helpers
# ────────────────────────────────────────────────────────────────────

async def ensure_default_session(
    db: AsyncSession,
    config: WhatsAppPluginConfig,
    client: OpenWAClient,
) -> WhatsAppSession:
    """Ensure default session exists and is running."""
    # Check DB for existing default session
    result = await db.execute(
        select(WhatsAppSession).where(WhatsAppSession.is_default == True)
    )
    session = result.scalar_one_or_none()

    if not session:
        # Create new session via OpenWA
        try:
            created = await client.create_session(config.default_session_name)
            session_id = created.get("id") or created.get("sessionId")
            if session_id:
                await client.start_session(session_id)
        except Exception as e:
            logger.error(f"Failed to create default session: {e}")

        # Check again
        result = await db.execute(
            select(WhatsAppSession).where(WhatsAppSession.session_id == session_id)
        )
        session = result.scalar_one_or_none()

        if not session and session_id:
            session = WhatsAppSession(
                session_id=session_id,
                name=config.default_session_name,
                is_default=True,
            )
            db.add(session)
            await db.flush()

    return session


async def get_session_status(
    db: AsyncSession,
    client: OpenWAClient,
    session_id: str,
) -> Dict[str, Any]:
    """Get combined session status from DB and OpenWA."""
    # Get from DB
    result = await db.execute(
        select(WhatsAppSession).where(WhatsAppSession.session_id == session_id)
    )
    session = result.scalar_one_or_none()

    # Get from OpenWA
    try:
        openwa_status = await client.get_session_status(session_id)
    except Exception as e:
        openwa_status = {"error": str(e)}

    return {
        "db": {
            "id": session.session_id if session else None,
            "name": session.name if session else None,
            "status": session.status if session else None,
            "phone": session.phone_number if session else None,
            "platform": session.platform if session else None,
            "qr_code": session.qr_code if session else None,
            "last_connected": session.last_connected_at.isoformat() if session and session.last_connected_at else None,
        },
        "openwa": openwa_status,
    }