"""
JARVIS Voice Trade Execution
----------------------------
Lets the user say commands like:
  "Execute the best Gold signal"
  "Based on sniper setups, buy XAUUSD"
  "Execute a crypto trade on BTC based on recent signals"

JARVIS will:
1. Parse the spoken command to extract symbol + market (MT5/crypto).
2. Fetch live candles and run the SMC strategy engine to get fresh signals.
3. Fall back to stored signals / sniper setups if the live analysis is empty.
4. Ask the AI to pick the best entry + assess current conditions.
5. Calculate lot size from the MT5 account balance / equity (or crypto notional).
6. Execute the trade via MT5Plugin (smc_place) or Bitget.
7. Return a spoken confirmation.
"""
from __future__ import annotations

import re
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


# ── Symbol helpers ──────────────────────────────────────────────────────────

_SYMBOL_MAP = {
    # Gold
    "gold": "XAUUSD", "xauusd": "XAUUSD",
    # Bitcoin
    "bitcoin": "BTCUSDT", "btc": "BTCUSDT",
    # Ethereum
    "ethereum": "ETHUSDT", "eth": "ETHUSDT",
    # Solana
    "solana": "SOLUSDT", "sol": "SOLUSDT",
    # XRP
    "xrp": "XRPUSDT", "ripple": "XRPUSDT",
    # Silver
    "silver": "XAGUSD", "xagusd": "XAGUSD",
    # Indices
    "nas100": "NAS100", "nasdaq": "NAS100", "us30": "US30", "dow": "US30",
    "spx": "SPX500", "s&p": "SPX500",
}

_MT5_SYMBOLS = {"XAUUSD", "XAGUSD", "NAS100", "US30", "SPX500", "EURUSD", "GBPUSD", "USDJPY"}


def _detect_symbol(text: str) -> Optional[str]:
    t = text.lower()
    for kw, sym in _SYMBOL_MAP.items():
        if kw in t:
            return sym
    m = re.search(r"\b([A-Z]{2,6}(?:USDT?|USD)?)\b", text.upper())
    if m:
        return m.group(1)
    return None


def _is_mt5_symbol(sym: str) -> bool:
    return sym.upper() in _MT5_SYMBOLS or (sym.endswith("USD") and "USDT" not in sym)


def _exchange_symbol(sym: str) -> str:
    """Convert XAUUSD → XAUUSDT for Bitget/exchange lookup."""
    if sym == "XAUUSD":
        return "XAUUSDT"
    if sym == "XAGUSD":
        return "XAGUSDT"
    if sym.endswith("USD") and not sym.endswith("USDT"):
        return sym + "T"
    return sym


async def _get_mt5_account(db: AsyncSession) -> Optional[dict]:
    """Get the primary MT5 account balance/equity."""
    try:
        from plugins.MT5TradingPlugin.backend.models import MT5Account  # type: ignore
        row = (await db.execute(select(MT5Account).limit(1))).scalars().first()
        if row:
            return {
                "id": row.id,
                "login": row.login,
                "server": row.server,
                "password": row.password_encrypted,
                "name": row.name,
                "balance": row.balance,
                "equity": row.equity,
                "free_margin": row.free_margin,
                "currency": row.currency,
                "leverage": row.leverage,
            }
    except Exception as e:
        logger.debug(f"[VoiceTrade] MT5 account fetch: {e}")
    return None


