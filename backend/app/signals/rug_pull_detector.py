"""
Rug Pull Detector

Scans CoinGecko for tokens that pumped 30%+ in 24h.
Tracks them, monitors price action, and uses sniper entries
(Fibonacci retracements, support/resistance, momentum) for pullback shorts.
Most of these tokens are pump-and-dump / rug pulls.
"""
import json
import math
import aiohttp
from loguru import logger
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import timedelta

from app.core.config import settings
from app.utils.precision import smart_round
from app.core.timezone import now_sast
from app.models.database import RugPullToken, RugPullStatus, Signal, SignalAction, SignalSource, SignalStatus, Trade, LiveTradeSettings, PumpToken, PumpStatus

# ── Configuration ───────────────────────────────────────────
MIN_PUMP_PCT = 30.0             # Minimum 24h % gain to flag
MAX_WATCH_HOURS = 72            # Stop watching after 72h
COINGECKO_TIMEOUT = 20
BUYING_POWER_DECLINE_THRESHOLD = 0.6  # 60% of candles must be bearish to confirm decline
HARD_PULLBACK_PCT = 12.0        # 12%+ bounce from position low = hard pullback (observation only)
RE_ENTRY_COOLDOWN_MINUTES = 10  # Wait 10 min after TP before looking for re-entry
HIGH_RISK_THRESHOLD = 0.70      # Risk score >= 70% = HIGH RISK (informational only)
TRAILING_TRIGGER_ROE = 500.0    # Activate trailing SL when ROE% >= 500%
TRAILING_BUFFER_ROE = 250.0     # Lock in current_profit − 250% (min 250% at trigger)
SNIPER_BASE_MARGIN_USDT = 5.0   # Open with $5 margin
SNIPER_MAX_MARGIN_USDT = 15.0   # Scale in to max $15 total margin per token
_NON_FUTURES_CLEANUP_DONE = False  # One-time runtime cleanup of legacy non-futures watch rows


