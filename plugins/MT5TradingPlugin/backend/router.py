"""
MT5 Trading Plugin — API Router

All routes under /api/v1/plugins/mt5/
Mounts as a standalone FastAPI router — discovered by plugin loader.
"""
from typing import Optional, List
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, delete
from loguru import logger

from app.core.database import AsyncSessionLocal
from plugins.MT5TradingPlugin.backend.models import (
    MT5Base, MT5Account, MT5AccountGroup, MT5AccountGroupMember,
    MT5Order, MT5Position, MT5Deal, MT5CopyProfile, MT5CopySimTrade,
    MT5ReplayRun, MT5AccountSnapshot, MT5PluginSetting,
    MT5AccountType, MT5AccountStatus,
)
from plugins.MT5TradingPlugin.backend.schemas import (
    MT5AccountCreate, MT5AccountResponse, MT5AccountUpdate,
    MT5OrderResponse, MT5PlaceOrderRequest, MT5ModifyOrderRequest,
    MT5PositionResponse, MT5DealResponse,
    MT5GroupCreate, MT5GroupResponse, MT5GroupMemberUpdate,
    MT5SnapshotResponse, MT5ReplayRequest, MT5ReplayResponse,
    MT5CopyProfileCreate, MT5CopyProfileResponse, MT5CopySimTradeResponse,
    MT5RiskOverviewResponse, MT5OverlayResponse,
    MT5CandlesResponse, MT5CandleResponse, MT5PriceResponse,
    MT5PlaceMarketOrderRequest, MT5ClosePositionRequest, MT5TradeResultResponse,
    MT5SymbolInfo, MT5EquityPoint,
    MT5SmcAnalyzeResponse, MT5BacktestRequest, MT5BacktestResponse,
    MT5SmcPlaceRequest, MT5SmcAnalyzeDataRequest, MT5BacktestDataRequest,
)
from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client
from plugins.MT5TradingPlugin.backend.services.sync_service import MT5SyncService
from plugins.MT5TradingPlugin.backend.services.aggregation_service import MT5AggregationService
from plugins.MT5TradingPlugin.backend.services.risk_metrics import MT5RiskMetricsService
from plugins.MT5TradingPlugin.backend.services.replay_service import MT5ReplayService
from plugins.MT5TradingPlugin.backend.services.copy_sim_engine import MT5CopySimEngine
from plugins.MT5TradingPlugin.backend.services.smc_strategy import (
    SMCStrategyEngine, smc_engine, candles_from_payload, contract_size_for_symbol,
)
from plugins.MT5TradingPlugin.backend.services.smc_ai import ai_review
from plugins.MT5TradingPlugin.backend.config import mt5_config

router = APIRouter(prefix="/plugins/mt5", tags=["MT5 Trading"])

# Default user_id for single-user mode (no auth system in core yet)
DEFAULT_USER_ID = 1


def _acct(a: MT5Account) -> MT5AccountResponse:
    """Convert MT5Account ORM object to response schema (single place)."""
    return MT5AccountResponse(
        id=a.id, name=a.name, server=a.server, login=a.login,
        status=a.status.value if hasattr(a.status, 'value') else str(a.status),
        account_type=a.account_type.value if hasattr(a.account_type, 'value') else str(a.account_type),
        balance=a.balance, equity=a.equity, margin=a.margin,
        free_margin=a.free_margin, margin_level=a.margin_level,
        floating_pnl=a.floating_pnl, currency=a.currency,
        leverage=a.leverage, api_reachable=a.api_reachable,
        last_sync_at=a.last_sync_at, created_at=a.created_at,
    )


# ── Accounts ───────────────────────────────────────────────

