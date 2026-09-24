"""Real-time management of the room's open trades, on every account it trades.

The guardian (``app.agents.guardian``) decides, once a minute, whether a trade
that is under threat is being swept or genuinely broken. This module runs on a
few-second clock beside it and does the things that cannot wait a minute:

* **break-even and trailing** — ``trading.trailing.protective_stop``, forward
  only, sent only when the stop actually improves;
* **margin protection** — below ``margin_warn_pct`` the account takes no new
  entries; below ``margin_critical_pct`` the worst-losing room position is cut
  in half, tick after tick, until margin recovers; a floating loss beyond
  ``max_account_drawdown_pct`` of balance closes every room position there;
* **taking profit when the market turns** — a fresh confirmed OBLIVION signal
  the other way on 15m closes a winning trade; a 5m opposite signal, a
  structure break, or a momentum flip during a sudden adverse move banks
  ``shift_take_profit_fraction`` of it and locks the stop at break-even or
  better. A losing trade is left to the guardian's sweep-versus-break logic.

Only positions the room opened (``ROOM#`` comment / the room's trade rows) are
touched; a position someone opened by hand is theirs. Nothing here ever widens
a stop. Every action is recorded in the AI brain.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from loguru import logger

#: Seconds of price history kept per position for sudden-move detection.
MOVE_WINDOW_S = 90.0
#: The slower checks (OBLIVION re-reads, structure) run at most this often per position.
SHIFT_CHECK_S = 60.0
#: Quiet period after any action on one position.
ACTION_COOLDOWN_S = 20.0
#: Accounts with no room position are only polled this often.
IDLE_POLL_S = 30.0

_samples: Dict[str, Deque[Tuple[float, float]]] = {}
_partial_taken: set[str] = set()
_last_action: Dict[str, float] = {}
_last_shift_check: Dict[str, float] = {}
_last_poll: Dict[str, float] = {}
_entries_blocked: Dict[str, str] = {}
_alerted: set[str] = set()
_atr_cache: Dict[str, Tuple[float, Optional[float]]] = {}
_read_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
_candle_cache: Dict[Tuple[str, str], Tuple[float, list]] = {}
_status: Dict[str, Any] = {"last_cycle": None, "accounts": {}, "actions": 0, "errors": []}


def _under_test() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def reset_state() -> None:
    for store in (_samples, _last_action, _last_shift_check, _last_poll, _entries_blocked,
                  _atr_cache, _read_cache, _candle_cache):
        store.clear()
    _partial_taken.clear()
    _alerted.clear()


def status() -> Dict[str, Any]:
    return {**_status, "entries_blocked": dict(_entries_blocked)}


def entries_blocked(venue: str, account_id: Any) -> Optional[str]:
    """Why new entries are refused on this account right now, or None."""
    return _entries_blocked.get(f"{venue}:{account_id}")


def _round_lot(volume: float) -> float:
    return max(0.0, round(volume / 0.01) * 0.01)


# ── Pure decisions ──────────────────────────────────────────────────────────


def margin_level(equity: float, margin: float, reported: Any = None) -> Optional[float]:
    """Margin level in %, or None when nothing is margined."""
    try:
        lvl = float(reported) if reported not in (None, "") else 0.0
    except (TypeError, ValueError):
        lvl = 0.0
    if lvl > 0:
        return lvl
    if margin and margin > 0:
        return equity / margin * 100.0
    return None


def margin_plan(
    *, level: Optional[float], balance: float, positions: List[Dict[str, Any]],
    warn_pct: float, critical_pct: float, max_drawdown_pct: float,
) -> Dict[str, Any]:
    """What margin protection must do on one account.

    ``positions`` are the room's own, each ``{key, profit, volume}``.
    Returns ``{action: ok|warn|reduce|flatten, cuts: [(key, volume)], reason}``.
    """
    floating = sum(float(p.get("profit") or 0) for p in positions)
    if positions and balance > 0 and max_drawdown_pct > 0 and floating < -balance * max_drawdown_pct / 100.0:
        return {
            "action": "flatten",
            "cuts": [(p["key"], float(p.get("volume") or 0)) for p in positions],
            "reason": (f"room positions are down {floating:,.2f} — beyond the "
                       f"{max_drawdown_pct:g}% account drawdown limit ({balance * max_drawdown_pct / 100:,.2f})"),
        }
    if level is None:
        return {"action": "ok", "cuts": [], "reason": "no margin in use"}
    if level < critical_pct and positions:
        worst = min(positions, key=lambda p: float(p.get("profit") or 0))
        vol = float(worst.get("volume") or 0)
        cut = _round_lot(vol * 0.5)
        if cut < 0.01 or vol - cut < 0.01:
            cut = vol
        return {
            "action": "reduce", "cuts": [(worst["key"], cut)],
            "reason": (f"margin level {level:.0f}% is below the {critical_pct:g}% critical floor — "
                       f"cutting the worst-losing room position"),
        }
    if level < warn_pct:
        return {"action": "warn", "cuts": [],
                "reason": f"margin level {level:.0f}% is below {warn_pct:g}% — no new entries on this account"}
    return {"action": "ok", "cuts": [], "reason": f"margin level {level:.0f}%"}


def adverse_move(samples: List[Tuple[float, float]], *, is_long: bool, atr: Optional[float], mult: float) -> bool:
    """Has price just run against the position by more than ``mult`` × ATR?"""
    if not samples or not atr or atr <= 0 or mult <= 0:
        return False
    prices = [p for _, p in samples]
    last = prices[-1]
    move = (max(prices) - last) if is_long else (last - min(prices))
    return move > mult * atr


def shift_plan(
    *, side: str, entry: float, price: float, stop: Optional[float], volume: float,
    opposite_15m: bool, opposite_5m: bool, structure_broke: bool,
    momentum_against: bool, sudden_move: bool, fraction: float, already_partial: bool,
) -> Dict[str, Any]:
    """Take profit when the market turns against a winning trade.

    Returns ``{action: hold|take_partial|close|lock, volume, new_stop, reason}``.
    Stops only ever move toward profit.
    """
    is_long = str(side).lower() in {"buy", "long"}
    in_profit = (price - entry) > 0 if is_long else (entry - price) > 0
    hold = {"action": "hold", "volume": 0.0, "new_stop": None, "reason": ""}
    if not in_profit:
        return hold

    def _locked_stop() -> Optional[float]:
        if stop is None:
            return entry
        return max(stop, entry) if is_long else min(stop, entry)

    if opposite_15m:
        return {"action": "close", "volume": volume, "new_stop": None,
                "reason": "OBLIVION confirmed the opposite signal on 15m — banking the whole trade"}
    triggers = []
    if opposite_5m:
        triggers.append("OBLIVION confirmed the opposite signal on 5m")
    if structure_broke:
        triggers.append("15m structure broke against the trade")
    if momentum_against and sudden_move:
        triggers.append("momentum flipped during a sudden move against the trade")
    if not triggers:
        return hold
    reason = "; ".join(triggers)
    new_stop = _locked_stop()
    improves = new_stop is not None and (stop is None or (new_stop > stop if is_long else new_stop < stop))
    if already_partial:
        if improves:
            return {"action": "lock", "volume": 0.0, "new_stop": new_stop,
                    "reason": f"{reason} — profit already banked, stop locked at break-even"}
        return hold
    cut = _round_lot(volume * max(0.0, min(1.0, fraction)))
    if cut < 0.01 or volume - cut < 0.01:
        if improves:
            return {"action": "lock", "volume": 0.0, "new_stop": new_stop,
                    "reason": f"{reason} — too small to split, stop locked at break-even"}
        return hold
    return {"action": "take_partial", "volume": cut, "new_stop": new_stop if improves else None,
            "reason": f"{reason} — banking {fraction:.0%} and locking the rest at break-even"}


# ── Market reads (cached) ───────────────────────────────────────────────────


async def _candles(symbol: str, tf: str) -> list:
    key = (symbol, tf)
    hit = _candle_cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    from app.services import candles as candle_source

    try:
        rows = await asyncio.wait_for(candle_source.fetch(symbol, tf), timeout=30)
    except asyncio.TimeoutError:
        rows = []
    _candle_cache[key] = (time.time(), rows or [])
    return rows or []


async def _atr(symbol: str) -> Optional[float]:
    hit = _atr_cache.get(symbol)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    from app.agents import guard_read

    rows = await _candles(symbol, "15m")
    value = guard_read.atr(rows) if len(rows) >= 20 else None
    _atr_cache[symbol] = (time.time(), value)
    return value


async def _oblivion(symbol: str, tf: str) -> Dict[str, Any]:
    key = (symbol, tf)
    hit = _read_cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    from app.agents import oblivion_gate

    read = await oblivion_gate.read_pair(symbol, tf, source="trade_manager")
    verdict = read.get("verdict") or {}
    _read_cache[key] = (time.time(), verdict)
    return verdict


async def market_shift(symbol: str, side: str) -> Dict[str, bool]:
    """The slower reads behind a take-profit decision."""
    from app.agents import guard_read

    is_long = str(side).lower() in {"buy", "long"}
    opposite = "sell" if is_long else "buy"
    v15, v5 = await _oblivion(symbol, "15m"), await _oblivion(symbol, "5m")
    rows = await _candles(symbol, "15m")
    broke = bool(rows) and len(rows) >= 30 and guard_read.broke_structure(rows, is_long=is_long)
    mom = guard_read.momentum_bias(rows) if rows else "neutral"
    return {
        "opposite_15m": bool(v15.get("confirmed") and v15.get("action") == opposite),
        "opposite_5m": bool(v5.get("confirmed") and v5.get("action") == opposite),
        "structure_broke": broke,
        "momentum_against": mom == ("bearish" if is_long else "bullish"),
    }


# ── Recording ───────────────────────────────────────────────────────────────


async def _record(symbol: str, action: str, reason: str, detail: Dict[str, Any]) -> None:
    from app.agents import brain_log

    _status["actions"] += 1
    await brain_log.record_decision(
        agent_name="Trade Manager", agent_role="trade_manager", symbol=symbol,
        action=action, confidence=1.0, reasoning=reason, market_data=detail,
        ai_called=False, origin="trade_manager",
    )
    try:
        from app.core.events import Topics, event_bus

        await event_bus.publish(Topics.ROOM_EXECUTION, {"guard": True, "manager": True,
                                                        "symbol": symbol, "action": action,
                                                        "reason": reason, **detail})
    except Exception:  # noqa: BLE001
        pass


async def _announce_once(key: str, text: str) -> None:
    if key in _alerted:
        return
    _alerted.add(key)
    from app.agents import guardian

    await guardian._announce(text)


def _cooled(key: str) -> bool:
    return time.time() - _last_action.get(key, 0.0) >= ACTION_COOLDOWN_S


def _sample(key: str, price: float) -> List[Tuple[float, float]]:
    buf = _samples.setdefault(key, deque())
    now = time.time()
    buf.append((now, price))
    while buf and now - buf[0][0] > MOVE_WINDOW_S:
        buf.popleft()
    return list(buf)


# ── MT5 ─────────────────────────────────────────────────────────────────────


def _mt5_room_positions(raw: List[Dict[str, Any]], account_id: Any) -> List[Dict[str, Any]]:
    from app.trading.order_tags import SOURCE_ROOM, classify
    from plugins.MT5TradingPlugin.backend.services.mt5_client import normalize_order_type

    out = []
    for p in raw or []:
        if classify(p.get("comment"))["source"] != SOURCE_ROOM:
            continue
        side = normalize_order_type(p)
        if side not in ("buy", "sell"):
            continue
        try:
            out.append({
                "key": f"mt5:{account_id}:{int(p.get('ticket'))}",
                "ticket": int(p.get("ticket")),
                "symbol": str(p.get("symbol") or ""),
                "side": side,
                "volume": float(p.get("lots", p.get("volume", 0)) or 0),
                "entry": float(p.get("openPrice") or 0),
                "price": float(p.get("currentPrice") or p.get("openPrice") or 0),
                "sl": float(p["stopLoss"]) if p.get("stopLoss") else None,
                "tp": float(p["takeProfit"]) if p.get("takeProfit") else None,
                "profit": float(p.get("profit") or 0),
            })
        except (TypeError, ValueError):
            continue
    return out


async def _mt5_has_room_positions(db: Any, account_id: Any) -> bool:
    from sqlalchemy import select

    from app.trading.order_tags import SOURCE_ROOM, classify
    from plugins.MT5TradingPlugin.backend.models import MT5Position

    rows = (await db.execute(
        select(MT5Position.comment).where(MT5Position.account_id == account_id)
    )).all()
    return any(classify(r[0])["source"] == SOURCE_ROOM for r in rows)


async def manage_mt5_account(db: Any, account: Any, s: Any, *, send: bool) -> List[Dict[str, Any]]:
    from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client

    acct_key = f"mt5:{account.id}"
    now = time.time()
    if not await _mt5_has_room_positions(db, account.id) and now - _last_poll.get(acct_key, 0) < IDLE_POLL_S:
        return []
    _last_poll[acct_key] = now
    creds = dict(login=account.login, server=account.server, password=account.password_encrypted)
    info = await mt5_client.get_account_info(**creds)
    raw = await mt5_client.get_positions(**creds)
    positions = _mt5_room_positions(raw, account.id)
    acts: List[Dict[str, Any]] = []

    # ── margin ──
    level = margin_level(float(info.get("equity") or 0), float(info.get("margin") or 0), info.get("marginLevel"))
    plan = margin_plan(
        level=level, balance=float(info.get("balance") or 0), positions=positions,
        warn_pct=float(getattr(s, "margin_warn_pct", 300) or 300),
        critical_pct=float(getattr(s, "margin_critical_pct", 150) or 150),
        max_drawdown_pct=float(getattr(s, "max_account_drawdown_pct", 5) or 5),
    )
    _status["accounts"][acct_key] = {"margin_level": level, "positions": len(positions),
                                     "margin_action": plan["action"], "at": now}
    if plan["action"] in ("warn", "reduce", "flatten"):
        _entries_blocked[acct_key] = plan["reason"]
        await _announce_once(f"{acct_key}:margin:{plan['action']}",
                             f"⚠️ <b>{account.login}@{account.server}</b>: {plan['reason']}")
    else:
        _entries_blocked.pop(acct_key, None)
        for tag in ("warn", "reduce", "flatten"):
            _alerted.discard(f"{acct_key}:margin:{tag}")
    if plan["action"] in ("reduce", "flatten"):
        by_key = {p["key"]: p for p in positions}
        for key, volume in plan["cuts"]:
            pos = by_key.get(key)
            if pos is None or not _cooled(key):
                continue
            _last_action[key] = time.time()
            full = volume >= pos["volume"]
            if send:
                await mt5_client.close_position(**creds, ticket=pos["ticket"], volume=None if full else volume)
            detail = {"venue": "mt5", "account_id": account.id, "ticket": pos["ticket"],
                      "closed_volume": volume, "margin_level": level, "sent": send}
            await _record(pos["symbol"], "close" if full else "reduce", plan["reason"], detail)
            acts.append(detail)
        if plan["action"] == "flatten":
            return acts
        positions = [p for p in positions if p["key"] not in {k for k, _ in plan["cuts"]}]

    # ── OBLIVION ladders (room profile trades) ──
    planned = await run_ladders(db, account, positions, creds, send=send)
    acts += planned["acts"]
    positions = [p for p in positions if p["ticket"] not in planned["tickets"]]

    # ── per position ──
    from app.trading.trailing import protective_stop

    for pos in positions:
        key = pos["key"]
        if pos["entry"] <= 0 or pos["price"] <= 0:
            continue
        samples = _sample(key, pos["price"])
        if not _cooled(key):
            continue

        # Take profit when the market turns.
        is_long = pos["side"] == "buy"
        atr = await _atr(pos["symbol"])
        sudden = adverse_move(samples, is_long=is_long, atr=atr,
                              mult=float(getattr(s, "shift_atr_mult", 1.5) or 1.5))
        if sudden or now - _last_shift_check.get(key, 0) >= SHIFT_CHECK_S:
            _last_shift_check[key] = now
            shift = await market_shift(pos["symbol"], pos["side"])
            plan_s = shift_plan(
                side=pos["side"], entry=pos["entry"], price=pos["price"], stop=pos["sl"],
                volume=pos["volume"], sudden_move=sudden,
                fraction=float(getattr(s, "shift_take_profit_fraction", 0.5) or 0.5),
                already_partial=key in _partial_taken, **shift,
            )
            if plan_s["action"] != "hold":
                _last_action[key] = time.time()
                if plan_s["action"] in ("take_partial", "close"):
                    full = plan_s["action"] == "close"
                    if send:
                        await mt5_client.close_position(
                            **creds, ticket=pos["ticket"], volume=None if full else plan_s["volume"],
                        )
                    if not full:
                        _partial_taken.add(key)
                if plan_s.get("new_stop") is not None and plan_s["action"] != "close" and send:
                    await mt5_client.modify_order(**creds, ticket=pos["ticket"],
                                                  sl=round(float(plan_s["new_stop"]), 5), tp=pos["tp"])
                detail = {"venue": "mt5", "account_id": account.id, "ticket": pos["ticket"],
                          "action": plan_s["action"], "volume": plan_s["volume"],
                          "new_stop": plan_s.get("new_stop"), "price": pos["price"],
                          "entry": pos["entry"], "sent": send, **shift, "sudden_move": sudden}
                await _record(pos["symbol"], plan_s["action"], plan_s["reason"], detail)
                await _announce_once(f"{key}:{plan_s['action']}",
                                     f"💰 <b>{pos['symbol']}</b> #{pos['ticket']}: {plan_s['reason']}")
                acts.append(detail)
                continue

        # Break-even and trailing, forward only.
        new_sl = protective_stop(side=pos["side"], entry=pos["entry"], current_price=pos["price"],
                                 take_profit=pos["tp"], current_sl=pos["sl"])
        if new_sl is not None:
            _last_action[key] = time.time()
            if send:
                await mt5_client.modify_order(**creds, ticket=pos["ticket"], sl=round(new_sl, 5), tp=pos["tp"])
            beyond = new_sl > pos["entry"] if is_long else new_sl < pos["entry"]
            detail = {"venue": "mt5", "account_id": account.id, "ticket": pos["ticket"],
                      "action": "trail" if beyond else "break_even", "new_stop": new_sl,
                      "old_stop": pos["sl"], "price": pos["price"], "sent": send}
            await _record(pos["symbol"], detail["action"],
                          f"stop {'trailed' if beyond else 'moved to break-even'} → {new_sl:.5g}", detail)
            acts.append(detail)
    return acts


# ── OBLIVION ladder ─────────────────────────────────────────────────────────


def ladder_step(plan: Dict[str, Any], pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The next rung for one planned position, or None. Pure.

    At TP1 the stop moves to TP1 itself (``floor_at_tp1``) or to break-even,
    banking ``tp1_close`` of the position if that is set. A fixed ladder then
    banks ``tp2_close`` at TP2. A trailing plan (``trail_atr``) is carried from
    there by :func:`trail_stop`.
    """
    is_long = plan["side"] == "buy"
    price, stage = pos["price"], int(plan.get("stage") or 0)

    def reached(level: Optional[float]) -> bool:
        return level is not None and ((price >= level) if is_long else (price <= level))

    if stage == 0 and reached(plan.get("tp1")):
        frac = float(plan.get("tp1_close") or 0.0)
        stop = plan.get("tp1") if plan.get("floor_at_tp1") else pos["entry"]
        return {"stage": 1, "close": _round_lot(pos["volume"] * frac) if frac > 0 else 0.0,
                "stop": stop, "why": "TP1"}
    if stage == 1 and plan.get("trail_atr") and int(plan.get("trail_from") or 1) >= 2 and reached(plan.get("tp2")):
        # TP2 reached: the trail takes over from here (stop stays at the TP1 floor until it moves).
        return {"stage": 2, "close": 0.0, "stop": None, "why": "TP2"}
    if stage == 1 and not plan.get("trail_atr") and float(plan.get("tp2_close") or 0) > 0 and reached(plan.get("tp2")):
        left = max(1e-9, 1.0 - float(plan.get("tp1_close") or 0.5))
        frac = min(1.0, float(plan.get("tp2_close") or 0.25) / left)
        return {"stage": 2, "close": _round_lot(pos["volume"] * frac), "stop": plan.get("tp1"), "why": "TP2"}
    return None


