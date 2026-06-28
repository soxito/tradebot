"""
Database connection and session management
"""
import json
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import text, event, select, func
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.pool import NullPool
from app.core.config import settings
from app.models.database import Base, AgentDecision, PineScript
from loguru import logger


# Create async engine
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    poolclass=NullPool,  # Use NullPool for better async compatibility
)

# Create async session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


PINE_SCRIPT_SEED_FILE = Path(__file__).resolve().parents[3] / "pinescripts.json"


# ── Prevent quota error decisions from being persisted ──
@event.listens_for(SyncSession, "before_flush")
def _reject_quota_decisions(session, flush_context, instances):
    """Remove AgentDecision objects containing quota errors before they reach the DB."""
    to_expunge = []
    for obj in list(session.new):
        if isinstance(obj, AgentDecision) and obj.is_quota_error():
            to_expunge.append(obj)
    for obj in to_expunge:
        session.expunge(obj)


def _normalize_seed_pairs(raw_pairs: object) -> list[str]:
    """Normalize pairs into a clean symbol list for DB storage."""
    pairs: list[object]
    if isinstance(raw_pairs, list):
        pairs = raw_pairs
    elif isinstance(raw_pairs, str):
        try:
            decoded = json.loads(raw_pairs)
            pairs = decoded if isinstance(decoded, list) else []
        except Exception:
            pairs = []
    else:
        pairs = []

    return [str(p).strip() for p in pairs if str(p).strip()]


def _load_seed_pinescripts() -> list[dict[str, object]]:
    """Load canonical Pine scripts from repository seed file."""
    if not PINE_SCRIPT_SEED_FILE.exists():
        logger.warning(f"Pine script seed file not found: {PINE_SCRIPT_SEED_FILE}")
        return []

    try:
        payload = json.loads(PINE_SCRIPT_SEED_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"Unable to parse Pine script seed file: {exc}")
        return []

    if not isinstance(payload, list):
        logger.warning("Pine script seed file must contain a JSON array")
        return []

    scripts: list[dict[str, object]] = []
    seen_names: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict):
            continue

        name = str(entry.get("name") or "").strip()
        code = str(entry.get("code") or "")
        if not name or not code.strip():
            continue

        normalized_name = name.lower()
        if normalized_name in seen_names:
            continue
        seen_names.add(normalized_name)

        strategy_id = entry.get("strategy_id")
        scripts.append(
            {
                "name": name,
                "description": str(entry.get("description") or ""),
                "strategy_id": strategy_id if isinstance(strategy_id, int) else None,
                "script_type": str(entry.get("script_type") or "indicator"),
                "code": code,
                "pairs": _normalize_seed_pairs(entry.get("pairs")),
                "is_active": bool(entry.get("is_active", False)),
            }
        )

    return scripts


async def _ensure_seed_pinescripts() -> None:
    """Insert missing Pine scripts from seed file without duplicating existing rows."""
    seed_scripts = _load_seed_pinescripts()
    if not seed_scripts:
        return

    created_count = 0
    async with AsyncSessionLocal() as session:
        for seed in seed_scripts:
            seed_name = str(seed["name"]).strip()
            existing = await session.execute(
                select(PineScript.id).where(func.lower(PineScript.name) == seed_name.lower())
            )
            if existing.scalar_one_or_none() is not None:
                continue

            session.add(
                PineScript(
                    name=seed_name,
                    description=str(seed["description"]),
                    strategy_id=seed["strategy_id"],
                    script_type=str(seed["script_type"]),
                    code=str(seed["code"]),
                    pairs=json.dumps(seed["pairs"]),
                    is_active=bool(seed["is_active"]),
                )
            )
            created_count += 1

        if created_count > 0:
            await session.commit()
            logger.info(f"✅ Seeded {created_count} Pine script(s) from {PINE_SCRIPT_SEED_FILE.name}")


