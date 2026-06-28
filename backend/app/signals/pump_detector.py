"""
Pre-Pump Detector v2 — Deep Analysis Engine

Major improvements over v1:
 - BTC market context: filters out market-wide rallies to only flag true outliers
 - Multi-timeframe momentum: uses 1h, 24h, 7d to detect sustained vs flash pumps
 - ATH breakout detection: coins near ATH with volume = confirmed breakout
 - Volatility analysis: uses high/low spread to detect healthy vs noisy moves
 - BTC-relative outperformance: only flags coins beating BTC by significant margin
 - Always-watch list: BTC, ETH, SOL, XRP are always monitored for major moves
 - FDV/MCap ratio: detects potential unlock pressure / supply dilution risk
 - Trending momentum cross-check: trending + volume + price = high conviction
 - Adaptive thresholds: different scoring for large-cap vs mid-cap vs small-cap

Detection philosophy:
 - A real pump has MULTIPLE confirming factors, not just one metric spiking
 - Market context matters: if BTC is up 5%, an altcoin up 7% is NOT special
 - Volume WITHOUT price = accumulation (early signal)
 - Price WITHOUT volume = manipulation (avoid)
 - Volume + Price + Social = highest conviction signal
"""
import aiohttp
import json
from loguru import logger
from sqlalchemy import select, func, delete, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import timedelta

from app.core.config import settings
from app.core.timezone import now_sast
from app.models.database import (
    PumpToken, PumpStatus,
    Signal, SignalAction, SignalSource, SignalStatus,
    Trade,
)
from app.sentiment.cmc_community import fetch_cmc_community_sentiment, SymbolSentiment

# ── Configuration ───────────────────────────────────────────
COINGECKO_TIMEOUT = 20

# Thresholds
PUMP_SCORE_THRESHOLD = 0.55       # Minimum combined score to flag
CONFIRM_SCORE_THRESHOLD = 0.70    # Score to confirm pump is building
MAX_WATCH_HOURS = 48              # Extended from 24h for better tracking
MAX_MARKET_CAP_RANK = 500         # Top 500 by volume
PUMPED_RETENTION_HOURS = settings.PUMP_MONITOR_PUMPED_RETENTION_HOURS

# Volume analysis
VOLUME_MCAP_RATIO_HIGH = 0.30     # 30%+ vol/mcap = very unusual
VOLUME_MCAP_RATIO_MED = 0.15      # 15%+ vol/mcap = moderately unusual
VOLUME_MCAP_RATIO_LOW = 0.08      # 8%+ vol/mcap = starting to show interest

# Price acceleration
PRICE_ACCEL_HIGH = 10.0           # 10%+ in 1h = strong acceleration
PRICE_ACCEL_MED = 5.0             # 5%+ in 1h = moderate acc
PRICE_ACCEL_LOW = 2.0             # 2%+ in 1h = mild acc

# BTC relative outperformance
BTC_OUTPERFORM_MIN = 3.0          # Must beat BTC 1h% by at least 3%
BTC_OUTPERFORM_STRONG = 8.0       # Beating BTC by 8%+ = very strong signal

# ATH breakout
ATH_PROXIMITY_PCT = 15.0          # Within 15% of ATH = breakout zone
ATH_DEEP_DISCOUNT_PCT = 80.0      # 80%+ below ATH = reversal candidate

# Market regime
BTC_BULL_THRESHOLD_1H = 2.0       # BTC up 2%+ in 1h = bull market
BTC_STRONG_BULL_1H = 5.0          # BTC up 5%+ in 1h = strong rally (discount alts)
BTC_BEAR_THRESHOLD_1H = -2.0      # BTC down 2%+ in 1h = bearish

# Stablecoins, wrapped, and derivative tokens to skip
SKIP_SYMBOLS = frozenset({
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "FRAX", "USDD",
    "GUSD", "PYUSD", "FDUSD", "USDE", "CRVUSD", "GHO", "LUSD",
    "WBTC", "WETH", "STETH", "RETH", "CBETH", "WSTETH", "MSOL",
    "HBTC", "RENBTC", "TBTC", "WBETH",
})

# Always-watch coins — monitored regardless of score
WATCHLIST_COINS = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
    "ripple": "XRP",
}

# Market cap tiers for adaptive scoring
LARGE_CAP_RANK = 20
MID_CAP_RANK = 100