def trail_stop(plan: Dict[str, Any], current_stop: Optional[float], last_close: float,
               atr: Optional[float]) -> Optional[float]:
    """Where the trailing stop belongs after the last closed bar, or None if it
    would not move forward. Never below TP1 once TP1 was reached. Pure."""
    if not plan.get("trail_atr") or int(plan.get("stage") or 0) < max(1, int(plan.get("trail_from") or 1)) \
            or not atr or atr <= 0:
        return None
    is_long = plan["side"] == "buy"
    trail = last_close - plan["trail_atr"] * atr if is_long else last_close + plan["trail_atr"] * atr
    floor = plan.get("tp1") if plan.get("floor_at_tp1") else None
    candidates = [x for x in (trail, floor) if x is not None]
    best = max(candidates) if is_long else min(candidates)
    if current_stop is not None and ((best <= current_stop) if is_long else (best >= current_stop)):
        return None
    # Only worth a broker call when it moves at least a tenth of an ATR.
    if current_stop is not None and abs(best - current_stop) < 0.1 * atr:
        return None
    return best


async def _closed_bar(symbol: str, tf: str) -> tuple[Optional[float], Optional[float]]:
    """(close, ATR) of the last *closed* bar on ``tf`` — what the backtest trails from."""
    from app.backtest.oblivion_backtest import atr_series

    rows = await _candles(symbol, tf)
    if len(rows) < 30:
        return None, None
    closed = rows[:-1]
    return float(closed[-1][4]), atr_series(closed)[-1]


