"""
Agent Paul — JARVIS Knowledge Base & Chat Memory

Responsibilities:
  * Persist every JARVIS chat message so the assistant builds long-term memory.
  * Keep a single active conversation per client (session_key) — history is
    preserved across reloads/navigation until the user starts a NEW chat.
  * Store distilled "learnings", ingested news, and online research into a
    searchable knowledge table the assistant grounds future answers on.

All persistence is best-effort: failures are logged and swallowed so a DB
hiccup never breaks the chat stream.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from loguru import logger
from sqlalchemy import select, func, desc as sqldesc, or_
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.AgentPaulPlugin.backend.models import (
    PaulConversation,
    PaulChatMessage,
    PaulKnowledge,
)


# ── Conversations ──────────────────────────────────────────


async def get_active_conversation(
    db: AsyncSession, session_key: str
) -> Optional[PaulConversation]:
    """Return the current non-archived conversation for a client, or None."""
    try:
        row = (await db.execute(
            select(PaulConversation)
            .where(
                PaulConversation.session_key == session_key,
                PaulConversation.archived == False,  # noqa: E712
            )
            .order_by(sqldesc(PaulConversation.created_at))
            .limit(1)
        )).scalar_one_or_none()
        return row
    except Exception as exc:
        logger.debug(f"[KB] get_active_conversation error: {exc}")
        return None


async def get_or_create_conversation(
    db: AsyncSession, session_key: str
) -> Optional[PaulConversation]:
    """Get the active conversation or create a fresh one."""
    conv = await get_active_conversation(db, session_key)
    if conv:
        return conv
    try:
        conv = PaulConversation(session_key=session_key, archived=False, message_count=0)
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
        return conv
    except Exception as exc:
        logger.debug(f"[KB] create conversation error: {exc}")
        await db.rollback()
        return None


async def start_new_conversation(
    db: AsyncSession, session_key: str
) -> Optional[PaulConversation]:
    """Archive any active conversation and open a fresh one."""
    try:
        actives = (await db.execute(
            select(PaulConversation).where(
                PaulConversation.session_key == session_key,
                PaulConversation.archived == False,  # noqa: E712
            )
        )).scalars().all()
        for c in actives:
            c.archived = True
        await db.commit()
    except Exception as exc:
        logger.debug(f"[KB] archive error: {exc}")
        await db.rollback()
    return await get_or_create_conversation(db, session_key)


async def save_message(
    db: AsyncSession,
    session_key: str,
    role: str,
    content: str,
    pathname: str = "/",
) -> None:
    """Append a message to the active conversation (creates one if needed)."""
    if not content:
        return
    try:
        conv = await get_or_create_conversation(db, session_key)
        if not conv:
            return
        msg = PaulChatMessage(
            conversation_id=conv.id,
            role=role,
            content=content[:8000],
            pathname=pathname,
        )
        db.add(msg)
        conv.message_count = (conv.message_count or 0) + 1
        # Title from first user message
        if role == "user" and not conv.title:
            conv.title = content.strip()[:80]
        await db.commit()
    except Exception as exc:
        logger.debug(f"[KB] save_message error: {exc}")
        await db.rollback()


async def get_history(
    db: AsyncSession, session_key: str, limit: int = 40
) -> list[dict]:
    """Return ordered message dicts for the active conversation.

    Returns the MOST RECENT ``limit`` messages (re-ordered chronologically) so
    that long conversations never appear to "lose" their latest messages on
    reload — previously this fetched the OLDEST ``limit`` rows, which made every
    message past the cap vanish from the restored history. Nothing is deleted;
    the active conversation is kept intact until the user starts a new chat.
    """
    try:
        conv = await get_active_conversation(db, session_key)
        if not conv:
            return []
        rows = (await db.execute(
            select(PaulChatMessage)
            .where(PaulChatMessage.conversation_id == conv.id)
            .order_by(sqldesc(PaulChatMessage.created_at))
            .limit(limit)
        )).scalars().all()
        rows = list(reversed(rows))  # back to chronological (oldest → newest)
        return [
            {"role": r.role, "content": r.content, "ts": r.created_at.isoformat()}
            for r in rows
        ]
    except Exception as exc:
        logger.debug(f"[KB] get_history error: {exc}")
        return []


# ── Learned knowledge ──────────────────────────────────────


async def record_knowledge(
    db: AsyncSession,
    *,
    kind: str = "insight",
    content: str,
    source: Optional[str] = None,
    title: Optional[str] = None,
    url: Optional[str] = None,
    symbol: Optional[str] = None,
    topic: Optional[str] = None,
    sentiment: Optional[float] = None,
    importance: float = 0.5,
    published_at: Optional[datetime] = None,
) -> None:
    """Persist one knowledge item (de-duplicated by url/title when present)."""
    if not content:
        return
    try:
        # De-dup news/research by url or title
        if url or title:
            existing = (await db.execute(
                select(PaulKnowledge.id).where(
                    or_(
                        PaulKnowledge.url == url if url else False,
                        PaulKnowledge.title == title if title else False,
                    )
                ).limit(1)
            )).first()
            if existing:
                return
        db.add(PaulKnowledge(
            kind=kind, content=content[:6000], source=source, title=title,
            url=url, symbol=symbol, topic=topic, sentiment=sentiment,
            importance=importance, published_at=published_at,
        ))
        await db.commit()
    except Exception as exc:
        logger.debug(f"[KB] record_knowledge error: {exc}")
        await db.rollback()


async def search_knowledge(
    db: AsyncSession, query: str, limit: int = 8, symbol: Optional[str] = None
) -> list[dict]:
    """Keyword search over learned knowledge (case-insensitive ILIKE)."""
    try:
        stmt = select(PaulKnowledge)
        if symbol:
            stmt = stmt.where(PaulKnowledge.symbol.ilike(f"%{symbol}%"))
        if query:
            terms = [t for t in query.split() if len(t) > 2][:5]
            if terms:
                conds = [
                    or_(
                        PaulKnowledge.content.ilike(f"%{t}%"),
                        PaulKnowledge.title.ilike(f"%{t}%"),
                    )
                    for t in terms
                ]
                stmt = stmt.where(or_(*conds))
        rows = (await db.execute(
            stmt.order_by(sqldesc(PaulKnowledge.created_at)).limit(limit)
        )).scalars().all()
        return [
            {
                "kind": r.kind, "source": r.source, "title": r.title,
                "url": r.url, "symbol": r.symbol, "content": r.content,
                "sentiment": r.sentiment,
                "ts": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    except Exception as exc:
        logger.debug(f"[KB] search_knowledge error: {exc}")
        return []


async def recent_news(
    db: AsyncSession, limit: int = 12, symbol: Optional[str] = None
) -> list[dict]:
    """Most recent ingested news items, optionally filtered by symbol."""
    try:
        stmt = select(PaulKnowledge).where(PaulKnowledge.kind == "news")
        if symbol:
            stmt = stmt.where(PaulKnowledge.symbol.ilike(f"%{symbol}%"))
        rows = (await db.execute(
            stmt.order_by(sqldesc(PaulKnowledge.created_at)).limit(limit)
        )).scalars().all()
        return [
            {
                "source": r.source, "title": r.title, "url": r.url,
                "symbol": r.symbol, "sentiment": r.sentiment,
                "ts": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    except Exception as exc:
        logger.debug(f"[KB] recent_news error: {exc}")
        return []


async def seed_trading_glossary(db: AsyncSession) -> int:
    """Persist core trading term definitions to the knowledge base.

    Uses its own DB session so it never interferes with the caller's
    transaction. Idempotent: terms are de-duped by title. Returns count inserted.
    """
    from app.core.database import AsyncSessionLocal  # lazy import avoids circular dep

    TERMS: list[dict] = [
        # ── Risk Management ────────────────────────────────────────────────
        {"abbr": "SL",  "full": "Stop Loss",
         "def": "The price level at which a losing position is automatically closed to cap the maximum loss on a trade. Always size SL in ATR multiples."},
        {"abbr": "TP",  "full": "Take Profit",
         "def": "The target price where a winning trade is closed to lock in profit. Often set at TP1, TP2, TP3 for tiered exits."},
        {"abbr": "RR",  "full": "Risk/Reward Ratio",
         "def": "RR = distance to TP ÷ distance to SL. A 1:3 RR risks $1 to make $3. Minimum recommended RR for SMC setups is 1.5."},
        {"abbr": "BE",  "full": "Break Even",
         "def": "Moving the Stop Loss to the entry price after the trade moves in your favour, eliminating monetary risk."},
        {"abbr": "PnL", "full": "Profit and Loss",
         "def": "The net realised or unrealised gain/loss on a trade or account."},
        {"abbr": "ATR", "full": "Average True Range",
         "def": "Volatility indicator. Used to set dynamic SL distances (e.g. 1× ATR below entry)."},
        {"abbr": "DCA", "full": "Dollar Cost Averaging",
         "def": "Adding to an existing position at lower (or higher for short) prices to reduce the average entry."},
        # ── SMC ────────────────────────────────────────────────────────────
        {"abbr": "OB",  "full": "Order Block",
         "def": "The last opposing candle before a Break of Structure. A Bullish OB is the last bearish candle before a bullish BOS; price often returns to this zone to continue higher. A Bearish OB is the last bullish candle before a bearish BOS."},
        {"abbr": "FVG", "full": "Fair Value Gap",
         "def": "A 3-candle price imbalance: the wicks of candles 1 and 3 do not overlap candle 2's body. Represents an inefficiency that price is drawn back to fill. Also called Imbalance, Gap, or Void."},
        {"abbr": "IFVG","full": "Inverse/Inverted Fair Value Gap",
         "def": "A previously filled FVG that now acts as support (if was bullish FVG) or resistance (if was bearish FVG)."},
        {"abbr": "CE",  "full": "Consequent Encroachment",
         "def": "The exact midpoint (50%) of a Fair Value Gap. Acts as a magnetic level; price often reacts precisely at CE before reversing."},
        {"abbr": "BOS", "full": "Break of Structure",
         "def": "A candle close beyond the previous significant swing high (bullish BOS) or swing low (bearish BOS), confirming continuation of the current trend."},
        {"abbr": "CHoCH","full": "Change of Character",
         "def": "Price breaks the OPPOSITE structure swing (e.g. breaks a swing low during an uptrend), signalling a potential reversal. Also called MSS (Market Structure Shift)."},
        {"abbr": "MSS", "full": "Market Structure Shift",
         "def": "See CHoCH — a structural break that signals the current trend may be reversing."},
        {"abbr": "LQZ", "full": "Liquidity Zone",
         "def": "A price area where resting orders (stop losses of trapped traders) are clustered. SM targets these zones to fuel their entries."},
        {"abbr": "BSL", "full": "Buy-Side Liquidity",
         "def": "Clusters of buy stops sitting above swing highs / equal highs. SM raids BSL to fill sell orders before a reversal lower."},
        {"abbr": "SSL", "full": "Sell-Side Liquidity",
         "def": "Clusters of sell stops sitting below swing lows / equal lows. SM raids SSL to fill buy orders before a reversal higher."},
        {"abbr": "OTE", "full": "Optimal Trade Entry",
         "def": "The 61.8%–79% Fibonacci retracement zone of a swing move; the ideal area to enter within an Order Block or FVG. Also aligns with the RTO level."},
        {"abbr": "POI", "full": "Point of Interest",
         "def": "Any high-probability zone where price may react — includes OBs, FVGs, Liquidity Zones, and historical support/resistance."},
        {"abbr": "DOL", "full": "Draw on Liquidity",
         "def": "The nearest liquidity pool that price is being engineered toward (e.g. equal highs / previous day high). Knowing the DOL helps set TP levels."},
        {"abbr": "IDM", "full": "Inducement",
         "def": "A smaller swing high/low deliberately engineered by SM to trap retail traders before the true directional move."},
        {"abbr": "RTO", "full": "Return to Origin",
         "def": "Price revisiting the candle or zone (often an OB) where a displacement move originated."},
        {"abbr": "PD",  "full": "Premium / Discount",
         "def": "Premium = price above the 50% (EQ) of a range — expensive, favoured for sells. Discount = price below 50% — cheap, favoured for buys."},
        {"abbr": "EQ",  "full": "Equilibrium",
         "def": "The 50% midpoint of a price range or swing. Acts as a reference for premium/discount assessment."},
        {"abbr": "IPDA","full": "Interbank Price Delivery Algorithm",
         "def": "The internal mechanism by which price seeks and raids liquidity pools (BSL/SSL) across time frames before delivering price to the next POI."},
        {"abbr": "AMD", "full": "Accumulation Manipulation Distribution",
         "def": "ICT model: SM first accumulates positions during low volatility (Asian), then manipulates price to raid liquidity (Judas Swing at session open), then distributes/delivers in the true direction."},
        # ── Structure ──────────────────────────────────────────────────────
        {"abbr": "HH",  "full": "Higher High", "def": "A swing high above the previous swing high — confirms an uptrend."},
        {"abbr": "HL",  "full": "Higher Low",  "def": "A swing low above the previous swing low — confirms an uptrend and ideal entry zone."},
        {"abbr": "LH",  "full": "Lower High",  "def": "A swing high below the previous swing high — confirms a downtrend."},
        {"abbr": "LL",  "full": "Lower Low",   "def": "A swing low below the previous swing low — confirms a downtrend and ideal short entry zone."},
        {"abbr": "HTF", "full": "Higher Time Frame",
         "def": "A longer-duration chart (e.g. D1, H4, W1) used to determine the macro bias before drilling down to the LTF for entry."},
        {"abbr": "LTF", "full": "Lower Time Frame",
         "def": "A shorter-duration chart (e.g. M5, M1) used for precision entry after the HTF bias is confirmed."},
        {"abbr": "PDH", "full": "Previous Day High", "def": "The high of the prior trading day — major liquidity and reference level."},
        {"abbr": "PDL", "full": "Previous Day Low",  "def": "The low of the prior trading day — major liquidity and reference level."},
        {"abbr": "PWH", "full": "Previous Week High", "def": "The high of last week — important liquidity target on HTF."},
        {"abbr": "PWL", "full": "Previous Week Low",  "def": "The low of last week — important liquidity target on HTF."},
        {"abbr": "NDOG","full": "New Day Opening Gap",
         "def": "The gap between yesterday's close and today's open (if any); a draw for price in early session."},
        {"abbr": "NWOG","full": "New Week Opening Gap",
         "def": "The gap between Friday's close and Monday's open; a high-probability draw level for the week."},
        # ── Sessions ───────────────────────────────────────────────────────
        {"abbr": "KZ",  "full": "Kill Zone",
         "def": "High-probability entry windows near major session opens: London Open KZ (07:00-09:00 GMT), NY Open KZ (08:30-11:00 EST), Silver Bullet (03:00, 10:00, 14:00 EST)."},
        {"abbr": "Judas Swing", "full": "Judas Swing",
         "def": "A false breakout at or near a session open (especially London) that raids liquidity in one direction before price reverses and delivers in the true direction."},
        # ── Indicators ─────────────────────────────────────────────────────
        {"abbr": "RSI", "full": "Relative Strength Index",
         "def": "Momentum oscillator (0-100). >70 = overbought (potential sell), <30 = oversold (potential buy). In SMC, RSI divergence + POI = high-probability setup."},
        {"abbr": "MACD","full": "Moving Average Convergence Divergence",
         "def": "Trend and momentum indicator using two EMAs. MACD cross above signal = bullish momentum; below = bearish."},
        {"abbr": "VWAP","full": "Volume Weighted Average Price",
         "def": "Intraday average price weighted by volume; institutions use it as a benchmark. Price above VWAP = bullish intraday bias."},
        {"abbr": "EMA", "full": "Exponential Moving Average",
         "def": "Moving average that weighs recent prices more heavily. Common: 8, 21, 50, 200 EMA."},
        {"abbr": "OI",  "full": "Open Interest",
         "def": "Total number of open futures/options contracts. Rising OI + rising price = bullish. Falling OI + rising price = possible trend exhaustion."},
        {"abbr": "FR",  "full": "Funding Rate",
         "def": "Periodic payment between long and short futures holders. High positive FR = overleveraged longs, bearish signal. High negative FR = overleveraged shorts, bullish signal."},
    ]

    inserted = 0
    async with AsyncSessionLocal() as session:
        for t in TERMS:
            title = f"{t['abbr']} ({t['full']})"
            content = f"{t['abbr']} = {t['full']}. {t['def']}"
            try:
                existing = (await session.execute(
                    select(PaulKnowledge.id).where(PaulKnowledge.title == title).limit(1)
                )).first()
                if existing:
                    continue
                session.add(PaulKnowledge(
                    kind="definition",
                    title=title,
                    content=content[:2000],
                    source="trading_glossary",
                    topic="trading_terms",
                    importance=0.9,
                ))
                inserted += 1
            except Exception as exc:
                logger.debug(f"[KB] glossary item error for {t['abbr']}: {exc}")
        if inserted:
            try:
                await session.commit()
            except Exception as exc:
                logger.debug(f"[KB] glossary commit error: {exc}")
                await session.rollback()
                inserted = 0
    return inserted


async def knowledge_stats(db: AsyncSession) -> dict:
    """Counts used by the brain-map intelligence panel."""
    try:
        total = (await db.execute(
            select(func.count(PaulKnowledge.id))
        )).scalar() or 0
        news = (await db.execute(
            select(func.count(PaulKnowledge.id)).where(PaulKnowledge.kind == "news")
        )).scalar() or 0
        insights = (await db.execute(
            select(func.count(PaulKnowledge.id)).where(PaulKnowledge.kind == "insight")
        )).scalar() or 0
        msgs = (await db.execute(
            select(func.count(PaulChatMessage.id))
        )).scalar() or 0
        return {
            "knowledge_total": int(total),
            "news_items": int(news),
            "insights": int(insights),
            "messages_learned": int(msgs),
        }
    except Exception as exc:
        logger.debug(f"[KB] knowledge_stats error: {exc}")
        return {"knowledge_total": 0, "news_items": 0, "insights": 0, "messages_learned": 0}
