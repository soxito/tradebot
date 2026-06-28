"""
Trending Pairs Auto-Sync

Fetches CoinGecko trending coins and auto-manages SignalMonitorPair entries:
- Adds trending coins as pairs with source="trending"
- Removes previously-trending pairs that are no longer trending
- Never touches user-configured pairs (source="user")
"""
import aiohttp
from loguru import logger
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.database import SignalMonitorPair


async def _fetch_trending_symbols() -> list[str]:
    """Fetch trending coin symbols from CoinGecko and return as SYMBOL/USDT pairs."""
    api_key = settings.COINGECKO_API_KEY
    base = "https://api.coingecko.com/api/v3"
    headers = {"x-cg-demo-api-key": api_key} if api_key else {}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{base}/search/trending",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"CoinGecko trending returned {resp.status}")
                    return []
                data = await resp.json()
        except Exception as e:
            logger.warning(f"Failed to fetch CoinGecko trending: {e}")
            return []

    symbols = []
    for c in data.get("coins", [])[:15]:
        item = c.get("item", {})
        sym = item.get("symbol", "").upper().strip()
        if sym:
            symbols.append(f"{sym}/USDT")
    return symbols


async def sync_trending_pairs(db: AsyncSession) -> dict:
    """
    Sync trending coins into SignalMonitorPair.

    Returns summary: {added: [...], removed: [...], kept: [...]}
    """
    trending_pairs = await _fetch_trending_symbols()
    if not trending_pairs:
        logger.info("📈 [TRENDING] No trending data — skipping sync")
        return {"added": [], "removed": [], "kept": [], "error": None}

    trending_set = set(trending_pairs)

    # Get all current pairs
    all_rows = (await db.execute(select(SignalMonitorPair))).scalars().all()

    existing_trending = {}  # symbol -> row for source="trending"
    user_symbols = set()    # symbols added by user

    for row in all_rows:
        if row.source == "trending":
            existing_trending[row.symbol] = row
        else:
            user_symbols.add(row.symbol)

    existing_trending_set = set(existing_trending.keys())

    # Pairs to add: trending and not already in DB (neither user nor trending)
    all_existing_symbols = user_symbols | existing_trending_set
    to_add = [s for s in trending_pairs if s not in all_existing_symbols]

    # Pairs to remove: previously trending but no longer in trending list
    to_remove = existing_trending_set - trending_set

    # Pairs to keep: still trending
    kept = existing_trending_set & trending_set

    # Execute removals
    for sym in to_remove:
        await db.execute(
            delete(SignalMonitorPair).where(
                SignalMonitorPair.symbol == sym,
                SignalMonitorPair.source == "trending",
            )
        )

    # Execute additions
    for sym in to_add:
        db.add(SignalMonitorPair(symbol=sym, is_active=True, source="trending"))

    if to_add or to_remove:
        await db.commit()

    result = {
        "added": to_add,
        "removed": list(to_remove),
        "kept": list(kept),
        "total_trending": len(trending_pairs),
    }

    if to_add or to_remove:
        logger.info(
            f"📈 [TRENDING] Synced: +{len(to_add)} added, "
            f"-{len(to_remove)} removed, {len(kept)} kept"
        )
        if to_add:
            logger.info(f"📈 [TRENDING] Added: {', '.join(to_add)}")
        if to_remove:
            logger.info(f"📈 [TRENDING] Removed: {', '.join(to_remove)}")
    else:
        logger.debug(f"📈 [TRENDING] No changes — {len(kept)} trending pairs current")

    return result