async def run_ladders(db: Any, account: Any, positions: List[Dict[str, Any]], creds: Dict[str, Any],
                      *, send: bool) -> Dict[str, Any]:
    """Expire unfilled pullback limits and step every filled plan up its ladder."""
    from sqlalchemy import select

    from app.core.timezone import now_sast
    from app.models.database import RoomTradePlan
    from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client

    out: Dict[str, Any] = {"acts": [], "tickets": set()}
    if db is None:
        return out
    plans = (await db.execute(select(RoomTradePlan).where(
        RoomTradePlan.venue == "mt5", RoomTradePlan.account_id == account.id,
        RoomTradePlan.status.in_(("pending", "open")),
    ))).scalars().all()
    if not plans:
        return out
    by_ticket = {str(p["ticket"]): p for p in positions}
    changed = False
    for plan in plans:
        pos = by_ticket.get(str(plan.ticket))
        if pos is None:
            flipped = False
            news = None
            if plan.status == "pending":
                from app.signals.news_gate import live_block

                news = await live_block(plan.symbol)
            if plan.status == "pending" and plan.timeframe and news is None:
                v = await _oblivion(plan.symbol, plan.timeframe)
                flipped = bool(v.get("confirmed") and v.get("action") not in (None, "wait", plan.side))
            if news is not None:
                flipped = True
            if plan.status == "pending" and (flipped or (plan.expires_at and now_sast() > plan.expires_at)):
                if send:
                    try:
                        await mt5_client.cancel_order(**creds, ticket=int(plan.ticket))
                    except Exception as exc:  # noqa: BLE001 — may already have been filled or removed
                        logger.debug(f"[TRADE MANAGER] cancel #{plan.ticket}: {exc}")
                plan.status = "cancelled"
                changed = True
                await _record(plan.symbol, "cancel",
                              (news["reason"] + " — resting entry cancelled") if news is not None
                              else "OBLIVION confirmed the opposite signal — pullback limit cancelled" if flipped
                              else "pullback limit expired unfilled — the setup has moved on",
                              {"venue": "mt5", "account_id": account.id, "ticket": plan.ticket, "sent": send})
            elif plan.status == "open":
                plan.status = "closed"   # stopped or at TP3 on the broker
                changed = True
            continue
        out["tickets"].add(pos["ticket"])
        if plan.status == "pending":
            plan.status = "open"
            changed = True
        key = pos["key"]
        _sample(key, pos["price"])
        if not _cooled(key):
            continue
        pdict = {"side": plan.side, "stage": plan.stage, "tp1": plan.tp1, "tp2": plan.tp2,
                 "tp1_close": plan.tp1_close, "tp2_close": plan.tp2_close,
                 "trail_atr": plan.trail_atr, "trail_from": plan.trail_from or 1,
                 "floor_at_tp1": plan.floor_at_tp1}
        step = ladder_step(pdict, pos)
        if step is None and plan.trail_atr and plan.stage >= max(1, plan.trail_from or 1) and plan.timeframe:
            close, atr = await _closed_bar(pos["symbol"], plan.timeframe)
            new_stop = trail_stop(pdict, pos["sl"], close, atr) if close else None
            if new_stop is not None:
                _last_action[key] = time.time()
                if send:
                    await mt5_client.modify_order(**creds, ticket=pos["ticket"], sl=round(float(new_stop), 5), tp=pos["tp"])
                detail = {"venue": "mt5", "account_id": account.id, "ticket": pos["ticket"],
                          "action": "trail", "old_stop": pos["sl"], "new_stop": new_stop, "sent": send}
                await _record(pos["symbol"], "trail",
                              f"trailing stop → {new_stop:.5g} ({plan.trail_atr} ATR behind the {plan.timeframe} close)", detail)
                out["acts"].append(detail)
                continue
        if step is None:
            # A winning trade is closed when OBLIVION confirms the other way on its timeframe.
            in_profit = (pos["price"] > pos["entry"]) if plan.side == "buy" else (pos["price"] < pos["entry"])
            if in_profit and plan.timeframe and time.time() - _last_shift_check.get(key, 0) >= SHIFT_CHECK_S:
                _last_shift_check[key] = time.time()
                v = await _oblivion(pos["symbol"], plan.timeframe)
                opposite = "sell" if plan.side == "buy" else "buy"
                if v.get("confirmed") and v.get("action") == opposite:
                    _last_action[key] = time.time()
                    if send:
                        await mt5_client.close_position(**creds, ticket=pos["ticket"])
                    plan.status = "closed"
                    changed = True
                    detail = {"venue": "mt5", "account_id": account.id, "ticket": pos["ticket"], "sent": send}
                    await _record(pos["symbol"], "close",
                                  f"OBLIVION confirmed {opposite.upper()} on {plan.timeframe} — banking the winning trade", detail)
                    out["acts"].append(detail)
            continue
        _last_action[key] = time.time()
        cut = step["close"]
        if send and cut >= 0.01 and pos["volume"] - cut >= 0.01:
            await mt5_client.close_position(**creds, ticket=pos["ticket"], volume=cut)
        new_stop = step["stop"]
        better = new_stop is not None and (pos["sl"] is None or (
            new_stop > pos["sl"] if plan.side == "buy" else new_stop < pos["sl"]))
        if send and better:
            await mt5_client.modify_order(**creds, ticket=pos["ticket"], sl=round(float(new_stop), 5), tp=pos["tp"])
        plan.stage = step["stage"]
        changed = True
        detail = {"venue": "mt5", "account_id": account.id, "ticket": pos["ticket"], "rung": step["why"],
                  "closed_volume": cut, "new_stop": new_stop if better else None, "sent": send}
        await _record(pos["symbol"], f"ladder_{step['why'].lower()}",
                      f"{step['why']} reached — " + (f"banked {cut} lots, " if cut else "")
                      + f"stop → {new_stop}" + (" (TP1 locked, trailing from here)" if plan.trail_atr else ""), detail)
        out["acts"].append(detail)
    if changed:
        await db.commit()
    return out