@router.get("/accounts", response_model=List[MT5AccountResponse])
async def list_accounts():
    """List all MT5 accounts for the current user."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(MT5Account).where(MT5Account.user_id == DEFAULT_USER_ID)
        )
        accounts = result.scalars().all()
        return [_acct(a) for a in accounts]


@router.post("/accounts", response_model=MT5AccountResponse)
async def create_account(data: MT5AccountCreate):
    """Add a new MT5 account connection."""
    async with AsyncSessionLocal() as db:
        # Check account limit
        count = await db.execute(
            select(MT5Account).where(MT5Account.user_id == DEFAULT_USER_ID)
        )
        if len(count.scalars().all()) >= mt5_config.max_accounts_per_user:
            raise HTTPException(400, f"Max {mt5_config.max_accounts_per_user} accounts allowed")

        # Test connection before saving
        reachable = await mt5_client.check_connection(data.login, data.server, data.password)

        account = MT5Account(
            user_id=DEFAULT_USER_ID,
            name=data.name,
            server=data.server,
            login=data.login,
            password_encrypted=data.password,  # TODO: encrypt at rest
            account_type=MT5AccountType(data.account_type),
            api_reachable=reachable,
        )
        db.add(account)
        await db.commit()
        await db.refresh(account)

        # Initial sync if reachable
        if reachable:
            await MT5SyncService.sync_account(db, account)

        return _acct(account)


@router.put("/accounts/{account_id}", response_model=MT5AccountResponse)
async def update_account(account_id: int, data: MT5AccountUpdate):
    """Update an MT5 account's connection details, re-test, and re-sync.

    Password is only changed when a non-empty value is supplied. Cached session
    tokens are invalidated so the new credentials take effect immediately.
    """
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, account_id)
        if not account or account.user_id != DEFAULT_USER_ID:
            raise HTTPException(404, "Account not found")

        old_login, old_server = account.login, account.server

        if data.name is not None:
            account.name = data.name
        if data.server is not None:
            account.server = data.server
        if data.login is not None:
            account.login = data.login
        if data.password:  # only change when a new password is supplied
            account.password_encrypted = data.password
        if data.account_type is not None:
            try:
                account.account_type = MT5AccountType(data.account_type)
            except ValueError:
                raise HTTPException(400, "Invalid account_type (live/demo/prop)")

        # Clear cached session tokens (old + new) so new credentials are used.
        try:
            mt5_client._tokens.invalidate(old_login, old_server)
            mt5_client._tokens.invalidate(account.login, account.server)
        except Exception:
            pass

        # Re-test connection with the (possibly) new credentials.
        reachable = await mt5_client.check_connection(
            account.login, account.server, account.password_encrypted
        )
        account.api_reachable = reachable
        await db.commit()
        await db.refresh(account)

        if reachable:
            try:
                await MT5SyncService.sync_account(db, account)
            except Exception as e:
                logger.warning(f"[MT5] post-update sync failed for {account.login}: {e}")

        return _acct(account)


@router.post("/accounts/{account_id}/sync")
async def sync_account(account_id: int):
    """Trigger manual sync for an account."""
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        success = await MT5SyncService.sync_account(db, account)
        return {"success": success, "last_sync": account.last_sync_at}


@router.post("/accounts/{account_id}/test")
async def test_saved_account(account_id: int):
    """
    Test the connection for an already-saved account using its stored credentials.
    Refreshes the session token and returns live account info.
    Updates api_reachable flag in DB.
    """
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, account_id)
        if not account:
            raise HTTPException(404, "Account not found")

        # Force a fresh ConnectEx by invalidating cached token
        mt5_client._tokens.invalidate(account.login, account.server)

        try:
            info = await mt5_client.get_account_info(
                account.login, account.server, account.password_encrypted
            )
            account.api_reachable = True
            account.status = MT5AccountStatus.ACTIVE
            account.balance = info.get("balance", account.balance)
            account.equity = info.get("equity", account.equity)
            account.currency = info.get("currency", account.currency)
            account.leverage = info.get("leverage", account.leverage)
            await db.commit()
            return {
                "reachable":  True,
                "account_id": account_id,
                "balance":    info.get("balance", 0),
                "equity":     info.get("equity", 0),
                "currency":   info.get("currency", "USD"),
                "leverage":   info.get("leverage", 100),
                "company":    info.get("company", ""),
                "server":     account.server,
                "mtapi_url":  mt5_client.base_url,
            }
        except Exception as e:
            account.api_reachable = False
            account.status = MT5AccountStatus.ERROR
            await db.commit()
            return {
                "reachable":  False,
                "account_id": account_id,
                "error":      str(e),
                "mtapi_url":  mt5_client.base_url,
                "hint": (
                    f"mtapi-io at {mt5_client.base_url} is unreachable. "
                    "Run: docker run -d --name mt5rest --restart always "
                    "--platform linux/amd64 -p 8092:80 timurila/mt5rest"
                ),
            }


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: int):
    """Remove an MT5 account connection."""
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        await db.delete(account)
        await db.commit()
        return {"deleted": True}


# ── Orders ─────────────────────────────────────────────────

@router.get("/accounts/{account_id}/orders", response_model=List[MT5OrderResponse])
async def list_orders(account_id: int, symbol: Optional[str] = None):
    """List pending orders for an account."""
    async with AsyncSessionLocal() as db:
        query = select(MT5Order).where(MT5Order.account_id == account_id)
        if symbol:
            query = query.where(MT5Order.symbol == symbol)
        result = await db.execute(query)
        orders = result.scalars().all()
        return [MT5OrderResponse(
            id=o.id, account_id=o.account_id, mt5_ticket=o.mt5_ticket,
            symbol=o.symbol,
            order_type=o.order_type.value if hasattr(o.order_type, 'value') else str(o.order_type),
            volume=o.volume, price=o.price, sl=o.sl, tp=o.tp,
            status=o.status.value if hasattr(o.status, 'value') else str(o.status),
            comment=o.comment, expiration=o.expiration, created_at=o.created_at,
        ) for o in orders]


@router.post("/orders", response_model=dict)
async def place_order(data: MT5PlaceOrderRequest):
    """Place a new pending order via MT5."""
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, data.account_id)
        if not account:
            raise HTTPException(404, "Account not found")

        result = await mt5_client.place_order(
            login=account.login, server=account.server,
            password=account.password_encrypted,
            symbol=data.symbol, order_type=data.order_type,
            volume=data.volume, price=data.price,
            sl=data.sl, tp=data.tp, comment=data.comment,
        )

        # Sync orders after placement
        await MT5SyncService.sync_account(db, account)
        return {"success": True, "mt5_response": result}


@router.put("/orders/{ticket}/modify")
async def modify_order(ticket: int, data: MT5ModifyOrderRequest, account_id: int = Query(...)):
    """Modify a pending order's price, SL, or TP."""
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, account_id)
        if not account:
            raise HTTPException(404, "Account not found")

        result = await mt5_client.modify_order(
            login=account.login, server=account.server,
            password=account.password_encrypted,
            ticket=ticket, price=data.price, sl=data.sl, tp=data.tp,
        )
        await MT5SyncService.sync_account(db, account)
        return {"success": True, "mt5_response": result}


@router.delete("/orders/{ticket}")
async def cancel_order(ticket: int, account_id: int = Query(...)):
    """Cancel a pending order."""
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, account_id)
        if not account:
            raise HTTPException(404, "Account not found")

        result = await mt5_client.cancel_order(
            login=account.login, server=account.server,
            password=account.password_encrypted, ticket=ticket,
        )
        await MT5SyncService.sync_account(db, account)
        return {"success": True, "mt5_response": result}


# ── Positions ──────────────────────────────────────────────

@router.get("/accounts/{account_id}/positions", response_model=List[MT5PositionResponse])
async def list_positions(account_id: int, symbol: Optional[str] = None):
    """List open positions for an account."""
    async with AsyncSessionLocal() as db:
        query = select(MT5Position).where(MT5Position.account_id == account_id)
        if symbol:
            query = query.where(MT5Position.symbol == symbol)
        result = await db.execute(query)
        positions = result.scalars().all()

        items = []
        for p in positions:
            rr = MT5RiskMetricsService.calc_rr(p.price_open, p.sl, p.tp)
            items.append(MT5PositionResponse(
                id=p.id, account_id=p.account_id, mt5_ticket=p.mt5_ticket,
                symbol=p.symbol,
                side=p.side.value if hasattr(p.side, 'value') else str(p.side),
                volume=p.volume, price_open=p.price_open,
                price_current=p.price_current, sl=p.sl, tp=p.tp,
                swap=p.swap, profit=p.profit, commission=p.commission,
                comment=p.comment, mt5_time_open=p.mt5_time_open,
                created_at=p.created_at,
                rr_ratio=rr["rr_ratio"],
                risk_pips=rr["risk_pips"],
                reward_pips=rr["reward_pips"],
            ))
        return items


# ── Deals ──────────────────────────────────────────────────

