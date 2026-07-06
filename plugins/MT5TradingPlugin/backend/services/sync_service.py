"""
MT5 Account Sync Service — maps real mtapi-io field names to DB models.

mtapi-io OpenedOrders item (market position type 0/1):
  ticket, symbol, type (int), lots, openPrice, currentPrice,
  stopLoss, takeProfit, profit, commission, swap, comment, openTime

mtapi-io OpenedOrders item (pending order type 2-7):
  ticket, symbol, type (int), lots, openPrice, stopLoss, takeProfit, comment

mtapi-io OrderHistory item (closed deal):
  ticket, order, positionId, symbol, type (int), lots,
  closePrice, profit, commission, swap, closeTime, comment
"""
from typing import Optional
from datetime import datetime
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from plugins.MT5TradingPlugin.backend.models import (
    MT5Account, MT5Order, MT5Position, MT5Deal,
    MT5AccountStatus, MT5OrderStatus, MT5OrderType, MT5PositionSide, MT5DealType,
)
from plugins.MT5TradingPlugin.backend.services.mt5_client import (
    mt5_client, MT5ClientError, MT5_MARKET_TYPES, MT5_POSITION_SIDE, MT5_PENDING_TYPE,
    normalize_order_type, is_pending_order,
)


def _parse_dt(val) -> Optional[datetime]:
    """Convert mtapi-io time (ISO string or unix seconds) to naive UTC datetime."""
    if val is None:
        return None
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    if isinstance(val, (int, float)):
        ts = val / 1000 if val > 1e10 else val
        return datetime.utcfromtimestamp(ts)
    return None