# ── Crypto ──────────────────────────────────────────────────────────────────


async def manage_crypto_account(db: Any, account: Any, s: Any, *, send: bool) -> List[Dict[str, Any]]:
    """Protective stops, drawdown cap and take-profit-on-shift for one exchange account."""
    from sqlalchemy import select

    from app.agents import guardian
    from app.models.database import Trade
    from app.services.crypto_accounts import connector_for
    from app.trading.trailing import protective_stop

    acct_key = f"crypto:{account.id}"
    trades = (await db.execute(select(Trade).where(
        Trade.exchange == "bitget", Trade.status == "open", Trade.trade_side == "open",
        Trade.crypto_account_id == account.id,
    ))).scalars().all()
    if not trades:
        _entries_blocked.pop(acct_key, None)
        return []
    connector = connector_for(account)
    live = await connector.get_futures_positions()
    by_symbol = {str(p.get("symbol") or "").upper().replace("/", ""): p
                 for p in (live or []) if guardian._position_size(p) > 0}
    balances = await connector.get_futures_balance()
    equity = sum(float((b or {}).get("accountEquity") or (b or {}).get("usdtEquity") or 0)
                 for b in (balances if isinstance(balances, list) else [balances]))
    acts: List[Dict[str, Any]] = []
    positions = []
    for t in trades:
        lp = by_symbol.get(str(t.symbol or "").upper().replace("/", "").replace(":USDT", ""))
        if lp is None:
            continue
        positions.append({"key": f"{acct_key}:{t.id}", "trade": t, "live": lp,
                          "profit": float(lp.get("unrealizedPL") or 0), "volume": guardian._position_size(lp)})
    plan = margin_plan(level=None, balance=equity, positions=positions, warn_pct=0, critical_pct=0,
                       max_drawdown_pct=float(getattr(s, "max_account_drawdown_pct", 5) or 5))
    _status["accounts"][acct_key] = {"equity": equity, "positions": len(positions),
                                     "margin_action": plan["action"], "at": time.time()}
    if plan["action"] == "flatten":
        _entries_blocked[acct_key] = plan["reason"]
        for p in positions:
            hold = str(p["live"].get("holdSide") or ("long" if p["trade"].side == "buy" else "short"))
            if send:
                await guardian._close_crypto(connector, str(p["live"].get("symbol")), hold, p["volume"])
            detail = {"venue": "crypto", "account_id": account.id, "trade_id": p["trade"].id, "sent": send}
            await _record(p["trade"].symbol, "close", plan["reason"], detail)
            acts.append(detail)
        return acts
    _entries_blocked.pop(acct_key, None)

    for p in positions:
        t, lp, key = p["trade"], p["live"], p["key"]
        entry = float(t.average_price or t.price or 0)
        price = float(lp.get("markPrice") or lp.get("marketPrice") or 0)
        if entry <= 0 or price <= 0 or not _cooled(key):
            continue
        hold = str(lp.get("holdSide") or ("long" if t.side == "buy" else "short"))
        raw_symbol = str(lp.get("symbol") or t.symbol)
        samples = _sample(key, price)
        atr = await _atr(t.symbol)
        sudden = adverse_move(samples, is_long=t.side == "buy", atr=atr,
                              mult=float(getattr(s, "shift_atr_mult", 1.5) or 1.5))
        now = time.time()
        if sudden or now - _last_shift_check.get(key, 0) >= SHIFT_CHECK_S:
            _last_shift_check[key] = now
            shift = await market_shift(t.symbol, t.side)
            size = p["volume"]
            plan_s = shift_plan(side=t.side, entry=entry, price=price, stop=t.stop_loss, volume=size,
                                sudden_move=sudden, already_partial=key in _partial_taken,
                                fraction=float(getattr(s, "shift_take_profit_fraction", 0.5) or 0.5), **shift)
            # Exchange sizes are not lots — split by fraction directly.
            if plan_s["action"] == "take_partial":
                plan_s["volume"] = size * float(getattr(s, "shift_take_profit_fraction", 0.5) or 0.5)
            if plan_s["action"] != "hold":
                _last_action[key] = now
                if send and plan_s["action"] in ("take_partial", "close"):
                    await guardian._close_crypto(connector, raw_symbol, hold,
                                                 size if plan_s["action"] == "close" else plan_s["volume"])
                if plan_s["action"] == "take_partial":
                    _partial_taken.add(key)
                if send and plan_s.get("new_stop") is not None and plan_s["action"] != "close":
                    await connector.place_tpsl_order(symbol=raw_symbol, margin_coin="USDT", plan_type="pos_loss",
                                                     trigger_price=float(plan_s["new_stop"]), hold_side=hold)
                    t.stop_loss = float(plan_s["new_stop"])
                    await db.commit()
                detail = {"venue": "crypto", "account_id": account.id, "trade_id": t.id,
                          "action": plan_s["action"], "volume": plan_s["volume"], "sent": send,
                          **shift, "sudden_move": sudden}
                await _record(t.symbol, plan_s["action"], plan_s["reason"], detail)
                acts.append(detail)
                continue
        new_sl = protective_stop(side=t.side, entry=entry, current_price=price,
                                 take_profit=t.take_profit, current_sl=t.stop_loss)
        if new_sl is not None:
            _last_action[key] = now
            if send:
                await connector.place_tpsl_order(symbol=raw_symbol, margin_coin="USDT", plan_type="pos_loss",
                                                 trigger_price=float(new_sl), hold_side=hold)
                t.stop_loss = float(new_sl)
                await db.commit()
            detail = {"venue": "crypto", "account_id": account.id, "trade_id": t.id,
                      "action": "trail", "new_stop": new_sl, "sent": send}
            await _record(t.symbol, "trail", f"stop advanced → {new_sl:.6g}", detail)
            acts.append(detail)
    return acts