@router.get("/accounts/{account_id}/deals", response_model=List[MT5DealResponse])
async def list_deals(
    account_id: int, symbol: Optional[str] = None,
    limit: int = Query(50, le=500),
):
    """List recent deals (trade history) for an account."""
    async with AsyncSessionLocal() as db:
        query = (
            select(MT5Deal)
            .where(MT5Deal.account_id == account_id)
            .order_by(MT5Deal.mt5_time.desc())
            .limit(limit)
        )
        if symbol:
            query = query.where(MT5Deal.symbol == symbol)
        result = await db.execute(query)
        deals = result.scalars().all()
        return [MT5DealResponse(
            id=d.id, account_id=d.account_id, mt5_ticket=d.mt5_ticket,
            symbol=d.symbol,
            deal_type=d.deal_type.value if hasattr(d.deal_type, 'value') else str(d.deal_type),
            volume=d.volume, price=d.price, profit=d.profit,
            commission=d.commission, swap=d.swap, mt5_time=d.mt5_time,
        ) for d in deals]


@router.post("/accounts/{account_id}/deals/sync")
async def sync_deals(
    account_id: int,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    """Trigger deal history sync for an account."""
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, account_id)
        if not account:
            raise HTTPException(404, "Account not found")

        df = datetime.fromisoformat(date_from) if date_from else None
        dt = datetime.fromisoformat(date_to) if date_to else None
        count = await MT5SyncService.sync_deals(db, account, df, dt)
        return {"new_deals": count}


# ── Account Groups (Aggregation) ──────────────────────────

@router.get("/groups", response_model=List[MT5GroupResponse])
async def list_groups():
    """List all account groups."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(MT5AccountGroup).where(MT5AccountGroup.user_id == DEFAULT_USER_ID)
        )
        groups = result.scalars().all()
        responses = []
        for g in groups:
            members = await MT5AggregationService.get_group_accounts(db, g.id)
            accounts = [_acct(m["account"]) for m in members]
            total_balance = sum(a.balance for a in accounts)
            total_equity = sum(a.equity for a in accounts)
            responses.append(MT5GroupResponse(
                id=g.id, name=g.name, is_default=g.is_default,
                accounts=accounts,
                total_balance=total_balance, total_equity=total_equity,
                total_floating_pnl=sum(a.floating_pnl for a in accounts),
                total_margin=sum(a.margin for a in accounts),
                created_at=g.created_at,
            ))
        return responses


@router.post("/groups", response_model=MT5GroupResponse)
async def create_group(data: MT5GroupCreate):
    """Create a new account group."""
    async with AsyncSessionLocal() as db:
        group = MT5AccountGroup(
            user_id=DEFAULT_USER_ID, name=data.name, is_default=data.is_default,
        )
        db.add(group)
        await db.commit()
        await db.refresh(group)

        for aid in data.account_ids:
            db.add(MT5AccountGroupMember(group_id=group.id, account_id=aid))
        await db.commit()

        return MT5GroupResponse(
            id=group.id, name=group.name, is_default=group.is_default,
            created_at=group.created_at,
        )


@router.put("/groups/{group_id}/members")
async def update_group_members(group_id: int, data: MT5GroupMemberUpdate):
    """Replace account members in a group."""
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(MT5AccountGroupMember).where(
                MT5AccountGroupMember.group_id == group_id
            )
        )
        weights = data.weights or [1.0] * len(data.account_ids)
        for aid, w in zip(data.account_ids, weights):
            db.add(MT5AccountGroupMember(group_id=group_id, account_id=aid, weight=w))
        await db.commit()
        return {"updated": True}


@router.post("/groups/{group_id}/snapshot")
async def take_group_snapshot(group_id: int):
    """Take a point-in-time snapshot of the group."""
    async with AsyncSessionLocal() as db:
        snapshot = await MT5AggregationService.build_group_snapshot(db, group_id)
        if not snapshot:
            raise HTTPException(404, "Group not found or empty")
        return {"equity": snapshot.equity, "balance": snapshot.balance}


# ── Equity Curve ───────────────────────────────────────────

@router.get("/equity-curve")
async def get_equity_curve(
    group_id: Optional[int] = None,
    account_id: Optional[int] = None,
    limit: int = Query(200, le=500),
):
    """Get equity curve for TradingView chart. Max 200-500 points (windowed)."""
    async with AsyncSessionLocal() as db:
        data = await MT5AggregationService.get_equity_curve(
            db, group_id=group_id, account_id=account_id, limit=limit,
        )
        return data


# ── Risk Metrics ───────────────────────────────────────────

@router.get("/risk", response_model=MT5RiskOverviewResponse)
async def get_risk_overview(
    account_id: Optional[int] = None,
    group_id: Optional[int] = None,
):
    """Get full risk overview: R:R ratios, exposure heatmap, PnL distribution."""
    async with AsyncSessionLocal() as db:
        # Resolve account IDs
        account_ids = []
        if account_id:
            account_ids = [account_id]
        elif group_id:
            members = await MT5AggregationService.get_group_accounts(db, group_id)
            account_ids = [m["account"].id for m in members]
        else:
            result = await db.execute(
                select(MT5Account.id).where(MT5Account.user_id == DEFAULT_USER_ID)
            )
            account_ids = [row[0] for row in result.fetchall()]

        if not account_ids:
            return MT5RiskOverviewResponse()

        positions_rr = await MT5RiskMetricsService.get_positions_rr(db, account_ids)
        exposure = await MT5RiskMetricsService.get_exposure_heatmap(db, account_ids)
        pnl_hour = await MT5RiskMetricsService.get_pnl_heatmap(db, account_ids, "hour")
        pnl_day = await MT5RiskMetricsService.get_pnl_heatmap(db, account_ids, "weekday")

        return MT5RiskOverviewResponse(
            positions_rr=positions_rr,
            exposure_heatmap=exposure,
            pnl_by_hour=pnl_hour,
            pnl_by_weekday=pnl_day,
        )


# ── Chart Overlays ─────────────────────────────────────────

@router.get("/overlays", response_model=MT5OverlayResponse)
async def get_chart_overlays(
    account_id: Optional[int] = None,
    group_id: Optional[int] = None,
    symbol: Optional[str] = None,
):
    """
    Get chart overlay data (orders, positions, SL/TP, deal markers).
    
    Frontend should diff this against previous state and only update changed items.
    Markers capped at 200 for performance.
    """
    async with AsyncSessionLocal() as db:
        account_ids = []
        if account_id:
            account_ids = [account_id]
        elif group_id:
            members = await MT5AggregationService.get_group_accounts(db, group_id)
            account_ids = [m["account"].id for m in members]
        else:
            result = await db.execute(
                select(MT5Account.id).where(MT5Account.user_id == DEFAULT_USER_ID)
            )
            account_ids = [row[0] for row in result.fetchall()]

        return await MT5RiskMetricsService.build_overlay(db, account_ids, symbol)


# ── Trade Replay ───────────────────────────────────────────

@router.post("/replay", response_model=MT5ReplayResponse)
async def create_replay(data: MT5ReplayRequest):
    """Create and execute a trade replay from MT5 deal history."""
    async with AsyncSessionLocal() as db:
        run = await MT5ReplayService.create_replay(
            db, user_id=DEFAULT_USER_ID,
            account_id=data.account_id, group_id=data.group_id,
            date_from=data.date_from, date_to=data.date_to,
            symbol_filter=data.symbol_filter,
        )
        # Execute synchronously for now (could be async job for large datasets)
        run = await MT5ReplayService.execute_replay(db, run.id)
        return MT5ReplayResponse(
            id=run.id,
            status=run.status.value if hasattr(run.status, 'value') else str(run.status),
            total_trades=run.total_trades, total_pnl=run.total_pnl,
            max_drawdown=run.max_drawdown, win_rate=run.win_rate,
            sharpe_ratio=run.sharpe_ratio, equity_curve=run.equity_curve,
            created_at=run.created_at,
        )


@router.get("/replay", response_model=List[MT5ReplayResponse])
async def list_replays():
    """List all replay runs."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(MT5ReplayRun)
            .where(MT5ReplayRun.user_id == DEFAULT_USER_ID)
            .order_by(MT5ReplayRun.created_at.desc())
            .limit(20)
        )
        runs = result.scalars().all()
        return [MT5ReplayResponse(
            id=r.id,
            status=r.status.value if hasattr(r.status, 'value') else str(r.status),
            total_trades=r.total_trades, total_pnl=r.total_pnl,
            max_drawdown=r.max_drawdown, win_rate=r.win_rate,
            sharpe_ratio=r.sharpe_ratio, equity_curve=r.equity_curve,
            created_at=r.created_at,
        ) for r in runs]