async def _get_exchange_tradeable_symbols() -> set[str]:
    """Get set of symbols tradeable on the configured exchange (e.g. {'BTC', 'ETH', ...}).

    Uses the Bitget precision cache; falls back to loading markets directly.
    Returns empty set if exchange is not available (disables filtering).
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
                logger.warning(f"[PUMP MONITOR] Failed to refresh exchange contract cache: {e}")

    for key in cache:
        if key.endswith("USDT") and "/" not in key:
            symbols.add(key.replace("USDT", "").upper())
        elif key.endswith("/USDT"):
            symbols.add(key.split("/")[0].upper())

    return symbols


async def _fetch_coingecko_markets() -> list[dict]:
    """Fetch coins from CoinGecko sorted by volume for pump detection.

    Now fetches 7d price change for deeper multi-timeframe analysis.
    """
    api_key = settings.COINGECKO_API_KEY
    base = "https://api.coingecko.com/api/v3"
    headers = {"x-cg-demo-api-key": api_key} if api_key else {}

    all_coins: list[dict] = []
    async with aiohttp.ClientSession() as session:
        for page in range(1, 4):  # 3 pages x 250 = 750 coins
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
                        "price_change_percentage": "1h,24h,7d",
                    },
                    timeout=aiohttp.ClientTimeout(total=COINGECKO_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        all_coins.extend(data)
                    elif resp.status == 429:
                        logger.warning("CoinGecko rate limited during pump scan")
                        break
                    else:
                        logger.warning(f"CoinGecko markets page {page} returned {resp.status}")
                        break
            except Exception as e:
                logger.warning(f"Failed to fetch CoinGecko markets page {page}: {e}")
                break

    return all_coins


async def _fetch_trending_coins() -> set[str]:
    """Fetch trending coin IDs from CoinGecko search/trending endpoint."""
    api_key = settings.COINGECKO_API_KEY
    base = "https://api.coingecko.com/api/v3"
    headers = {"x-cg-demo-api-key": api_key} if api_key else {}

    trending_ids: set[str] = set()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{base}/search/trending",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=COINGECKO_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data.get("coins", []):
                        coin = item.get("item", {})
                        coin_id = coin.get("id")
                        if coin_id:
                            trending_ids.add(coin_id)
    except Exception as e:
        logger.warning(f"Failed to fetch trending coins: {e}")

    return trending_ids


def _extract_btc_context(all_coins: list[dict]) -> dict:
    """Extract BTC, ETH market data for context.

    Returns dict with BTC/ETH 1h%, 24h%, 7d% and overall market sentiment.
    This is CRITICAL — without market context every altcoin pump looks the same.
    """
    btc_data = {}
    eth_data = {}

    for coin in all_coins:
        cid = coin.get("id", "")
        if cid == "bitcoin":
            btc_data = coin
        elif cid == "ethereum":
            eth_data = coin
        if btc_data and eth_data:
            break

    btc_1h = btc_data.get("price_change_percentage_1h_in_currency") or 0
    btc_24h = btc_data.get("price_change_percentage_24h") or 0
    btc_7d = btc_data.get("price_change_percentage_7d_in_currency") or 0
    eth_1h = eth_data.get("price_change_percentage_1h_in_currency") or 0
    eth_24h = eth_data.get("price_change_percentage_24h") or 0

    # Determine market regime
    if btc_1h >= BTC_STRONG_BULL_1H:
        sentiment = "strong_bull"
    elif btc_1h >= BTC_BULL_THRESHOLD_1H:
        sentiment = "bullish"
    elif btc_1h <= BTC_BEAR_THRESHOLD_1H:
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    return {
        "btc_1h": btc_1h,
        "btc_24h": btc_24h,
        "btc_7d": btc_7d,
        "eth_1h": eth_1h,
        "eth_24h": eth_24h,
        "sentiment": sentiment,
        "btc_price": btc_data.get("current_price") or 0,
        "eth_price": eth_data.get("current_price") or 0,
    }


def _score_pump_potential(
    coin: dict,
    trending_ids: set[str],
    market_ctx: dict,
    cmc_sentiment: dict[str, SymbolSentiment] | None = None,
) -> dict | None:
    """
    Deep multi-factor pump scoring with 8 indicators.

    Returns None if coin should be skipped entirely.
    Returns dict with 8 individual scores and combined pump_score.

    KEY DIFFERENCE from v1: Uses BTC market context to filter out
    market-wide rallies. A coin must OUTPERFORM the market, not just go up.
    """
    mcap = coin.get("market_cap") or 0
    vol_24h = coin.get("total_volume") or 0
    pct_1h = coin.get("price_change_percentage_1h_in_currency") or 0
    pct_24h = coin.get("price_change_percentage_24h") or 0
    pct_7d = coin.get("price_change_percentage_7d_in_currency") or 0
    coin_id = coin.get("id", "")
    rank = coin.get("market_cap_rank") or 9999
    symbol = (coin.get("symbol") or "").upper()
    high_24h = coin.get("high_24h") or 0
    low_24h = coin.get("low_24h") or 0
    price = coin.get("current_price") or 0
    ath = coin.get("ath") or 0
    ath_change_pct = coin.get("ath_change_percentage") or 0
    fdv = coin.get("fully_diluted_valuation") or 0

    # Skip stablecoins, wrapped, derivatives
    if symbol in SKIP_SYMBOLS:
        return None

    btc_1h = market_ctx.get("btc_1h", 0)
    btc_24h = market_ctx.get("btc_24h", 0)
    sentiment = market_ctx.get("sentiment", "neutral")
    is_watchlist = coin_id in WATCHLIST_COINS

    # Derived Metrics
    vol_mcap_ratio = (vol_24h / mcap) if mcap > 0 else 0
    price_range_24h = ((high_24h - low_24h) / low_24h * 100) if low_24h > 0 else 0
    fdv_mcap_ratio = (fdv / mcap) if mcap > 0 and fdv > 0 else 1.0
    btc_relative_1h = pct_1h - btc_1h
    btc_relative_24h = pct_24h - btc_24h

    # Cap Tier
    if rank <= LARGE_CAP_RANK:
        cap_tier = "large"
    elif rank <= MID_CAP_RANK:
        cap_tier = "mid"
    else:
        cap_tier = "small"

    # ── INDICATOR 1: Volume Spike Score (0-1) ──
    if vol_mcap_ratio >= VOLUME_MCAP_RATIO_HIGH:
        volume_spike_score = min(1.0, 0.6 + (vol_mcap_ratio - VOLUME_MCAP_RATIO_HIGH) * 2)
    elif vol_mcap_ratio >= VOLUME_MCAP_RATIO_MED:
        ratio = (vol_mcap_ratio - VOLUME_MCAP_RATIO_MED) / (VOLUME_MCAP_RATIO_HIGH - VOLUME_MCAP_RATIO_MED)
        volume_spike_score = 0.3 + ratio * 0.3
    elif vol_mcap_ratio >= VOLUME_MCAP_RATIO_LOW:
        ratio = (vol_mcap_ratio - VOLUME_MCAP_RATIO_LOW) / (VOLUME_MCAP_RATIO_MED - VOLUME_MCAP_RATIO_LOW)
        volume_spike_score = 0.1 + ratio * 0.2
    else:
        volume_spike_score = vol_mcap_ratio / VOLUME_MCAP_RATIO_LOW * 0.1 if VOLUME_MCAP_RATIO_LOW > 0 else 0

    # Large caps normally have lower vol/mcap, so boost
    if cap_tier == "large" and vol_mcap_ratio >= 0.05:
        volume_spike_score = min(1.0, volume_spike_score + 0.15)

    # ── INDICATOR 2: Price Acceleration Score (0-1) ──
    if pct_1h >= PRICE_ACCEL_HIGH:
        price_accel_score = min(1.0, 0.7 + (pct_1h - PRICE_ACCEL_HIGH) / 20 * 0.3)
    elif pct_1h >= PRICE_ACCEL_MED:
        price_accel_score = 0.4 + (pct_1h - PRICE_ACCEL_MED) / (PRICE_ACCEL_HIGH - PRICE_ACCEL_MED) * 0.3
    elif pct_1h >= PRICE_ACCEL_LOW:
        price_accel_score = 0.15 + (pct_1h - PRICE_ACCEL_LOW) / (PRICE_ACCEL_MED - PRICE_ACCEL_LOW) * 0.25
    elif pct_1h > 0:
        price_accel_score = pct_1h / PRICE_ACCEL_LOW * 0.15
    else:
        price_accel_score = 0.0

    # For large caps: even 3% in 1h is significant
    if cap_tier == "large" and pct_1h >= 3.0:
        price_accel_score = min(1.0, price_accel_score + 0.2)
    elif cap_tier == "mid" and pct_1h >= 5.0:
        price_accel_score = min(1.0, price_accel_score + 0.1)

    # ── INDICATOR 3: Social / Trending + CMC Community Score (0-1) ──
    social_score = 0.0
    if coin_id in trending_ids:
        social_score = 0.6
        if rank > 100:
            social_score = 0.85
        elif rank > 50:
            social_score = 0.7
        # Trending + volume spike = very high confidence
        if vol_mcap_ratio >= VOLUME_MCAP_RATIO_MED:
            social_score = min(1.0, social_score + 0.15)

    # CMC news headline sentiment boost
    cmc_sym = (cmc_sentiment or {}).get(symbol)
    cmc_score_component = 0.0
    if cmc_sym and cmc_sym.mention_count > 0:
        # Mentions alone = mild boost; bullish sentiment = strong boost
        mention_boost = min(0.3, cmc_sym.mention_count * 0.07)
        sentiment_val = cmc_sym.avg_sentiment  # -1 to +1
        if sentiment_val >= 0.15:
            # Bullish news articles — strong addition
            cmc_score_component = min(0.6, mention_boost + sentiment_val * 0.4)
        elif sentiment_val <= -0.15:
            # Bearish news articles — mild penalty
            cmc_score_component = max(-0.2, sentiment_val * 0.15)
        else:
            # Neutral mentions still count as attention
            cmc_score_component = mention_boost * 0.5

        # Multiple news sources = higher confidence
        if len(cmc_sym.sources) >= 2:
            cmc_score_component = min(0.8, cmc_score_component * 1.3)

        social_score = max(0.0, min(1.0, social_score + cmc_score_component))

    # ── INDICATOR 4: Order Flow Score (0-1) ──
    order_flow_score = 0.0
    if pct_1h > 0 and vol_mcap_ratio > VOLUME_MCAP_RATIO_LOW:
        order_flow_score = min(1.0, (pct_1h / 10) * (vol_mcap_ratio / 0.15))

    # Price near 24h high with volume = strong buy pressure
    if high_24h > 0 and price > 0:
        high_proximity = price / high_24h
        if high_proximity >= 0.97 and vol_mcap_ratio >= VOLUME_MCAP_RATIO_LOW:
            order_flow_score = min(1.0, order_flow_score + 0.2)

    # ── INDICATOR 5: Momentum Consistency Score (0-1) — NEW ──
    momentum_score = 0.0
    positive_tfs = sum(1 for x in [pct_1h, pct_24h, pct_7d] if x > 0)

    if positive_tfs == 3:
        # All timeframes positive — sustained uptrend
        momentum_score = 0.5
        avg_24h_per_hour = abs(pct_24h) / 24 if pct_24h > 0 else 0
        if avg_24h_per_hour > 0 and pct_1h > avg_24h_per_hour * 3:
            momentum_score = 0.75
        if avg_24h_per_hour > 0 and pct_1h > avg_24h_per_hour * 5:
            momentum_score = 0.9
    elif positive_tfs == 2 and pct_1h > 0:
        momentum_score = 0.3
        if pct_1h > 3:
            momentum_score = 0.45
    elif pct_1h > PRICE_ACCEL_MED and pct_24h <= 0:
        # Sharp 1h reversal from 24h downtrend
        momentum_score = 0.35

    # 7d downtrend with sudden 1h spike = reversal signal
    if pct_7d < -15 and pct_1h >= 5:
        momentum_score = max(momentum_score, 0.6)

    # ── INDICATOR 6: BTC-Relative Outperformance Score (0-1) — NEW ──
    btc_relative_score = 0.0

    if btc_relative_1h >= BTC_OUTPERFORM_STRONG:
        btc_relative_score = min(1.0, 0.7 + (btc_relative_1h - BTC_OUTPERFORM_STRONG) / 20 * 0.3)
    elif btc_relative_1h >= BTC_OUTPERFORM_MIN:
        ratio = (btc_relative_1h - BTC_OUTPERFORM_MIN) / (BTC_OUTPERFORM_STRONG - BTC_OUTPERFORM_MIN)
        btc_relative_score = 0.3 + ratio * 0.4
    elif btc_relative_1h > 0:
        btc_relative_score = btc_relative_1h / BTC_OUTPERFORM_MIN * 0.3
    else:
        # Underperforming BTC — this is NOT a pump
        btc_relative_score = 0.0

    # In strong bull market, raise the bar
    if sentiment == "strong_bull":
        btc_relative_score *= 0.7

    # Watchlist coins (BTC itself): use absolute change instead
    if is_watchlist:
        if abs(pct_1h) >= 5.0:
            btc_relative_score = min(1.0, abs(pct_1h) / 10)
        elif abs(pct_1h) >= 3.0:
            btc_relative_score = 0.4
        else:
            btc_relative_score = abs(pct_1h) / 5 * 0.3

    # ── INDICATOR 7: Volatility Health Score (0-1) — NEW ──
    volatility_score = 0.0

    if price_range_24h > 0:
        if 10 <= price_range_24h <= 50 and high_24h > 0:
            high_pct = (price / high_24h) if high_24h > 0 else 0
            if high_pct >= 0.90:
                # Price is near the high end of its range — bullish
                volatility_score = min(1.0, price_range_24h / 30 * 0.7 + 0.3)
            elif high_pct >= 0.75:
                volatility_score = price_range_24h / 40 * 0.4
            else:
                # Price near the low — this dumped, not pumping
                volatility_score = 0.05
        elif price_range_24h > 50:
            # Extremely volatile — could be pump & dump
            volatility_score = 0.2
        elif price_range_24h >= 5:
            # Moderate volatility
            volatility_score = price_range_24h / 10 * 0.3

    # ── INDICATOR 8: ATH Breakout Score (0-1) — NEW ──
    ath_breakout_score = 0.0

    if ath > 0 and price > 0:
        pct_from_ath = abs(ath_change_pct)

        if pct_from_ath <= 5:
            # Within 5% of ATH — potential breakout!
            ath_breakout_score = 0.9
            if pct_1h > 2:
                ath_breakout_score = 1.0
        elif pct_from_ath <= ATH_PROXIMITY_PCT:
            # Within 15% of ATH — approaching breakout zone
            ath_breakout_score = 0.5 + (ATH_PROXIMITY_PCT - pct_from_ath) / ATH_PROXIMITY_PCT * 0.4
        elif pct_from_ath >= ATH_DEEP_DISCOUNT_PCT:
            # 80%+ below ATH — deep value if volume is spiking
            if vol_mcap_ratio >= VOLUME_MCAP_RATIO_MED and pct_1h > 3:
                ath_breakout_score = 0.4

    # ── COMBINED PUMP SCORE ──
    # Weight architecture (v2.1 — social boosted with CMC community data):
    #   BTC-relative:      18%  (filtering market noise is #1 priority)
    #   Price acceleration: 17%  (short-term momentum)
    #   Volume spike:       17%  (institutional interest)
    #   Momentum:           14%  (multi-timeframe confirmation)
    #   Order flow:         11%  (buy-side pressure)
    #   Social + CMC:       10%  (KOL sentiment + CoinGecko trending)
    #   Volatility:          7%  (move quality)
    #   ATH breakout:        6%  (breakout context)
    pump_score = (
        btc_relative_score * 0.18 +
        price_accel_score * 0.17 +
        volume_spike_score * 0.17 +
        momentum_score * 0.14 +
        order_flow_score * 0.11 +
        social_score * 0.10 +
        volatility_score * 0.07 +
        ath_breakout_score * 0.06
    )

    # ── Conviction Multipliers ──
    active_signals = sum(1 for s in [
        volume_spike_score, price_accel_score, btc_relative_score, momentum_score
    ] if s >= 0.4)
    if active_signals >= 3:
        pump_score = min(1.0, pump_score * 1.15)

    # Volume WITHOUT price = possible accumulation (early but uncertain)
    if volume_spike_score >= 0.5 and price_accel_score < 0.15:
        pump_score *= 0.8

    # Price WITHOUT volume = possible manipulation
    if price_accel_score >= 0.5 and volume_spike_score < 0.15:
        pump_score *= 0.7

    # FDV significantly higher than MCap = supply dilution risk
    if fdv_mcap_ratio > 5.0:
        pump_score *= 0.85

    # ── Watchlist Adjustment ──
    if is_watchlist and cap_tier == "large":
        if abs(pct_1h) >= 3.0 or abs(pct_24h) >= 8.0:
            pump_score = max(pump_score, 0.50)
        if abs(pct_1h) >= 5.0 or abs(pct_24h) >= 12.0:
            pump_score = max(pump_score, 0.65)

    return {
        "volume_spike_score": round(volume_spike_score, 3),
        "price_accel_score": round(price_accel_score, 3),
        "social_score": round(social_score, 3),
        "order_flow_score": round(order_flow_score, 3),
        "momentum_score": round(momentum_score, 3),
        "btc_relative_score": round(btc_relative_score, 3),
        "volatility_score": round(volatility_score, 3),
        "ath_breakout_score": round(ath_breakout_score, 3),
        "pump_score": round(pump_score, 3),
        "vol_mcap_ratio": round(vol_mcap_ratio, 4),
        "btc_relative_1h": round(btc_relative_1h, 2),
        "price_range_24h": round(price_range_24h, 2),
        "cap_tier": cap_tier,
        "is_watchlist": is_watchlist,
        "market_sentiment": market_ctx.get("sentiment", "neutral"),
        "cmc_mentions": cmc_sym.mention_count if cmc_sym else 0,
        "cmc_sentiment": round(cmc_sym.avg_sentiment, 3) if cmc_sym else 0.0,
        "cmc_label": cmc_sym.signal_label if cmc_sym else "none",
        "cmc_sources": len(cmc_sym.sources) if cmc_sym else 0,
    }


async def scan_for_pumps(db: AsyncSession) -> dict:
    """
    Scan CoinGecko for tokens showing pre-pump signals.

    v2: Extracts BTC market context first, uses adaptive thresholds,
    always includes watchlist coins (BTC/ETH/SOL/XRP).
    """
    all_coins = await _fetch_coingecko_markets()
    if not all_coins:
        logger.info("[PUMP MONITOR] No market data available")
        return {"new": [], "updated": [], "total_scanned": 0, "market_ctx": {}}

    # Load exchange tradeable symbols to filter out untradeable tokens
    exchange_symbols = await _get_exchange_tradeable_symbols()
    if exchange_symbols:
        logger.info(f"[PUMP MONITOR] Exchange has {len(exchange_symbols)} tradeable contracts")
    else:
        logger.warning("[PUMP MONITOR] No exchange symbols loaded — skipping exchange filter")

    # Fetch CMC community KOL sentiment (cached, non-blocking)
    try:
        cmc_sentiment = await fetch_cmc_community_sentiment()
    except Exception as e:
        logger.warning(f"[PUMP MONITOR] CMC community fetch failed: {e}")
        cmc_sentiment = {}

    # Extract BTC market context FIRST
    market_ctx = _extract_btc_context(all_coins)
    trending_ids = await _fetch_trending_coins()

    logger.info(
        f"[PUMP MONITOR] Market: BTC 1h={market_ctx['btc_1h']:+.1f}% "
        f"24h={market_ctx['btc_24h']:+.1f}% | Sentiment: {market_ctx['sentiment']}"
        f" | CMC News: {len(cmc_sentiment)} symbols tracked"
    )

    # Get existing active pump tokens
    active_statuses = [PumpStatus.DETECTED, PumpStatus.CONFIRMED, PumpStatus.SIGNALLED]
    existing = (await db.execute(
        select(PumpToken).where(PumpToken.status.in_(active_statuses))
    )).scalars().all()
    existing_map = {t.coin_id: t for t in existing}

    new_tokens = []
    updated_tokens = []
    now = now_sast()

    for coin in all_coins:
        coin_id = coin.get("id", "")
        symbol = (coin.get("symbol") or "").upper()
        rank = coin.get("market_cap_rank") or 9999
        is_watchlist = coin_id in WATCHLIST_COINS

        # Skip coins outside our focus range (unless watchlist)
        if rank > MAX_MARKET_CAP_RANK and not is_watchlist:
            continue

        # Skip coins not tradeable on the configured exchange
        if exchange_symbols and symbol not in exchange_symbols:
            continue

        scores = _score_pump_potential(coin, trending_ids, market_ctx, cmc_sentiment)
        if scores is None:
            continue

        pump_score = scores.get("pump_score", 0)

        # Below threshold — skip (but still update existing tracked tokens)
        if pump_score < PUMP_SCORE_THRESHOLD and coin_id not in existing_map and not is_watchlist:
            continue

        price = coin.get("current_price") or 0
        pct_1h = coin.get("price_change_percentage_1h_in_currency") or 0
        pct_24h = coin.get("price_change_percentage_24h") or 0
        pct_7d = coin.get("price_change_percentage_7d_in_currency") or 0
        vol = coin.get("total_volume") or 0
        mcap = coin.get("market_cap") or 0
        high_24h = coin.get("high_24h") or 0
        low_24h = coin.get("low_24h") or 0
        ath = coin.get("ath") or 0
        ath_change_pct = coin.get("ath_change_percentage") or 0
        fdv = coin.get("fully_diluted_valuation") or 0

        if coin_id in existing_map:
            # ── Update existing token ──
            token = existing_map[coin_id]
            token.current_price = price
            token.price_change_1h = pct_1h
            token.price_change_24h = pct_24h
            token.price_change_7d = pct_7d
            token.volume_24h = vol
            token.volume_change_pct = scores.get("vol_mcap_ratio", 0) * 100
            token.high_24h = high_24h
            token.low_24h = low_24h
            token.ath = ath
            token.ath_change_pct = ath_change_pct
            token.fully_diluted_valuation = fdv
            token.market_cap = mcap
            token.market_cap_rank = rank

            # Update all 8 scores
            token.volume_spike_score = scores["volume_spike_score"]
            token.price_accel_score = scores["price_accel_score"]
            token.social_score = scores["social_score"]
            token.order_flow_score = scores["order_flow_score"]
            token.momentum_score = scores["momentum_score"]
            token.btc_relative_score = scores["btc_relative_score"]
            token.volatility_score = scores["volatility_score"]
            token.ath_breakout_score = scores["ath_breakout_score"]
            token.pump_score = pump_score

            # Update BTC context
            token.btc_price_1h_pct = market_ctx["btc_1h"]
            token.btc_price_24h_pct = market_ctx["btc_24h"]
            token.market_sentiment = market_ctx["sentiment"]

            # Track peak
            if price and (not token.peak_price or price > token.peak_price):
                token.peak_price = price
                if token.price_at_detection and token.price_at_detection > 0:
                    token.peak_gain_pct = ((price - token.price_at_detection) / token.price_at_detection) * 100

            # Update gain since detection
            if token.price_at_detection and token.price_at_detection > 0:
                token.gain_since_detection = ((price - token.price_at_detection) / token.price_at_detection) * 100

            # ── Status Transitions ──
            if pump_score >= CONFIRM_SCORE_THRESHOLD and token.status == PumpStatus.DETECTED:
                token.status = PumpStatus.CONFIRMED
                logger.info(
                    f"[PUMP MONITOR] {symbol} CONFIRMED "
                    f"(score={pump_score:.2f}, btc_rel={scores['btc_relative_1h']:+.1f}%)"
                )

            # Fade if score dropped significantly
            fade_threshold = PUMP_SCORE_THRESHOLD * 0.65
            if pump_score < fade_threshold and token.status == PumpStatus.DETECTED:
                if not token.is_watchlist:
                    token.status = PumpStatus.FADED
                    token.expired_at = now
                    logger.info(f"[PUMP MONITOR] {symbol} faded (score={pump_score:.2f})")

            # Expire old tokens
            if token.detected_at and (now - token.detected_at) > timedelta(hours=MAX_WATCH_HOURS):
                if token.status in (PumpStatus.DETECTED, PumpStatus.CONFIRMED):
                    if not token.is_watchlist:
                        token.status = PumpStatus.EXPIRED
                        token.expired_at = now

            updated_tokens.append(symbol)
        else:
            # ── New detection ──
            min_score = 0.30 if is_watchlist else PUMP_SCORE_THRESHOLD
            if pump_score < min_score:
                continue

            initial_status = PumpStatus.CONFIRMED if pump_score >= CONFIRM_SCORE_THRESHOLD else PumpStatus.DETECTED

            token = PumpToken(
                coin_id=coin_id,
                symbol=symbol,
                name=coin.get("name", symbol),
                image=coin.get("image"),
                price_at_detection=price,
                current_price=price,
                price_change_1h=pct_1h,
                price_change_24h=pct_24h,
                price_change_7d=pct_7d,
                volume_24h=vol,
                volume_change_pct=scores.get("vol_mcap_ratio", 0) * 100,
                high_24h=high_24h,
                low_24h=low_24h,
                ath=ath,
                ath_change_pct=ath_change_pct,
                fully_diluted_valuation=fdv,
                market_cap=mcap,
                market_cap_rank=rank,
                volume_spike_score=scores["volume_spike_score"],
                price_accel_score=scores["price_accel_score"],
                social_score=scores["social_score"],
                order_flow_score=scores["order_flow_score"],
                momentum_score=scores["momentum_score"],
                btc_relative_score=scores["btc_relative_score"],
                volatility_score=scores["volatility_score"],
                ath_breakout_score=scores["ath_breakout_score"],
                pump_score=pump_score,
                btc_price_1h_pct=market_ctx["btc_1h"],
                btc_price_24h_pct=market_ctx["btc_24h"],
                market_sentiment=market_ctx["sentiment"],
                is_watchlist=is_watchlist,
                peak_price=price,
                status=initial_status,
            )
            db.add(token)
            new_tokens.append(symbol)

            tag = "WATCHLIST" if is_watchlist else "PRE-PUMP"
            logger.info(
                f"[PUMP MONITOR] [{tag}] {symbol} "
                f"(score={pump_score:.2f}, 1h={pct_1h:+.1f}%, "
                f"btc_rel={scores['btc_relative_1h']:+.1f}%, "
                f"vol/mcap={scores['vol_mcap_ratio']:.1%}, "
                f"sentiment={market_ctx['sentiment']})"
            )

    await db.commit()

    total_new = len(new_tokens)
    total_updated = len(updated_tokens)
    if total_new > 0 or total_updated > 0:
        logger.info(
            f"[PUMP MONITOR] Scanned {len(all_coins)} coins | "
            f"New: {total_new} | Updated: {total_updated} | "
            f"Market: {market_ctx['sentiment']} | BTC: {market_ctx['btc_1h']:+.1f}%"
        )

    return {
        "new": new_tokens,
        "updated": updated_tokens,
        "total_scanned": len(all_coins),
        "market_ctx": {
            "btc_1h": market_ctx["btc_1h"],
            "btc_24h": market_ctx["btc_24h"],
            "btc_7d": market_ctx.get("btc_7d", 0),
            "sentiment": market_ctx["sentiment"],
            "btc_price": market_ctx.get("btc_price", 0),
        },
        "cmc_community": {
            "symbols_tracked": len(cmc_sentiment),
            "bullish": [s.symbol for s in cmc_sentiment.values() if s.signal_label == "bullish"],
            "bearish": [s.symbol for s in cmc_sentiment.values() if s.signal_label == "bearish"],
        },
    }


async def _create_pump_signal(
    db: AsyncSession,
    token: PumpToken,
    cmc_sentiment: dict[str, SymbolSentiment] | None = None,
) -> Signal:
    """Create a BUY signal for a confirmed pre-pump token.

    Signal confidence is derived from multi-indicator confirmation,
    not just the raw pump_score.
    """
    symbol = f"{token.symbol}/USDT"
    price = token.current_price or token.price_at_detection

    # Lookup CMC community sentiment for this symbol
    cmc_sym = (cmc_sentiment or {}).get(token.symbol)

    # Calculate confirmation count for confidence
    strong_indicators = sum(1 for s in [
        token.volume_spike_score,
        token.price_accel_score,
        token.btc_relative_score,
        token.momentum_score,
        token.order_flow_score,
    ] if (s or 0) >= 0.4)

    base_confidence = token.pump_score or 0
    confirmation_bonus = min(0.15, strong_indicators * 0.03)
    confidence = min(0.95, base_confidence + confirmation_bonus)

    signal = Signal(
        source=SignalSource.SYSTEM,
        symbol=symbol,
        action=SignalAction.BUY,
        price=price,
        timeframe="1h",
        strength=token.pump_score,
        confidence=confidence,
        raw_data=json.dumps({
            "type": "pre_pump_v2",
            "pump_score": token.pump_score,
            "volume_spike": token.volume_spike_score,
            "price_accel": token.price_accel_score,
            "social": token.social_score,
            "order_flow": token.order_flow_score,
            "momentum": token.momentum_score,
            "btc_relative": token.btc_relative_score,
            "volatility": token.volatility_score,
            "ath_breakout": token.ath_breakout_score,
            "1h_change": token.price_change_1h,
            "24h_change": token.price_change_24h,
            "7d_change": token.price_change_7d,
            "btc_1h": token.btc_price_1h_pct,
            "market_sentiment": token.market_sentiment,
            "is_watchlist": token.is_watchlist,
            "strong_indicators": strong_indicators,
            "cmc_community": {
                "mentions": cmc_sym.mention_count if cmc_sym else 0,
                "sentiment": round(cmc_sym.avg_sentiment, 3) if cmc_sym else 0.0,
                "label": cmc_sym.signal_label if cmc_sym else "none",
                "sources": cmc_sym.sources if cmc_sym else [],
            },
        }),
        indicators=json.dumps({
            "pump_score": token.pump_score,
            "volume_spike_score": token.volume_spike_score,
            "price_accel_score": token.price_accel_score,
            "momentum_score": token.momentum_score,
            "btc_relative_score": token.btc_relative_score,
            "social_score": token.social_score,
            "ath_breakout_score": token.ath_breakout_score,
        }),
        status=SignalStatus.PENDING,
    )
    db.add(signal)
    await db.flush()

    token.signal_id = signal.id
    token.status = PumpStatus.SIGNALLED

    tag = " [WATCHLIST]" if token.is_watchlist else ""
    logger.info(
        f"[PUMP SIGNAL]{tag} BUY {symbol} "
        f"@ {price} | Score: {token.pump_score:.2f} | "
        f"Confidence: {confidence:.2f} | Confirmations: {strong_indicators}/5"
    )
    return signal


async def run_pump_monitor_cycle(db: AsyncSession) -> dict:
    """
    Full pump monitor cycle:
    1. Scan for new pre-pump tokens (with BTC context)
    2. Create signals for confirmed tokens
    3. Detect pumped tokens (adaptive threshold per cap tier)
    """
    # Phase 1: Scan
    scan_result = await scan_for_pumps(db)

    # Get cached CMC sentiment for signal creation
    from app.sentiment.cmc_community import get_cached_cmc_sentiment
    cmc_sentiment = get_cached_cmc_sentiment()

    # Phase 2: Create signals for CONFIRMED tokens without signals
    confirmed = (await db.execute(
        select(PumpToken).where(
            PumpToken.status == PumpStatus.CONFIRMED,
            PumpToken.signal_id.is_(None),
        )
    )).scalars().all()

    signals_created = 0
    for token in confirmed:
        try:
            await _create_pump_signal(db, token, cmc_sentiment)
            signals_created += 1
        except Exception as e:
            logger.warning(f"Failed to create signal for {token.symbol}: {e}")

    # Phase 3: Mark tokens that have pumped significantly
    signalled = (await db.execute(
        select(PumpToken).where(
            PumpToken.status.in_([PumpStatus.SIGNALLED, PumpStatus.TRADED]),
        )
    )).scalars().all()

    pumped_count = 0
    for token in signalled:
        pump_gain_threshold = 10 if token.is_watchlist else 20
        if token.gain_since_detection and token.gain_since_detection >= pump_gain_threshold:
            token.status = PumpStatus.PUMPED
            pumped_count += 1
            logger.info(
                f"[PUMP MONITOR] {token.symbol} PUMPED! "
                f"+{token.gain_since_detection:.1f}% since detection"
            )

    # Phase 4: Remove stale pumped tokens to keep monitor output current
    cutoff = now_sast() - timedelta(hours=PUMPED_RETENTION_HOURS)
    stale_pumped_filter = and_(
        PumpToken.status == PumpStatus.PUMPED,
        or_(
            and_(PumpToken.updated_at.is_not(None), PumpToken.updated_at < cutoff),
            and_(PumpToken.updated_at.is_(None), PumpToken.detected_at < cutoff),
        ),
    )
    delete_result = await db.execute(delete(PumpToken).where(stale_pumped_filter))
    cleaned_pumped_count = max(delete_result.rowcount or 0, 0)
    if cleaned_pumped_count:
        logger.info(
            f"[PUMP MONITOR] Removed {cleaned_pumped_count} stale pumped token(s) "
            f"older than {PUMPED_RETENTION_HOURS}h"
        )

    await db.commit()

    return {
        "scan": scan_result,
        "signals_created": signals_created,
        "confirmed_count": len(confirmed),
        "pumped_count": pumped_count,
        "cleaned_pumped_count": cleaned_pumped_count,
    }