class MT5SyncService:

    @staticmethod
    async def sync_account(db: AsyncSession, account: MT5Account) -> bool:
        """Pull latest state from mtapi-io and update DB cache."""
        try:
            password = account.password_encrypted

            # ── 1. Account info ────────────────────────────────────────────
            info = await mt5_client.get_account_info(account.login, account.server, password)
            account.balance      = info["balance"]
            account.equity       = info["equity"]
            account.margin       = info["margin"]
            account.free_margin  = info["freeMargin"]
            account.margin_level = info.get("marginLevel")
            account.floating_pnl = info["profit"]
            account.leverage     = info["leverage"]
            account.currency     = info["currency"]
            account.status       = MT5AccountStatus.ACTIVE
            account.api_reachable = True
            account.last_sync_at = datetime.utcnow()

            # ── 2. Pending orders ──────────────────────────────────────────
            all_open  = await mt5_client.get_orders(account.login, account.server, password)
            pending   = [o for o in all_open if is_pending_order(o)]
            positions = [o for o in all_open if not is_pending_order(o)]

            await db.execute(delete(MT5Order).where(MT5Order.account_id == account.id))
            for o in pending:
                type_str = normalize_order_type(o) or "buy_limit"
                try:
                    ot = MT5OrderType(type_str)
                except ValueError:
                    ot = MT5OrderType.BUY_LIMIT
                db.add(MT5Order(
                    account_id=account.id,
                    mt5_ticket=int(o.get("ticket", 0)),
                    symbol=str(o.get("symbol", "")),
                    order_type=ot,
                    volume=float(o.get("lots", o.get("volume", 0))),
                    price=float(o.get("openPrice", o.get("price", 0))),
                    sl=float(o["stopLoss"])   if o.get("stopLoss")   else None,
                    tp=float(o["takeProfit"]) if o.get("takeProfit") else None,
                    status=MT5OrderStatus.PENDING,
                    comment=o.get("comment"),
                    mt5_time_setup=_parse_dt(o.get("openTime")),
                    raw_data=o,
                ))

            # ── 3. Market positions ────────────────────────────────────────
            await db.execute(delete(MT5Position).where(MT5Position.account_id == account.id))
            for p in positions:
                side_str = normalize_order_type(p) or "buy"
                if side_str not in ("buy", "sell"):
                    side_str = "buy"
                try:
                    side = MT5PositionSide(side_str)
                except ValueError:
                    side = MT5PositionSide.BUY
                db.add(MT5Position(
                    account_id=account.id,
                    mt5_ticket=int(p.get("ticket", 0)),
                    symbol=str(p.get("symbol", "")),
                    side=side,
                    volume=float(p.get("lots", p.get("volume", 0))),
                    price_open=float(p.get("openPrice",    0)),
                    price_current=float(p.get("currentPrice", p.get("openPrice", 0))),
                    sl=float(p["stopLoss"])   if p.get("stopLoss")   else None,
                    tp=float(p["takeProfit"]) if p.get("takeProfit") else None,
                    swap=float(p.get("swap",       0)),
                    profit=float(p.get("profit",   0)),
                    commission=float(p.get("commission", 0)),
                    comment=p.get("comment"),
                    mt5_time_open=_parse_dt(p.get("openTime")),
                    raw_data=p,
                ))

            await db.commit()
            logger.debug(
                f"[MT5Sync] {account.login}: balance={account.balance:.2f} "
                f"{len(pending)} pending {len(positions)} positions"
            )

            # Also refresh closed-trade history (Trade History). Best-effort and
            # upsert-only, so a history hiccup never fails the main sync. Without
            # this the deals table stays empty and Trade History shows nothing,
            # because the view flow only calls sync_account (not sync_deals).
            # Use a 30-day window — large enough to recover from multi-day gaps,
            # fast enough to avoid broker-API timeouts that the 90-day window hit.
            try:
                thirty_days_ago = datetime.utcnow() - timedelta(days=30)
                await MT5SyncService.sync_deals(db, account, date_from=thirty_days_ago)
            except Exception as e:  # noqa: BLE001 - never block the primary sync
                logger.warning(f"[MT5Sync] {account.login} deal-history sync skipped: {e}")

            return True

        except MT5ClientError as e:
            account.api_reachable = False
            account.status = MT5AccountStatus.ERROR
            await db.commit()
            logger.warning(f"[MT5Sync] {account.login} failed: {e}")
            return False
        except Exception as e:
            logger.error(f"[MT5Sync] {account.login} unexpected: {e}")
            return False

    @staticmethod
    async def sync_deals(
        db: AsyncSession, account: MT5Account,
        date_from=None, date_to=None,
        force_today: bool = False,
    ) -> int:
        """Upsert deal history from OrderHistory (never delete old records).

        ``force_today=True`` fetches only the last 24 hours and updates deals
        even if they already exist (used by the on-load auto-sync to recover
        deals that landed in the DB with a zero/null timestamp).
        """
        try:
            password = account.password_encrypted
            if force_today:
                date_from = datetime.utcnow() - timedelta(hours=24)
                date_to   = datetime.utcnow()
            deals_data = await mt5_client.get_deals(
                account.login, account.server, password, date_from, date_to
            )
            existing = await db.execute(
                select(MT5Deal.mt5_ticket).where(MT5Deal.account_id == account.id)
            )
            seen = {row[0] for row in existing.fetchall()}

            new_count = 0
            for d in deals_data:
                ticket = int(d.get("ticket", 0))
                # Skip deals with no ticket — they cannot be uniquely identified
                if ticket == 0:
                    continue
                if ticket in seen and not force_today:
                    continue

                # Map type int → MT5DealType
                t = d.get("type", 0)
                if t == 0:
                    dtype = MT5DealType.BUY
                elif t == 1:
                    dtype = MT5DealType.SELL
                elif t == 2:
                    dtype = MT5DealType.BALANCE
                elif t == 3:
                    dtype = MT5DealType.CREDIT
                elif t == 4:
                    dtype = MT5DealType.COMMISSION
                else:
                    dtype = MT5DealType.BUY  # fallback

                # Time from closeTime (closed orders) or time field
                mt5_time = _parse_dt(
                    d.get("closeTime") or d.get("time") or d.get("openTime")
                )

                db.add(MT5Deal(
                    account_id=account.id,
                    mt5_ticket=ticket,
                    mt5_order_ticket=d.get("order"),
                    mt5_position_ticket=d.get("positionId"),
                    symbol=d.get("symbol"),
                    deal_type=dtype,
                    volume=float(d["lots"])  if d.get("lots")  else (
                           float(d["volume"]) if d.get("volume") else None),
                    price=float(d.get("closePrice") or d.get("price") or 0) or None,
                    profit=float(d.get("profit", 0)),
                    commission=float(d.get("commission", 0)),
                    swap=float(d.get("swap", 0)),
                    fee=float(d.get("fee", 0)),
                    comment=d.get("comment"),
                    mt5_time=mt5_time,
                    raw_data=d,
                ))
                new_count += 1

            if new_count:
                await db.commit()
            logger.debug(f"[MT5Sync] {account.login}: {new_count} new deals")
            return new_count

        except Exception as e:
            logger.error(f"[MT5Sync] deals {account.login}: {e}")
            return 0

    @staticmethod
    async def sync_all_accounts(db: AsyncSession, user_id=None):
        from sqlalchemy import select
        from plugins.MT5TradingPlugin.backend.models import MT5Account as _A
        q = select(_A)
        if user_id:
            q = q.where(_A.user_id == user_id)
        result = await db.execute(q)
        for account in result.scalars().all():
            await MT5SyncService.sync_account(db, account)