# ── Copy-Trading Simulation ───────────────────────────────

@router.get("/copy-profiles", response_model=List[MT5CopyProfileResponse])
async def list_copy_profiles():
    """List copy-trading simulation profiles."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(MT5CopyProfile).where(MT5CopyProfile.user_id == DEFAULT_USER_ID)
        )
        profiles = result.scalars().all()
        return [MT5CopyProfileResponse(
            id=p.id, name=p.name,
            source_account_id=p.source_account_id,
            source_group_id=p.source_group_id,
            allocation_mode=p.allocation_mode,
            allocation_value=p.allocation_value,
            max_open_positions=p.max_open_positions,
            symbol_whitelist=p.symbol_whitelist,
            enabled=p.enabled,
            paper_balance=p.paper_balance,
            paper_equity=p.paper_equity,
            created_at=p.created_at,
        ) for p in profiles]


@router.post("/copy-profiles", response_model=MT5CopyProfileResponse)
async def create_copy_profile(data: MT5CopyProfileCreate):
    """Create a new copy-trading simulation profile."""
    async with AsyncSessionLocal() as db:
        profile = MT5CopyProfile(
            user_id=DEFAULT_USER_ID,
            name=data.name,
            source_account_id=data.source_account_id,
            source_group_id=data.source_group_id,
            allocation_mode=data.allocation_mode,
            allocation_value=data.allocation_value,
            max_open_positions=data.max_open_positions,
            symbol_whitelist=data.symbol_whitelist,
        )
        db.add(profile)
        await db.commit()
        await db.refresh(profile)
        return MT5CopyProfileResponse(
            id=profile.id, name=profile.name,
            source_account_id=profile.source_account_id,
            source_group_id=profile.source_group_id,
            allocation_mode=profile.allocation_mode,
            allocation_value=profile.allocation_value,
            max_open_positions=profile.max_open_positions,
            symbol_whitelist=profile.symbol_whitelist,
            enabled=profile.enabled,
            paper_balance=profile.paper_balance,
            paper_equity=profile.paper_equity,
            created_at=profile.created_at,
        )


@router.post("/copy-profiles/{profile_id}/toggle")
async def toggle_copy_profile(profile_id: int):
    """Enable/disable a copy simulation profile."""
    async with AsyncSessionLocal() as db:
        profile = await db.get(MT5CopyProfile, profile_id)
        if not profile:
            raise HTTPException(404, "Profile not found")
        profile.enabled = not profile.enabled
        await db.commit()
        return {"enabled": profile.enabled}


@router.get("/copy-profiles/{profile_id}/trades", response_model=List[MT5CopySimTradeResponse])
async def list_copy_sim_trades(profile_id: int, limit: int = Query(50, le=200)):
    """List simulated copy trades for a profile."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(MT5CopySimTrade)
            .where(MT5CopySimTrade.copy_profile_id == profile_id)
            .order_by(MT5CopySimTrade.created_at.desc())
            .limit(limit)
        )
        trades = result.scalars().all()
        return [MT5CopySimTradeResponse(
            id=t.id, symbol=t.symbol, side=t.side,
            qty_sim=t.qty_sim, entry_time=t.entry_time,
            entry_price=t.entry_price, exit_time=t.exit_time,
            exit_price=t.exit_price, pnl_sim=t.pnl_sim,
            status=t.status.value if hasattr(t.status, 'value') else str(t.status),
        ) for t in trades]


@router.get("/copy-profiles/{profile_id}/performance")
async def get_copy_performance(profile_id: int):
    """Get performance summary for a copy simulation profile."""
    async with AsyncSessionLocal() as db:
        return await MT5CopySimEngine.get_profile_performance(db, profile_id)


# ── Sync All ───────────────────────────────────────────────

@router.post("/sync-all")
async def sync_all():
    """Trigger sync for all accounts (manual trigger, normally runs on schedule)."""
    async with AsyncSessionLocal() as db:
        await MT5SyncService.sync_all_accounts(db, user_id=DEFAULT_USER_ID)
        return {"synced": True}


# ── Test Connection (no DB write) ──────────────────────────

@router.post("/test-connection")
async def test_connection(data: MT5AccountCreate):
    """
    Test mtapi-io connectivity with the given credentials without saving.
    Returns connection result + account info if successful.
    """
    try:
        info = await mt5_client.get_account_info(data.login, data.server, data.password)
        return {
            "reachable": True,
            "balance":  info.get("balance", 0),
            "equity":   info.get("equity", 0),
            "currency": info.get("currency", "USD"),
            "leverage": info.get("leverage", 100),
            "company":  info.get("company", ""),
            "name":     info.get("name", ""),
        }
    except Exception as e:
        return {
            "reachable": False,
            "error": str(e),
            "hint": (
                "Make sure mtapi-io is running on the URL configured in MT5_API_URL "
                f"(currently: {mt5_client.base_url}) and your MT5 terminal is open."
            ),
        }


