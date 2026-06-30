"""
JARVIS Skills & Hooks Service
─────────────────────────────
Skills:  domain knowledge that JARVIS injects into its system prompt when
         the user's message matches trigger keywords.
Hooks:   automated reactions to events (new signal, price threshold, etc.)

Default skills and hooks are seeded once on first startup and are labelled
is_default=True so they show separately from user-created entries.
"""
from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.AgentPaulPlugin.backend.models import PaulSkill, PaulHook, PaulHookTrigger


# ── Default seeds ───────────────────────────────────────────────────────────

DEFAULT_SKILLS: list[dict] = [
    {
        "name": "MT5 Trading Specialist",
        "description": "Deep knowledge of MT5 positions, P&L, account management and risk.",
        "trigger_keywords": ["mt5", "position", "trade", "order", "lot", "margin", "equity",
                             "balance", "execute", "open position", "close position"],
        "system_prompt_addition": (
            "You are an MT5 trading specialist. When answering, always reference "
            "the live MT5 positions and account data provided in the context. "
            "Calculate lot sizes using 1% risk per trade unless specified otherwise. "
            "Always state entry, SL, TP and lot size when discussing trade setups."
        ),
        "is_default": True,
    },
    {
        "name": "Crypto Market Analyst",
        "description": "Expert on crypto trading, Bitget futures, DeFi, on-chain data.",
        "trigger_keywords": ["crypto", "bitcoin", "btc", "eth", "ethereum", "solana",
                             "defi", "altcoin", "futures", "perpetual", "binance",
                             "bitget", "pump", "rug", "sniper"],
        "system_prompt_addition": (
            "You are a crypto market expert. Reference live sentiment, news, and "
            "on-chain signals when available. For trade ideas, always mention "
            "funding rate implications, liquidity zones, and whether the setup is "
            "spot or futures. Default to USDT-margined perpetuals on Bitget."
        ),
        "is_default": True,
    },
    {
        "name": "Gold / Precious Metals Expert",
        "description": "Specialist in XAUUSD, XAGUSD, commodity correlations and DXY.",
        "trigger_keywords": ["gold", "xauusd", "silver", "xagusd", "commodity",
                             "dxy", "dollar index", "precious metal", "inflation"],
        "system_prompt_addition": (
            "You are a gold and precious metals trading expert. When discussing "
            "XAUUSD: consider DXY correlation (inverse), 10Y yields, geopolitical "
            "risk, and COT positioning. XAUUSD pip value is $10/lot on a standard "
            "account. Round to 2 decimal places for entry/SL/TP."
        ),
        "is_default": True,
    },
    {
        "name": "SMC & Trading Terminology Expert",
        "description": "Deep knowledge of Smart Money Concepts and all standard trading abbreviations.",
        "trigger_keywords": [
            # Risk management
            "sl", "tp", "tp1", "tp2", "tp3", "rr", "rrr", "be", "be+",
            "stop loss", "take profit", "risk reward", "break even",
            # SMC concepts
            "ob", "fvg", "bos", "choch", "mss", "ifvg", "ce",
            "order block", "fair value gap", "imbalance", "liquidity",
            "lqz", "bsl", "ssl", "poi", "rto", "ote", "inducement", "idm",
            "displacement", "mitigation", "draw on liquidity", "dol",
            "ipda", "premium", "discount", "equilibrium",
            # Structure
            "hh", "hl", "lh", "ll", "swing high", "swing low",
            "htf", "ltf", "higher time frame", "lower time frame",
            "pdh", "pdl", "pwh", "pwl", "pmh", "pml",
            "nwog", "ndog",
            # Sessions
            "kill zone", "kz", "judas swing", "amd", "session open",
            # Indicators
            "rsi", "atr", "macd", "vwap", "ema", "sma", "poc",
            "bollinger", "funding rate", "open interest",
            # Patterns
            "h&s", "head and shoulders", "double top", "double bottom",
            "bull flag", "bear flag", "wedge",
        ],
        "system_prompt_addition": (
            "You are an SMC (Smart Money Concepts) and technical analysis expert. "
            "The user is fluent in trading shorthand. Mirror their vocabulary naturally:\n"
            "• SL = Stop Loss  |  TP = Take Profit  |  RR = Risk/Reward ratio\n"
            "• OB = Order Block (supply/demand zone where SM placed orders)\n"
            "• FVG = Fair Value Gap / imbalance (3-candle inefficiency to be filled)\n"
            "• CE = Consequent Encroachment (midpoint of FVG, highly magnetic)\n"
            "• BOS = Break of Structure (trend continuation signal)\n"
            "• CHoCH / MSS = Change of Character / Market Structure Shift (reversal signal)\n"
            "• LQZ / BSL / SSL = Liquidity Zone / Buy-Side / Sell-Side liquidity\n"
            "• OTE = Optimal Trade Entry (61.8%–79% fib inside OB)\n"
            "• BE = Break Even  |  PDH/PDL = Previous Day High/Low\n"
            "• HTF = Higher Time Frame  |  LTF = Lower Time Frame\n"
            "• DOL = Draw on Liquidity (where price is engineered to reach next)\n"
            "• IDM = Inducement (false swing before true direction)\n"
            "• AMD = Accumulation → Manipulation → Distribution\n"
            "• IPDA = Interbank Price Delivery Algorithm\n"
            "When discussing setups, always state: entry zone (OB/FVG), SL placement, "
            "TP1/TP2/TP3 targets, and the RR. Flag the HTF bias first, then LTF entry."
        ),
        "is_default": True,
    },
    {
        "name": "Signal & Sniper Analyst",
        "description": "Interprets TradingView signals and Telegram sniper setups.",
        "trigger_keywords": ["signal", "sniper", "setup", "best signal", "execute signal",
                             "tradingview", "telegram signal", "entry signal",
                             "sniper signal", "based on signals"],
        "system_prompt_addition": (
            "You are a signal and sniper setup analyst. When the user asks about "
            "signals or asks you to execute one, review ALL available signals in "
            "the context, rank them by confidence and recency, and clearly recommend "
            "the single best setup with entry/SL/TP. State your reasoning explicitly."
        ),
        "is_default": True,
    },
    {
        "name": "Risk Management Specialist",
        "description": "Position sizing, drawdown control, portfolio risk.",
        "trigger_keywords": ["risk", "lot size", "position size", "drawdown", "exposure",
                             "risk reward", "r:r", "portfolio", "max loss"],
        "system_prompt_addition": (
            "You are a risk management specialist. Default to 1% risk per trade. "
            "Always calculate maximum positions open, total exposure, and warn if "
            "combined risk exceeds 5% of account equity. Use the live MT5 "
            "balance/equity provided in context for calculations."
        ),
        "is_default": True,
    },
    {
        "name": "Market News & Sentiment Analyst",
        "description": "Interprets live news, sentiment and macro events for trading impact.",
        "trigger_keywords": ["news", "sentiment", "market update", "what happened",
                             "geopolitical", "fed", "fomc", "inflation", "cpi",
                             "iran", "war", "conflict", "rate hike", "economic"],
        "system_prompt_addition": (
            "You are a market news and sentiment analyst. Summarise the latest "
            "headlines from the Live Web Search Results section in the context. "
            "Explain the likely market impact on Gold, BTC, and the dollar index. "
            "Always cite your sources (news article titles) and provide a sentiment "
            "score (bullish/bearish/neutral) for the top 3 assets."
        ),
        "is_default": True,
    },
]