async def _fetch_markets_sorted_by_gain(min_pump_pct: float = MIN_PUMP_PCT) -> list[dict]:
    """Fetch top coins sorted by 24h % change from CoinGecko."""
    api_key = settings.COINGECKO_API_KEY
    base = "https://api.coingecko.com/api/v3"
    headers = {"x-cg-demo-api-key": api_key} if api_key else {}

    all_coins: list[dict] = []
    async with aiohttp.ClientSession() as session:
        # Fetch multiple pages of coins sorted by volume (catches pumped tokens)
        for page in range(1, 4):  # 3 pages × 250 = 750 coins
            try:
                async with session.get(
                    f"{base}/coins/markets",
                    headers=headers,
                    params={
                        "vs_currency": "usd",
                        "order": "volume_desc",
                        "per_page": 250,
                        "page": page,
                        "sparkline": "false",
                        "price_change_percentage": "24h",
                    },
                    timeout=aiohttp.ClientTimeout(total=COINGECKO_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        all_coins.extend(data)
                    elif resp.status == 429:
                        logger.warning("🚫 CoinGecko rate limited during rug pull scan")
                        break
                    else:
                        logger.warning(f"CoinGecko markets page {page} returned {resp.status}")
                        break
            except Exception as e:
                logger.warning(f"Failed to fetch CoinGecko markets page {page}: {e}")
                break

    # Filter to tokens with min_pump_pct+ gain
    pumped = [
        c for c in all_coins
        if (c.get("price_change_percentage_24h") or 0) >= min_pump_pct
    ]
    # Sort by highest pump first
    pumped.sort(key=lambda c: c.get("price_change_percentage_24h", 0), reverse=True)
    return pumped


async def _get_bitget_tradeable_symbols() -> set[str]:
    """
    Get set of all symbols tradeable on Bitget futures (e.g. {'BTC', 'ETH', 'SOL', ...}).
    Uses the precision cache (class-level, populated by _refresh_leverage_cache).
    Falls back to fetching contracts directly if cache is empty.
    """
    from app.exchanges.bitget import BitgetConnector
    from app.exchanges.manager import exchange_manager, SupportedExchange

    cache = BitgetConnector._precision_cache
    symbols: set[str] = set()

    # If cache is empty, try to populate it
    if not cache:
        connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
        if connector and hasattr(connector, '_refresh_leverage_cache'):
            try:
                await connector._refresh_leverage_cache()
                cache = BitgetConnector._precision_cache
            except Exception as e:
                logger.warning(f"[RUG PULL] Failed to refresh Bitget contract cache: {e}")

    # Extract base symbols from cache keys (e.g. 'BTCUSDT' → 'BTC', 'BTC/USDT' → 'BTC')
    for key in cache:
        if key.endswith("USDT") and "/" not in key:
            symbols.add(key.replace("USDT", "").upper())
        elif key.endswith("/USDT"):
            symbols.add(key.split("/")[0].upper())

    return symbols


async def _cleanup_non_futures_watch_rows(db: AsyncSession, bitget_symbols: set[str]) -> dict:
    """Remove legacy non-futures rows from active rug-pull watch states.

    This is intentionally conservative and only targets active watch states.
    """
    if not bitget_symbols:
        logger.warning("⚠️ [RUG PULL] Skipping non-futures cleanup: no Bitget symbols available")
        return {"executed": False, "removed": 0}

    active_watch_statuses = [
        RugPullStatus.WATCHING,
        RugPullStatus.ENTRY_READY,
        RugPullStatus.COOLING,
    ]
    rows = (await db.execute(
        select(RugPullToken).where(RugPullToken.status.in_(active_watch_statuses))
    )).scalars().all()

    removed = 0
    for token in rows:
        symbol = (token.symbol or "").upper()
        if symbol and symbol not in bitget_symbols:
            await db.delete(token)
            removed += 1

    if removed > 0:
        await db.commit()
        logger.info(f"🧹 [RUG PULL] Removed {removed} legacy non-futures watch rows")

    return {"executed": True, "removed": removed}


async def scan_for_pumps(db: AsyncSession) -> dict:
    """
    Scan CoinGecko for tokens pumped N%+ (configurable via settings).
    Adds only Bitget-futures-tradeable pumped tokens to rug-pull monitoring.
    Returns: {new: [...], existing: [...], total_pumped: int, filtered_out: int, monitor_only: int, cleanup_removed: int}
    """
    global _NON_FUTURES_CLEANUP_DONE

    from app.trading.live import LiveTradeEngine
    s = await LiveTradeEngine.get_or_create_settings(db)
    pump_threshold = getattr(s, "min_pump_pct", MIN_PUMP_PCT) or MIN_PUMP_PCT

    pumped_coins = await _fetch_markets_sorted_by_gain(min_pump_pct=pump_threshold)

    # Load Bitget tradeable symbols and fail closed to futures-only intake.
    bitget_symbols = await _get_bitget_tradeable_symbols()
    logger.info(f"🔍 [RUG PULL] Bitget has {len(bitget_symbols)} futures contracts")

    cleanup_removed = 0
    if not _NON_FUTURES_CLEANUP_DONE:
        try:
            cleanup_stats = await _cleanup_non_futures_watch_rows(db, bitget_symbols)
            cleanup_removed = int(cleanup_stats.get("removed", 0) or 0)
            if cleanup_stats.get("executed"):
                _NON_FUTURES_CLEANUP_DONE = True
        except Exception:
            logger.exception("⚠️ [RUG PULL] One-time non-futures cleanup failed; will retry next scan")

    if not pumped_coins:
        logger.info(f"🔍 [RUG PULL] No tokens with {pump_threshold}%+ pump found")
        return {
            "new": [],
            "existing": [],
            "total_pumped": 0,
            "filtered_out": 0,
            "monitor_only": 0,
            "cleanup_removed": cleanup_removed,
        }

    logger.info(f"🔍 [RUG PULL] Found {len(pumped_coins)} tokens with {pump_threshold}%+ pump")

    # Get existing active tokens across the full sniper lifecycle to avoid
    # duplicate rows when sniper-only loops run without the main scheduler.
    active_statuses = [
        RugPullStatus.WATCHING,
        RugPullStatus.ENTRY_READY,
        RugPullStatus.SHORTED,
        RugPullStatus.COOLING,
    ]
    existing_rows = (await db.execute(
        select(RugPullToken).where(RugPullToken.status.in_(active_statuses))
    )).scalars().all()
    existing_ids = {r.coin_id for r in existing_rows}

    new_tokens = []
    existing_tokens = []
    filtered_out = 0
    monitor_only = 0

    for coin in pumped_coins:
        coin_id = coin.get("id", "")
        if coin_id in existing_ids:
            existing_tokens.append(coin_id)
            continue

        symbol = coin.get("symbol", "").upper()
        tradeable_on_bitget = symbol in bitget_symbols
        if not tradeable_on_bitget:
            filtered_out += 1
            continue

        price = coin.get("current_price") or 0
        pct = coin.get("price_change_percentage_24h") or 0

        token = RugPullToken(
            coin_id=coin_id,
            symbol=symbol,
            name=coin.get("name", ""),
            image=coin.get("image", ""),
            price_at_detection=price,
            price_change_24h=pct,
            market_cap=coin.get("market_cap"),
            volume_24h=coin.get("total_volume"),
            market_cap_rank=coin.get("market_cap_rank"),
            current_price=price,
            peak_price=price,
            status=RugPullStatus.WATCHING,
        )
        db.add(token)
        new_tokens.append({
            "coin_id": coin_id,
            "symbol": token.symbol,
            "name": token.name,
            "pump_pct": round(pct, 1),
            "price": price,
            "tradeable_on_bitget": tradeable_on_bitget,
        })

    if new_tokens:
        await db.commit()
        logger.info(
            f"🆕 [RUG PULL] Added {len(new_tokens)} new pump tokens to watch "
            f"(filtered_out_non_futures={filtered_out})"
        )

    return {
        "new": new_tokens,
        "existing": existing_tokens,
        "total_pumped": len(pumped_coins),
        "filtered_out": filtered_out,
        "monitor_only": 0,
        "cleanup_removed": cleanup_removed,
    }


async def update_watched_tokens(db: AsyncSession) -> dict:
    """
    Update current prices for all watched tokens.
    Detect dumps, expire old ones, update peak prices.
    Returns: {updated: int, dumped: [], expired: []}
    """
    active_statuses = [RugPullStatus.WATCHING, RugPullStatus.ENTRY_READY]
    tokens = (await db.execute(
        select(RugPullToken).where(RugPullToken.status.in_(active_statuses))
    )).scalars().all()

    if not tokens:
        return {"updated": 0, "dumped": [], "expired": []}

    # Build coin ID list for batch price fetch
    coin_ids = [t.coin_id for t in tokens]
    prices = await _fetch_prices(coin_ids)

    now = now_sast()
    cutoff = now - timedelta(hours=MAX_WATCH_HOURS)

    updated = 0
    dumped = []
    expired = []

    for token in tokens:
        price = prices.get(token.coin_id)
        if price is None:
            continue

        token.current_price = price
        updated += 1

        # Update peak
        if price > (token.peak_price or 0):
            token.peak_price = price
            if token.price_at_detection > 0:
                token.peak_change_pct = ((price - token.price_at_detection) / token.price_at_detection) * 100

        # Calculate change since detection
        if token.price_at_detection > 0:
            token.price_change_since_detection = ((price - token.price_at_detection) / token.price_at_detection) * 100

        # Check if token has dumped (dropped 30%+ from peak)
        if token.peak_price and token.peak_price > 0:
            drop_from_peak = ((token.peak_price - price) / token.peak_price) * 100
            if drop_from_peak >= 30:
                was_watching = token.status == RugPullStatus.WATCHING
                token.status = RugPullStatus.DUMPED
                dumped.append({"symbol": token.symbol, "drop_pct": round(drop_from_peak, 1)})
                logger.info(f"💀 [RUG PULL] {token.symbol} dumped {drop_from_peak:.1f}% from peak — rug confirmed")

                # Create a SELL signal if we haven't already (was still watching, not entry_ready)
                if was_watching and token.risk_score is None:
                    risk = _compute_risk_score(token)
                    token.risk_score = risk["score"]
                    entry = _compute_entry_levels(token)
                    token.recommended_entry = entry.get("entry")
                    token.recommended_sl = entry.get("stop_loss")
                    token.recommended_tp = entry.get("take_profit")
                    await _create_rug_pull_signal(db, token, risk)

        # Expire old watches
        if token.detected_at and token.detected_at < cutoff and token.status == RugPullStatus.WATCHING:
            token.status = RugPullStatus.EXPIRED
            token.expired_at = now
            expired.append(token.symbol)

    await db.commit()
    return {"updated": updated, "dumped": dumped, "expired": expired}


async def _fetch_prices(coin_ids: list[str]) -> dict[str, float]:
    """Batch fetch current prices from CoinGecko."""
    if not coin_ids:
        return {}

    api_key = settings.COINGECKO_API_KEY
    base = "https://api.coingecko.com/api/v3"
    headers = {"x-cg-demo-api-key": api_key} if api_key else {}

    prices = {}
    # CoinGecko simple/price supports up to ~400 IDs per call
    batch_size = 200
    for i in range(0, len(coin_ids), batch_size):
        batch = coin_ids[i:i + batch_size]
        ids_str = ",".join(batch)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{base}/simple/price",
                    headers=headers,
                    params={"ids": ids_str, "vs_currencies": "usd"},
                    timeout=aiohttp.ClientTimeout(total=COINGECKO_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for cid, pdata in data.items():
                            if "usd" in pdata:
                                prices[cid] = pdata["usd"]
        except Exception as e:
            logger.warning(f"Failed to fetch batch prices: {e}")

    return prices


async def _fetch_ohlc(coin_id: str, days: int = 1) -> list[list] | None:
    """
    Fetch OHLC candlestick data from CoinGecko for sniper entry analysis.
    Returns list of [timestamp, open, high, low, close] candles.
    1 day = 30-min candles, 7 days = 4h candles.
    """
    api_key = settings.COINGECKO_API_KEY
    base = "https://api.coingecko.com/api/v3"
    headers = {"x-cg-demo-api-key": api_key} if api_key else {}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{base}/coins/{coin_id}/ohlc",
                headers=headers,
                params={"vs_currency": "usd", "days": str(days)},
                timeout=aiohttp.ClientTimeout(total=COINGECKO_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        logger.debug(f"Fetched {len(data)} OHLC candles for {coin_id}")
                        return data
                elif resp.status == 429:
                    logger.warning(f"CoinGecko rate limited fetching OHLC for {coin_id}")
                else:
                    logger.warning(f"CoinGecko OHLC for {coin_id} returned {resp.status}")
    except Exception as e:
        logger.warning(f"Failed to fetch OHLC for {coin_id}: {e}")

    return None


def _detect_buying_power_decrease(ohlc: list[list] | None, token: RugPullToken) -> dict:
    """
    Analyze OHLC candles to detect whether buying power is fading.

    Looks for:
    1. Bearish candle dominance (more red candles than green in recent window)
    2. Declining highs — price failing to make new highs (lower highs pattern)
    3. Upper wick dominance — long upper wicks = sellers rejecting higher prices
    4. Volume decline proxy — shrinking candle ranges = drying up momentum
    5. Price below VWAP — sellers in control

    Returns: {
        declining: bool,  # True if buying power is declining
        score: float,     # 0-1 severity (higher = more bearish)
        signals: [str],   # list of detected bearish signals
    }
    """
    if not ohlc or len(ohlc) < 6:
        return {"declining": False, "score": 0.0, "signals": ["insufficient_data"]}

    # Use the most recent candles (last 12 or all if fewer)
    recent = ohlc[-12:]
    signals = []
    score = 0.0

    opens = [c[1] for c in recent if len(c) >= 5 and c[1] is not None]
    highs = [c[2] for c in recent if len(c) >= 5 and c[2] is not None]
    lows = [c[3] for c in recent if len(c) >= 5 and c[3] is not None]
    closes = [c[4] for c in recent if len(c) >= 5 and c[4] is not None]

    if len(closes) < 4:
        return {"declining": False, "score": 0.0, "signals": ["insufficient_data"]}

    n = len(closes)

    # ── 1. Bearish candle dominance ──
    bearish_count = sum(1 for i in range(min(n, len(opens))) if closes[i] < opens[i])
    bearish_ratio = bearish_count / n if n > 0 else 0
    if bearish_ratio >= BUYING_POWER_DECLINE_THRESHOLD:
        score += 0.25
        signals.append(f"bearish_candles:{bearish_count}/{n}")

    # ── 2. Lower highs pattern (declining peaks) ──
    if len(highs) >= 4:
        # Check last 4 highs for declining pattern
        recent_highs = highs[-4:]
        lower_highs = sum(
            1 for i in range(1, len(recent_highs))
            if recent_highs[i] < recent_highs[i - 1]
        )
        if lower_highs >= 2:  # at least 2 out of 3 transitions are lower
            score += 0.20
            signals.append(f"lower_highs:{lower_highs}/3")

    # ── 3. Upper wick dominance (sellers rejecting higher prices) ──
    wick_ratios = []
    for i in range(min(n, len(opens), len(highs), len(lows))):
        candle_range = highs[i] - lows[i]
        if candle_range > 0:
            upper_wick = highs[i] - max(opens[i], closes[i])
            wick_ratios.append(upper_wick / candle_range)
    if wick_ratios:
        avg_wick = sum(wick_ratios) / len(wick_ratios)
        if avg_wick > 0.45:  # upper wick dominates the candle
            score += 0.20
            signals.append(f"upper_wick_avg:{avg_wick:.2f}")
        elif avg_wick > 0.30:
            score += 0.10
            signals.append(f"upper_wick_moderate:{avg_wick:.2f}")

    # ── 4. Volume decline proxy (shrinking candle ranges) ──
    if len(highs) >= 6 and len(lows) >= 6:
        first_half_ranges = [highs[i] - lows[i] for i in range(n // 2)]
        second_half_ranges = [highs[i] - lows[i] for i in range(n // 2, n)]
        avg_first = sum(first_half_ranges) / len(first_half_ranges) if first_half_ranges else 0
        avg_second = sum(second_half_ranges) / len(second_half_ranges) if second_half_ranges else 0
        if avg_first > 0 and avg_second < avg_first * 0.6:
            score += 0.15
            signals.append(f"volume_decline:{avg_second/avg_first:.2f}x")

    # ── 5. Price below VWAP proxy ──
    total_weight = 0.0
    vwap = 0.0
    for i in range(min(n, len(highs), len(lows))):
        candle_vol = highs[i] - lows[i]
        vwap += closes[i] * candle_vol
        total_weight += candle_vol
    if total_weight > 0:
        vwap = vwap / total_weight
        current_price = closes[-1]
        if current_price < vwap:
            score += 0.15
            signals.append(f"below_vwap:{current_price:.6g}<{vwap:.6g}")

    # ── 6. Drop from peak already starting ──
    peak = token.peak_price or 0
    current = token.current_price or 0
    if peak > 0 and current > 0:
        drop_pct = ((peak - current) / peak) * 100
        if drop_pct >= 15:
            score += 0.15
            signals.append(f"dropping_from_peak:-{drop_pct:.1f}%")
        elif drop_pct >= 8:
            score += 0.08
            signals.append(f"starting_to_drop:-{drop_pct:.1f}%")

    score = min(score, 1.0)
    declining = score >= 0.35  # threshold for "buying power declining"

    return {
        "declining": declining,
        "score": round(score, 3),
        "signals": signals,
    }


async def analyze_token_with_ai(db: AsyncSession, token_id: int) -> dict:
    """
    Analyze a pumped token for sniper short entry.
    Fetches OHLC data for Fibonacci/S&R analysis, computes risk score,
    and generates precise entry/SL/TP levels.
    """
    token = (await db.execute(
        select(RugPullToken).where(RugPullToken.id == token_id)
    )).scalar_one_or_none()

    if not token:
        return {"error": "Token not found"}

    # Fetch fresh market data
    prices = await _fetch_prices([token.coin_id])
    if token.coin_id in prices:
        token.current_price = prices[token.coin_id]

    # Build analysis context
    analysis = _build_analysis(token)

    # Simple heuristic risk scoring (AI can override later)
    risk = _compute_risk_score(token)

    symbol_pair = f"{token.symbol}/USDT"
    agent_decision = {}
    if settings.ENABLE_AI_AGENTS:
        try:
            from app.agents.orchestrator import AgentOrchestrator

            agent_decision = await AgentOrchestrator.analyze_symbol(db, symbol_pair, "15m")
        except Exception as exc:
            logger.warning(f"[SNIPER] Agent analysis failed for {symbol_pair}: {exc}")
            agent_decision = {"error": str(exc), "symbol": symbol_pair}

    ai_action = str(agent_decision.get("final_action", "")).lower()
    ai_confidence = float(agent_decision.get("final_confidence") or 0.0)
    if ai_action == "sell":
        boost = min(0.20, 0.05 + (ai_confidence * 0.20))
        risk["score"] = max(0.0, min(1.0, float(risk.get("score", 0.0)) + boost))
        risk.setdefault("reasons", []).append(
            f"AI agents favor SELL ({ai_confidence:.2f})"
        )
    elif ai_action == "buy":
        penalty = min(0.22, 0.06 + (ai_confidence * 0.22))
        risk["score"] = max(0.0, min(1.0, float(risk.get("score", 0.0)) - penalty))
        risk.setdefault("reasons", []).append(
            f"AI agents caution BUY ({ai_confidence:.2f})"
        )

    analysis["ai_agents"] = {
        "final_action": agent_decision.get("final_action"),
        "final_confidence": ai_confidence,
        "final_reasoning": agent_decision.get("final_reasoning"),
        "agents_used": agent_decision.get("agents_used"),
        "ai_calls": agent_decision.get("ai_calls"),
        "errors": agent_decision.get("errors"),
    }
    token.ai_analysis = json.dumps(analysis)
    token.risk_score = risk["score"]

    # Fetch OHLC candle data for sniper entry analysis
    ohlc = await _fetch_ohlc(token.coin_id, days=1)

    # Generate entry recommendations (threshold 0.5 — all pumps are risky)
    if risk["score"] >= 0.5:
        entry = _compute_entry_levels(token, ohlc)
        token.recommended_entry = entry.get("entry")
        token.recommended_sl = entry.get("stop_loss")
        token.recommended_tp = entry.get("take_profit")

        # Store sniper analysis details
        analysis["sniper_entry"] = {
            "method": entry.get("method"),
            "fib_levels": entry.get("fib_levels"),
            "rejection_candles": entry.get("rejection_candles"),
            "risk_reward": entry.get("risk_reward"),
            "ohlc_candles": len(ohlc) if ohlc else 0,
        }
        token.ai_analysis = json.dumps(analysis)

        # Only create signal once (when transitioning to ENTRY_READY)
        if token.status != RugPullStatus.ENTRY_READY:
            token.status = RugPullStatus.ENTRY_READY
            await _create_rug_pull_signal(db, token, risk, agent_decision)

    await db.commit()

    return {
        "token_id": token.id,
        "symbol": token.symbol,
        "risk_score": token.risk_score,
        "analysis": analysis,
        "recommended_entry": token.recommended_entry,
        "recommended_sl": token.recommended_sl,
        "recommended_tp": token.recommended_tp,
        "ai_agents": analysis.get("ai_agents"),
        "status": token.status.value,
    }


def _build_analysis(token: RugPullToken) -> dict:
    """Build analysis summary for a token."""
    current = token.current_price or 0
    detection = token.price_at_detection or 1
    peak = token.peak_price or current

    change_since = ((current - detection) / detection * 100) if detection else 0
    drop_from_peak = ((peak - current) / peak * 100) if peak > 0 else 0

    return {
        "symbol": token.symbol,
        "name": token.name,
        "pump_24h": round(token.price_change_24h, 2),
        "price_at_detection": detection,
        "current_price": current,
        "peak_price": peak,
        "change_since_detection_pct": round(change_since, 2),
        "drop_from_peak_pct": round(drop_from_peak, 2),
        "market_cap": token.market_cap,
        "volume_24h": token.volume_24h,
        "market_cap_rank": token.market_cap_rank,
        "risk_indicators": {
            "extreme_pump": token.price_change_24h >= 200,
            "low_market_cap": (token.market_cap or 0) < 50_000_000,
            "no_rank": token.market_cap_rank is None or token.market_cap_rank > 500,
            "already_dumping": drop_from_peak > 15,
            "volume_spike": (token.volume_24h or 0) > (token.market_cap or 1) * 0.5,
        },
    }


async def _enrich_with_pump_and_sentiment(
    db: AsyncSession, token: RugPullToken
) -> dict:
    """
    Cross-reference a rug-pull token with pump monitor data and
    sentiment insights to produce a richer picture for entry decisions.

    Returns:
        {
          "pump_token": PumpToken | None,
          "pump_score": float,           # 0-1 from pump monitor
          "momentum_fading": bool,       # pump momentum indicators declining
          "sentiment_score": float,      # -1 to 1
          "sentiment_bearish": bool,     # True if sentiment < -0.1
          "confidence_boost": float,     # 0-0.3 extra confidence from confluence
          "reasons": [str],
        }
    """
    result = {
        "pump_token": None,
        "pump_score": 0.0,
        "momentum_fading": False,
        "sentiment_score": 0.0,
        "sentiment_bearish": False,
        "confidence_boost": 0.0,
        "reasons": [],
    }

    # ── 1. Cross-reference with PumpToken (pump monitor) ──
    try:
        pump_row = (await db.execute(
            select(PumpToken).where(
                PumpToken.symbol == token.symbol,
                PumpToken.status.in_([
                    PumpStatus.DETECTED, PumpStatus.CONFIRMED,
                    PumpStatus.SIGNALLED, PumpStatus.TRADED,
                    PumpStatus.PUMPED, PumpStatus.FADED,
                ]),
            ).order_by(PumpToken.detected_at.desc()).limit(1)
        )).scalar_one_or_none()

        if pump_row:
            result["pump_token"] = pump_row
            result["pump_score"] = pump_row.pump_score or 0.0

            # Pump momentum fading = bullish momentum dying = good for short
            momentum = pump_row.momentum_score or 0
            price_accel = pump_row.price_accel_score or 0
            order_flow = pump_row.order_flow_score or 0

            # If any of these are declining (below 0.4) it means the pump is losing steam
            fading_indicators = sum(1 for s in [momentum, price_accel, order_flow] if s < 0.4)
            if fading_indicators >= 2:
                result["momentum_fading"] = True
                result["confidence_boost"] += 0.10
                result["reasons"].append(
                    f"Pump momentum fading ({fading_indicators}/3 indicators weak: "
                    f"mom={momentum:.2f}, accel={price_accel:.2f}, flow={order_flow:.2f})"
                )

            # If the token pumped hard (high pump_score) but is now fading — ideal short
            if result["pump_score"] >= 0.7 and result["momentum_fading"]:
                result["confidence_boost"] += 0.10
                result["reasons"].append(
                    f"High pump score ({result['pump_score']:.2f}) + momentum fading = classic dump setup"
                )

            # If pump_score is low, the "pump" might not be real — lower confidence
            if result["pump_score"] < 0.4:
                result["confidence_boost"] -= 0.05
                result["reasons"].append(
                    f"Low pump score ({result['pump_score']:.2f}) — pump may not be significant"
                )

            # Token already FADED in pump monitor = pump over, ideal short timing
            if pump_row.status == PumpStatus.FADED:
                result["confidence_boost"] += 0.10
                result["reasons"].append("Pump already FADED in pump monitor")

    except Exception as e:
        logger.debug(f"[SNIPER] Pump cross-ref failed for {token.symbol}: {e}")

    # ── 2. Cross-reference with sentiment ──
    try:
        from app.sentiment.cmc_community import get_cached_cmc_sentiment
        cmc_data = get_cached_cmc_sentiment()
        cmc_sym = cmc_data.get(token.symbol) if cmc_data else None

        if cmc_sym:
            result["sentiment_score"] = cmc_sym.avg_sentiment
            if cmc_sym.avg_sentiment < -0.1:
                result["sentiment_bearish"] = True
                result["confidence_boost"] += 0.05
                result["reasons"].append(
                    f"CMC sentiment bearish ({cmc_sym.avg_sentiment:.3f}, "
                    f"{cmc_sym.mention_count} mentions, label={cmc_sym.signal_label})"
                )
            elif cmc_sym.avg_sentiment > 0.3:
                # Community still very bullish — risky to short
                result["confidence_boost"] -= 0.10
                result["reasons"].append(
                    f"CMC sentiment still bullish ({cmc_sym.avg_sentiment:.3f}) — caution shorting"
                )
    except Exception as e:
        logger.debug(f"[SNIPER] Sentiment cross-ref failed for {token.symbol}: {e}")

    # ── 3. Cross-reference with stored news sentiment ──
    try:
        from app.models.database import SentimentScore
        latest_sent = (await db.execute(
            select(SentimentScore).where(
                SentimentScore.symbol == token.symbol,
            ).order_by(SentimentScore.created_at.desc()).limit(1)
        )).scalar_one_or_none()

        if latest_sent and latest_sent.score is not None:
            if latest_sent.score < -0.2:
                result["confidence_boost"] += 0.05
                result["reasons"].append(
                    f"News sentiment bearish ({latest_sent.score:.3f})"
                )
            elif latest_sent.score > 0.3:
                result["confidence_boost"] -= 0.05
                result["reasons"].append(
                    f"News sentiment bullish ({latest_sent.score:.3f}) — caution"
                )
    except Exception:
        pass  # SentimentScore may not exist for this symbol

    result["confidence_boost"] = max(-0.20, min(0.30, result["confidence_boost"]))
    return result


def _compute_risk_score(token: RugPullToken) -> dict:
    """
    Heuristic rug pull risk score (0-1).
    Higher = more likely a rug pull / pump-and-dump.
    """
    score = 0.0
    reasons = []

    # Extreme pump magnitude
    pct = token.price_change_24h or 0
    if pct >= 500:
        score += 0.25
        reasons.append(f"Extreme pump: +{pct:.0f}%")
    elif pct >= 200:
        score += 0.15
        reasons.append(f"Large pump: +{pct:.0f}%")
    else:
        score += 0.10
        reasons.append(f"Pump: +{pct:.0f}%")

    # Low market cap = easier to manipulate
    mcap = token.market_cap or 0
    if mcap < 10_000_000:
        score += 0.20
        reasons.append(f"Micro cap: ${mcap:,.0f}")
    elif mcap < 50_000_000:
        score += 0.15
        reasons.append(f"Small cap: ${mcap:,.0f}")
    elif mcap < 200_000_000:
        score += 0.10
        reasons.append(f"Mid-low cap: ${mcap:,.0f}")

    # No market cap rank or very low rank
    rank = token.market_cap_rank
    if rank is None or rank > 1000:
        score += 0.15
        reasons.append("No ranking / very low rank")
    elif rank > 500:
        score += 0.10
        reasons.append(f"Low rank: #{rank}")

    # Volume:MCap ratio (high = suspected wash trading)
    vol = token.volume_24h or 0
    if mcap > 0 and vol > mcap:
        score += 0.15
        reasons.append(f"Volume exceeds market cap ({vol/mcap:.1f}x)")
    elif mcap > 0 and vol > mcap * 0.5:
        score += 0.10
        reasons.append(f"High vol/mcap ratio ({vol/mcap:.1f}x)")

    # Already dropping from peak
    if token.peak_price and token.current_price and token.peak_price > 0:
        drop = ((token.peak_price - token.current_price) / token.peak_price) * 100
        if drop > 20:
            score += 0.15
            reasons.append(f"Already dropping: -{drop:.1f}% from peak")
        elif drop > 10:
            score += 0.10
            reasons.append(f"Starting to drop: -{drop:.1f}% from peak")

    return {"score": min(score, 1.0), "reasons": reasons}


def _compute_entry_levels(token: RugPullToken, ohlc: list[list] | None = None) -> dict:
    """
    Sniper entry calculator — finds the perfect pullback entry for a short.

    Uses:
    1. Fibonacci retracement levels from the pump (0.618, 0.786 are prime reversal zones)
    2. OHLC-based resistance levels (recent highs where price got rejected)
    3. Volume-weighted price clustering (where most trading happened)
    4. Momentum-based entry refinement (wait for rejection candles)

    Returns precise entry, SL, and TP with proper precision.
    """
    current = token.current_price or 0
    peak = token.peak_price or current
    detection = token.price_at_detection or current

    if current <= 0:
        return {}

    pump_range = peak - detection
    if pump_range <= 0:
        pump_range = current * 0.01  # fallback

    # ── 1. Fibonacci retracement levels ──────────────────────
    # Measured from detection (swing low) to peak (swing high)
    # For a short entry on pullback: we want price to retrace UP to a fib level
    # then reject — these are resistance zones where shorts enter
    fib_levels = {
        "0.236": peak - pump_range * 0.236,  # shallow retracement
        "0.382": peak - pump_range * 0.382,  # moderate
        "0.500": peak - pump_range * 0.500,  # midpoint
        "0.618": peak - pump_range * 0.618,  # golden ratio — strongest
        "0.786": peak - pump_range * 0.786,  # deep retracement
    }

    # ── 2. Analyze OHLC for support/resistance ───────────────
    resistance_levels = []
    support_levels = []
    rejection_candles = 0
    total_volume_weight = 0.0
    vwap_price = 0.0

    if ohlc and len(ohlc) >= 5:
        # OHLC format: [timestamp, open, high, low, close]
        highs = [c[2] for c in ohlc if len(c) >= 5 and c[2] is not None]
        lows = [c[3] for c in ohlc if len(c) >= 5 and c[3] is not None]
        closes = [c[4] for c in ohlc if len(c) >= 5 and c[4] is not None]
        opens = [c[1] for c in ohlc if len(c) >= 5 and c[1] is not None]

        if highs and lows and closes:
            # Find resistance: price levels where highs clustered and price rejected
            recent_highs = highs[-12:]  # last 12 candles
            recent_lows = lows[-12:]

            # Resistance = areas where multiple candle highs cluster
            for i, h in enumerate(recent_highs):
                nearby_count = sum(1 for oh in recent_highs if abs(oh - h) / h < 0.01)
                if nearby_count >= 2:
                    resistance_levels.append(h)

            # Support = areas where multiple candle lows cluster  
            for i, l in enumerate(recent_lows):
                nearby_count = sum(1 for ol in recent_lows if abs(ol - l) / l < 0.01)
                if nearby_count >= 2:
                    support_levels.append(l)

            # Count rejection candles (long upper wicks = selling pressure)
            for i in range(len(ohlc)):
                if len(ohlc[i]) < 5:
                    continue
                o, h, l, c = ohlc[i][1], ohlc[i][2], ohlc[i][3], ohlc[i][4]
                if o and h and l and c and h > l:
                    body = abs(c - o)
                    upper_wick = h - max(o, c)
                    candle_range = h - l
                    if candle_range > 0 and upper_wick / candle_range > 0.5:
                        rejection_candles += 1

            # Simple VWAP proxy: volume-weighted average of closing prices
            # CoinGecko OHLC doesn't include volume, use candle range as proxy
            for i in range(len(closes)):
                if i < len(highs) and i < len(lows):
                    candle_vol = highs[i] - lows[i]  # range as volume proxy
                    vwap_price += closes[i] * candle_vol
                    total_volume_weight += candle_vol

    if total_volume_weight > 0:
        vwap_price = vwap_price / total_volume_weight

    # ── 3. Calculate sniper entry ────────────────────────────
    # Priority: Fibonacci confluence with OHLC resistance

    entry_candidates = []

    # Candidate 1: Fibonacci 0.618 retracement (golden ratio — strongest reversal)
    fib_618 = fib_levels["0.618"]
    entry_candidates.append(("fib_0.618", fib_618, 0.9))

    # Candidate 2: Fibonacci 0.786 (deep retracement — aggressive entry)
    fib_786 = fib_levels["0.786"]
    entry_candidates.append(("fib_0.786", fib_786, 0.7))

    # Candidate 3: Fibonacci 0.500 (conservative midpoint)
    fib_500 = fib_levels["0.500"]
    entry_candidates.append(("fib_0.500", fib_500, 0.6))

    # Candidate 4: OHLC resistance cluster (if found near current price)
    if resistance_levels:
        avg_resistance = sum(resistance_levels) / len(resistance_levels)
        # Only use if resistance is above current price (pullback target)
        if avg_resistance > current * 1.005:
            entry_candidates.append(("resistance", avg_resistance, 0.85))

    # Candidate 5: VWAP-based entry (where most trading happened)
    if vwap_price > current * 1.005:
        entry_candidates.append(("vwap", vwap_price, 0.75))

    # ── Score and select best entry ──────────────────────────
    # Prefer entries that are:
    # - Above current price (wait for pullback UP before shorting)
    # - Near Fibonacci levels (strong reversal probability)
    # - Near OHLC resistance (proven rejection zone)
    # - Not too far above current (realistic to reach)

    best_entry = None
    best_score = 0
    best_method = "fib_0.618"

    for method, price_level, base_score in entry_candidates:
        if price_level <= 0:
            continue

        score = base_score

        # Bonus: entry is above current price (pullback hasn't happened yet — good)
        if price_level > current:
            pct_above = (price_level - current) / current
            if 0.005 < pct_above < 0.15:  # 0.5% to 15% above = sweet spot
                score += 0.2
            elif pct_above <= 0.005:  # too close to current
                score += 0.05
            else:  # too far above — unrealistic
                score -= 0.1

        # Bonus: confluence with Fibonacci levels
        for fib_name, fib_val in fib_levels.items():
            if fib_val > 0 and abs(price_level - fib_val) / fib_val < 0.015:
                score += 0.15  # confluence bonus
                break

        # Bonus: rejection candles confirm sellers are present
        if rejection_candles >= 3:
            score += 0.1

        # Penalty: if price already dropped past this level
        if price_level < current * 0.98:
            score -= 0.3

        if score > best_score:
            best_score = score
            best_entry = price_level
            best_method = method

    # Fallback: if no good entry above current, use fib 0.618 or current * 1.02
    if best_entry is None or best_entry <= current * 0.99:
        # Price may have already pulled back — enter near current on next rejection
        if fib_618 > current * 0.99:
            best_entry = fib_618
            best_method = "fib_0.618"
        else:
            best_entry = current * 1.02  # wait for a small bounce
            best_method = "bounce_rejection"

    entry = best_entry

    # ── 4. Smart SL — above peak or nearest Fibonacci above entry ──
    # SL should be tight but above the last significant high
    sl_candidates = [peak * 1.05]  # 5% above peak as safety

    # Tighter SL: just above the 0.236 fib (shallow retracement top)
    fib_236 = fib_levels["0.236"]
    if fib_236 > entry:
        sl_candidates.append(fib_236 * 1.02)

    # If OHLC shows a clear rejection high, use that
    if resistance_levels:
        max_resistance = max(resistance_levels)
        if max_resistance > entry:
            sl_candidates.append(max_resistance * 1.02)

    # Pick the tightest SL that's still above entry
    valid_sls = [sl for sl in sl_candidates if sl > entry]
    if valid_sls:
        stop_loss = min(valid_sls)  # tightest valid SL
    else:
        stop_loss = peak * 1.05

    # ── 5. Smart TP — Fibonacci extension + detection price ──
    # Target 1: 70% retracement of the pump
    tp1 = detection + pump_range * 0.30

    # Target 2: Fibonacci 1.618 extension below  
    tp2 = current - (entry - current) * 1.618 if entry > current else current * 0.50

    # Target 3: Back near detection price (full dump)
    tp3 = detection * 1.05

    # Target 4: OHLC support level (if found)
    tp4 = None
    if support_levels:
        tp4 = min(support_levels)

    # Pick the best TP — at least 2:1 reward:risk
    risk = abs(stop_loss - entry)
    min_tp = entry - risk * 2  # minimum 2:1 R:R

    tp_candidates = [tp for tp in [tp1, tp2, tp3, tp4] if tp is not None and tp > 0 and tp < entry]
    if tp_candidates:
        # Prefer the TP that gives best R:R while being realistic
        take_profit = min(tp_candidates)  # most aggressive target
        if take_profit > min_tp:
            take_profit = min_tp  # ensure at least 2:1
    else:
        take_profit = min_tp

    # Ensure TP is positive and below entry
    if take_profit <= 0 or take_profit >= entry:
        take_profit = entry * 0.50  # 50% drop target as last resort

    logger.info(
        f"🎯 [SNIPER ENTRY] {token.symbol}: method={best_method} | "
        f"entry={smart_round(entry, current)} | sl={smart_round(stop_loss, current)} | "
        f"tp={smart_round(take_profit, current)} | "
        f"R:R=1:{abs(entry - take_profit) / risk:.1f}" if risk > 0 else
        f"🎯 [SNIPER ENTRY] {token.symbol}: method={best_method}"
    )

    return {
        "entry": smart_round(entry, current),
        "stop_loss": smart_round(stop_loss, current),
        "take_profit": smart_round(take_profit, current),
        "method": best_method,
        "fib_levels": {k: smart_round(v, current) for k, v in fib_levels.items()},
        "rejection_candles": rejection_candles,
        "risk_reward": round(abs(entry - take_profit) / risk, 2) if risk > 0 else 0,
    }


async def _create_rug_pull_signal(
    db: AsyncSession,
    token: RugPullToken,
    risk: dict,
    agent_decision: dict | None = None,
) -> Signal | None:
    """Create a SELL signal on the trading page for a rug pull token.

    Rug-pull sniper entries are forced: confidence is informational and does not gate signal creation.
    """
    symbol = f"{token.symbol}/USDT"
    price = token.current_price or token.price_at_detection or 0

    # Confidence is tracked for observability, but does not block rug-pull sniper entries.
    confidence = min(1.0, risk.get("score", 0.6) * 1.2)

    ai_summary = None
    if isinstance(agent_decision, dict) and agent_decision:
        ai_summary = {
            "final_action": agent_decision.get("final_action"),
            "final_confidence": agent_decision.get("final_confidence"),
            "final_reasoning": agent_decision.get("final_reasoning"),
            "agents_used": agent_decision.get("agents_used"),
            "ai_calls": agent_decision.get("ai_calls"),
            "errors": agent_decision.get("errors"),
        }

    raw_data = json.dumps({
        "source": "rug_pull_detector",
        "rug_pull_token_id": token.id,
        "coin_id": token.coin_id,
        "risk_score": risk.get("score", 0),
        "risk_reasons": risk.get("reasons", []),
        "pump_pct": token.price_change_24h,
        "recommended_entry": token.recommended_entry,
        "recommended_sl": token.recommended_sl,
        "recommended_tp": token.recommended_tp,
        "market_cap": token.market_cap,
        "volume_24h": token.volume_24h,
        "ai_agents": ai_summary,
        "market_analysis": (ai_summary or {}).get("final_reasoning"),
    })

    signal = Signal(
        source=SignalSource.SYSTEM,
        symbol=symbol,
        action=SignalAction.SELL,
        price=price,
        timeframe="rug_pull",
        strength=min(1.0, risk.get("score", 0.6)),
        confidence=confidence,
        raw_data=raw_data,
        indicators=json.dumps({
            "price": price,
            "entry": token.recommended_entry,
            "stop_loss": token.recommended_sl,
            "take_profit": token.recommended_tp,
            "risk_score": risk.get("score", 0),
        }),
        status=SignalStatus.PENDING,
    )
    db.add(signal)
    await db.flush()

    logger.info(
        f"🚨 [RUG PULL SIGNAL] Created SELL signal for {symbol} "
        f"@ {price} | Risk: {risk.get('score', 0):.0%} | "
        f"Entry: {token.recommended_entry} SL: {token.recommended_sl} TP: {token.recommended_tp}"
    )

    return signal


async def run_rug_pull_cycle(db: AsyncSession) -> dict:
    """Full cycle: scan for new pumps, auto-analyze, update existing watched tokens."""
    scan_result = await scan_for_pumps(db)

    # Keep monitor-only tokens active: execution guards in sniper phase already
    # skip non-tradeable symbols on Bitget, but we still want to track their decay.

    update_result = await update_watched_tokens(db)

    # Auto-analyze all WATCHING tokens (re-analyze as prices change)
    watching_tokens = (await db.execute(
        select(RugPullToken).where(
            RugPullToken.status == RugPullStatus.WATCHING,
        )
    )).scalars().all()

    analyzed = []
    for token in watching_tokens:
        try:
            result = await analyze_token_with_ai(db, token.id)
            if result.get("status") == "entry_ready":
                analyzed.append(token.symbol)
        except Exception as e:
            logger.warning(f"Failed to auto-analyze {token.symbol}: {e}")

    if analyzed:
        logger.info(f"🎯 [RUG PULL] Auto-analyzed {len(analyzed)} tokens, entry_ready: {analyzed}")

    return {
        "scan": scan_result,
        "updates": update_result,
        "auto_analyzed": analyzed,
    }


async def run_sniper_cycle(db: AsyncSession) -> dict:
    """
    Fast 60-second sniper cycle with 3 phases:

    Phase 1: SCAN — Fetch WATCHING + ENTRY_READY tokens, detect buying power decline,
             compute sniper entries, validate symbol on Bitget, execute SHORT (SL only, no TP).
    Phase 2: MONITOR — Check SHORTED positions, detect hard pullback (bounce),
             take profit only on hard pullback.
    Phase 3: RE-ENTRY — After closing a position, set token back to WATCHING for re-entry.

    Returns: {pump_intake, scanned, declining, signals_created, trades_executed,
              positions_monitored, profits_taken, re_entries, details}
    """
    from app.trading.live import LiveTradeEngine
    from app.exchanges.manager import exchange_manager, SupportedExchange
    from app.exchanges.bitget import BitgetConnector
    from typing import cast, Optional

    scanned = 0
    declining_count = 0
    signals_created = 0
    trades_executed = 0
    positions_monitored = 0
    profits_taken = 0
    re_entries = 0
    details = []

    # Get the Bitget connector for symbol validation and position checks
    connector = cast(
        Optional[BitgetConnector],
        exchange_manager.get_exchange(SupportedExchange.BITGET),
    )

    # Get live trade settings for sniper_max_entries
    s = await LiveTradeEngine.get_or_create_settings(db)

    # Keep sniper-only runs self-sufficient by ingesting newly pumped tokens
    # before sniper phase logic executes.
    pump_intake = {"new": [], "existing": [], "total_pumped": 0, "filtered_out": 0, "cleanup_removed": 0}
    try:
        pump_intake = await scan_for_pumps(db)
    except Exception:
        logger.exception("⚠️ [SNIPER] Pump intake scan failed; continuing sniper cycle")

    intake_new = len(pump_intake.get("new", [])) if isinstance(pump_intake.get("new"), list) else 0
    intake_existing = len(pump_intake.get("existing", [])) if isinstance(pump_intake.get("existing"), list) else 0
    intake_total = int(pump_intake.get("total_pumped", 0) or 0)
    intake_filtered = int(pump_intake.get("filtered_out", 0) or 0)
    intake_monitor_only = int(pump_intake.get("monitor_only", 0) or 0)
    intake_cleanup_removed = int(pump_intake.get("cleanup_removed", 0) or 0)

    if intake_total or intake_new or intake_existing or intake_filtered or intake_monitor_only or intake_cleanup_removed:
        logger.info(
            f"📥 [SNIPER] Pump intake summary | total={intake_total} | "
            f"new={intake_new} | existing={intake_existing} | "
            f"filtered_out_non_futures={intake_filtered} | "
            f"cleanup_removed={intake_cleanup_removed}"
        )

    try:
        from app.monitoring.metrics import record_sniper_pump_intake

        record_sniper_pump_intake(pump_intake)
    except Exception:
        logger.debug("[SNIPER] Failed to emit pump-intake metric")

    # ══════════════════════════════════════════════════════════
    # PHASE 1: SCAN — detect declining tokens and open shorts
    # ══════════════════════════════════════════════════════════

    sniper_max = max(int(getattr(s, "sniper_max_entries", 1) or 1), 3)
    entry_gap_pct = getattr(s, "min_entry_gap_pct", 2.0) or 2.0
    # Include SHORTED tokens in Phase 1 scan when multi-entry is enabled
    # Always include COOLING tokens so we can re-evaluate their risk
    if sniper_max > 1:
        active_statuses = [RugPullStatus.WATCHING, RugPullStatus.ENTRY_READY, RugPullStatus.SHORTED, RugPullStatus.COOLING]
    else:
        active_statuses = [RugPullStatus.WATCHING, RugPullStatus.ENTRY_READY, RugPullStatus.COOLING]
    tokens = (await db.execute(
        select(RugPullToken).where(RugPullToken.status.in_(active_statuses))
    )).scalars().all()

    if tokens:
        coin_ids = [t.coin_id for t in tokens]
        prices = await _fetch_prices(coin_ids)

        for token in tokens:
            scanned += 1
            price = prices.get(token.coin_id)
            if price is None:
                continue

            token.current_price = price

            # Update peak tracking
            if price > (token.peak_price or 0):
                token.peak_price = price
                if token.price_at_detection > 0:
                    token.peak_change_pct = ((price - token.price_at_detection) / token.price_at_detection) * 100

            # Fetch OHLC for momentum analysis
            ohlc = await _fetch_ohlc(token.coin_id, days=1)

            # Detect buying power decrease
            bp = _detect_buying_power_decrease(ohlc, token)

            detail = {
                "symbol": token.symbol,
                "price": price,
                "buying_power": bp,
                "action": "none",
            }

            if bp["declining"]:
                declining_count += 1
                logger.info(
                    f"📉 [SNIPER] {token.symbol} buying power declining "
                    f"(score={bp['score']}, signals={bp['signals']})"
                )

                # Compute risk score
                risk = _compute_risk_score(token)

                # ── Enrich with pump monitor + sentiment data ──
                enrichment = await _enrich_with_pump_and_sentiment(db, token)
                if enrichment["confidence_boost"] != 0:
                    # Apply boost to risk score (higher risk = more likely dump = better short)
                    original_score = risk["score"]
                    risk["score"] = max(0.0, min(1.0, risk["score"] + enrichment["confidence_boost"]))
                    risk["reasons"].extend(enrichment["reasons"])
                    logger.info(
                        f"🔬 [SNIPER] {token.symbol} enrichment: "
                        f"risk {original_score:.2f} → {risk['score']:.2f} "
                        f"(boost={enrichment['confidence_boost']:+.2f}) | "
                        f"pump_score={enrichment['pump_score']:.2f} "
                        f"momentum_fading={enrichment['momentum_fading']} "
                        f"sentiment={enrichment['sentiment_score']:.3f} | "
                        f"reasons={enrichment['reasons']}"
                    )

                symbol_pair = f"{token.symbol}/USDT"
                agent_decision = {}
                if settings.ENABLE_AI_AGENTS:
                    try:
                        from app.agents.orchestrator import AgentOrchestrator

                        agent_decision = await AgentOrchestrator.analyze_symbol(
                            db,
                            symbol_pair,
                            "15m",
                        )
                    except Exception as exc:
                        logger.warning(f"[SNIPER] Agent analysis failed for {symbol_pair}: {exc}")
                        agent_decision = {"error": str(exc), "symbol": symbol_pair}

                ai_action = str(agent_decision.get("final_action", "")).lower()
                ai_confidence = float(agent_decision.get("final_confidence") or 0.0)
                if ai_action == "sell":
                    ai_boost = min(0.20, 0.05 + (ai_confidence * 0.20))
                    risk["score"] = max(0.0, min(1.0, float(risk.get("score", 0.0)) + ai_boost))
                    risk.setdefault("reasons", []).append(
                        f"AI agents favor SELL ({ai_confidence:.2f})"
                    )
                elif ai_action == "buy":
                    ai_penalty = min(0.22, 0.06 + (ai_confidence * 0.22))
                    risk["score"] = max(0.0, min(1.0, float(risk.get("score", 0.0)) - ai_penalty))
                    risk.setdefault("reasons", []).append(
                        f"AI agents caution BUY ({ai_confidence:.2f})"
                    )

                if ai_action == "buy" and ai_confidence >= 0.65 and float(risk.get("score", 0.0)) < 0.85:
                    detail["action"] = "ai_rejected_short"
                    detail["ai_decision"] = {
                        "final_action": ai_action,
                        "final_confidence": ai_confidence,
                        "final_reasoning": agent_decision.get("final_reasoning"),
                    }
                    logger.info(
                        f"🤖 [SNIPER] {token.symbol} skipped by AI consensus "
                        f"(action={ai_action}, conf={ai_confidence:.2f}, risk={risk['score']:.2f})"
                    )
                    details.append(detail)
                    continue

                token.risk_score = risk["score"]

                # Compute sniper entry at the highest level
                entry = _compute_entry_levels(token, ohlc)
                token.recommended_entry = entry.get("entry")
                token.recommended_sl = entry.get("stop_loss")
                token.recommended_tp = None  # NO TP — let position ride down

                # Store analysis
                analysis = _build_analysis(token)
                analysis["buying_power"] = bp
                analysis["enrichment"] = {
                    "pump_score": enrichment["pump_score"],
                    "momentum_fading": enrichment["momentum_fading"],
                    "sentiment_score": enrichment["sentiment_score"],
                    "sentiment_bearish": enrichment["sentiment_bearish"],
                    "confidence_boost": enrichment["confidence_boost"],
                    "reasons": enrichment["reasons"],
                }
                analysis["ai_agents"] = {
                    "final_action": agent_decision.get("final_action"),
                    "final_confidence": ai_confidence,
                    "final_reasoning": agent_decision.get("final_reasoning"),
                    "agents_used": agent_decision.get("agents_used"),
                    "ai_calls": agent_decision.get("ai_calls"),
                    "errors": agent_decision.get("errors"),
                }
                analysis["sniper_entry"] = {
                    "method": entry.get("method"),
                    "fib_levels": entry.get("fib_levels"),
                    "rejection_candles": entry.get("rejection_candles"),
                    "risk_reward": entry.get("risk_reward"),
                    "ohlc_candles": len(ohlc) if ohlc else 0,
                }
                token.ai_analysis = json.dumps(analysis)

                logger.info(
                    f"🔍 [SNIPER] {token.symbol} risk_score={risk['score']:.2f} "
                    f"reasons={risk['reasons']} | status={token.status} | "
                    f"entry={entry.get('entry')} | sl={entry.get('stop_loss')}"
                )

                # Always allow entry flow for rug-pull sniper tokens.
                if risk["score"] >= HIGH_RISK_THRESHOLD:
                    detail["risk_score"] = risk["score"]
                    logger.info(
                        f"⚡ [SNIPER] {token.symbol} high risk {risk['score']:.0%} — entry flow forced"
                    )

                # Token cooled off from COOLING → ready for entry
                if token.status == RugPullStatus.COOLING:
                    logger.info(
                        f"❄️ [SNIPER] {token.symbol} cooled off! Risk {risk['score']:.0%} < {HIGH_RISK_THRESHOLD:.0%} — "
                        f"resuming entry flow"
                    )

                # Skip if no valid entry computed
                if not entry.get("entry"):
                    detail["action"] = "no_entry"
                    logger.info(f"⏭️ [SNIPER] {token.symbol} — no valid entry level computed, skipping")
                    details.append(detail)
                    continue

                # ── Validate symbol exists on Bitget before attempting trade ──
                tradeable = await _is_tradeable_on_bitget(connector, token.symbol)
                if not tradeable:
                    detail["action"] = "not_on_exchange"
                    logger.info(f"⏭️ [SNIPER] {token.symbol} not available on Bitget futures — skipping trade")
                    details.append(detail)
                    continue

                # ── Create signal + execute when buying power declining & entry ready ──
                # Buying power decline IS the trigger — risk score is informational only

                # Count existing entries for this token:
                # 1. Open DB trades (filled positions)
                # 2. Open exchange orders (unfilled limit orders still pending)
                bitget_sym_check = f"{token.symbol}USDT"
                existing_entries_result = await db.execute(
                    select(func.count(Trade.id)).where(
                        Trade.exchange == "bitget",
                        Trade.symbol == f"{token.symbol}/USDT",
                        Trade.status == "open",
                        Trade.trade_side == "open",
                    )
                )
                db_entry_count = existing_entries_result.scalar() or 0

                # Also count unfilled limit orders on the exchange
                exchange_order_count = 0
                if connector:
                    try:
                        open_orders = await connector.get_futures_open_orders(symbol=bitget_sym_check)
                        exchange_order_count = sum(
                            1 for o in (open_orders or [])
                            if (o.get("side", "") or "").lower() == "sell"
                            and (o.get("tradeSide", "") or o.get("posSide", "") or "").lower() in ("open", "short", "")
                        )
                    except Exception:
                        pass

                existing_entry_count = db_entry_count + exchange_order_count

                if existing_entry_count >= sniper_max:
                    detail["action"] = "max_entries_reached"
                    detail["entries"] = f"{existing_entry_count}/{sniper_max} (db={db_entry_count}, orders={exchange_order_count})"
                    logger.info(
                        f"⛔ [SNIPER] {token.symbol} max entries reached "
                        f"({existing_entry_count}/{sniper_max}, db={db_entry_count}, orders={exchange_order_count}) — skipping"
                    )
                    details.append(detail)
                    continue

                # Additional entries require an adverse move against the active short.
                # This avoids over-stacking entries before price actually pushes up.
                if existing_entry_count > 0:
                    latest_trade = (
                        await db.execute(
                            select(Trade)
                            .where(
                                Trade.exchange == "bitget",
                                Trade.symbol == f"{token.symbol}/USDT",
                                Trade.status == "open",
                                Trade.trade_side == "open",
                            )
                            .order_by(Trade.created_at.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    ref_entry = float(latest_trade.price or 0) if latest_trade else float(entry.get("entry") or 0)
                    adverse_move_pct = (
                        ((price - ref_entry) / ref_entry) * 100 if ref_entry > 0 and price > 0 else 0.0
                    )
                    required_move_pct = max(float(entry_gap_pct), 1.0) * existing_entry_count
                    if adverse_move_pct < required_move_pct:
                        detail["action"] = "awaiting_adverse_move"
                        detail["adverse_move_pct"] = round(adverse_move_pct, 2)
                        detail["required_move_pct"] = round(required_move_pct, 2)
                        logger.info(
                            f"⏳ [SNIPER] {token.symbol} add-entry waiting: "
                            f"adverse={adverse_move_pct:.2f}% < required={required_move_pct:.2f}%"
                        )
                        details.append(detail)
                        continue

                # ── Offset entry price for multi-entry spacing ──
                # Each subsequent entry is placed higher (better short entry)
                # Entry #0 = base, #1 = base * (1 + gap%), #2 = base * (1 + 2*gap%)
                if existing_entry_count > 0 and entry_gap_pct > 0:
                    offset_mult = 1 + (existing_entry_count * entry_gap_pct / 100)
                    base_entry = entry.get("entry", 0)
                    base_sl = entry.get("stop_loss", 0)
                    base_tp = entry.get("take_profit", 0)
                    if base_entry and base_entry > 0:
                        new_entry = smart_round(base_entry * offset_mult, base_entry)
                        new_sl = smart_round(base_sl * offset_mult, base_sl) if base_sl else None
                        new_tp = smart_round(base_tp * offset_mult, base_tp) if base_tp else None
                        logger.info(
                            f"📐 [SNIPER] {token.symbol} entry #{existing_entry_count}: "
                            f"offset +{existing_entry_count * entry_gap_pct:.1f}% | "
                            f"entry {base_entry} → {new_entry} | sl {base_sl} → {new_sl}"
                        )
                        entry["entry"] = new_entry
                        entry["stop_loss"] = new_sl
                        if new_tp:
                            entry["take_profit"] = new_tp

                token.status = RugPullStatus.ENTRY_READY

                # Check for existing pending signal first
                existing_signal = (await db.execute(
                    select(Signal).where(
                        Signal.source == SignalSource.SYSTEM,
                        Signal.symbol == f"{token.symbol}/USDT",
                        Signal.status == SignalStatus.PENDING,
                        Signal.action == SignalAction.SELL,
                    ).order_by(Signal.created_at.desc()).limit(1)
                )).scalar_one_or_none()

                if not existing_signal:
                    existing_signal = await _create_rug_pull_signal(
                        db,
                        token,
                        risk,
                        agent_decision,
                    )
                    if existing_signal:
                        signals_created += 1
                        logger.info(f"📝 [SNIPER] Signal created for {token.symbol}")

                if not existing_signal:
                    detail["action"] = "low_confidence"
                    detail["confidence"] = min(1.0, risk.get("score", 0.6) * 1.2)
                    detail["enrichment"] = {
                        "pump_score": enrichment["pump_score"],
                        "momentum_fading": enrichment["momentum_fading"],
                        "sentiment_score": enrichment["sentiment_score"],
                        "confidence_boost": enrichment["confidence_boost"],
                    }
                    details.append(detail)
                    continue

                # ── Execute sniper trade (SL only, no TP) ──
                try:
                    await db.commit()
                    exec_result = await _execute_sniper_short(
                        db, connector, existing_signal, token, entry
                    )
                    if exec_result.get("success") or exec_result.get("dry_run"):
                        trades_executed += 1
                        token.status = RugPullStatus.SHORTED
                        token.trade_id = exec_result.get("trade_id")
                        detail["action"] = "trade_executed"
                        detail["trade"] = exec_result
                        logger.info(
                            f"🎯 [SNIPER] Opened SHORT {token.symbol} "
                            f"@ {entry.get('entry')} | SL: {entry.get('stop_loss')} | NO TP (ride it down)"
                        )
                    elif exec_result.get("error"):
                        detail["action"] = "trade_failed"
                        detail["error"] = exec_result["error"]
                        logger.warning(f"⚠️ [SNIPER] Failed {token.symbol}: {exec_result['error']}")
                except Exception as e:
                    detail["action"] = "trade_error"
                    detail["error"] = str(e)
                    logger.error(f"❌ [SNIPER] Trade error {token.symbol}: {e}")

            details.append(detail)

    # ══════════════════════════════════════════════════════════
    # PHASE 2: MONITOR — manage SHORTED positions (hold-through-pullback, close on dump)
    # ══════════════════════════════════════════════════════════

    shorted_tokens = (await db.execute(
        select(RugPullToken).where(RugPullToken.status == RugPullStatus.SHORTED)
    )).scalars().all()

    if shorted_tokens and connector:
        shorted_coin_ids = [t.coin_id for t in shorted_tokens]
        shorted_prices = await _fetch_prices(shorted_coin_ids)

        # Get all open positions from exchange for cross-referencing
        try:
            pos_data = await connector.get_futures_positions()
            open_positions = {
                (p.get("symbol", "").replace("USDT", "")): p
                for p in (pos_data or [])
                if float(p.get("available", 0) or p.get("total", 0) or 0) > 0
            }
        except Exception as e:
            logger.warning(f"[SNIPER] Failed to fetch positions: {e}")
            open_positions = {}

        for token in shorted_tokens:
            positions_monitored += 1
            price = shorted_prices.get(token.coin_id)
            if price is None:
                continue

            old_price = token.current_price or price
            token.current_price = price

            # Track the lowest price this token reached (position low)
            position_low = token.recommended_tp or price  # reuse TP field as position low tracker
            if price < (position_low or price):
                position_low = price
                token.recommended_tp = price  # store the lowest point reached

            detail = {
                "symbol": token.symbol,
                "price": price,
                "position_low": position_low,
                "action": "monitoring",
            }

            # ── Trailing SL for high-profit positions ──
            trailing_active = False
            pos_info = open_positions.get(token.symbol)
            if pos_info:
                entry_price = float(pos_info.get("openPriceAvg", 0) or 0)
                leverage = float(pos_info.get("leverage", 1) or 1)
                margin_size = float(pos_info.get("marginSize", 0) or 0)
                total_amt = float(
                    pos_info.get("available", 0) or pos_info.get("total", 0) or 0
                )
                unrealized_pnl = float(pos_info.get("unrealizedPL", 0) or 0)

                # ROE% = unrealizedPL / initialMargin * 100
                notional = entry_price * total_amt
                initial_margin = (
                    margin_size if margin_size > 0
                    else (notional / leverage if leverage > 0 else notional)
                )
                roe_pct = (
                    (unrealized_pnl / initial_margin * 100)
                    if initial_margin > 0 else 0.0
                )

                if roe_pct >= TRAILING_TRIGGER_ROE and entry_price > 0 and leverage > 0:
                    trailing_active = True
                    # Lock in current_profit − buffer (at trigger: 500 − 250 = 250%)
                    locked_roe = roe_pct - TRAILING_BUFFER_ROE
                    # SHORT trailing SL: price must stay below this to keep profit
                    trail_sl = entry_price * (1 - locked_roe / (leverage * 100))

                    try:
                        await connector.replace_tpsl_orders(
                            symbol=f"{token.symbol}/USDT",
                            hold_side="short",
                            new_sl=trail_sl,
                        )
                        logger.info(
                            f"🔒 [SNIPER] TRAILING SL on {token.symbol}: "
                            f"ROE={roe_pct:.0f}% | locked={locked_roe:.0f}% | "
                            f"SL→{smart_round(trail_sl, entry_price)} "
                            f"(entry={smart_round(entry_price, entry_price)}, {leverage}x)"
                        )
                    except Exception as e:
                        logger.warning(
                            f"[SNIPER] Failed to update trailing SL for {token.symbol}: {e}"
                        )

                    # Sync trailing SL back to DB trade record
                    try:
                        _trail_trade = (await db.execute(
                            select(Trade).where(
                                Trade.symbol == f"{token.symbol}/USDT",
                                Trade.exchange == "bitget",
                                Trade.status == "open",
                                Trade.side == "sell",
                            ).order_by(Trade.created_at.desc()).limit(1)
                        )).scalar_one_or_none()
                        if _trail_trade:
                            _trail_trade.stop_loss = trail_sl
                    except Exception:
                        pass

                    detail["action"] = "trailing_sl"
                    detail["roe_pct"] = round(roe_pct, 1)
                    detail["locked_roe"] = round(locked_roe, 1)
                    detail["trailing_sl"] = trail_sl

            # ── Read the trade's current SL from DB ──
            _sniper_trade = (await db.execute(
                select(Trade).where(
                    Trade.symbol == f"{token.symbol}/USDT",
                    Trade.exchange == "bitget",
                    Trade.status == "open",
                    Trade.side == "sell",
                ).order_by(Trade.created_at.desc()).limit(1)
            )).scalar_one_or_none()
            trade_sl = _sniper_trade.stop_loss if _sniper_trade else None

            # ── Detect hard pullback (bounce from position low) ──
            if not trailing_active and position_low and position_low > 0 and price > position_low:
                bounce_pct = ((price - position_low) / position_low) * 100

                if bounce_pct >= HARD_PULLBACK_PCT:
                    # ── Check if SL is already protecting profit ──
                    # For SHORT: SL < entry_price means closing at SL still locks profit.
                    # If the bounce hasn't reached near the SL, let the trailing SL
                    # manage the exit instead of closing prematurely.
                    _entry = (
                        float(pos_info.get("openPriceAvg", 0) or 0)
                        if pos_info else 0.0
                    )
                    sl_protecting_profit = False

                    if trade_sl and _entry > 0:
                        sl_in_profit = trade_sl < _entry  # SHORT: SL below entry = profit locked
                        # How far is current price from the SL?
                        price_to_sl_pct = ((trade_sl - price) / price * 100) if price > 0 else 999

                        if sl_in_profit and price_to_sl_pct > 3.0:
                            # SL is in profit AND price is still >3% below the SL.
                            # The trailing SL will manage the exit — don't close prematurely.
                            sl_protecting_profit = True
                            logger.info(
                                f"🛡️ [SNIPER] {token.symbol} bounce +{bounce_pct:.1f}% but "
                                f"SL is in profit (SL={smart_round(trade_sl, price)}, "
                                f"entry={smart_round(_entry, price)}, "
                                f"price→SL={price_to_sl_pct:.1f}%) — "
                                f"letting SL manage exit"
                            )
                            detail["action"] = "sl_protecting_profit"
                            detail["bounce_pct"] = round(bounce_pct, 1)
                            detail["trade_sl"] = trade_sl
                            detail["sl_distance_pct"] = round(price_to_sl_pct, 1)

                    if not sl_protecting_profit:
                        # Hold through pullbacks; only close on dump-confirmed logic or SL handling.
                        detail["action"] = "hold_until_dump"
                        detail["bounce_pct"] = round(bounce_pct, 1)
                        logger.info(
                            f"🧲 [SNIPER] {token.symbol} bounce +{bounce_pct:.1f}% — holding until dump confirmation"
                        )
                else:
                    # Small bounce, not a hard pullback — keep riding
                    detail["bounce_pct"] = round(bounce_pct, 1)

            # Close short only after dump confirmation from peak.
            if token.peak_price and token.peak_price > 0:
                drop_from_peak = ((token.peak_price - price) / token.peak_price) * 100
                if drop_from_peak >= 30:
                    close_result = await _close_sniper_position(
                        db, connector, token, price, "dump_confirmed"
                    )
                    if close_result.get("success"):
                        profits_taken += 1
                        token.status = RugPullStatus.DUMPED
                        token.recommended_tp = None
                        token.trade_id = None
                        detail["action"] = "dump_confirmed_close"
                        detail["drop_from_peak_pct"] = round(drop_from_peak, 1)
                        detail["close"] = close_result
                        logger.info(
                            f"💀 [SNIPER] Dump confirmed on {token.symbol} ({drop_from_peak:.1f}% from peak) — closing short"
                        )
                    else:
                        detail["action"] = "dump_close_failed"
                        detail["error"] = close_result.get("error")

            # ── Check if position still exists on exchange ──
            bitget_sym = token.symbol
            if bitget_sym not in open_positions:
                # Position may have been closed by exchange SL
                # Check if we have a DB trade still marked open
                open_trade = (await db.execute(
                    select(Trade).where(
                        Trade.symbol == f"{token.symbol}/USDT",
                        Trade.exchange == "bitget",
                        Trade.status == "open",
                        Trade.side == "sell",
                    ).order_by(Trade.created_at.desc()).limit(1)
                )).scalar_one_or_none()

                if not open_trade:
                    # Also check for unfilled limit orders on the exchange
                    has_pending_orders = False
                    if connector:
                        try:
                            pending = await connector.get_futures_open_orders(symbol=f"{token.symbol}USDT")
                            has_pending_orders = any(
                                (o.get("side", "") or "").lower() == "sell"
                                and (o.get("tradeSide", "") or o.get("posSide", "") or "").lower() in ("open", "short", "")
                                for o in (pending or [])
                            )
                        except Exception:
                            pass

                    if has_pending_orders:
                        # Unfilled limit orders still pending — keep SHORTED status
                        logger.info(
                            f"⏳ [SNIPER] {token.symbol} has pending limit orders — "
                            f"keeping SHORTED status"
                        )
                        detail["action"] = "pending_orders"
                    else:
                        # SL was hit or position closed externally — reset for re-entry
                        logger.info(
                            f"🔄 [SNIPER] {token.symbol} position no longer open — "
                            f"resetting to WATCHING for re-entry"
                        )
                        token.status = RugPullStatus.WATCHING
                        token.recommended_tp = None
                        token.trade_id = None
                        detail["action"] = "sl_hit_reentry"
                        re_entries += 1

            details.append(detail)

    await db.commit()

    total_active = scanned + positions_monitored
    if declining_count > 0 or trades_executed > 0 or profits_taken > 0:
        logger.info(
            f"📊 [SNIPER CYCLE] Scanned {scanned} | Declining {declining_count} | "
            f"Signals {signals_created} | Trades {trades_executed} | "
            f"Monitoring {positions_monitored} | Profits {profits_taken} | "
            f"Re-entries {re_entries}"
        )

    return {
        "pump_intake": pump_intake,
        "scanned": scanned,
        "declining": declining_count,
        "signals_created": signals_created,
        "trades_executed": trades_executed,
        "positions_monitored": positions_monitored,
        "profits_taken": profits_taken,
        "re_entries": re_entries,
        "details": details,
    }


async def _is_tradeable_on_bitget(connector, symbol: str) -> bool:
    """Check if a symbol is available on Bitget futures by trying to get its ticker."""
    if not connector:
        return False

    from app.exchanges.bitget import BitgetConnector
    bitget_sym = f"{symbol}USDT"

    # First check the precision cache (fast — already in memory)
    if BitgetConnector._precision_cache.get(bitget_sym) or BitgetConnector._precision_cache.get(f"{symbol}/USDT"):
        return True

    # Fallback: try to fetch ticker (makes a real API call)
    try:
        ticker = await connector.get_ticker(f"{symbol}/USDT")
        return ticker is not None and float(ticker.get("last", 0) or 0) > 0
    except Exception:
        return False


async def _execute_sniper_short(
    db: AsyncSession,
    connector,
    signal: Signal,
    token: RugPullToken,
    entry: dict,
) -> dict:
    """
    Execute a sniper SHORT trade with SL only (no TP).
    The position rides down with the dump — TP is only triggered
    on hard pullback detection in the monitor phase.
    """
    from app.trading.live import LiveTradeEngine
    from app.trading.simulation import SimulationEngine
    from app.models.database import LiveTradeSettings, Trade
    from app.core.config import settings as app_settings
    from sqlalchemy import select as sa_select, func

    s = await LiveTradeEngine.get_or_create_settings(db)
    dry_run = bool(s.dry_run)

    if not app_settings.ENABLE_AUTO_TRADING and not dry_run:
        return {"error": "ENABLE_AUTO_TRADING is disabled"}

    if not connector:
        return {"error": "Bitget connector not available"}

    symbol = f"{token.symbol}/USDT"
    bitget_symbol = f"{token.symbol}USDT"
    side = "sell"
    hold_side = "short"

    margin_mode = s.auto_trade_margin_mode or "crossed"
    margin_size = SNIPER_BASE_MARGIN_USDT
    max_pos_size = s.max_position_size_usdt or 500.0

    # Get entry price from signal
    entry_price = entry.get("entry") or signal.price or token.current_price
    sl_price = entry.get("stop_loss")

    if not entry_price or entry_price <= 0:
        return {"error": "No valid entry price"}

    # ── Leverage (capped by exchange max) ──
    try:
        _, pair_max_lever = await connector.get_max_leverage(symbol)
        if isinstance(pair_max_lever, (int, float)) and 1 <= pair_max_lever <= 200:
            leverage = min(int(s.auto_trade_leverage or 10), int(pair_max_lever))
        else:
            leverage = max(1, int(s.auto_trade_leverage or 10))
    except Exception:
        leverage = max(1, int(s.auto_trade_leverage or 10))

    # ── Check entry count vs max allowed ──
    # Count both DB trades (filled) and exchange orders (unfilled limit)
    sniper_max = max(int(getattr(s, "sniper_max_entries", 1) or 1), 3)
    sniper_max_positions = getattr(s, "sniper_max_positions", 5) or 5

    # Total sniper position limit (across all tokens)
    total_sniper_result = await db.execute(
        sa_select(func.count(Trade.id)).where(
            Trade.exchange == "bitget",
            Trade.source == "sniper",
            Trade.status == "open",
            Trade.trade_side == "open",
        )
    )
    total_sniper_count = total_sniper_result.scalar() or 0
    if total_sniper_count >= sniper_max_positions:
        return {"error": f"Max total sniper positions reached ({total_sniper_count}/{sniper_max_positions})"}

    # Per-token entry limit
    existing_entries_result = await db.execute(
        sa_select(func.count(Trade.id)).where(
            Trade.exchange == "bitget",
            Trade.symbol == symbol,
            Trade.status == "open",
            Trade.trade_side == "open",
        )
    )
    db_entry_count = existing_entries_result.scalar() or 0

    # Also count unfilled sell limit orders on the exchange for this symbol
    exchange_order_count = 0
    try:
        open_orders = await connector.get_futures_open_orders(symbol=bitget_symbol)
        exchange_order_count = sum(
            1 for o in (open_orders or [])
            if (o.get("side", "") or "").lower() == "sell"
            and (o.get("tradeSide", "") or o.get("posSide", "") or "").lower() in ("open", "short", "")
        )
    except Exception:
        pass

    existing_entry_count = db_entry_count + exchange_order_count
    if existing_entry_count >= sniper_max:
        return {"error": f"Max sniper entries reached ({existing_entry_count}/{sniper_max}, db={db_entry_count}, orders={exchange_order_count}) for {symbol}"}

    # ── Balance check ──
    try:
        bal_data = await connector.get_futures_balance()
        available_balance = LiveTradeEngine._sum_available_margin(bal_data)
    except Exception as e:
        return {"error": f"Failed to fetch balance: {e}"}

    if available_balance <= 0:
        return {
            "error": (
                f"No tradable futures margin available ({available_balance:.4f} USDT)"
            )
        }

    # ── Position sizing (start $5, scale to max $15 per token) ──
    used_margin_for_token = float(existing_entry_count) * SNIPER_BASE_MARGIN_USDT
    remaining_token_margin = max(0.0, SNIPER_MAX_MARGIN_USDT - used_margin_for_token)
    if remaining_token_margin <= 0:
        return {
            "error": (
                f"Max sniper margin reached ({SNIPER_MAX_MARGIN_USDT:.0f} USDT) for {symbol}"
            )
        }

    risk_amount = min(margin_size, available_balance, max_pos_size, remaining_token_margin)
    notional = risk_amount * leverage
    amount = notional / entry_price

    if amount <= 0:
        return {"error": "Calculated order amount is zero"}

    # ── Get current market price for limit vs market decision ──
    try:
        ticker = await connector.get_ticker(symbol)
        current_market_price = float(ticker.get("last") or ticker.get("close") or 0)
    except Exception:
        current_market_price = 0.0

    # For shorts: use limit if our entry is above market (waiting for bounce)
    use_limit = False
    if current_market_price > 0 and entry_price > current_market_price * 1.001:
        use_limit = True

    order_type = "limit" if use_limit else "market"
    order_price = str(smart_round(entry_price, entry_price)) if use_limit else None
    execution_price = entry_price if use_limit else current_market_price or entry_price

    if dry_run:
        logger.info(
            f"[SNIPER][DRY-RUN] SHORT {amount:.6f} {symbol} @ {execution_price} ({order_type}) "
            f"| SL={sl_price} | NO TP | leverage={leverage}x"
        )
        return {
            "success": True,
            "dry_run": True,
            "symbol": symbol,
            "side": "sell",
            "amount": round(amount, 6),
            "price": execution_price,
            "leverage": leverage,
            "order_type": order_type,
            "sl": sl_price,
            "tp": None,
        }

    # ── Place the order ──
    logger.info(
        f"[SNIPER] Placing SHORT {amount:.6f} {symbol} @ {order_type}"
        f"{' ' + str(order_price) if order_price else ''}"
        f" | SL={sl_price} | NO TP | leverage={leverage}x | margin={margin_mode}"
    )

    try:
        order_result = await connector.create_futures_order(
            symbol=bitget_symbol,
            margin_coin="USDT",
            side="sell",
            order_type=order_type,
            size=str(round(amount, 6)),
            price=order_price,
            margin_mode=margin_mode,
            leverage=leverage,
            trade_side="open",
            stop_loss=None,
            take_profit=None,
        )
    except Exception as e:
        return {"error": f"Order placement failed: {e}"}

    order_id = order_result.get("orderId", "")

    # ── Place SL only (no TP — let it ride) ──
    if sl_price:
        try:
            await connector.place_tpsl_order(
                symbol=bitget_symbol,
                margin_coin="USDT",
                plan_type="loss_plan",
                trigger_price=sl_price,
                hold_side="short",
                size=str(round(amount, 6)),
            )
            logger.info(f"[SNIPER] SL placed for {symbol} short: {sl_price}")
        except Exception as e:
            logger.error(
                f"[SNIPER] Failed to place SL for {symbol}: "
                + str(e).replace("{", "{{").replace("}", "}}")
            )

    # ── Record trade in DB ──
    trade = Trade(
        exchange="bitget",
        exchange_order_id=order_id,
        signal_id=signal.id,
        symbol=symbol,
        side="sell",
        trade_side="open",
        order_type=order_type,
        amount=amount,
        price=execution_price,
        stop_loss=sl_price,
        take_profit=None,  # NO TP — managed by sniper monitor
        margin_mode=margin_mode,
        leverage=leverage,
        status="open",
        source="sniper",
        raw_response=json.dumps({
            "order": order_result,
            "sniper_entry": True,
            "entry_method": "rug_pull_sniper",
            "buying_power_declining": True,
        }),
    )
    db.add(trade)
    await db.flush()

    # Mark signal as executed
    signal.status = SignalStatus.EXECUTED

    logger.info(
        f"✅ [SNIPER] SHORT opened: {symbol} | Order: {order_id} | "
        f"Amount: {amount:.6f} | Entry: {execution_price} | SL: {sl_price} | No TP"
    )

    return {
        "success": True,
        "trade_id": trade.id,
        "order_id": order_id,
        "symbol": symbol,
        "side": "sell",
        "amount": round(amount, 6),
        "price": execution_price,
        "leverage": leverage,
        "order_type": order_type,
        "sl": sl_price,
        "tp": None,
    }


async def _close_sniper_position(
    db: AsyncSession,
    connector,
    token: RugPullToken,
    current_price: float,
    reason: str,
) -> dict:
    """Close a sniper SHORT position (buy to close) on hard pullback."""
    symbol = f"{token.symbol}/USDT"
    bitget_symbol = f"{token.symbol}USDT"

    # Find the open position on exchange
    try:
        pos_data = await connector.get_futures_positions()
    except Exception as e:
        return {"error": f"Failed to fetch positions: {e}"}

    short_pos = next(
        (
            p for p in (pos_data or [])
            if bitget_symbol in (p.get("symbol", ""))
            and (p.get("holdSide", "") or "").lower() == "short"
            and float(p.get("available", 0) or p.get("total", 0) or 0) > 0
        ),
        None,
    )

    if not short_pos:
        # Position may already be closed (SL hit)
        # Update any DB trades
        open_trade = (await db.execute(
            select(Trade).where(
                Trade.symbol == symbol,
                Trade.exchange == "bitget",
                Trade.status == "open",
                Trade.side == "sell",
            ).order_by(Trade.created_at.desc()).limit(1)
        )).scalar_one_or_none()

        if open_trade:
            open_trade.status = "closed"
            open_trade.closed_at = now_sast()

        return {"success": True, "already_closed": True, "reason": reason}

    # Get position size
    position_size = float(short_pos.get("available", 0) or short_pos.get("total", 0) or 0)
    unrealized_pnl = float(short_pos.get("unrealizedPL", 0) or 0)

    if position_size <= 0:
        return {"error": "Position size is zero"}

    # ── Place BUY order to close the short ──
    try:
        close_result = await connector.create_futures_order(
            symbol=bitget_symbol,
            margin_coin="USDT",
            side="buy",
            order_type="market",
            size=str(round(position_size, 6)),
            price=None,
            margin_mode="crossed",
            leverage=None,
            trade_side="close",
        )
    except Exception as e:
        return {"error": f"Close order failed: {e}"}

    # Update DB trade
    open_trade = (await db.execute(
        select(Trade).where(
            Trade.symbol == symbol,
            Trade.exchange == "bitget",
            Trade.status == "open",
            Trade.side == "sell",
        ).order_by(Trade.created_at.desc()).limit(1)
    )).scalar_one_or_none()

    if open_trade:
        open_trade.status = "closed"
        open_trade.closed_at = now_sast()
        open_trade.pnl = unrealized_pnl

    logger.info(
        f"💰 [SNIPER] Closed SHORT {symbol} | Reason: {reason} | "
        f"PnL: {unrealized_pnl:.4f} USDT | Size: {position_size}"
    )

    return {
        "success": True,
        "symbol": symbol,
        "reason": reason,
        "pnl": unrealized_pnl,
        "close_price": current_price,
        "size": position_size,
    }