# ── Plugin Status ──────────────────────────────────────────

@router.get("/status")
async def plugin_status():
    """MT5 plugin health check + mtapi-io ping."""
    reachable = await mt5_client.ping()
    return {
        "plugin": "MT5 Trading",
        "version": "2.0.0",
        "mtapi_url": mt5_client.base_url,
        "mtapi_reachable": reachable,
        "features": {
            "aggregation":   mt5_config.enable_aggregation,
            "copy_sim":      mt5_config.enable_copy_sim,
            "trade_replay":  mt5_config.enable_trade_replay,
            "heatmap":       mt5_config.overlay_heatmap_enabled,
        },
    }


# ── OHLCV Candles ────────────────────────────────────────────────────────

@router.get("/candles", response_model=MT5CandlesResponse)
async def get_candles(
    account_id: int,
    symbol: str,
    timeframe: str = Query(default="H1"),
    count: int = Query(default=300, ge=10, le=1000),
):
    """
    Fetch real OHLCV bars from MT5 terminal via PriceHistoryEx.
    Handles weekends/holidays automatically (iterates back monthly).
    """
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        try:
            bars = await mt5_client.get_candles(
                account.login, account.server, account.password_encrypted,
                symbol, timeframe, count,
            )
        except Exception as e:
            # Return empty candles gracefully so the frontend can fall back to the
            # exchange feed.  A 502 here would prevent the exchange fallback from
            # activating in browsers that don't catch non-2xx axios errors cleanly.
            logger.warning(f"[MT5/candles] {symbol}: {e}")
            return MT5CandlesResponse(symbol=symbol, timeframe=timeframe, candles=[])

        candles = []
        for b in bars:
            candles.append(MT5CandleResponse(
                time=int(b["time"]),
                open=float(b["open"]),
                high=float(b["high"]),
                low=float(b["low"]),
                close=float(b["close"]),
                volume=b.get("volume"),
            ))
        candles.sort(key=lambda c: c.time)
        return MT5CandlesResponse(symbol=symbol, timeframe=timeframe, candles=candles)


# ── Live Price ──────────────────────────────────────────────────────────

@router.get("/price", response_model=MT5PriceResponse)
async def get_live_price(account_id: int, symbol: str):
    """Current bid/ask from MT5 via GetQuote."""
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        try:
            data = await mt5_client.get_symbol_price(
                account.login, account.server, account.password_encrypted, symbol
            )
        except Exception as e:
            raise HTTPException(502, f"MT5 API error fetching price: {e}")
        return MT5PriceResponse(
            symbol=symbol,
            bid=data["bid"],
            ask=data["ask"],
            time=int(datetime.utcnow().timestamp()),
        )


# ── SMC Sniper Strategy ─────────────────────────────────────────────────

# MT5 timeframe → ccxt/exchange timeframe string
_MT5_TF_TO_EX: dict[str, str] = {
    "M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "1h", "H4": "4h", "D1": "1d", "W1": "1w",
}

# MT5 symbol → Bitget/exchange symbol (explicit overrides)
_MT5_SYMBOL_TO_EX: dict[str, str] = {
    "XAUUSD": "XAU/USDT",
    "XAGUSD": "XAG/USDT",
    "BTCUSD": "BTC/USDT",
    "ETHUSD": "ETH/USDT",
    "EURUSD": "EUR/USDT",
    "GBPUSD": "GBP/USDT",
    "USDJPY": "BTC/USDT",  # no JPY pair on Bitget; fall through gracefully
}


def _symbol_to_exchange(symbol: str) -> Optional[str]:
    """Map an MT5 symbol to a Bitget/exchange tradeable pair."""
    s = (symbol or "").upper().replace("/", "")
    if s in _MT5_SYMBOL_TO_EX:
        return _MT5_SYMBOL_TO_EX[s]
    if s.endswith("USDT") and len(s) > 4:
        return f"{s[:-4]}/USDT"
    if s.endswith("USD") and not s.endswith("USDT") and len(s) > 3:
        return f"{s[:-3]}/USDT"
    return None


async def _exchange_candles_fallback(symbol: str, timeframe: str, count: int):
    """Fetch candles from Bitget when MT5 history is stale / empty."""
    try:
        from app.exchanges.manager import exchange_manager, SupportedExchange  # type: ignore

        ex_symbol = _symbol_to_exchange(symbol)
        if not ex_symbol:
            return []

        ex_tf = _MT5_TF_TO_EX.get((timeframe or "").upper(), "1h")

        conn = exchange_manager.get_exchange(SupportedExchange.BITGET)
        if conn is None:
            # try Binance as secondary fallback
            conn = exchange_manager.get_exchange(SupportedExchange.BINANCE)
        if conn is None:
            return []

        raw = await conn.get_ohlcv(ex_symbol, ex_tf, count)
        if not raw:
            return []

        return candles_from_payload([
            {
                "time": int(c[0] / 1000),
                "open": float(c[1]), "high": float(c[2]),
                "low": float(c[3]), "close": float(c[4]),
                "volume": float(c[5] or 0),
            }
            for c in raw
        ])
    except Exception as exc:
        logger.debug(f"[MT5/strategy] exchange candle fallback error for {symbol}/{timeframe}: {exc}")
        return []


async def _load_candles(account_id: int, symbol: str, timeframe: str, count: int):
    """Fetch + normalise candles for an account (shared by strategy endpoints).

    First tries the MT5 bridge.  If it returns fewer than 40 bars (common with
    metals/forex brokers where mtapi-io ignores the fromDate parameter), falls
    back to the same Bitget/exchange feed used by the SMC chart's own candle
    loader — so the analysis is never blocked by a stale MT5 history.
    """
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        try:
            bars = await mt5_client.get_candles(
                account.login, account.server, account.password_encrypted,
                symbol, timeframe, count,
            )
        except Exception as e:
            logger.warning(f"[MT5/strategy] MT5 candle fetch failed for {symbol}/{timeframe}: {e}")
            bars = []

    candles = candles_from_payload([
        {"time": b["time"], "open": b["open"], "high": b["high"],
         "low": b["low"], "close": b["close"], "volume": b.get("volume")}
        for b in bars
    ])

    if len(candles) < 40:
        logger.info(
            f"[MT5/strategy] MT5 returned {len(candles)} candles for "
            f"{symbol}/{timeframe} — trying exchange fallback"
        )
        ex_candles = await _exchange_candles_fallback(symbol, timeframe, count)
        if len(ex_candles) >= 40:
            logger.info(
                f"[MT5/strategy] exchange fallback provided {len(ex_candles)} "
                f"candles for {symbol}/{timeframe}"
            )
            return ex_candles
        # Both sources sparse — return whatever we have; engine will report the
        # proper "Not enough candles" error to the caller.
        if ex_candles:
            return ex_candles

    return candles


