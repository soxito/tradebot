"""Exchange accounts the trading room can trade on — one connector per account.

Only Bitget is wired for room execution (``LiveTradeEngine`` is Bitget-native).
A demo account uses Bitget demo trading (demo API keys + ``paptrading`` header).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import select

from app.core import secrets_box
from app.models.database import CryptoAccount, RoomTradingAccount

SUPPORTED = {"bitget"}

_connectors: Dict[int, Tuple[Any, Any]] = {}


def is_demo(acct: CryptoAccount) -> bool:
    return str(acct.account_type or "").lower() == "demo"


def connector_for(acct: CryptoAccount):
    """A cached connector built from this account's own decrypted keys."""
    from app.exchanges.bitget import BitgetConnector

    if str(acct.exchange or "").lower() not in SUPPORTED:
        raise ValueError(f"{acct.exchange} is not supported for room trading (Bitget only)")
    stamp = acct.updated_at
    cached = _connectors.get(acct.id)
    if cached and cached[0] == stamp:
        return cached[1]
    conn = BitgetConnector(
        api_key=secrets_box.decrypt(acct.api_key_enc) or "",
        api_secret=secrets_box.decrypt(acct.api_secret_enc) or "",
        passphrase=secrets_box.decrypt(acct.passphrase_enc),
        testnet=False,
        demo=is_demo(acct),
    )
    _connectors[acct.id] = (stamp, conn)
    return conn


def forget(account_id: int) -> None:
    _connectors.pop(account_id, None)


async def live_enabled_ids(db, venue: str) -> Dict[int, bool]:
    """``{account_id: live_enabled}`` for every toggle row of this venue."""
    rows = (await db.execute(
        select(RoomTradingAccount).where(RoomTradingAccount.venue == venue)
    )).scalars().all()
    return {r.account_id: bool(r.live_enabled) for r in rows}


async def crypto_targets(db, s) -> Dict[str, Any]:
    """Crypto accounts to trade: every demo always, enabled live ones when armed."""
    try:
        accounts: List[CryptoAccount] = list((await db.execute(select(CryptoAccount))).scalars().all())
        toggles = await live_enabled_ids(db, "crypto")
    except Exception as exc:  # noqa: BLE001 — table missing on an old schema
        logger.debug(f"[crypto-accounts] lookup failed: {exc}")
        accounts, toggles = [], {}
    usable = [a for a in accounts if str(a.exchange or "").lower() in SUPPORTED and a.status != "error"]
    demos = [a for a in usable if is_demo(a)]
    lives = [a for a in usable if not is_demo(a) and toggles.get(a.id)]
    dry = bool(s.dry_run)
    targets = demos + ([] if dry else lives)
    if dry:
        note = "live trading off — demo accounts only"
    else:
        note = f"live trading on — {len(demos)} demo + {len(lives)} live account(s)"
    return {"demos": demos, "lives": lives, "targets": targets, "dry_run": dry,
            "note": note, "has_accounts": bool(accounts)}


async def test_account(db, acct: CryptoAccount) -> Dict[str, Any]:
    """Reach the exchange with this account's keys and store what it reports."""
    try:
        forget(acct.id)
        conn = connector_for(acct)
        balances = await conn.get_futures_balance()
        rows = balances if isinstance(balances, list) else [balances] if balances else []
        equity = 0.0
        available = 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            equity += float(row.get("accountEquity") or row.get("usdtEquity") or row.get("equity") or 0)
            available += float(row.get("available") or row.get("crossedMaxAvailable") or 0)
        acct.status = "active"
        acct.status_message = None
        acct.equity = equity
        acct.balance = available or equity
        acct.last_sync_at = datetime.utcnow()
        await db.commit()
        return {"ok": True, "equity": equity, "available": available}
    except Exception as exc:  # noqa: BLE001
        acct.status = "error"
        acct.status_message = str(exc)[:500]
        await db.commit()
        return {"ok": False, "error": str(exc)[:300]}


async def backfill() -> Dict[str, Any]:
    """One-time carry-over from the single-account settings. Idempotent.

    * the configured live MT5 account keeps its live switch ON (it was already
      the room's live account);
    * the .env Bitget account becomes a connected live account with its switch
      OFF, so nothing starts trading live without someone turning it on;
    * MT5 passwords still stored as plaintext are encrypted.
    """
    import os

    from app.core.config import settings
    from app.core.database import AsyncSessionLocal
    from app.models.database import RoomSettings

    out: Dict[str, Any] = {"mt5_toggle": False, "crypto_seeded": False, "passwords_encrypted": 0}
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return out  # the suite's app lifespan must never rewrite the real database
    async with AsyncSessionLocal() as db:
        s = (await db.execute(select(RoomSettings).where(RoomSettings.id == 1))).scalar_one_or_none()
        live_id = getattr(s, "mt5_account_id", None) if s else None
        if live_id:
            exists = (await db.execute(select(RoomTradingAccount).where(
                RoomTradingAccount.venue == "mt5", RoomTradingAccount.account_id == live_id,
            ))).scalar_one_or_none()
            if exists is None:
                db.add(RoomTradingAccount(venue="mt5", account_id=live_id, live_enabled=True))
                out["mt5_toggle"] = True

        has_crypto = (await db.execute(select(CryptoAccount.id).limit(1))).scalar_one_or_none()
        if has_crypto is None and settings.BITGET_API_KEY and settings.BITGET_API_SECRET:
            acct = CryptoAccount(
                exchange="bitget", name="Bitget (from .env)",
                api_key_enc=secrets_box.encrypt(settings.BITGET_API_KEY),
                api_secret_enc=secrets_box.encrypt(settings.BITGET_API_SECRET),
                passphrase_enc=secrets_box.encrypt(settings.BITGET_PASSPHRASE or None),
                account_type="live", status="unverified",
            )
            db.add(acct)
            await db.flush()
            db.add(RoomTradingAccount(venue="crypto", account_id=acct.id, live_enabled=False))
            out["crypto_seeded"] = True
        await db.commit()
    try:
        out["passwords_encrypted"] = await secrets_box.reencrypt_mt5_passwords()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[accounts] MT5 password encryption skipped: {exc}")
    if any(out.values()):
        logger.info(f"[accounts] backfill: {out}")
    return out