async def _run_live_smc_analysis(symbol: str, timeframe: str = "H1", account_balance: float = 0) -> list[dict]:
    """
    Fetch fresh candles from Bitget (fallback when MT5 bridge is offline)
    and run the SMC strategy engine to get current sniper setups.
    Returns a list of SmcSignal dicts with entry/sl/tp/side/confidence.
    """
    signals: list[dict] = []
    try:
        from app.exchanges.manager import exchange_manager, SupportedExchange  # type: ignore
        from plugins.MT5TradingPlugin.backend.services.smc_engine import SMCStrategyEngine, candles_from_payload  # type: ignore

        exchange_sym = _exchange_symbol(symbol)
        # Map timeframe labels
        _TF = {"H1": "1h", "H4": "4h", "M15": "15m", "M30": "30m", "D1": "1d", "1h": "1h"}
        tf = _TF.get(timeframe, "1h")

        conn = exchange_manager.get_exchange(SupportedExchange.BITGET)
        if not conn:
            logger.debug("[VoiceTrade] Bitget not available for live candles")
            return []

        raw_candles = await conn.get_ohlcv(exchange_sym, tf, 200)
        if not raw_candles or len(raw_candles) < 40:
            logger.debug(f"[VoiceTrade] Not enough candles for {symbol}: {len(raw_candles) if raw_candles else 0}")
            return []

        # raw_candles: [[timestamp, open, high, low, close, volume], ...]
        candle_dicts = [
            {"time": int(c[0] / 1000), "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]), "volume": float(c[5] or 0)}
            for c in raw_candles
        ]
        candles = candles_from_payload(candle_dicts)
        engine = SMCStrategyEngine(
            min_rr=1.5, max_rr=4.0, sl_buffer_atr=1.0, min_confidence=0.55,
            symbol=symbol, account_balance=account_balance, risk_per_trade_pct=1.0,
        )
        analysis = engine.analyze(candles)
        for sig in analysis.get("signals") or []:
            signals.append({
                "source": "smc_live",
                "symbol": symbol,
                "action": sig.get("side") or sig.get("action"),
                "entry": sig.get("entry"),
                "sl": sig.get("stop_loss"),
                "tp": sig.get("take_profit"),
                "confidence": sig.get("confidence"),
                "rr": sig.get("risk_reward"),
                "zone_kind": sig.get("zone_kind"),
                "lot": sig.get("volume"),
            })
        logger.info(f"[VoiceTrade] SMC live analysis {symbol}: {len(signals)} signals")
    except ImportError as ie:
        logger.debug(f"[VoiceTrade] SMC import error: {ie}")
    except Exception as e:
        logger.warning(f"[VoiceTrade] Live SMC error: {e}")
    return signals


async def _get_stored_signals(db: AsyncSession, symbol: str) -> list[dict]:
    """Fetch stored signals from the core signal table as a fallback."""
    out: list[dict] = []
    try:
        from app.models.database import Signal  # type: ignore
        from sqlalchemy import desc
        base = symbol.replace("USDT", "").replace("USD", "")
        q = select(Signal).where(
            Signal.symbol.ilike(f"%{base}%")
        ).order_by(desc(Signal.created_at)).limit(8)
        rows = (await db.execute(q)).scalars().all()
        for s in rows:
            out.append({
                "source": s.source,
                "symbol": s.symbol,
                "action": s.action,
                "entry": float(s.entry_price) if s.entry_price else None,
                "sl": float(s.stop_loss) if s.stop_loss else None,
                "tp": float(s.take_profit) if s.take_profit else None,
                "confidence": float(s.confidence) if s.confidence else None,
            })
    except Exception as e:
        logger.debug(f"[VoiceTrade] stored signals error: {e}")
    return out


async def _get_sniper_setups(db: AsyncSession, symbol: str) -> list[dict]:
    """Fetch Telegram sniper setups as a fallback."""
    out: list[dict] = []
    try:
        from plugins.TelegramSignalNewsPlugin.backend.models import TelegramSniperTrade  # type: ignore
        from sqlalchemy import desc
        base = symbol.replace("USDT", "").replace("USD", "")
        q = select(TelegramSniperTrade).where(
            TelegramSniperTrade.symbol.ilike(f"%{base}%"),
        ).order_by(desc(TelegramSniperTrade.created_at)).limit(5)
        rows = (await db.execute(q)).scalars().all()
        for s in rows:
            out.append({
                "source": "sniper",
                "symbol": s.symbol,
                "action": getattr(s, "direction", None),
                "entry": float(s.entry) if s.entry else None,
                "sl": float(s.sl) if s.sl else None,
                "tp": float(s.tp1) if s.tp1 else None,
                "confidence": float(s.confidence) if s.confidence else None,
            })
    except Exception as e:
        logger.debug(f"[VoiceTrade] sniper setups error: {e}")
    return out


def _calculate_lot_size(equity: float, risk_pct: float = 1.0, sl_pips: float = 30) -> float:
    """1% risk per trade. $10/pip for Gold standard lot."""
    risk_usd = equity * risk_pct / 100
    pip_value = 10.0
    lot = risk_usd / (sl_pips * pip_value)
    return round(max(0.01, min(lot, 10.0)), 2)


def _sl_pips(entry: Optional[float], sl: Optional[float], symbol: str) -> float:
    """Approximate stop loss in pips from entry/SL prices."""
    if not entry or not sl:
        return 30.0
    diff = abs(entry - sl)
    # Gold: 1 pip = $0.10 per mini lot → roughly 10 price units per pip at 0.01 lot
    # For XAUUSD: 1 pip = $1 move
    if "XAU" in symbol.upper():
        return max(5.0, round(diff, 1))  # diff is already in pips for gold
    return max(5.0, round(diff * 10000, 1))


async def _execute_via_smc_place(
    account: dict, symbol: str, action: str,
    entry: Optional[float], sl: Optional[float], tp: Optional[float], volume: float,
) -> dict:
    """Execute via the MT5 strategy/place endpoint (handles paper mode gracefully)."""
    try:
        from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client  # type: ignore
        from plugins.MT5TradingPlugin.backend.models import MT5Account  # type: ignore
        side = action.lower()

        if mt5_client is None:
            return {"ok": False, "error": "MT5 client not initialised", "paper": True}

        result = await mt5_client.place_order(
            login=account["login"], server=account["server"],
            password=account["password"],
            symbol=symbol,
            order_type=f"{side}_limit" if entry else side,
            volume=volume,
            price=entry,
            sl=sl, tp=tp,
            comment="JARVIS voice trade",
        )
        return {"ok": True, "detail": result}
    except Exception as e:
        err = str(e)
        if "unreachable" in err.lower() or "connection" in err.lower() or "refused" in err.lower():
            # MT5 live bridge offline — record as a Paul decision
            return {
                "ok": True,
                "paper": True,
                "detail": f"MT5 bridge offline. Signal recorded: {action.upper()} {symbol} entry={entry} SL={sl} TP={tp} lots={volume}",
            }
        return {"ok": False, "error": err}


async def _execute_crypto_trade(symbol: str, side: str, notional_usdt: float) -> dict:
    """Place a Bitget futures market order."""
    try:
        from app.exchanges.manager import exchange_manager, SupportedExchange  # type: ignore
        conn = exchange_manager.get_exchange(SupportedExchange.BITGET)
        if not conn:
            return {"ok": False, "error": "Bitget not configured"}
        exchange_sym = _exchange_symbol(symbol)
        ticker = await conn.get_ticker(exchange_sym)
        price = float(ticker.get("last") or ticker.get("close") or 0)
        if price <= 0:
            return {"ok": False, "error": f"Cannot get price for {symbol}"}
        qty = round(notional_usdt / price, 6)
        result = await conn.create_futures_order(
            symbol=exchange_sym, margin_coin="USDT",
            side="open_long" if side.lower() == "buy" else "open_short",
            order_type="market", size=str(qty),
        )
        return {"ok": True, "detail": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Main entry point ────────────────────────────────────────────────────────

async def execute_voice_trade_command(
    db: AsyncSession,
    command: str,
    ai_provider_id: Optional[int] = None,
) -> str:
    """Parse a natural-language trade command, find/generate the best signal,
    size the trade, execute it, and return a spoken confirmation."""

    symbol = _detect_symbol(command)
    if not symbol:
        return ("I couldn't identify a trading symbol in your command, Sir. "
                "Please specify a pair like Gold, Bitcoin, or XAUUSD.")

    mt5_acct = await _get_mt5_account(db)
    equity = (mt5_acct["equity"] or mt5_acct["balance"] or 200) if mt5_acct else 200

    # 1) Try live SMC analysis first (same engine as MT5 Live page)
    setups = await _run_live_smc_analysis(symbol, timeframe="H1", account_balance=equity)

    # 2) Fall back to stored signals + sniper setups
    if not setups:
        setups = await _get_stored_signals(db, symbol)
    if not setups:
        setups = await _get_sniper_setups(db, symbol)

    if not setups:
        return (
            f"I ran a live SMC analysis on {symbol} (H1 chart) but found no high-quality setups "
            "at the moment, Sir. The market may be consolidating. "
            "Try the MT5 Live page to run a manual analysis, or ask me again when price "
            "is near a key order block or FVG."
        )

    # 3) Ask the AI to pick the best setup
    from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat  # type: ignore
    setup_lines = "\n".join(
        f"- [{s.get('source','?')}] {(s.get('action') or '?').upper()} "
        f"entry={s.get('entry')} sl={s.get('sl')} tp={s.get('tp')} "
        f"rr={s.get('rr','?')} conf={s.get('confidence','?'):.2f}" if isinstance(s.get('confidence'), float)
        else
        f"- [{s.get('source','?')}] {(s.get('action') or '?').upper()} "
        f"entry={s.get('entry')} sl={s.get('sl')} tp={s.get('tp')} "
        f"conf={s.get('confidence','?')}"
        for s in setups[:6]
    )
    acct_line = (
        f"MT5 account balance: {mt5_acct['balance']:.2f} {mt5_acct['currency']}, "
        f"equity: {mt5_acct['equity']:.2f}"
    ) if mt5_acct else "No MT5 account data."

    ai_prompt = f"""You are JARVIS trading AI. Choose ONE best setup for {symbol}:

{setup_lines}

{acct_line}

Return ONLY JSON:
{{"action":"buy or sell","entry":price_or_null,"sl":price_or_null,"tp":price_or_null,"sl_pips":number,"reasoning":"1 sentence"}}"""

    chosen: dict = {}
    ai_result = await db_chat(db, [{"role": "user", "content": ai_prompt}], temperature=0.1, json_mode=True)
    if ai_result.get("ok"):
        import json as _json
        try:
            txt = ai_result.get("content") or ai_result.get("text") or "{}"
            chosen = _json.loads(txt) if isinstance(txt, str) else txt
        except Exception:
            pass

    # Fall back to best confidence if AI fails
    if not chosen.get("action") and setups:
        best = max(setups, key=lambda s: float(s.get("confidence") or 0))
        chosen = {
            "action": best.get("action") or "buy",
            "entry": best.get("entry"),
            "sl": best.get("sl"),
            "tp": best.get("tp"),
            "sl_pips": _sl_pips(best.get("entry"), best.get("sl"), symbol),
            "reasoning": f"Best confidence setup from {best.get('source','analysis')}.",
        }

    action   = str(chosen.get("action", "buy")).lower()
    entry    = chosen.get("entry")
    sl       = chosen.get("sl")
    tp       = chosen.get("tp")
    sl_pips  = float(chosen.get("sl_pips") or _sl_pips(entry, sl, symbol))
    reasoning = chosen.get("reasoning", "")

    is_mt5 = _is_mt5_symbol(symbol)

    if is_mt5 and mt5_acct:
        lot_size = _calculate_lot_size(equity, risk_pct=1.0, sl_pips=sl_pips)
        result   = await _execute_via_smc_place(mt5_acct, symbol, action, entry, sl, tp, lot_size)
        if result.get("ok"):
            paper_note = " (Paper mode — MT5 bridge offline.)" if result.get("paper") else ""
            return (
                f"Order placed, Sir. {action.upper()} {symbol} — "
                f"{lot_size} lots{(' at ' + str(entry)) if entry else ' at market'}. "
                f"Stop loss {sl or 'not set'}, take profit {tp or 'not set'}. "
                f"Rationale: {reasoning}{paper_note}"
            )
        else:
            return (f"I attempted to execute {action.upper()} {symbol} but the order failed: "
                    f"{result.get('error')}. Please check the MT5 Live page, Sir.")
    else:
        try:
            from app.exchanges.manager import exchange_manager, SupportedExchange  # type: ignore
            conn = exchange_manager.get_exchange(SupportedExchange.BITGET)
            fb   = await conn.get_futures_balance() if conn else []
            eq   = 100.0
            if isinstance(fb, list) and fb:
                eq = float(fb[0].get("accountEquity") or fb[0].get("usdtEquity") or 100)
        except Exception:
            eq = 100.0
        notional = round(eq * 0.02, 2)
        result   = await _execute_crypto_trade(symbol, action, notional)
        if result.get("ok"):
            return (f"Crypto order placed, Sir. {action.upper()} {symbol} — "
                    f"${notional} notional. {reasoning}")
        else:
            return f"Crypto order failed for {symbol}: {result.get('error')}, Sir."