async def _kronos_from_candles(candles, symbol: str, timeframe: str):
    """Run the Kronos ML forecast on the SAME candles the SMC engine analysed.

    Works for ANY symbol (XAUUSD, FX, indices, crypto) because it forecasts the
    exact series shown on the chart rather than re-fetching from a crypto exchange.
    Fully graceful: returns ``None`` if the Kronos plugin is unavailable or the
    data is insufficient, so the SMC analysis never breaks.
    """
    try:
        from plugins.KronosForecastPlugin.backend.services import forecast_service as _kronos
        rows = []
        for c in list(candles)[-400:]:
            t = int(getattr(c, "time", 0) or 0)
            t_ms = t * 1000 if t < 1_000_000_000_000 else t
            rows.append([
                t_ms, float(c.open), float(c.high), float(c.low), float(c.close),
                float(getattr(c, "volume", 0.0) or 0.0),
            ])
        _tfmap = {"M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
                  "H1": "1h", "H4": "4h", "D1": "1d"}
        _ktf = _tfmap.get(str(timeframe).upper(), str(timeframe).lower())
        fc = await _kronos.forecast_from_rows(
            rows, symbol=symbol, timeframe=_ktf, exchange="mt5", pred_len=12,
        )
        if not fc or not fc.signal:
            return None
        s = fc.signal
        return {
            "engine": fc.engine,
            "direction": s.direction,
            "pct_change": round(s.pct_change, 3),
            "confidence": round(s.confidence, 3),
            "target_price": s.target_price,
            "anchor_price": fc.anchor_price,
            "summary": s.summary,
            "overlays": [o.model_dump() for o in (fc.overlays or [])],
            "markers": [m.model_dump() for m in (fc.markers or [])],
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[MT5/strategy] Kronos forecast skipped: {exc}")
        return None


@router.get("/strategy/analyze", response_model=MT5SmcAnalyzeResponse)
async def smc_analyze(
    account_id: int,
    symbol: str,
    timeframe: str = Query(default="H1"),
    count: int = Query(default=400, ge=60, le=1000),
    min_rr: float = Query(default=1.5, ge=1.0, le=10.0),
    max_rr: float = Query(default=3.0, ge=1.0, le=10.0),
    sl_buffer_atr: float = Query(default=1.0, ge=0.0, le=3.0),
    min_confidence: float = Query(default=0.6, ge=0.0, le=1.0),
    use_ai: bool = Query(default=True),
):
    """
    Smart Money Concepts sniper analysis: market structure, order blocks, FVGs,
    liquidity, premium/discount and high-probability limit setups. Optionally
    enriched by an LLM review using the DB-backed AI router (same providers as
    telegram-signals and /agents).
    """
    candles = await _load_candles(account_id, symbol, timeframe, count)
    engine = SMCStrategyEngine(
        min_rr=min_rr, max_rr=max_rr, sl_buffer_atr=sl_buffer_atr,
        min_confidence=min_confidence, symbol=symbol,
        contract_size=contract_size_for_symbol(symbol),
    )
    analysis = engine.analyze(candles)

    kronos_block = await _kronos_from_candles(candles, symbol, timeframe)

    ai_block = None
    if use_ai and not analysis.get("error") and analysis.get("signals"):
        async with AsyncSessionLocal() as db:
            try:
                ai_block = await ai_review(
                    db=db, symbol=symbol, timeframe=timeframe,
                    analysis=analysis, kronos_forecast=kronos_block,
                )
            except Exception as e:
                logger.warning(f"[MT5/strategy] AI review failed: {e}")
                ai_block = {"available": False, "reason": str(e)}

    return MT5SmcAnalyzeResponse(
        symbol=symbol, timeframe=timeframe, ai=ai_block, kronos=kronos_block, **analysis,
    )


@router.post("/strategy/backtest", response_model=MT5BacktestResponse)
async def smc_backtest(data: MT5BacktestRequest):
    """Walk-forward backtest of the SMC sniper model over historical candles."""
    candles = await _load_candles(data.account_id, data.symbol, data.timeframe, data.count)
    engine = SMCStrategyEngine(
        min_rr=data.min_rr, max_rr=data.max_rr, sl_buffer_atr=data.sl_buffer_atr,
        min_confidence=data.min_confidence, symbol=data.symbol,
        contract_size=contract_size_for_symbol(data.symbol),
    )
    result = engine.backtest(candles, expiry_bars=data.expiry_bars)
    return MT5BacktestResponse(
        symbol=data.symbol, timeframe=data.timeframe,
        stats=result.get("stats", {}), trades=result.get("trades", []),
        error=result.get("error"),
    )


@router.post("/strategy/analyze-data", response_model=MT5SmcAnalyzeResponse)
async def smc_analyze_data(data: MT5SmcAnalyzeDataRequest):
    """
    Source-agnostic SMC analysis: run on candles supplied by the caller. Lets the
    UI fall back to an exchange feed (e.g. XAUUSDT) when MT5 history is unavailable.
    Uses the DB-backed AI router (same providers as telegram-signals and /agents).
    """
    candles = candles_from_payload([c.model_dump() for c in data.candles])
    engine = SMCStrategyEngine(
        min_rr=data.min_rr, max_rr=data.max_rr, sl_buffer_atr=data.sl_buffer_atr,
        min_confidence=data.min_confidence, symbol=data.symbol,
        account_balance=data.account_balance,
        risk_per_trade_pct=data.risk_per_trade_pct,
        contract_size=data.contract_size or contract_size_for_symbol(data.symbol),
        max_total_loss=data.max_total_loss,
        daily_profit_target_pct=data.daily_profit_target_pct,
        us_session_only=data.us_session_only,
    )
    analysis = engine.analyze(candles)

    kronos_block = await _kronos_from_candles(candles, data.symbol, data.timeframe)

    ai_block = None
    if data.use_ai and not analysis.get("error") and analysis.get("signals"):
        async with AsyncSessionLocal() as db:
            try:
                ai_block = await ai_review(
                    db=db, symbol=data.symbol, timeframe=data.timeframe,
                    analysis=analysis, kronos_forecast=kronos_block,
                )
            except Exception as e:
                logger.warning(f"[MT5/strategy] AI review failed: {e}")
                ai_block = {"available": False, "reason": str(e)}

    return MT5SmcAnalyzeResponse(
        symbol=data.symbol, timeframe=data.timeframe, ai=ai_block, kronos=kronos_block, **analysis,
    )


@router.post("/strategy/backtest-data", response_model=MT5BacktestResponse)
async def smc_backtest_data(data: MT5BacktestDataRequest):
    """Source-agnostic backtest: run on caller-supplied candles."""
    from plugins.MT5TradingPlugin.backend.services.smc_ai import ai_backtest_review
    
    candles = candles_from_payload([c.model_dump() for c in data.candles])
    engine = SMCStrategyEngine(
        min_rr=data.min_rr, max_rr=data.max_rr, sl_buffer_atr=data.sl_buffer_atr,
        min_confidence=data.min_confidence, symbol=data.symbol,
        risk_per_trade_pct=data.risk_per_trade_pct,
        contract_size=data.contract_size or contract_size_for_symbol(data.symbol),
        recovery_enabled=data.recovery_enabled,
        max_risk_multiplier=data.max_risk_multiplier,
        max_total_loss=data.max_total_loss,
        daily_profit_target_pct=data.daily_profit_target_pct,
    )
    result = engine.backtest(
        candles, expiry_bars=data.expiry_bars,
        starting_balance=data.starting_balance,
    )
    
    ai_block = None
    if data.use_ai and result.get("stats", {}).get("total", 0) > 0:
        async with AsyncSessionLocal() as db:
            try:
                ai_block = await ai_backtest_review(
                    db=db, symbol=data.symbol, timeframe=data.timeframe,
                    stats=result.get("stats", {}),
                    trades=result.get("trades", []),
                )
            except Exception as e:
                logger.warning(f"[MT5/backtest] AI review failed: {e}")
                ai_block = {"available": False, "reason": str(e)}
    
    return MT5BacktestResponse(
        symbol=data.symbol, timeframe=data.timeframe,
        stats=result.get("stats", {}), trades=result.get("trades", []),
        error=result.get("error"), ai=ai_block,
    )


@router.post("/strategy/place", response_model=MT5TradeResultResponse)
async def smc_place(data: MT5SmcPlaceRequest):
    """
    Place a resting limit order with SL + TP derived from an SMC sniper setup.

    Server-side guards enforce sane stop/target geometry before the order is sent
    to the broker (paper-safe: rejects inverted SL/TP).

    After a successful placement the order is written directly to the DB from the
    broker response so it appears in the /orders endpoint immediately — even if
    the subsequent sync races with the broker's order propagation delay.
    """
    # Geometry validation (fail-closed).
    if data.side == "buy":
        if not (data.stop_loss < data.entry < data.take_profit):
            raise HTTPException(400, "Buy limit requires SL < entry < TP")
        operation = "buy_limit"
    else:
        if not (data.take_profit < data.entry < data.stop_loss):
            raise HTTPException(400, "Sell limit requires TP < entry < SL")
        operation = "sell_limit"

    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, data.account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        try:
            result = await mt5_client.place_order(
                login=account.login, server=account.server,
                password=account.password_encrypted,
                symbol=data.symbol, order_type=operation,
                volume=data.volume, price=data.entry,
                sl=data.stop_loss, tp=data.take_profit, comment=data.comment,
            )
        except Exception as e:
            raise HTTPException(502, f"Limit order failed: {e}")

        ticket = result.get("ticket") if isinstance(result, dict) else None

        # ── Immediately persist the placed order to the DB ─────────────────
        # This ensures the order shows in /orders right away without waiting
        # for the broker to propagate it back through the periodic sync.
        if ticket:
            try:
                from plugins.MT5TradingPlugin.backend.models import (
                    MT5Order, MT5OrderType, MT5OrderStatus,
                )
                try:
                    ot = MT5OrderType(operation)
                except ValueError:
                    ot = MT5OrderType.BUY_LIMIT

                # Upsert by ticket so a subsequent sync won't create a duplicate.
                existing = (
                    await db.execute(
                        select(MT5Order).where(
                            MT5Order.account_id == account.id,
                            MT5Order.mt5_ticket == int(ticket),
                        ).limit(1)
                    )
                ).scalar_one_or_none()
                if not existing:
                    db.add(MT5Order(
                        account_id=account.id,
                        mt5_ticket=int(ticket),
                        symbol=data.symbol,
                        order_type=ot,
                        volume=data.volume,
                        price=data.entry,
                        sl=data.stop_loss,
                        tp=data.take_profit,
                        status=MT5OrderStatus.PENDING,
                        comment=data.comment,
                        raw_data=result,
                    ))
                    await db.commit()
            except Exception as insert_err:
                logger.debug(f"[MT5/place] direct DB insert skipped: {insert_err}")
                await db.rollback()

        # ── Background sync to refresh positions, balance, other orders ────
        try:
            await MT5SyncService.sync_account(db, account)
        except Exception as sync_err:
            # Non-fatal — the order is already in DB; sync will catch up later.
            logger.debug(f"[MT5/place] post-place sync error: {sync_err}")

        return MT5TradeResultResponse(
            success=True,
            ticket=ticket,
            message=f"{operation} placed @ {data.entry}",
            raw=result,
        )


# ── Symbols List ────────────────────────────────────────────────────────

@router.get("/symbols")
async def get_symbols(account_id: int):
    """All tradeable symbols on this MT5 account (broker's full list)."""
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        symbols = await mt5_client.get_symbols(
            account.login, account.server, account.password_encrypted
        )
        return {"symbols": symbols, "count": len(symbols)}


@router.get("/symbols/{symbol}/params")
async def get_symbol_params(symbol: str, account_id: int = Query(...)):
    """Full symbol info: digits, tick size, contract size, lot limits."""
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        try:
            data = await mt5_client.get_symbol_params(
                account.login, account.server, account.password_encrypted, symbol
            )
        except Exception as e:
            raise HTTPException(502, f"Could not fetch symbol params: {e}")
        return data


# ── Trading ───────────────────────────────────────────────────────────────

@router.post("/trade/order", response_model=MT5TradeResultResponse)
async def send_order(data: MT5PlaceMarketOrderRequest):
    """
    Place a market (Buy/Sell) or pending order via OrderSendSafe.
    OrderSendSafe is resilient to connection drops during execution.
    """
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, data.account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        try:
            result = await mt5_client.place_order(
                login=account.login, server=account.server,
                password=account.password_encrypted,
                symbol=data.symbol, order_type=data.operation,
                volume=data.volume, price=data.price or 0,
                sl=data.sl, tp=data.tp, comment=data.comment,
            )
            await MT5SyncService.sync_account(db, account)
            return MT5TradeResultResponse(
                success=True,
                ticket=result.get("ticket") if isinstance(result, dict) else None,
                raw=result,
            )
        except Exception as e:
            raise HTTPException(502, f"Order failed: {e}")


@router.put("/trade/modify", response_model=MT5TradeResultResponse)
async def modify_trade(
    account_id: int,
    ticket: int,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    price: Optional[float] = None,
):
    """Modify SL/TP (or price for pending orders) via OrderModifySafe."""
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        try:
            result = await mt5_client.modify_order(
                account.login, account.server, account.password_encrypted,
                ticket=ticket, price=price, sl=sl, tp=tp,
            )
            await MT5SyncService.sync_account(db, account)
            return MT5TradeResultResponse(success=True, ticket=ticket, raw=result)
        except Exception as e:
            raise HTTPException(502, f"Modify failed: {e}")


@router.post("/trade/close", response_model=MT5TradeResultResponse)
async def close_trade(data: MT5ClosePositionRequest):
    """Close an open market position (full or partial) via OrderCloseSafe."""
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, data.account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        try:
            result = await mt5_client.close_position(
                account.login, account.server, account.password_encrypted,
                ticket=data.ticket, volume=data.volume,
            )
            await MT5SyncService.sync_account(db, account)
            return MT5TradeResultResponse(success=True, ticket=data.ticket, raw=result)
        except Exception as e:
            raise HTTPException(502, f"Close failed: {e}")


@router.delete("/trade/cancel/{ticket}", response_model=MT5TradeResultResponse)
async def cancel_pending_order(ticket: int, account_id: int = Query(...)):
    """Cancel a pending order via OrderCancelTask."""
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        try:
            result = await mt5_client.cancel_order(
                account.login, account.server, account.password_encrypted, ticket
            )
            await MT5SyncService.sync_account(db, account)
            return MT5TradeResultResponse(success=True, ticket=ticket, raw=result)
        except Exception as e:
            raise HTTPException(502, f"Cancel failed: {e}")


# ── Equity History / Stats ────────────────────────────────────────

@router.get("/equity-history")
async def get_equity_history(account_id: int):
    """
    Account equity curve from EquityHistory.
    Returns [{time (unix), equity, balance}] for charting.
    """
    async with AsyncSessionLocal() as db:
        account = await db.get(MT5Account, account_id)
        if not account:
            raise HTTPException(404, "Account not found")
        try:
            points = await mt5_client.get_equity_history(
                account.login, account.server, account.password_encrypted
            )
        except Exception as e:
            raise HTTPException(502, f"EquityHistory failed: {e}")

        # Normalise time field
        out = []
        for p in points:
            t = p.get("time") or p.get("date") or 0
            if isinstance(t, str):
                try:
                    t = int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    t = 0
            elif isinstance(t, (int, float)) and t > 1e10:
                t = int(t / 1000)
            out.append({
                "time":    int(t),
                "equity":  float(p.get("equity", 0)),
                "balance": float(p.get("balance", 0)),
            })
        out.sort(key=lambda x: x["time"])
        return {"account_id": account_id, "points": out, "count": len(out)}


# ── Broker Search ───────────────────────────────────────────────────

@router.get("/broker-search")
async def broker_search(name: str):
    """Find broker server IP/port by company name (uses /Search endpoint)."""
    results = await mt5_client.search_broker(name)
    return {"query": name, "results": results, "count": len(results)}


# ── Auto-Manage Loop ───────────────────────────────────────────────────────────
# Periodic SMC+AI analysis loop that auto-updates position TP/SL and pending
# orders across all connected MT5 accounts based on validated signals.

from plugins.MT5TradingPlugin.backend.services.auto_manage_service import (
    start_loop as _am_start,
    stop_loop as _am_stop,
    get_loop_status as _am_status,
    run_cycle as _am_run_cycle,
    analyze_positions_for_account as _am_analyze,
    apply_position_suggestions as _am_apply,
)


class PositionSuggestionItem(BaseModel):
    ticket: int
    account_id: int
    sl: Optional[float] = None
    tp: Optional[float] = None


class PositionSuggestionsApplyRequest(BaseModel):
    suggestions: List[PositionSuggestionItem]


@router.post("/auto-manage/loop/start")
async def start_auto_manage_loop(interval: int = Query(default=60, ge=10, le=3600)):
    """
    Start the MT5 auto-manage background loop.

    The loop periodically runs SMC+AI analysis across all active accounts and
    watchlist symbols, then updates position TP/SL and pending orders based on
    the best validated signal.

    - interval: seconds between cycles (default 60, min 10)
    """
    started = _am_start(interval)
    status = "started" if started else "already_running"
    return {"status": status, **_am_status()}


@router.post("/auto-manage/loop/stop")
async def stop_auto_manage_loop():
    """Stop the MT5 auto-manage background loop."""
    stopped = _am_stop()
    status = "stopped" if stopped else "not_running"
    return {"status": status, **_am_status()}


@router.get("/auto-manage/loop/status")
async def get_auto_manage_loop_status():
    """Get the current state of the MT5 auto-manage background loop."""
    return _am_status()


@router.post("/auto-manage/cycle")
async def run_auto_manage_cycle_once():
    """
    Trigger a single one-shot auto-manage cycle immediately (without starting the loop).
    Useful for manual testing and verification.
    """
    try:
        summary = await _am_run_cycle()
        return {"status": "completed", "summary": summary}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/auto-manage/analyze-positions")
async def analyze_positions_manual(account_id: int = Query(...)):
    """
    Analyse ALL open positions for the given account with SMC+AI and return
    per-position TP/SL suggestions WITHOUT applying any changes.

    Frontend can show these as suggestions and let the user apply them all
    at once via /auto-manage/apply-suggestions.
    """
    suggestions = await _am_analyze(account_id)
    return {
        "account_id": account_id,
        "suggestions": suggestions,
        "with_suggestion": sum(1 for s in suggestions if s.get("has_suggestion")),
        "total": len(suggestions),
    }


@router.post("/auto-manage/apply-suggestions")
async def apply_position_suggestions_endpoint(data: PositionSuggestionsApplyRequest):
    """
    Batch-apply SL/TP updates to a set of positions.

    Body: { "suggestions": [{"ticket": 123, "account_id": 1, "sl": 1.23, "tp": 1.30}, ...] }
    """
    items = [s.model_dump() for s in data.suggestions]
    results = await _am_apply(items)
    applied = sum(1 for r in results if r.get("success"))
    failed = len(results) - applied
    return {"applied": applied, "failed": failed, "results": results}