async def init_db():
    """Initialize database tables and migrate new columns"""
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("✅ Database tables created successfully")

        # Migrate new columns onto existing tables
        # Whitelist of allowed identifiers to prevent SQL injection
        _VALID_TABLES = {"sim_orders", "sim_positions", "sim_accounts", "pine_scripts", "trades", "live_trade_settings"}
        _VALID_TYPES = {"VARCHAR", "INTEGER", "BOOLEAN", "TEXT", "FLOAT"}
        migrations = [
            ("sim_orders", "trade_type", "VARCHAR DEFAULT 'spot'"),
            ("sim_orders", "margin_mode", "VARCHAR"),
            ("sim_orders", "leverage", "INTEGER"),
            ("sim_positions", "trade_type", "VARCHAR DEFAULT 'spot'"),
            ("sim_positions", "margin_mode", "VARCHAR"),
            ("sim_positions", "leverage", "INTEGER"),
            ("sim_accounts", "auto_trade_mode", "VARCHAR DEFAULT 'spot'"),
            ("sim_accounts", "auto_trade_leverage", "INTEGER DEFAULT 10"),
            ("sim_accounts", "auto_trade_margin_mode", "VARCHAR DEFAULT 'crossed'"),
            ("sim_accounts", "auto_trade_amount_mode", "VARCHAR DEFAULT 'quote'"),
            ("pine_scripts", "pairs", "TEXT DEFAULT '[]'"),
            ("pine_scripts", "is_active", "BOOLEAN DEFAULT FALSE"),
            ("sim_accounts", "auto_trade_pine_script_id", "INTEGER"),
            ("trades", "trade_side", "VARCHAR"),
            ("trades", "stop_loss", "FLOAT"),
            ("trades", "take_profit", "FLOAT"),
            ("trades", "margin_mode", "VARCHAR"),
            ("trades", "leverage", "INTEGER"),
            ("live_trade_settings", "dry_run", "BOOLEAN DEFAULT TRUE"),
            ("live_trade_settings", "auto_trade_amount_mode", "VARCHAR DEFAULT 'quote'"),
            ("signal_monitor_pairs", "source", "VARCHAR DEFAULT 'user'"),
            ("sim_accounts", "enable_ai", "BOOLEAN DEFAULT FALSE"),
            ("live_trade_settings", "enable_ai", "BOOLEAN DEFAULT TRUE"),
            ("live_trade_settings", "margin_size_usdt", "FLOAT DEFAULT 10.0"),
            ("live_trade_settings", "min_entry_gap_pct", "FLOAT DEFAULT 2.0"),
            ("sim_accounts", "margin_size_usdt", "FLOAT DEFAULT 10.0"),
            ("sim_accounts", "min_entry_gap_pct", "FLOAT DEFAULT 2.0"),
            ("live_trade_settings", "min_confidence", "FLOAT DEFAULT 0.90"),
            ("sim_accounts", "min_confidence", "FLOAT DEFAULT 0.90"),
            ("live_trade_settings", "sniper_max_entries", "INTEGER DEFAULT 1"),
            ("sim_accounts", "sniper_max_entries", "INTEGER DEFAULT 1"),
            ("live_trade_settings", "sniper_max_positions", "INTEGER DEFAULT 5"),
            ("sim_accounts", "sniper_max_positions", "INTEGER DEFAULT 5"),
            ("trades", "source", "VARCHAR DEFAULT 'signal'"),
            ("live_trade_settings", "min_pump_pct", "FLOAT DEFAULT 30.0"),
            ("sim_accounts", "min_pump_pct", "FLOAT DEFAULT 30.0"),
            ("live_trade_settings", "auto_trade_ai_provider", "VARCHAR DEFAULT 'orchestrator'"),
            ("sim_accounts", "auto_trade_ai_provider", "VARCHAR DEFAULT 'orchestrator'"),
            ("live_trade_settings", "tradingagents_llm_provider", "VARCHAR DEFAULT 'openai'"),
            ("sim_accounts", "tradingagents_llm_provider", "VARCHAR DEFAULT 'openai'"),
            ("live_trade_settings", "tradingagents_deep_think_llm", "VARCHAR DEFAULT 'gpt-5.4'"),
            ("sim_accounts", "tradingagents_deep_think_llm", "VARCHAR DEFAULT 'gpt-5.4'"),
            ("live_trade_settings", "tradingagents_quick_think_llm", "VARCHAR DEFAULT 'gpt-5.4-mini'"),
            ("sim_accounts", "tradingagents_quick_think_llm", "VARCHAR DEFAULT 'gpt-5.4-mini'"),
            ("live_trade_settings", "tradingagents_backend_url", "VARCHAR"),
            ("sim_accounts", "tradingagents_backend_url", "VARCHAR"),
            ("live_trade_settings", "tradingagents_max_debate_rounds", "INTEGER DEFAULT 2"),
            ("sim_accounts", "tradingagents_max_debate_rounds", "INTEGER DEFAULT 2"),
            ("live_trade_settings", "tradingagents_max_risk_discuss_rounds", "INTEGER DEFAULT 2"),
            ("sim_accounts", "tradingagents_max_risk_discuss_rounds", "INTEGER DEFAULT 2"),
        ]
        _VALID_TABLES.add("signal_monitor_pairs")
        async with engine.begin() as conn:
            for table, column, col_type in migrations:
                # Validate identifiers against whitelist
                base_type = col_type.split()[0].upper()
                if table not in _VALID_TABLES or base_type not in _VALID_TYPES:
                    logger.warning(f"Skipping invalid migration: {table}.{column} {col_type}")
                    continue
                if not column.isidentifier():
                    logger.warning(f"Skipping invalid column name: {column}")
                    continue
                try:
                    await conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}")
                    )
                except Exception:
                    pass  # Column already exists

        # Backfill NULL values for enable_ai columns (first-time migration only)
        async with engine.begin() as conn:
            try:
                await conn.execute(text(
                    "UPDATE live_trade_settings SET enable_ai = TRUE WHERE enable_ai IS NULL"
                ))
                await conn.execute(text(
                    "UPDATE sim_accounts SET enable_ai = FALSE WHERE enable_ai IS NULL"
                ))
                await conn.execute(text(
                    "UPDATE live_trade_settings SET margin_size_usdt = 10.0 WHERE margin_size_usdt IS NULL"
                ))
                await conn.execute(text(
                    "UPDATE live_trade_settings SET min_entry_gap_pct = 2.0 WHERE min_entry_gap_pct IS NULL"
                ))
                await conn.execute(text(
                    "UPDATE sim_accounts SET margin_size_usdt = 10.0 WHERE margin_size_usdt IS NULL"
                ))
                await conn.execute(text(
                    "UPDATE sim_accounts SET min_entry_gap_pct = 2.0 WHERE min_entry_gap_pct IS NULL"
                ))
                await conn.execute(text(
                    "UPDATE live_trade_settings SET min_confidence = 0.90 WHERE min_confidence IS NULL"
                ))
                await conn.execute(text(
                    "UPDATE sim_accounts SET min_confidence = 0.90 WHERE min_confidence IS NULL"
                ))
                await conn.execute(text(
                    "UPDATE live_trade_settings SET sniper_max_entries = 1 WHERE sniper_max_entries IS NULL"
                ))
                await conn.execute(text(
                    "UPDATE sim_accounts SET sniper_max_entries = 1 WHERE sniper_max_entries IS NULL"
                ))
                await conn.execute(text(
                    "UPDATE live_trade_settings SET sniper_max_positions = 5 WHERE sniper_max_positions IS NULL"
                ))
                await conn.execute(text(
                    "UPDATE sim_accounts SET sniper_max_positions = 5 WHERE sniper_max_positions IS NULL"
                ))
                # Backfill Trade.source — mark existing sniper trades
                await conn.execute(text(
                    "UPDATE trades SET source = 'signal' WHERE source IS NULL"
                ))
                await conn.execute(text(
                    "UPDATE trades SET source = 'sniper' WHERE source = 'signal' AND raw_response LIKE '%sniper_entry%'"
                ))
                await conn.execute(text(
                    "UPDATE live_trade_settings SET min_pump_pct = 30.0 WHERE min_pump_pct IS NULL OR min_pump_pct = 55.0"
                ))
                await conn.execute(text(
                    "UPDATE sim_accounts SET min_pump_pct = 30.0 WHERE min_pump_pct IS NULL OR min_pump_pct = 55.0"
                ))
                await conn.execute(text(
                    "UPDATE live_trade_settings SET auto_trade_ai_provider = 'orchestrator' WHERE auto_trade_ai_provider IS NULL OR auto_trade_ai_provider = ''"
                ))
                await conn.execute(text(
                    "UPDATE sim_accounts SET auto_trade_ai_provider = 'orchestrator' WHERE auto_trade_ai_provider IS NULL OR auto_trade_ai_provider = ''"
                ))
                await conn.execute(text(
                    "UPDATE live_trade_settings SET tradingagents_llm_provider = 'openai' WHERE tradingagents_llm_provider IS NULL OR tradingagents_llm_provider = ''"
                ))
                await conn.execute(text(
                    "UPDATE sim_accounts SET tradingagents_llm_provider = 'openai' WHERE tradingagents_llm_provider IS NULL OR tradingagents_llm_provider = ''"
                ))
                await conn.execute(text(
                    "UPDATE live_trade_settings SET tradingagents_deep_think_llm = 'gpt-5.4' WHERE tradingagents_deep_think_llm IS NULL OR tradingagents_deep_think_llm = ''"
                ))
                await conn.execute(text(
                    "UPDATE sim_accounts SET tradingagents_deep_think_llm = 'gpt-5.4' WHERE tradingagents_deep_think_llm IS NULL OR tradingagents_deep_think_llm = ''"
                ))
                await conn.execute(text(
                    "UPDATE live_trade_settings SET tradingagents_quick_think_llm = 'gpt-5.4-mini' WHERE tradingagents_quick_think_llm IS NULL OR tradingagents_quick_think_llm = ''"
                ))
                await conn.execute(text(
                    "UPDATE sim_accounts SET tradingagents_quick_think_llm = 'gpt-5.4-mini' WHERE tradingagents_quick_think_llm IS NULL OR tradingagents_quick_think_llm = ''"
                ))
                await conn.execute(text(
                    "UPDATE live_trade_settings SET tradingagents_max_debate_rounds = 2 WHERE tradingagents_max_debate_rounds IS NULL"
                ))
                await conn.execute(text(
                    "UPDATE sim_accounts SET tradingagents_max_debate_rounds = 2 WHERE tradingagents_max_debate_rounds IS NULL"
                ))
                await conn.execute(text(
                    "UPDATE live_trade_settings SET tradingagents_max_risk_discuss_rounds = 2 WHERE tradingagents_max_risk_discuss_rounds IS NULL"
                ))
                await conn.execute(text(
                    "UPDATE sim_accounts SET tradingagents_max_risk_discuss_rounds = 2 WHERE tradingagents_max_risk_discuss_rounds IS NULL"
                ))
            except Exception:
                pass

        # Add COOLING value to rugpullstatus enum if not exists
        async with engine.begin() as conn:
            try:
                await conn.execute(text(
                    "ALTER TYPE rugpullstatus ADD VALUE IF NOT EXISTS 'COOLING'"
                ))
            except Exception:
                pass

        # Update Position Reviewer prompt to enhanced reversal-detection version
        async with engine.begin() as conn:
            try:
                from app.agents.specialists import POSITION_REVIEWER_PROMPT
                await conn.execute(
                    text("UPDATE agents SET system_prompt = :prompt WHERE role = 'position_reviewer'"),
                    {"prompt": POSITION_REVIEWER_PROMPT},
                )
            except Exception:
                pass

        await _ensure_seed_pinescripts()

        logger.info("✅ Database migration complete")
    except Exception as e:
        logger.error(f"❌ Failed to initialize database: {e}")
        raise


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency for getting async database sessions
    Usage: db: AsyncSession = Depends(get_db)
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
