"""Trading-room accounts, OBLIVION coverage and AI-brain health.

* Every connected MT5 and exchange account, with its live switch. Demo accounts
  are always on and cannot be switched off; each live account has its own
  switch, and the room's "Live trading" master switch (``dry_run``) decides
  whether any live account trades at all.
* Exchange-account CRUD. Secrets are write-only — never returned.
* The OBLIVION ledger: the latest read of every pair the engine has seen.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secrets_box
from app.core.database import get_db
from app.models.database import CryptoAccount, OblivionRead, RoomTradingAccount

router = APIRouter(prefix="/agents/room", tags=["trading-room"])


def _mt5_is_demo(acct) -> bool:
    kind = getattr(acct, "account_type", None)
    return str(getattr(kind, "value", kind) or "").lower() == "demo"


def _crypto_payload(a: CryptoAccount, live_enabled: bool) -> dict:
    demo = str(a.account_type or "").lower() == "demo"
    return {
        "venue": "crypto", "id": a.id, "name": a.name, "exchange": a.exchange,
        "account_type": "demo" if demo else "live", "always_on": demo,
        "live_enabled": True if demo else live_enabled,
        "status": a.status, "status_message": a.status_message,
        "balance": a.balance, "equity": a.equity, "currency": "USDT",
        "last_sync_at": str(a.last_sync_at) if a.last_sync_at else None,
        "has_passphrase": bool(a.passphrase_enc),
    }


async def _toggles(db: AsyncSession, venue: str) -> dict:
    rows = (await db.execute(select(RoomTradingAccount).where(RoomTradingAccount.venue == venue))).scalars().all()
    return {r.account_id: bool(r.live_enabled) for r in rows}


@router.get("/accounts")
async def list_accounts(db: AsyncSession = Depends(get_db)):
    """Every account the room can trade, with its always-on / live switch state."""
    from app.agents.execution import get_settings
    from app.agents import trade_manager

    s = await get_settings(db)
    out = []
    try:
        from plugins.MT5TradingPlugin.backend.models import MT5Account

        toggles = await _toggles(db, "mt5")
        for a in (await db.execute(select(MT5Account).order_by(MT5Account.id))).scalars().all():
            demo = _mt5_is_demo(a) or a.id == s.mt5_demo_account_id
            # The configured live slot with no switch row yet counts as on — that
            # is how execution routes it.
            enabled = toggles.get(a.id, a.id == s.mt5_account_id)
            out.append({
                "venue": "mt5", "id": a.id, "name": a.name, "login": a.login, "server": a.server,
                "account_type": "demo" if demo else "live", "always_on": demo,
                "live_enabled": True if demo else enabled,
                "status": getattr(a.status, "value", a.status), "balance": a.balance,
                "equity": a.equity, "currency": a.currency,
                "margin_block": trade_manager.entries_blocked("mt5", a.id),
            })
    except Exception:  # noqa: BLE001 — MT5 plugin optional
        pass
    toggles = await _toggles(db, "crypto")
    for a in (await db.execute(select(CryptoAccount).order_by(CryptoAccount.id))).scalars().all():
        row = _crypto_payload(a, toggles.get(a.id, False))
        row["margin_block"] = trade_manager.entries_blocked("crypto", a.id)
        out.append(row)
    return {
        "live_trading": not bool(s.dry_run),
        "execution_enabled": bool(s.execution_enabled),
        "accounts": out,
    }


class LiveToggle(BaseModel):
    enabled: bool


@router.post("/accounts/{venue}/{account_id}/live")
async def set_live(venue: Literal["mt5", "crypto"], account_id: int, body: LiveToggle,
                   db: AsyncSession = Depends(get_db)):
    """Switch one live account on or off. Demo accounts are always on."""
    from app.agents.execution import get_settings

    if venue == "mt5":
        from plugins.MT5TradingPlugin.backend.models import MT5Account

        acct = await db.get(MT5Account, account_id)
        if acct is None:
            raise HTTPException(status_code=404, detail="MT5 account not found")
        s = await get_settings(db)
        if _mt5_is_demo(acct) or acct.id == s.mt5_demo_account_id:
            raise HTTPException(status_code=400, detail="demo accounts are always on and cannot be switched")
    else:
        acct = await db.get(CryptoAccount, account_id)
        if acct is None:
            raise HTTPException(status_code=404, detail="exchange account not found")
        if str(acct.account_type or "").lower() == "demo":
            raise HTTPException(status_code=400, detail="demo accounts are always on and cannot be switched")

    row = (await db.execute(select(RoomTradingAccount).where(
        RoomTradingAccount.venue == venue, RoomTradingAccount.account_id == account_id,
    ))).scalar_one_or_none()
    if row is None:
        row = RoomTradingAccount(venue=venue, account_id=account_id)
        db.add(row)
    row.live_enabled = bool(body.enabled)
    await db.commit()

    from app.agents import brain_log

    await brain_log.record_decision(
        agent_name="Operator", agent_role="settings", symbol="*",
        action=f"live_{'on' if body.enabled else 'off'}",
        reasoning=f"{venue} account {account_id} live trading switched {'ON' if body.enabled else 'OFF'}",
        ai_called=False, origin="settings",
    )
    return {"venue": venue, "id": account_id, "live_enabled": row.live_enabled}


# ── Exchange accounts ───────────────────────────────────────────────────────


class CryptoAccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    exchange: Literal["bitget"] = "bitget"
    account_type: Literal["demo", "live"] = "live"
    api_key: str = Field(min_length=4)
    api_secret: str = Field(min_length=4)
    passphrase: Optional[str] = None


class CryptoAccountPatch(BaseModel):
    name: Optional[str] = None
    account_type: Optional[Literal["demo", "live"]] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    passphrase: Optional[str] = None


@router.get("/crypto-accounts")
async def list_crypto_accounts(db: AsyncSession = Depends(get_db)):
    toggles = await _toggles(db, "crypto")
    rows = (await db.execute(select(CryptoAccount).order_by(CryptoAccount.id))).scalars().all()
    return [_crypto_payload(a, toggles.get(a.id, False)) for a in rows]


@router.post("/crypto-accounts")
async def create_crypto_account(body: CryptoAccountIn, db: AsyncSession = Depends(get_db)):
    from app.services.crypto_accounts import test_account

    acct = CryptoAccount(
        name=body.name.strip(), exchange=body.exchange, account_type=body.account_type,
        api_key_enc=secrets_box.encrypt(body.api_key.strip()),
        api_secret_enc=secrets_box.encrypt(body.api_secret.strip()),
        passphrase_enc=secrets_box.encrypt((body.passphrase or "").strip() or None),
        status="unverified",
    )
    db.add(acct)
    await db.flush()
    # A new live account starts switched OFF — going live is always a deliberate click.
    db.add(RoomTradingAccount(venue="crypto", account_id=acct.id, live_enabled=False))
    await db.commit()
    await db.refresh(acct)
    test = await test_account(db, acct)
    return {**_crypto_payload(acct, False), "test": test}


@router.put("/crypto-accounts/{account_id}")
async def update_crypto_account(account_id: int, body: CryptoAccountPatch, db: AsyncSession = Depends(get_db)):
    from app.services.crypto_accounts import forget

    acct = await db.get(CryptoAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="exchange account not found")
    if body.name is not None:
        acct.name = body.name.strip()
    if body.account_type is not None:
        acct.account_type = body.account_type
    if body.api_key:
        acct.api_key_enc = secrets_box.encrypt(body.api_key.strip())
    if body.api_secret:
        acct.api_secret_enc = secrets_box.encrypt(body.api_secret.strip())
    if body.passphrase is not None:
        acct.passphrase_enc = secrets_box.encrypt(body.passphrase.strip() or None)
    acct.status = "unverified"
    await db.commit()
    forget(account_id)
    toggles = await _toggles(db, "crypto")
    return _crypto_payload(acct, toggles.get(acct.id, False))


@router.delete("/crypto-accounts/{account_id}")
async def delete_crypto_account(account_id: int, db: AsyncSession = Depends(get_db)):
    from app.services.crypto_accounts import forget

    acct = await db.get(CryptoAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="exchange account not found")
    for row in (await db.execute(select(RoomTradingAccount).where(
        RoomTradingAccount.venue == "crypto", RoomTradingAccount.account_id == account_id,
    ))).scalars().all():
        await db.delete(row)
    await db.delete(acct)
    await db.commit()
    forget(account_id)
    return {"deleted": account_id}


@router.post("/crypto-accounts/{account_id}/test")
async def test_crypto_account(account_id: int, db: AsyncSession = Depends(get_db)):
    from app.services.crypto_accounts import test_account

    acct = await db.get(CryptoAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="exchange account not found")
    return await test_account(db, acct)


# ── OBLIVION coverage and the AI brain ──────────────────────────────────────


@router.get("/oblivion")
async def oblivion_coverage(db: AsyncSession = Depends(get_db)):
    """The latest OBLIVION read of every pair it has seen, and the scan's state."""
    from app.workers.room_worker import room_worker_status

    latest = (
        select(OblivionRead.symbol, OblivionRead.timeframe, func.max(OblivionRead.id).label("id"))
        .group_by(OblivionRead.symbol, OblivionRead.timeframe).subquery()
    )
    from app.agents import room
    from app.agents.scope import normalise

    focus = {normalise(p) for p in room.get_focus_symbols()}
    stmt = select(OblivionRead).join(latest, OblivionRead.id == latest.c.id).order_by(desc(OblivionRead.analyzed_at))
    if focus:
        stmt = stmt.where(OblivionRead.symbol.in_(focus))
    rows = (await db.execute(stmt)).scalars().all()
    status = room_worker_status()
    return {
        "scan": {
            "running": status.get("oblivion_scan_running"),
            "interval_s": status.get("oblivion_scan_interval_s"),
            "last": status.get("oblivion_last_scan"),
            "timeframe": status.get("focus_timeframe"),
        },
        "reads": [{
            "id": r.id, "symbol": r.symbol, "timeframe": r.timeframe, "source": r.source,
            "analyzed_at": str(r.analyzed_at), "price": r.price,
            "action": r.signal_action, "kind": r.signal_kind, "confirmed": r.signal_confirmed,
            "tradeable": r.tradeable, "score_buy": r.score_buy, "score_sell": r.score_sell,
            "entry": r.entry, "stop_loss": r.stop_loss, "tp1": r.tp1, "tp3": r.tp3,
            "bias": r.market_bias, "news_blocked": r.news_blocked, "reason": r.verdict_reason,
        } for r in rows],
    }


@router.get("/brain-health")
async def brain_health():
    """Is every decision reaching the AI brain? Plus the trade manager's state."""
    from app.agents import brain_log, trade_manager

    return {"brain": brain_log.health(), "trade_manager": trade_manager.status()}


@router.get("/target")
async def weekly_target(db: AsyncSession = Depends(get_db)):
    """This week's target, the room's progress toward it and the pacing multiplier."""
    from app.agents import target_pacer

    return await target_pacer.state(db, fresh=True)


@router.get("/pairs")
async def pair_status(db: AsyncSession = Depends(get_db)):
    """Every pinned pair per room timeframe: listed on an account? approved by its backtest?"""
    from app.agents import pair_gate, room
    from app.signals.oblivion_profiles import ROOM_SCAN_TIMEFRAMES

    return await pair_gate.status(db, await pair_gate.room_symbols(db), list(ROOM_SCAN_TIMEFRAMES))


@router.post("/pairs/rejudge")
async def rejudge_pairs(db: AsyncSession = Depends(get_db)):
    """Re-apply the approval bar to every stored backtest result."""
    from app.agents import pair_gate

    return {"rejudged": await pair_gate.rejudge_all(db)}
