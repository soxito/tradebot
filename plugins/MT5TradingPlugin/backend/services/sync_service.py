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
from datetime import datetime, timedelta
from sqlalchemy import select, delete, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert
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


async def _store_trade_deals_to_brain(account: "MT5Account", deals: list) -> None:
    """
    Store newly synced closed trade deals to the Jarvis knowledge brain.

    For each trade deal:
    1. Fetches economic events / news that were happening on the trade date
    2. Composes a rich lesson (what happened, why, news context)
    3. Writes to Obsidian vault (jarvis-learn endpoint)
    4. Writes to AI knowledge base for future AI gate decisions

    This drives the self-improvement loop — Jarvis learns from EVERY real
    broker-confirmed trade outcome, not just scalp bot trades.
    """
    import os as _os
    import json as _json
    base = "http://127.0.0.1:{}".format(_os.environ.get("BACKEND_PORT", "1448"))

    async with __import__("httpx").AsyncClient(timeout=15.0) as client:
        for deal in deals:
            try:
                symbol  = deal["symbol"]
                side    = deal["side"]
                price   = deal["price"]
                pnl     = deal["profit"]
                volume  = deal["volume"]
                ticket  = deal["ticket"]
                mt5_time = deal.get("mt5_time")
                outcome = "WIN" if pnl >= 0 else "LOSS"
                pnl_str = f"{pnl:+.2f}"
                trade_date = mt5_time.strftime("%Y-%m-%d") if mt5_time else "unknown"

                # ── Fetch economic events around the trade time ───────────────
                eco_context = ""
                try:
                    r = await client.get(
                        f"{base}/api/v1/sentiment/economic-calendar",
                        params={"date": trade_date, "currency": symbol[:3] if symbol else ""},
                        timeout=5.0,
                    )
                    if r.status_code == 200:
                        events = r.json() if isinstance(r.json(), list) else []
                        if events:
                            eco_context = "\nEconomic events on trade day: " + ", ".join(
                                f"{e.get('title','')} ({e.get('impact','?')} impact)"
                                for e in events[:4]
                            )
                except Exception:
                    pass

                # ── Fetch sentiment/news for this symbol on trade day ─────────
                news_context = ""
                try:
                    r = await client.get(
                        f"{base}/api/v1/sentiment/",
                        params={"symbol": symbol, "limit": 3},
                        timeout=5.0,
                    )
                    if r.status_code == 200:
                        news = r.json() if isinstance(r.json(), list) else []
                        if news:
                            news_context = "\nRecent sentiment: " + "; ".join(
                                f"{n.get('label','?')} ({n.get('score',0):+.2f})"
                                for n in news[:3] if n.get("symbol","").upper() == symbol.upper()
                            )
                except Exception:
                    pass

                # ── Compose trade lesson ──────────────────────────────────────
                account_label = f"LIVE" if getattr(account, "account_type", "") == "live" else "DEMO"
                if pnl >= 0:
                    lesson = (
                        f"✅ {account_label} TRADE WON {pnl_str} | {symbol} {side.upper()} "
                        f"@{price:.5g} vol={volume} ticket={ticket} date={trade_date}."
                        f"{eco_context}{news_context} "
                        f"Analysis: This {side} trade on {symbol} was profitable. "
                        f"Study the market structure and news alignment on {trade_date} "
                        f"for future similar setups."
                    )
                else:
                    lesson = (
                        f"❌ {account_label} TRADE LOST {pnl_str} | {symbol} {side.upper()} "
                        f"@{price:.5g} vol={volume} ticket={ticket} date={trade_date}."
                        f"{eco_context}{news_context} "
                        f"Analysis: This {side} trade on {symbol} lost. "
                        f"Review what opposing signals were present on {trade_date} "
                        f"to avoid similar setups."
                    )

                # ── Store to Obsidian vault (jarvis-learn) ────────────────────
                await client.post(
                    f"{base}/api/v1/plugins/obsidian-knowledge/jarvis-learn",
                    json={
                        "question": f"What happened on the {symbol} {side} trade on {trade_date}?",
                        "answer": lesson[:1500],
                        "tags": [
                            "trade-outcome", symbol.lower(), side, outcome.lower(),
                            account_label.lower(), trade_date,
                        ],
                    },
                )

                # ── Store to AI knowledge base ────────────────────────────────
                await client.post(
                    f"{base}/api/v1/plugins/ai-analyst/ai/knowledge",
                    json={
                        "title": f"Broker {outcome}: {symbol} {side.upper()} {pnl_str} on {trade_date} #{ticket}",
                        "content": lesson[:1500],
                        "kind": "broker_trade_outcome",
                        "symbol": symbol,
                        "weight": 2.5 if abs(pnl) > 5.0 else 1.5,
                        "source": "broker_sync",
                        "agent_role": "trade_journal",
                    },
                )

                logger.info(
                    f"[MT5Sync] 🧠 Trade #{ticket} {symbol} {side.upper()} {pnl_str} "
                    f"→ brain stored ({account_label} {trade_date})"
                )

            except Exception as exc:
                logger.debug(f"[MT5Sync] brain store deal #{deal.get('ticket')}: {exc}")


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
        days_back: int = 30,
    ) -> int:
        """Upsert deal history from OrderHistory (never delete old records).

        ``force_today=True`` now fetches the last ``days_back`` days (default 30)
        and upserts existing records — provides a full month of trade history on
        every sync.  Pass ``days_back`` to customise the window.

        ``date_from`` / ``date_to`` override the automatic window.
        """
        try:
            password = account.password_encrypted
            if force_today or (date_from is None and date_to is None):
                # Default to last 30 days so the user always sees full month history
                date_from = datetime.utcnow() - timedelta(days=days_back)
                date_to   = datetime.utcnow()
            deals_data = await mt5_client.get_deals(
                account.login, account.server, password, date_from, date_to
            )

            # Build rows to upsert. We resolve all values in Python first so the
            # INSERT ... ON CONFLICT DO NOTHING is a single atomic operation per row —
            # this is the only race-condition-safe approach when multiple async tasks
            # (sync_account + background scheduler) may call sync_deals concurrently.
            rows_to_upsert = []
            new_trade_deals_map: dict = {}  # ticket → deal info (only new ones)

            for d in deals_data:
                ticket = int(d.get("ticket", 0))
                if ticket == 0:
                    continue  # can't uniquely identify without a ticket

                # Map type int → MT5DealType string
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

                mt5_time = _parse_dt(
                    d.get("closeTime") or d.get("time") or d.get("openTime")
                )
                volume = float(d["lots"]) if d.get("lots") else (
                         float(d["volume"]) if d.get("volume") else None)
                price  = float(d.get("closePrice") or d.get("price") or 0) or None
                pnl    = float(d.get("profit", 0))
                symbol = d.get("symbol") or ""

                rows_to_upsert.append({
                    "account_id":          account.id,
                    "mt5_ticket":          ticket,
                    "mt5_order_ticket":    d.get("order"),
                    "mt5_position_ticket": d.get("positionId"),
                    "symbol":              symbol or None,
                    "deal_type":           dtype.value,
                    "volume":              volume,
                    "price":               price,
                    "profit":              pnl,
                    "commission":          float(d.get("commission", 0)),
                    "swap":                float(d.get("swap", 0)),
                    "fee":                 float(d.get("fee", 0)),
                    "comment":             d.get("comment"),
                    "mt5_time":            mt5_time,
                    "raw_data":            d,
                    "created_at":          datetime.utcnow(),
                    # For brain tracking
                    "_dtype":              dtype,
                    "_pnl":                pnl,
                    "_mt5_time":           mt5_time,
                })

            if not rows_to_upsert:
                return 0

            # Fetch existing tickets once for brain-tracking (only new deals need brain store)
            existing_res = await db.execute(
                select(MT5Deal.mt5_ticket).where(MT5Deal.account_id == account.id)
            )
            existing_tickets = {row[0] for row in existing_res.fetchall()}

            new_count = 0
            for row in rows_to_upsert:
                # Pop the helper fields before inserting
                dtype   = row.pop("_dtype")
                pnl     = row.pop("_pnl")
                mt5_t   = row.pop("_mt5_time")
                ticket  = row["mt5_ticket"]
                symbol  = row.get("symbol") or ""

                # INSERT ... ON CONFLICT (account_id, mt5_ticket) DO NOTHING
                # This is atomic and safe against concurrent sync calls.
                stmt = (
                    pg_insert(MT5Deal)
                    .values(**row)
                    .on_conflict_do_nothing(
                        index_elements=["account_id", "mt5_ticket"]
                    )
                )
                result = await db.execute(stmt)
                inserted = result.rowcount  # 1 if inserted, 0 if conflict skipped

                if inserted:
                    new_count += 1
                    # Track new BUY/SELL deals with non-zero profit for brain
                    if dtype in (MT5DealType.BUY, MT5DealType.SELL) and pnl != 0.0 and symbol:
                        new_trade_deals_map[ticket] = {
                            "ticket":   ticket,
                            "symbol":   symbol,
                            "side":     "buy" if dtype == MT5DealType.BUY else "sell",
                            "price":    row.get("price") or 0,
                            "volume":   row.get("volume") or 0,
                            "profit":   pnl,
                            "mt5_time": mt5_t,
                        }
                elif force_today and mt5_t is not None:
                    # On force re-sync: patch null/zero timestamps on existing rows
                    await db.execute(
                        text(
                            "UPDATE mt5_deals SET mt5_time=:ts, profit=:pnl "
                            "WHERE account_id=:aid AND mt5_ticket=:tk "
                            "  AND (mt5_time IS NULL OR profit = 0)"
                        ),
                        {"ts": mt5_t, "pnl": pnl, "aid": account.id, "tk": ticket},
                    )

            await db.commit()
            logger.debug(f"[MT5Sync] {account.login}: {new_count} new deals inserted")

            # Store new closed trades to knowledge brain (fire-and-forget)
            if new_trade_deals_map:
                import asyncio
                asyncio.ensure_future(
                    _store_trade_deals_to_brain(account, list(new_trade_deals_map.values()))
                )

            return new_count

        except Exception as e:
            err_str = str(e)
            # URL/network errors are expected when MT5 bridge is not running;
            # log at WARNING (not ERROR) so they don't flood the error log.
            _expected = ("missing an 'http://'", "Request URL is missing", "unreachable",
                         "connection refused", "suppressed", "No scheme")
            if any(p in err_str for p in _expected):
                logger.warning(f"[MT5Sync] deals {account.login}: {err_str}")
            else:
                logger.error(f"[MT5Sync] deals {account.login}: {err_str}")
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