DEFAULT_HOOKS: list[dict] = [
    {
        "name": "New High-Confidence Signal Alert",
        "description": "Speaks an alert whenever a new signal with confidence ≥ 80% is detected.",
        "trigger_type": PaulHookTrigger.ON_SIGNAL,
        "condition": {"min_confidence": 0.80},
        "action_template": (
            "A high-confidence {{direction}} signal has arrived for {{symbol}} with "
            "{{confidence}}% confidence. Entry at {{entry}}, stop loss {{sl}}, "
            "take profit {{tp}}. Do you want me to execute this, Sir?"
        ),
        "action_type": "speak",
        "is_default": True,
    },
    {
        "name": "Floating P&L Drop Alert",
        "description": "Warns when total floating P&L drops below -$50.",
        "trigger_type": PaulHookTrigger.ON_POSITION,
        "condition": {"pnl_threshold_usd": -50},
        "action_template": (
            "Warning Sir — your total floating P&L has dropped to {{pnl}}. "
            "You have {{open_positions}} open positions. "
            "Shall I review your positions and suggest risk reduction?"
        ),
        "action_type": "speak",
        "is_default": True,
    },
    {
        "name": "Execute Voice Trade Command",
        "description": "Listens for 'execute' + symbol in voice commands and runs the trade.",
        "trigger_type": PaulHookTrigger.ON_COMMAND,
        "condition": {"patterns": ["execute", "place order", "open trade", "buy now", "sell now"]},
        "action_template": (
            "Execute a trade for {{symbol}} based on the best available signal. "
            "Use 1% risk and the current MT5 account equity for sizing."
        ),
        "action_type": "trade",
        "is_default": True,
    },
    {
        "name": "Daily Market Briefing",
        "description": "Automatic morning briefing with live news and open positions.",
        "trigger_type": PaulHookTrigger.ON_SCHEDULE,
        "condition": {"cron": "0 7 * * 1-5"},  # weekdays at 07:00
        "action_template": (
            "Good morning, Sir. Give me a complete market briefing including: "
            "1) Current open MT5 positions and total P&L. "
            "2) Top 3 market-moving news from today. "
            "3) Gold and BTC sentiment. "
            "4) Any signals that triggered overnight."
        ),
        "action_type": "speak",
        "is_default": True,
    },
    {
        "name": "Sniper Setup Ready Alert",
        "description": "Speaks when a Telegram sniper setup transitions to 'triggered'.",
        "trigger_type": PaulHookTrigger.ON_SIGNAL,
        "condition": {"source": "sniper", "status": "triggered"},
        "action_template": (
            "Sir, a sniper setup for {{symbol}} has triggered. "
            "Direction: {{direction}}, entry {{entry}}, TP {{tp}}. "
            "Shall I execute it?"
        ),
        "action_type": "speak",
        "is_default": True,
    },
]


