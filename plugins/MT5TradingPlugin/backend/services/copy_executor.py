"""
MT5 Copy-Trading Executor

Position-snapshot diffing copier that drives both modes:

- SIM  (paper ledger): mirrors source positions into mt5_copy_sim_trades,
  auto-closes them when the source position closes.
- LIVE (real execution): opens/closes REAL positions on each follower
  account via the mtapi-io bridge, keeping a copied-ticket map per follower.

The worker loop calls ``sync_profile`` for every enabled copy profile.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.MT5TradingPlugin.backend.models import (
    MT5Account, MT5CopyProfile, MT5CopyFollower, MT5CopySimTrade,
    CopyMode, CopySimStatus,
)
from plugins.MT5TradingPlugin.backend.services.mt5_client import (
    mt5_client, MT5ClientError,
)

COPY_COMMENT_TAG = "copybot"


def _ticket(o) -> int:
    """Ticket id from a position/order payload (handles key casing variants)."""
    if not isinstance(o, dict):
        return 0
    for k in ("ticket", "order", "Ticket", "Order", "positionId", "position"):
        v = o.get(k)
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return 0


def _num(o: dict, *keys: str, default: float = 0.0) -> float:
    for k in keys:
        v = o.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return default


def compute_volume(
    allocation_mode: str, allocation_value: float,
    source_volume: float, equity_base: float,
) -> float:
    """Shared sizing math for sim profiles and live followers."""
    try:
        if allocation_mode == "multiplier":
            vol = max(0.0, source_volume) * float(allocation_value or 1.0)
        elif allocation_mode == "risk_percent":
            risk_amount = equity_base * ((allocation_value or 1.0) / 100.0)
            # Simplified: $10/pip per standard lot → ~1000 units of risk headroom
            vol = max(0.01, risk_amount / 1000.0)
        elif allocation_mode == "fixed_lot":
            vol = float(allocation_value or 0.01)
        else:
            # Unknown mode: fall back to the safest size, one micro lot.
            vol = 0.01
    except Exception:
        vol = 0.01
    return round(min(max(vol, 0.01), 100.0), 2)


class MT5CopyExecutor:
    """Drives one sync pass for a copy profile."""

    @staticmethod
    async def _source_positions(account: MT5Account) -> List[dict]:
        """Live source positions.

        The mtapi-io bridge lumps market positions and pending orders together
        under OpenedOrders (same as ``sync_service.sync_account``), so filter
        pendings out here.
        """
        from plugins.MT5TradingPlugin.backend.services.mt5_client import is_pending_order

        all_open = await mt5_client.get_orders(
            account.login, account.server, account.password_encrypted
        )
        return [o for o in all_open if not is_pending_order(o)]

    @staticmethod
    async def _resolve_source(db: AsyncSession, profile: MT5CopyProfile) -> Optional[MT5Account]:
        if profile.source_account_id is None:
            return None
        return await db.get(MT5Account, profile.source_account_id)

    @staticmethod
    def _allowed(profile: MT5CopyProfile, symbol: str) -> bool:
        wl = profile.symbol_whitelist
        if not wl:
            return True
        sym = (symbol or "").upper()
        return any(sym == s.upper() for s in wl)

    # ── Main entry ────────────────────────────────────────────────────────────

    @staticmethod
    async def sync_profile(db: AsyncSession, profile: MT5CopyProfile) -> dict:
        """One diff-and-mirror pass for an enabled profile."""
        if not profile.enabled:
            return {"skipped": "profile disabled"}

        mode = profile.mode.value if hasattr(profile.mode, "value") else str(profile.mode)

        source = await MT5CopyExecutor._resolve_source(db, profile)
        if source is None:
            return {"skipped": "no source account"}

        try:
            positions = await MT5CopyExecutor._source_positions(source)
        except (MT5ClientError, Exception) as e:  # noqa: BLE001
            logger.warning(f"[CopyBot] profile {profile.id}: source fetch failed: {e}")
            return {"error": str(e)[:200]}

        live_positions = [p for p in positions if _num(p, "lots", "volume") > 0]
        result: dict = {"mode": mode, "source_positions": len(live_positions)}

        if mode == CopyMode.LIVE.value:
            result.update(await MT5CopyExecutor._sync_live(db, profile, live_positions))
        else:
            result.update(await MT5CopyExecutor._sync_sim(db, profile, live_positions))

        return result

    # ── SIM mode ──────────────────────────────────────────────────────────────

    @staticmethod
    async def _sync_sim(db: AsyncSession, profile: MT5CopyProfile, positions: List[dict]) -> dict:
        opened, closed_count = 0, 0

        result = await db.execute(
            select(MT5CopySimTrade).where(
                MT5CopySimTrade.copy_profile_id == profile.id,
                MT5CopySimTrade.status == CopySimStatus.OPEN,
            )
        )
        open_trades = result.scalars().all()

        by_source_ticket: Dict[int, MT5CopySimTrade] = {}
        for t in open_trades:
            st = (t.meta or {}).get("source_ticket")
            try:
                by_source_ticket[int(st)] = t
            except (TypeError, ValueError):
                continue

        source_tickets = {_ticket(p) for p in positions}

        # 1. Track last known price on still-open copies; close those gone from source.
        for ticket, trade in list(by_source_ticket.items()):
            pos = next((p for p in positions if _ticket(p) == ticket), None)
            if pos is not None:
                meta = dict(trade.meta or {})
                last_price = _num(pos, "currentPrice", "openPrice")
                if last_price:
                    meta["last_price"] = last_price
                    trade.meta = meta
                continue

            # Source position closed → close the sim copy at best-known price.
            exit_price = (trade.meta or {}).get("last_price") or trade.entry_price or 0
            await MT5CopySimEngine_close(db, trade.id, exit_price)
            closed_count += 1

        # 2. Open new sim copies for unseen source tickets.
        open_now = len(open_trades) - closed_count
        for pos in positions:
            ticket = _ticket(pos)
            if not ticket or ticket in by_source_ticket:
                continue
            symbol = pos.get("symbol") or ""
            if not MT5CopyExecutor._allowed(profile, symbol):
                continue
            if open_now >= profile.max_open_positions:
                logger.debug(f"[CopyBot] profile {profile.id}: sim max positions reached")
                break

            side = "buy"
            t = pos.get("type")
            if isinstance(t, int):
                side = "buy" if t == 0 else "sell"
            elif isinstance(t, str):
                side = "sell" if "sell" in t.lower() else "buy"

            src_vol = _num(pos, "lots", "volume", default=0.01)
            qty = compute_volume(
                profile.allocation_mode, profile.allocation_value,
                src_vol, profile.paper_equity,
            )
            entry_price = _num(pos, "openPrice", "currentPrice")

            db.add(MT5CopySimTrade(
                copy_profile_id=profile.id,
                source_deal_id=None,
                symbol=symbol,
                side=side,
                qty_sim=qty,
                entry_time=datetime.utcnow(),
                entry_price=entry_price,
                status=CopySimStatus.OPEN,
                meta={
                    "source_ticket": ticket,
                    "source_volume": src_vol,
                    "last_price": _num(pos, "currentPrice", "openPrice") or entry_price,
                    "allocation_mode": profile.allocation_mode,
                },
            ))
            open_now += 1
            opened += 1

        await db.commit()
        return {"sim_opened": opened, "sim_closed": closed_count}

    # ── LIVE mode ─────────────────────────────────────────────────────────────

    @staticmethod
    async def _sync_live(db: AsyncSession, profile: MT5CopyProfile, positions: List[dict]) -> dict:
        result = await db.execute(
            select(MT5CopyFollower).where(MT5CopyFollower.copy_profile_id == profile.id)
        )
        followers = result.scalars().all()
        opened_total, closed_total = 0, 0

        source_by_ticket = {(_ticket(p)): p for p in positions}
        source_tickets = set(source_by_ticket.keys())

        for f in followers:
            f.last_sync_at = datetime.utcnow()
            if not f.enabled:
                continue

            account = await db.get(MT5Account, f.account_id)
            if account is None:
                f.last_error = "follower account missing"
                continue

            creds = (account.login, account.server, account.password_encrypted)
            copied: Dict[str, int] = dict(f.copied_tickets or {})
            changed = False

            # 1. Close copies whose source position is gone.
            for src_key in list(copied.keys()):
                try:
                    src_ticket = int(src_key)
                except ValueError:
                    continue
                if src_ticket in source_tickets:
                    continue
                f_ticket = copied.pop(src_key)
                try:
                    await mt5_client.close_position(*creds, ticket=int(f_ticket))
                    closed_total += 1
                    changed = True
                    f.last_error = None
                    logger.info(
                        f"[CopyBot] profile {profile.id} follower#{f.account_id}: "
                        f"closed mirror #{f_ticket} (source {src_key} gone)"
                    )
                except Exception as e:  # noqa: BLE001
                    f.last_error = f"close {f_ticket}: {str(e)[:300]}"
                    copied[src_key] = f_ticket  # keep mapping to retry next cycle

            # 2. Open copies for new source positions.
            open_count = len(copied)
            for ticket, pos in source_by_ticket.items():
                if str(ticket) in copied:
                    continue
                symbol = pos.get("symbol") or ""
                if not MT5CopyExecutor._allowed(profile, symbol):
                    continue
                if open_count >= f.max_open_positions:
                    break

                side = "buy" if _num(pos, "type") == 0 else "sell"
                if isinstance(pos.get("type"), str):
                    side = "sell" if "sell" in pos["type"].lower() else "buy"

                info = await mt5_client.get_account_info(*creds)
                equity_base = _num(info, "equity", "balance", default=1000.0)
                src_vol = _num(pos, "lots", "volume", default=0.01)
                volume = compute_volume(
                    f.allocation_mode, f.allocation_value, src_vol, equity_base
                )

                comment = f"{COPY_COMMENT_TAG}:{ticket}"
                try:
                    resp = await mt5_client.place_order(
                        login=creds[0], server=creds[1], password=creds[2],
                        symbol=symbol, order_type=side, volume=volume,
                        price=_num(pos, "currentPrice", "openPrice"),
                        sl=_num_or_none(pos, "stopLoss"),
                        tp=_num_or_none(pos, "takeProfit"),
                        comment=comment,
                    )
                    f_ticket = _ticket(resp)
                    if not f_ticket:
                        raise RuntimeError(f"no ticket in response: {str(resp)[:200]}")
                    copied[str(ticket)] = f_ticket
                    open_count += 1
                    opened_total += 1
                    changed = True
                    f.last_error = None
                    logger.info(
                        f"[CopyBot] profile {profile.id} follower#{f.account_id}: "
                        f"copied {side.upper()} {symbol} {volume} lots "
                        f"(src #{ticket} → #{f_ticket})"
                    )
                except Exception as e:  # noqa: BLE001
                    f.last_error = f"open {symbol}: {str(e)[:300]}"
                    logger.warning(
                        f"[CopyBot] profile {profile.id} follower#{f.account_id}: "
                        f"copy failed ({symbol} {side}): {e}"
                    )

            if changed or copied != (f.copied_tickets or {}):
                f.copied_tickets = copied

        await db.commit()
        return {"live_opened": opened_total, "live_closed": closed_total}


def _num_or_none(o: dict, key: str) -> Optional[float]:
    v = o.get(key)
    if v is None:
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


async def MT5CopySimEngine_close(db: AsyncSession, trade_id: int, exit_price: float):
    """Close a sim trade using the existing engine (updates paper balance)."""
    from plugins.MT5TradingPlugin.backend.services.copy_sim_engine import MT5CopySimEngine
    await MT5CopySimEngine.close_sim_trade(db, trade_id, exit_price)


async def run_all_profiles(db: AsyncSession) -> List[dict]:
    """Sync every enabled profile. Called by the background copy worker."""
    result = await db.execute(
        select(MT5CopyProfile).where(MT5CopyProfile.enabled == True)  # noqa: E712
    )
    profiles = result.scalars().all()
    out = []
    for profile in profiles:
        try:
            out.append(await MT5CopyExecutor.sync_profile(db, profile))
        except Exception as e:  # noqa: BLE001
            logger.error(f"[CopyBot] profile {profile.id} sync crashed: {e}")
    return out