# ── The loop ────────────────────────────────────────────────────────────────


async def manage_cycle() -> Dict[str, Any]:
    """One pass over every account the room trades."""
    from app.agents.execution import get_settings, mt5_targets
    from app.core.database import AsyncSessionLocal
    from app.services.crypto_accounts import crypto_targets

    summary: Dict[str, Any] = {"actions": [], "errors": []}
    async with AsyncSessionLocal() as db:
        s = await get_settings(db)
        # Actions reach the broker only while execution is on — and never from a test run.
        send = bool(s.execution_enabled) and not _under_test()
        try:
            mt5_accounts = (await mt5_targets(db, s))["targets"]
        except Exception as exc:  # noqa: BLE001 — plugin-optional
            mt5_accounts = []
            summary["errors"].append(f"mt5 routing: {exc}")
        for account in mt5_accounts:
            try:
                summary["actions"] += await manage_mt5_account(db, account, s, send=send)
            except Exception as exc:  # noqa: BLE001 — one account, not all
                summary["errors"].append(f"mt5 {account.id}: {str(exc)[:160]}")
        try:
            crypto = (await crypto_targets(db, s))["targets"]
        except Exception as exc:  # noqa: BLE001
            crypto = []
            summary["errors"].append(f"crypto routing: {exc}")
        for account in crypto:
            try:
                summary["actions"] += await manage_crypto_account(db, account, s, send=send)
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"crypto {account.id}: {str(exc)[:160]}")
    _status["last_cycle"] = time.time()
    _status["errors"] = summary["errors"][-10:]
    return summary


async def run_forever(is_running) -> None:
    """The fast management loop; ``is_running`` is the room worker's liveness check."""
    logger.info("💹 [TRADE MANAGER] Real-time management active")
    while is_running():
        tick = 5
        try:
            from app.agents.execution import get_settings
            from app.core.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                tick = int(getattr(await get_settings(db), "manage_tick_s", 5) or 5)
            summary = await manage_cycle()
            if summary["actions"]:
                logger.info(f"💹 [TRADE MANAGER] {len(summary['actions'])} action(s)")
            for err in summary["errors"]:
                logger.debug(f"💹 [TRADE MANAGER] {err}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bad pass must not end the watch
            logger.warning(f"💹 [TRADE MANAGER] cycle failed: {exc}")
        await asyncio.sleep(max(3, min(60, tick)))