# ── Seed function ────────────────────────────────────────────────────────────

async def seed_defaults(db: AsyncSession) -> None:
    """Insert default skills & hooks if they don't exist yet.

    Also seeds the trading glossary into the knowledge base so JARVIS
    can retrieve term definitions via the knowledge-search path.
    """
    existing_skills = (
        await db.execute(select(PaulSkill).where(PaulSkill.is_default == True))  # noqa: E712
    ).scalars().all()
    if not existing_skills:
        for s in DEFAULT_SKILLS:
            db.add(PaulSkill(**s))
        logger.info("[JARVIS] Seeded default skills")
    else:
        # Check if the SMC terminology skill is missing (upgrade for existing installs)
        names = {s.name for s in existing_skills}
        if "SMC & Trading Terminology Expert" not in names:
            smc_skill = next(
                (s for s in DEFAULT_SKILLS if s["name"] == "SMC & Trading Terminology Expert"),
                None,
            )
            if smc_skill:
                db.add(PaulSkill(**smc_skill))
                logger.info("[JARVIS] Added SMC & Trading Terminology Expert skill")

    existing_hooks = (
        await db.execute(select(PaulHook).where(PaulHook.is_default == True))  # noqa: E712
    ).scalars().all()
    if not existing_hooks:
        for h in DEFAULT_HOOKS:
            db.add(PaulHook(**h))
        logger.info("[JARVIS] Seeded default hooks")

    await db.commit()

    # Seed trading glossary into the knowledge base (idempotent)
    try:
        from plugins.AgentPaulPlugin.backend.services.knowledge_base import (  # noqa
            seed_trading_glossary,
        )
        inserted = await seed_trading_glossary(db)
        if inserted:
            logger.info(f"[JARVIS] Seeded {inserted} trading term definitions into knowledge base")
    except Exception as exc:
        logger.debug(f"[JARVIS] glossary seed error: {exc}")


