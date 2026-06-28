"""
Agent Paul Plugin — MT5 Adapter

Bridges the PAUL loop to the MT5 Trading plugin so Agent Paul can plan and
execute on MT5 accounts (e.g. XAUUSD on /mt5-live), in addition to crypto.

PLAN     — uses the MT5 plugin's SMC sniper engine (and optional AI review) on
           real MT5 candles to produce an entry / SL / TP / side setup.
EXECUTE  — places a resting limit order (buy_limit / sell_limit) on the account
           via the MT5 client, mirroring the /strategy/place sniper flow.

All MT5 imports are local + guarded so Agent Paul keeps working when the MT5
plugin is absent.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.AgentPaulPlugin.backend.models import PaulProvenance


class MT5Unavailable(Exception):
    """Raised when the MT5 plugin or account is not available."""


async def _resolve_account(db: AsyncSession, account_id: Optional[int]):
    from plugins.MT5TradingPlugin.backend.models import MT5Account

    if not account_id:
        raise MT5Unavailable("No MT5 account selected (set one in Decision Console or settings).")
    account = await db.get(MT5Account, account_id)
    if not account:
        raise MT5Unavailable(f"MT5 account {account_id} not found.")
    return account


async def plan_mt5(
    db: AsyncSession,
    symbol: str,
    timeframe: str,
    min_rr: float,
    account_id: Optional[int],
    use_ai: bool = True,
) -> Dict[str, Any]:
    """Return a plan dict (same shape as the crypto planner) for an MT5 symbol."""
    from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client
    from plugins.MT5TradingPlugin.backend.services.smc_strategy import (
        SMCStrategyEngine,
        candles_from_payload,
    )

    account = await _resolve_account(db, account_id)

    bars = await mt5_client.get_candles(
        account.login, account.server, account.password_encrypted,
        symbol, timeframe, 400,
    )
    candles = candles_from_payload(
        [
            {
                "time": b["time"], "open": b["open"], "high": b["high"],
                "low": b["low"], "close": b["close"], "volume": b.get("volume"),
            }
            for b in bars
        ]
    )
    data_source = "MT5"

    # MT5 history is often stale on forex/metals brokers (mtapi ignores fromDate),
    # so get_candles returns []. Mirror the MT5 sniper UI: fall back to an exchange
    # candle feed for the same instrument (e.g. XAUUSD → Bitget XAU/USDT).
    if len(candles) < 40:
        ex_candles = await _exchange_candles(account.server, symbol, timeframe)
        if len(ex_candles) >= 40:
            candles = ex_candles
            data_source = _fallback_exchange_name(account.server)

    analysis = SMCStrategyEngine(min_rr=min_rr).analyze(candles)
    signals = analysis.get("signals") or []

    base = {
        "action": "hold",
        "confidence": 0.0,
        "entry": analysis.get("last_price"),
        "stop_loss": None,
        "take_profit": None,
        "risk_reward": None,
        "reasoning": "",
        "provenance": PaulProvenance.HEURISTIC,
        "signal_id": None,
        "market": "mt5",
        "account_id": account.id,
    }

    if analysis.get("error") or not signals:
        base["reasoning"] = (
            f"SMC ({data_source}): {analysis.get('error') or 'no high-probability sniper setup'} "
            f"(bias {analysis.get('bias')}, RSI {analysis.get('rsi')})."
        )
        return base

    best = signals[0]
    side = str(best.get("side") or "").lower()
    if side not in ("buy", "sell"):
        base["reasoning"] = "SMC produced a setup with no clear side."
        return base

    reasoning = best.get("reason") or "SMC sniper setup."
    reasoning = f"{reasoning} [data: {data_source}]"
    provenance = PaulProvenance.HEURISTIC

    # Optional AI review — enriches reasoning / confidence using the same router
    if use_ai:
        try:
            from plugins.MT5TradingPlugin.backend.services.smc_ai import ai_review

            ai_block = await ai_review(db=db, symbol=symbol, timeframe=timeframe, analysis=analysis)
            if isinstance(ai_block, dict) and ai_block.get("available"):
                provenance = PaulProvenance.AI
                note = ai_block.get("summary") or ai_block.get("note")
                if note:
                    reasoning = f"{reasoning} | AI: {note}"
        except Exception as exc:  # noqa: BLE001 — AI is best-effort
            logger.debug(f"[Paul/MT5] AI review skipped: {exc}")

    base.update(
        {
            "action": side,
            "confidence": float(best.get("confidence") or 0.6),
            "entry": best.get("entry"),
            "stop_loss": best.get("stop_loss"),
            "take_profit": best.get("take_profit"),
            "risk_reward": best.get("rr"),
            "reasoning": reasoning,
            "provenance": provenance,
            "plan_json": {"smc": analysis, "selected": best},
        }
    )
    return base


async def execute_mt5(db: AsyncSession, decision) -> Dict[str, Any]:
    """Place a resting limit order on the MT5 account for an actionable decision."""
    from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client
    from plugins.MT5TradingPlugin.backend.services.sync_service import MT5SyncService

    account = await _resolve_account(db, decision.account_id)

    side = decision.action
    entry = decision.entry
    sl = decision.stop_loss
    tp = decision.take_profit

    # Fail-closed geometry validation (same rule as MT5 /strategy/place).
    if entry is None or sl is None or tp is None:
        return {"error": "Missing entry/SL/TP for MT5 order."}
    if side == "buy" and not (sl < entry < tp):
        return {"error": "Buy limit requires SL < entry < TP."}
    if side == "sell" and not (tp < entry < sl):
        return {"error": "Sell limit requires TP < entry < SL."}

    order_type = f"{side}_limit"
    volume = decision.volume or 0.01

    try:
        result = await mt5_client.place_order(
            login=account.login, server=account.server,
            password=account.password_encrypted,
            symbol=decision.symbol, order_type=order_type,
            volume=volume, price=entry, sl=sl, tp=tp,
            comment=f"AgentPaul#{decision.id}",
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"MT5 order failed: {exc}"}

    # Best-effort account sync so positions show on /mt5-live
    try:
        await MT5SyncService.sync_account(db, account)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[Paul/MT5] account sync skipped: {exc}")

    return {
        "mode": "mt5_live",
        "account_id": account.id,
        "order_type": order_type,
        "volume": volume,
        "ticket": result.get("ticket") if isinstance(result, dict) else None,
        "raw": result,
    }


# ── Exchange candle fallback (stale MT5 history) ───────────

# MT5 timeframe → ccxt/exchange timeframe
_MT5_TF_TO_EX = {
    "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "1h", "H4": "4h", "D1": "1d", "W1": "1w",
}


def _mt5_tf_to_exchange(tf: str) -> str:
    return _MT5_TF_TO_EX.get((tf or "").upper(), "1h")


def _to_exchange_symbol(symbol: str) -> Optional[str]:
    """Map an MT5 forex/metal symbol to a crypto-exchange pair (XAUUSD → XAU/USDT)."""
    s = (symbol or "").upper().replace("/", "")
    explicit = {
        "XAUUSD": "XAU/USDT", "XAGUSD": "XAG/USDT",
        "BTCUSD": "BTC/USDT", "ETHUSD": "ETH/USDT",
    }
    if s in explicit:
        return explicit[s]
    if s.endswith("USDT") and len(s) > 4:
        return f"{s[:-4]}/USDT"
    if s.endswith("USD") and len(s) > 3:
        return f"{s[:-3]}/USDT"
    return None


def _fallback_exchange(server: str):
    """Pick the crypto exchange whose feed backs this MT5 broker (BTGT → bitget)."""
    from app.exchanges.manager import SupportedExchange

    s = (server or "").lower()
    if "binance" in s:
        return SupportedExchange.BINANCE
    if "bybit" in s:
        return SupportedExchange.BYBIT
    if "okx" in s:
        return SupportedExchange.OKX
    if "kucoin" in s:
        return SupportedExchange.KUCOIN
    if "coinbase" in s:
        return SupportedExchange.COINBASE
    return SupportedExchange.BITGET  # default, incl. BTGT*Capital


def _fallback_exchange_name(server: str) -> str:
    return _fallback_exchange(server).value if hasattr(_fallback_exchange(server), "value") else "exchange"


async def _exchange_candles(server: str, symbol: str, timeframe: str):
    """Fetch SMC-ready candles from a crypto exchange when MT5 history is stale."""
    ex_symbol = _to_exchange_symbol(symbol)
    if not ex_symbol:
        return []
    try:
        from app.exchanges.manager import exchange_manager
        from plugins.MT5TradingPlugin.backend.services.smc_strategy import candles_from_payload

        connector = exchange_manager.get_exchange(_fallback_exchange(server))
        if not connector:
            return []
        ohlcv = await connector.get_ohlcv(
            symbol=ex_symbol, timeframe=_mt5_tf_to_exchange(timeframe), limit=400
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[Paul/MT5] exchange fallback failed for {symbol}: {exc}")
        return []

    rows = []
    for c in ohlcv or []:
        if not c or len(c) < 5:
            continue
        t = c[0]
        if isinstance(t, (int, float)) and t > 1e10:  # ms → s
            t = int(t / 1000)
        rows.append(
            {
                "time": int(t), "open": c[1], "high": c[2],
                "low": c[3], "close": c[4], "volume": c[5] if len(c) > 5 else 0,
            }
        )
    return candles_from_payload(rows)