# ── CRUD helpers ────────────────────────────────────────────────────────────

async def list_skills(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(select(PaulSkill).order_by(PaulSkill.is_default.desc(), PaulSkill.name))).scalars().all()
    return [_skill_to_dict(r) for r in rows]


async def list_hooks(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(select(PaulHook).order_by(PaulHook.is_default.desc(), PaulHook.name))).scalars().all()
    return [_hook_to_dict(r) for r in rows]


async def create_skill(db: AsyncSession, data: dict) -> dict:
    obj = PaulSkill(**data)
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return _skill_to_dict(obj)


async def update_skill(db: AsyncSession, skill_id: int, data: dict) -> dict:
    obj = (await db.execute(select(PaulSkill).where(PaulSkill.id == skill_id))).scalars().first()
    if not obj:
        raise ValueError(f"Skill {skill_id} not found")
    for k, v in data.items():
        setattr(obj, k, v)
    await db.commit()
    await db.refresh(obj)
    return _skill_to_dict(obj)


async def delete_skill(db: AsyncSession, skill_id: int) -> None:
    await db.execute(delete(PaulSkill).where(PaulSkill.id == skill_id))
    await db.commit()


async def create_hook(db: AsyncSession, data: dict) -> dict:
    obj = PaulHook(**data)
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return _hook_to_dict(obj)


async def update_hook(db: AsyncSession, hook_id: int, data: dict) -> dict:
    obj = (await db.execute(select(PaulHook).where(PaulHook.id == hook_id))).scalars().first()
    if not obj:
        raise ValueError(f"Hook {hook_id} not found")
    for k, v in data.items():
        setattr(obj, k, v)
    await db.commit()
    await db.refresh(obj)
    return _hook_to_dict(obj)


async def delete_hook(db: AsyncSession, hook_id: int) -> None:
    await db.execute(delete(PaulHook).where(PaulHook.id == hook_id))
    await db.commit()


async def get_active_skills_for_message(db: AsyncSession, message: str) -> list[dict]:
    """Return all enabled skills whose keywords appear in `message`."""
    skills = (
        await db.execute(select(PaulSkill).where(PaulSkill.enabled == True))  # noqa: E712
    ).scalars().all()
    msg = message.lower()
    matched = []
    for s in skills:
        kws = s.trigger_keywords or []
        if any(kw.lower() in msg for kw in kws):
            matched.append(_skill_to_dict(s))
    return matched


# ── Dict serialisers ─────────────────────────────────────────────────────────

def _skill_to_dict(s: PaulSkill) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "description": s.description,
        "trigger_keywords": s.trigger_keywords or [],
        "system_prompt_addition": s.system_prompt_addition,
        "ai_provider_id": s.ai_provider_id,
        "enabled": s.enabled,
        "is_default": s.is_default,
        "created_at": str(s.created_at) if s.created_at else None,
        "updated_at": str(s.updated_at) if s.updated_at else None,
    }


def _hook_to_dict(h: PaulHook) -> dict:
    return {
        "id": h.id,
        "name": h.name,
        "description": h.description,
        "trigger_type": h.trigger_type.value if hasattr(h.trigger_type, "value") else str(h.trigger_type),
        "condition": h.condition or {},
        "action_template": h.action_template,
        "action_type": h.action_type,
        "ai_provider_id": h.ai_provider_id,
        "enabled": h.enabled,
        "is_default": h.is_default,
        "last_fired_at": str(h.last_fired_at) if h.last_fired_at else None,
        "fire_count": h.fire_count,
        "created_at": str(h.created_at) if h.created_at else None,
        "updated_at": str(h.updated_at) if h.updated_at else None,
    }
