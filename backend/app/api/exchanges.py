"""
Exchange API Routes
"""
from typing import Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import ccxt.async_support as ccxt

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.core.database import get_db
from app.core.timezone import now_sast
from app.models.database import Trade, LiveTradeSettings
from app.exchanges.manager import exchange_manager, SupportedExchange
from app.exchanges.base import OrderSide, OrderType
from app.exchanges.bitget import BitgetConnector


def _utcnow():
    return now_sast()


router = APIRouter(prefix="/exchanges", tags=["exchanges"])


# Helper function to get or create exchange for public endpoints
# Cache temporary public instances so we don't pay the load-markets cost on every
# call (the ticker endpoint is polled every ~2.5s and would otherwise time out).
_PUBLIC_EXCHANGE_CACHE: Dict[str, "ccxt.Exchange"] = {}


async def get_exchange_for_public_data(exchange: SupportedExchange):
    """Get exchange connector or create (cached) temporary one for public data"""
    connector = exchange_manager.get_exchange(exchange)

    # If exchange is not initialized (no credentials), reuse a cached temporary
    # instance for public data instead of constructing a new one each call.
    if not connector:
        cached = _PUBLIC_EXCHANGE_CACHE.get(exchange.value)
        if cached is not None:
            return cached
        exchange_class = getattr(ccxt, exchange.value)
        inst = exchange_class({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
        _PUBLIC_EXCHANGE_CACHE[exchange.value] = inst
        return inst

    return connector.exchange


class OrderRequest(BaseModel):
    """Order creation request"""
    symbol: str
    side: OrderSide
    order_type: OrderType
    amount: float
    price: Optional[float] = None


@router.get("/status")
async def get_exchanges_status():
    """Get status of all exchanges"""
    return {
        "exchanges": exchange_manager.get_exchange_status(),
        "initialized_count": len(exchange_manager.get_all_exchanges()),
        "testnet_mode": exchange_manager.testnet,
    }


@router.get("/{exchange}/balance")
async def get_balance(
    exchange: SupportedExchange,
    currency: Optional[str] = Query(None, description="Specific currency (e.g., USDT, BTC)")
):
    """Get balance for a specific exchange"""
    connector = exchange_manager.get_exchange(exchange)
    if not connector:
        raise HTTPException(status_code=404, detail=f"Exchange {exchange} not initialized")
    
    try:
        balance = await connector.get_balance(currency)
        return {
            "exchange": exchange.value,
            "balance": balance,
        }
    except Exception as e:
        err_str = str(e)
        # Suppress repeated logging when the circuit breaker is already active
        # (back-off message contains "suppressed") — the connector already logged once.
        if "suppressed" not in err_str.lower():
            logger.warning(f"Failed to get balance for {exchange.value}: {e}")
        return {"exchange": exchange.value, "balance": None, "error": err_str}


@router.get("/{exchange}/ticker/{symbol}")
async def get_ticker(exchange: SupportedExchange, symbol: str):
    """Get ticker information for a trading pair"""
    # Crypto exchanges trade USDT pairs, not plain USD
    _CRYPTO_EXCHANGES = {"bitget", "binance", "bybit", "okx", "kucoin", "coinbase", "huobi", "gate"}
    _SYMBOL_ALIASES = {
        "XAUUSD": "XAU/USDT", "XAGUSD": "XAG/USDT", "BTCUSD": "BTC/USDT",
        "ETHUSD": "ETH/USDT", "BNBUSD": "BNB/USDT", "SOLUSD": "SOL/USDT",
        "XRPUSD": "XRP/USDT", "DOGEUSD": "DOGE/USDT",
    }
    is_crypto = exchange.value in _CRYPTO_EXCHANGES

    # Accept URL-safe format: BTCUSDT -> BTC/USDT, XAUUSD -> XAU/USDT (alias)
    if "/" not in symbol:
        upper = symbol.upper()
        if is_crypto and upper in _SYMBOL_ALIASES:
            symbol = _SYMBOL_ALIASES[upper]
        else:
            matched = False
            for quote in ("USDT", "USDC", "BTC", "ETH"):
                if upper.endswith(quote):
                    symbol = symbol[:-len(quote)] + "/" + quote
                    matched = True
                    break
            if not matched and is_crypto and upper.endswith("USD") and len(upper) > 3:
                symbol = symbol[:-3] + "/USDT"

    try:
        # Prefer the credentialed connector; fall back to a temporary public
        # instance (same pattern as OHLCV) so tickers work without API keys.
        connector = exchange_manager.get_exchange(exchange)
        if connector:
            ticker = await connector.get_ticker(symbol)
        else:
            exchange_instance = await get_exchange_for_public_data(exchange)
            swap_symbol = symbol
            if ":" not in swap_symbol and swap_symbol.endswith("/USDT"):
                swap_symbol += ":USDT"
            elif ":" not in swap_symbol and swap_symbol.endswith("/USDC"):
                swap_symbol += ":USDC"
            try:
                raw = await exchange_instance.fetch_ticker(swap_symbol)
            except Exception:
                # Retry spot symbol (no settle suffix) for non-swap markets like XAU/USDT
                raw = await exchange_instance.fetch_ticker(symbol)
            ticker = {
                "symbol": symbol,
                "last": raw.get("last"),
                "bid": raw.get("bid"),
                "ask": raw.get("ask"),
                "high": raw.get("high"),
                "low": raw.get("low"),
                "volume": raw.get("baseVolume"),
                "change": raw.get("percentage"),
            }
            # Do NOT close — the public instance is cached and reused across polls.
        return {
            "exchange": exchange.value,
            "symbol": symbol,
            "ticker": ticker,
        }
    except Exception as e:
        # Return empty ticker on transient exchange errors instead of 500/404
        logger.warning(f"Failed to get ticker for {symbol} on {exchange.value}: {e}")
        return {
            "exchange": exchange.value,
            "symbol": symbol,
            "ticker": None,
            "error": str(e),
        }


@router.get("/{exchange}/ohlcv/{symbol}")
async def get_ohlcv(
    exchange: SupportedExchange,
    symbol: str,
    timeframe: str = Query("1h", description="Timeframe (1m, 5m, 15m, 1h, 4h, 1d)"),
    limit: int = Query(100, description="Number of candles", ge=1, le=1500),
):
    """Get OHLCV (candlestick) data for a trading pair"""
    # Crypto exchanges trade USDT pairs, not plain USD
    _CRYPTO_EXCHANGES = {"bitget", "binance", "bybit", "okx", "kucoin", "coinbase", "huobi", "gate"}
    # Explicit MT5 symbol → exchange symbol for commodity/index pairs
    _SYMBOL_ALIASES = {
        "XAUUSD": "XAU/USDT",   # Gold
        "XAGUSD": "XAG/USDT",   # Silver
        "BTCUSD": "BTC/USDT",
        "ETHUSD": "ETH/USDT",
        "BNBUSD": "BNB/USDT",
        "SOLUSD": "SOL/USDT",
        "XRPUSD": "XRP/USDT",
        "DOGEUSD": "DOGE/USDT",
    }
    is_crypto = exchange.value in _CRYPTO_EXCHANGES

    # Accept URL-safe format: BTCUSDT -> BTC/USDT, PEPEUSDT -> PEPE/USDT
    if "/" not in symbol:
        upper = symbol.upper()
        # 1. Explicit alias map (highest priority — handles XAUUSD → XAU/USDT etc.)
        if is_crypto and upper in _SYMBOL_ALIASES:
            symbol = _SYMBOL_ALIASES[upper]
        else:
            # 2. Known USDT/USDC/BTC/ETH quote suffixes
            matched = False
            for quote in ("USDT", "USDC", "BTC", "ETH"):
                if upper.endswith(quote):
                    symbol = symbol[:-len(quote)] + "/" + quote
                    matched = True
                    break
            # 3. USD quote on crypto exchange → upgrade to USDT
            if not matched and is_crypto and upper.endswith("USD") and len(upper) > 3:
                symbol = symbol[:-3] + "/USDT"

    try:
        # Prefer the connector's get_ohlcv (handles swap symbol conversion)
        connector = exchange_manager.get_exchange(exchange)
        if connector:
            ohlcv_data = await connector.get_ohlcv(symbol, timeframe, limit)
        else:
            # No credentials — create temporary swap instance
            exchange_instance = await get_exchange_for_public_data(exchange)
            # Append settle currency for swap markets
            swap_symbol = symbol
            if ":" not in swap_symbol and swap_symbol.endswith("/USDT"):
                swap_symbol += ":USDT"
            elif ":" not in swap_symbol and swap_symbol.endswith("/USDC"):
                swap_symbol += ":USDC"
            ohlcv_data = await exchange_instance.fetch_ohlcv(
                symbol=swap_symbol, timeframe=timeframe, limit=limit,
            )
            # Do NOT close — the public instance is cached and reused across calls.
        
        # Transform to lightweight-charts format
        candlestick_data = [
            {
                "time": int(candle[0] / 1000),  # Convert ms to seconds
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": float(candle[5]) if len(candle) > 5 else 0,
            }
            for candle in ohlcv_data
        ]
        
        return {
            "exchange": exchange.value,
            "symbol": symbol,
            "timeframe": timeframe,
            "data": candlestick_data,
            "count": len(candlestick_data),
        }
    except Exception as e:
        logger.warning(f"Failed to get OHLCV for {symbol} on {exchange.value}: {e}")
        return {"exchange": exchange.value, "symbol": symbol, "timeframe": timeframe, "data": [], "count": 0, "error": str(e)}


@router.get("/{exchange}/markets")
async def get_markets(exchange: SupportedExchange):
    """Get all available markets on an exchange"""
    connector = exchange_manager.get_exchange(exchange)
    if not connector:
        raise HTTPException(status_code=404, detail=f"Exchange {exchange} not initialized")
    
    try:
        markets = await connector.get_markets()
        return {
            "exchange": exchange.value,
            "markets_count": len(markets),
            "markets": markets[:50],  # Limit to first 50 for readability
        }
    except Exception as e:
        logger.warning(f"Failed to get markets for {exchange.value}: {e}")
        return {"exchange": exchange.value, "markets_count": 0, "markets": [], "error": str(e)}


@router.post("/{exchange}/order")
async def create_order(exchange: SupportedExchange, order: OrderRequest):
    """Create an order on a specific exchange"""
    connector = exchange_manager.get_exchange(exchange)
    if not connector:
        raise HTTPException(status_code=404, detail=f"Exchange {exchange} not initialized")
    
    try:
        result = await connector.create_order(
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            amount=order.amount,
            price=order.price,
        )
        return {
            "exchange": exchange.value,
            "order": result,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{exchange}/orders")
async def get_open_orders(
    exchange: SupportedExchange,
    symbol: Optional[str] = Query(None, description="Filter by trading pair")
):
    """Get all open orders"""
    connector = exchange_manager.get_exchange(exchange)
    if not connector:
        raise HTTPException(status_code=404, detail=f"Exchange {exchange} not initialized")
    
    try:
        orders = await connector.get_open_orders(symbol)
        return {
            "exchange": exchange.value,
            "symbol": symbol,
            "orders": orders,
        }
    except Exception as e:
        logger.warning(f"Failed to get open orders for {exchange.value}: {e}")
        return {"exchange": exchange.value, "symbol": symbol, "orders": [], "error": str(e)}


@router.delete("/{exchange}/order/{order_id}")
async def cancel_order(
    exchange: SupportedExchange,
    order_id: str,
    symbol: str = Query(..., description="Trading pair for the order")
):
    """Cancel an order"""
    connector = exchange_manager.get_exchange(exchange)
    if not connector:
        raise HTTPException(status_code=404, detail=f"Exchange {exchange} not initialized")
    
    try:
        result = await connector.cancel_order(order_id, symbol)
        return {
            "exchange": exchange.value,
            "order_id": order_id,
            "result": result,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Bitget Native SDK Endpoints ────────────────────────────────


def _get_bitget_connector() -> BitgetConnector:
    """Helper to get Bitget connector with native SDK"""
    connector = exchange_manager.get_exchange(SupportedExchange.BITGET)
    if not connector:
        raise HTTPException(status_code=404, detail="Bitget not initialized. Set BITGET_API_KEY, BITGET_API_SECRET, BITGET_PASSPHRASE in .env")
    if not isinstance(connector, BitgetConnector) or not connector.native_client:
        raise HTTPException(status_code=500, detail="Bitget native SDK not available")
    return connector


@router.get("/bitget/account-info")
async def get_bitget_account_info():
    """Get Bitget account info via native v2 API"""
    connector = _get_bitget_connector()
    try:
        info = await connector.get_account_info()
        return {"exchange": "bitget", "account_info": info}
    except Exception as e:
        logger.warning(f"Failed to get Bitget account info: {e}")
        return {"exchange": "bitget", "account_info": None, "error": str(e)}


@router.get("/bitget/assets")
async def get_bitget_assets(coin: Optional[str] = Query(None, description="Filter by coin (e.g., USDT, BTC)")):
    """Get Bitget spot account assets via native v2 API (detailed view)"""
    connector = _get_bitget_connector()
    try:
        result = await connector.native_client.get_account_assets(coin=coin)
        assets = result.get("data", [])
        return {
            "exchange": "bitget",
            "assets": assets,
            "count": len(assets),
        }
    except Exception as e:
        logger.warning(f"Failed to get Bitget assets: {e}")
        return {"exchange": "bitget", "assets": [], "count": 0, "error": str(e)}


@router.get("/bitget/trade-fills/{symbol}")
async def get_bitget_trade_fills(
    symbol: str,
    limit: int = Query(100, ge=1, le=500),
):
    """Get Bitget trade fill history via native v2 API"""
    connector = _get_bitget_connector()
    try:
        fills = await connector.get_trade_fills(symbol, limit=limit)
        return {"exchange": "bitget", "symbol": symbol, "fills": fills}
    except Exception as e:
        logger.warning(f"Failed to get Bitget trade fills for {symbol}: {e}")
        return {"exchange": "bitget", "symbol": symbol, "fills": [], "error": str(e)}


@router.get("/bitget/order-history/{symbol}")
async def get_bitget_order_history(
    symbol: str,
    limit: int = Query(100, ge=1, le=500),
):
    """Get Bitget order history via native v2 API"""
    connector = _get_bitget_connector()
    try:
        orders = await connector.get_order_history(symbol, limit=limit)
        return {"exchange": "bitget", "symbol": symbol, "orders": orders}
    except Exception as e:
        logger.warning(f"Failed to get Bitget order history for {symbol}: {e}")
        return {"exchange": "bitget", "symbol": symbol, "orders": [], "error": str(e)}


@router.get("/bitget/open-orders")
async def get_bitget_open_orders(symbol: Optional[str] = Query(None)):
    """Get all open orders via native v2 API"""
    connector = _get_bitget_connector()
    try:
        orders = await connector.get_open_orders(symbol)
        return {"exchange": "bitget", "orders": orders}
    except Exception as e:
        logger.warning(f"Failed to get Bitget open orders: {e}")
        return {"exchange": "bitget", "orders": [], "error": str(e)}


# ─── Bitget Futures Endpoints ────────────────────────────────


@router.get("/bitget/futures/balance")
async def get_bitget_futures_balance(
    product_type: str = Query("USDT-FUTURES", description="USDT-FUTURES, COIN-FUTURES, USDC-FUTURES"),
    all_products: bool = Query(False, description="Aggregate balances across all futures product types (unified account)"),
):
    """Get Bitget futures account balances.

    With ``all_products=true`` the balances of every futures product type
    (USDT/USDC/COIN) are returned together — required for a unified
    (multi-assets) account that spreads margin across product types.
    """
    connector = _get_bitget_connector()
    try:
        if all_products:
            balance = await connector.get_all_futures_balances()
            return {"exchange": "bitget", "product_type": "ALL", "balance": balance}
        balance = await connector.get_futures_balance(product_type=product_type)
        return {"exchange": "bitget", "product_type": product_type, "balance": balance}
    except Exception as e:
        err_str = str(e)
        if "suppressed" not in err_str.lower():
            logger.warning(f"Failed to get Bitget futures balance: {e}")
        return {"exchange": "bitget", "product_type": product_type, "balance": [], "error": err_str}


@router.get("/bitget/futures/positions")
async def get_bitget_futures_positions(
    product_type: str = Query("USDT-FUTURES", description="USDT-FUTURES, COIN-FUTURES, USDC-FUTURES"),
    margin_coin: Optional[str] = Query(None, description="Filter by margin coin (e.g., USDT)"),
    all_products: bool = Query(False, description="Aggregate positions across all futures product types (unified account)"),
):
    """Get all open futures positions.

    With ``all_products=true`` positions from every futures product type are
    returned, each tagged with its ``product_type`` — required to see USDC-
    and COIN-margined positions on a unified account.
    """
    connector = _get_bitget_connector()
    try:
        if all_products:
            positions = await connector.get_all_futures_positions()
            return {"exchange": "bitget", "product_type": "ALL", "positions": positions}
        positions = await connector.get_futures_positions(
            product_type=product_type,
            margin_coin=margin_coin,
        )
        return {
            "exchange": "bitget",
            "product_type": product_type,
            "positions": positions,
        }
    except Exception as e:
        logger.warning(f"Failed to get Bitget futures positions: {e}")
        return {"exchange": "bitget", "product_type": product_type, "positions": [], "error": str(e)}


@router.get("/bitget/accounts")
async def get_bitget_linked_accounts():
    """Detect all linked Bitget accounts (main + sub-accounts) with balances.

    Returns the unified account overview (per-account-type balances, asset
    mode) plus each detected sub-account's futures equity. Degrades to
    main-account-only when the API key lacks sub-account permission.
    """
    connector = _get_bitget_connector()
    try:
        return {"exchange": "bitget", **(await connector.get_linked_accounts())}
    except Exception as e:
        logger.warning(f"Failed to get Bitget linked accounts: {e}")
        return {
            "exchange": "bitget",
            "accounts": [],
            "account_count": 0,
            "sub_accounts_supported": False,
            "asset_mode": None,
            "unified": False,
            "account_type_balances": [],
            "grand_total_usdt": 0,
            "error": str(e),
        }


class FuturesOrderRequest(BaseModel):
    """Futures order creation request"""
    symbol: str
    margin_coin: str
    side: str  # buy, sell
    order_type: str  # limit, market
    size: str
    price: Optional[str] = None
    margin_mode: str = "crossed"
    leverage: Optional[int] = None
    trade_side: str = "open"  # open, close
    product_type: str = "USDT-FUTURES"
    stop_loss_pct: Optional[float] = None   # e.g. 2.0 = 2%
    take_profit_pct: Optional[float] = None  # e.g. 4.0 = 4%


@router.post("/bitget/futures/order")
async def create_bitget_futures_order(
    order: FuturesOrderRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create a futures order on Bitget with optional auto TP/SL"""
    connector = _get_bitget_connector()

    # Determine reference price for SL/TP calculation
    sl_price = None
    tp_price = None
    ref_price = None
    if order.trade_side == "open" and (order.stop_loss_pct or order.take_profit_pct):
        if order.price and order.order_type == "limit":
            ref_price = float(order.price)
        else:
            # Get current market price via futures ticker
            try:
                tick_result = await connector.native_client.get_futures_ticker(
                    symbol=order.symbol, product_type=order.product_type
                )
                tick_data = tick_result.get("data", [{}])
                if isinstance(tick_data, list) and tick_data:
                    ref_price = float(tick_data[0].get("lastPr", 0))
                elif isinstance(tick_data, dict):
                    ref_price = float(tick_data.get("lastPr", 0))
            except Exception:
                ref_price = None

        if ref_price and ref_price > 0:
            is_buy = order.side == "buy"
            if order.stop_loss_pct:
                sl_price = ref_price * (1 - order.stop_loss_pct / 100) if is_buy else ref_price * (1 + order.stop_loss_pct / 100)
            if order.take_profit_pct:
                tp_price = ref_price * (1 + order.take_profit_pct / 100) if is_buy else ref_price * (1 - order.take_profit_pct / 100)

    try:
        result = await connector.create_futures_order(
            symbol=order.symbol,
            margin_coin=order.margin_coin,
            side=order.side,
            order_type=order.order_type,
            size=order.size,
            price=order.price,
            margin_mode=order.margin_mode,
            leverage=order.leverage,
            trade_side=order.trade_side,
            product_type=order.product_type,
            stop_loss=sl_price,
            take_profit=tp_price,
        )
        order_id = result.get("orderId", "")

        # Place explicit TPSL plan orders for more reliable SL/TP
        hold_side = "long" if order.side == "buy" else "short"
        if order.trade_side == "open" and sl_price:
            try:
                await connector.place_tpsl_order(
                    symbol=order.symbol,
                    margin_coin=order.margin_coin,
                    plan_type="loss_plan",
                    trigger_price=sl_price,
                    hold_side=hold_side,
                    size=order.size,
                )
                logger.info(f"[MANUAL] SL placed for {order.symbol} {hold_side}: {sl_price}")
            except Exception as e:
                logger.warning(f"[MANUAL] SL plan order failed (preset still applied): {e}")

        if order.trade_side == "open" and tp_price:
            try:
                await connector.place_tpsl_order(
                    symbol=order.symbol,
                    margin_coin=order.margin_coin,
                    plan_type="profit_plan",
                    trigger_price=tp_price,
                    hold_side=hold_side,
                    size=order.size,
                )
                logger.info(f"[MANUAL] TP placed for {order.symbol} {hold_side}: {tp_price}")
            except Exception as e:
                logger.warning(f"[MANUAL] TP plan order failed (preset still applied): {e}")

        # Save trade record to DB for tracking
        if order.trade_side == "open":
            symbol_display = order.symbol.replace("USDT", "/USDT")
            db.add(Trade(
                exchange="bitget",
                exchange_order_id=order_id,
                symbol=symbol_display,
                side=order.side,
                trade_side="open",
                order_type=order.order_type,
                amount=float(order.size),
                price=ref_price or 0,
                stop_loss=sl_price,
                take_profit=tp_price,
                margin_mode=order.margin_mode,
                leverage=order.leverage,
                status="open",
            ))
            await db.commit()
            logger.info(
                f"[MANUAL] Trade recorded: {order.side} {order.size} {symbol_display} "
                f"SL={sl_price} TP={tp_price}"
            )

        return {"exchange": "bitget", "order": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/bitget/futures/order/{order_id}")
async def cancel_bitget_futures_order(
    order_id: str,
    symbol: str = Query(...),
    margin_coin: str = Query(...),
    product_type: str = Query("USDT-FUTURES"),
):
    """Cancel a futures order"""
    connector = _get_bitget_connector()
    try:
        result = await connector.cancel_futures_order(
            symbol=symbol,
            margin_coin=margin_coin,
            order_id=order_id,
            product_type=product_type,
        )
        return {"exchange": "bitget", "order_id": order_id, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bitget/futures/open-orders")
async def get_bitget_futures_open_orders(
    product_type: str = Query("USDT-FUTURES"),
    symbol: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Get all open futures orders with TP/SL plan data merged in"""
    connector = _get_bitget_connector()
    try:
        orders = await connector.get_futures_open_orders(
            product_type=product_type,
            symbol=symbol,
        )
    except Exception as e:
        logger.warning(f"Failed to get Bitget futures open orders: {e}")
        return {"exchange": "bitget", "orders": [], "error": str(e)}

    # Cross-reference with Trade DB for SL/TP values
    try:
        order_ids = [o.get("orderId", "") for o in orders if o.get("orderId")]
        if order_ids:
            result = await db.execute(
                select(Trade).where(Trade.exchange_order_id.in_(order_ids))
            )
            db_trades = {t.exchange_order_id: t for t in result.scalars().all()}
            for order in orders:
                oid = order.get("orderId", "")
                if oid in db_trades:
                    trade = db_trades[oid]
                    if trade.stop_loss and (not order.get("presetStopLossPrice") or order["presetStopLossPrice"] in ("", "0")):
                        order["stopLoss"] = str(trade.stop_loss)
                    if trade.take_profit and (not order.get("presetStopSurplusPrice") or order["presetStopSurplusPrice"] in ("", "0")):
                        order["takeProfit"] = str(trade.take_profit)
    except Exception:
        pass

    return {"exchange": "bitget", "orders": orders}


class SetLeverageRequest(BaseModel):
    """Set leverage request"""
    symbol: str
    margin_coin: str
    leverage: int
    hold_side: str = "long"  # long, short
    product_type: str = "USDT-FUTURES"


@router.post("/bitget/futures/set-leverage")
async def set_bitget_leverage(request: SetLeverageRequest):
    """Set leverage for a futures symbol"""
    connector = _get_bitget_connector()
    try:
        result = await connector.set_leverage(
            symbol=request.symbol,
            margin_coin=request.margin_coin,
            leverage=request.leverage,
            hold_side=request.hold_side,
            product_type=request.product_type,
        )
        return {
            "exchange": "bitget",
            "symbol": request.symbol,
            "leverage": request.leverage,
            "result": result,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class SetMarginModeRequest(BaseModel):
    """Set margin mode request"""
    symbol: str
    margin_coin: str
    margin_mode: str  # crossed, isolated
    product_type: str = "USDT-FUTURES"


@router.post("/bitget/futures/set-margin-mode")
async def set_bitget_margin_mode(request: SetMarginModeRequest):
    """Set margin mode (cross/isolated) for a futures symbol"""
    connector = _get_bitget_connector()
    try:
        result = await connector.set_margin_mode(
            symbol=request.symbol,
            margin_coin=request.margin_coin,
            margin_mode=request.margin_mode,
            product_type=request.product_type,
        )
        return {
            "exchange": "bitget",
            "symbol": request.symbol,
            "margin_mode": request.margin_mode,
            "result": result,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/bitget/futures/contracts")
async def get_bitget_futures_contracts(
    product_type: str = Query("USDT-FUTURES", description="USDT-FUTURES, COIN-FUTURES, USDC-FUTURES")
):
    """Get all available futures contracts/pairs"""
    connector = _get_bitget_connector()
    try:
        result = await connector.native_client.get_futures_contracts(product_type=product_type)
        contracts = result.get("data", [])
        return {
            "exchange": "bitget",
            "product_type": product_type,
            "contracts": contracts,
            "count": len(contracts),
        }
    except Exception as e:
        logger.warning(f"Failed to get Bitget futures contracts: {e}")
        return {"exchange": "bitget", "product_type": product_type, "contracts": [], "count": 0, "error": str(e)}


class PairSettingsItem(BaseModel):
    """Settings for a single trading pair"""
    symbol: str
    margin_coin: str = "USDT"
    leverage: int = 10
    margin_mode: str = "crossed"  # crossed, isolated
    product_type: str = "USDT-FUTURES"


class BatchTradingSettingsRequest(BaseModel):
    """Batch apply trading settings for multiple pairs"""
    pairs: list[PairSettingsItem]


@router.post("/bitget/futures/batch-settings")
async def apply_batch_trading_settings(request: BatchTradingSettingsRequest):
    """Apply leverage and margin mode for multiple pairs in one call"""
    connector = _get_bitget_connector()
    results = []

    for pair in request.pairs:
        pair_result = {"symbol": pair.symbol, "errors": []}

        # Set margin mode first (must be set before leverage in some cases)
        try:
            margin_res = await connector.set_margin_mode(
                symbol=pair.symbol,
                margin_coin=pair.margin_coin,
                margin_mode=pair.margin_mode,
                product_type=pair.product_type,
            )
            pair_result["margin_mode"] = {"status": "success", "mode": pair.margin_mode, "result": margin_res}
        except Exception as e:
            pair_result["margin_mode"] = {"status": "error", "error": str(e)}
            pair_result["errors"].append(f"Margin mode: {e}")

        # Set leverage for long side
        try:
            lev_long = await connector.set_leverage(
                symbol=pair.symbol,
                margin_coin=pair.margin_coin,
                leverage=pair.leverage,
                hold_side="long",
                product_type=pair.product_type,
            )
            pair_result["leverage_long"] = {"status": "success", "leverage": pair.leverage, "result": lev_long}
        except Exception as e:
            pair_result["leverage_long"] = {"status": "error", "error": str(e)}
            pair_result["errors"].append(f"Leverage long: {e}")

        # Set leverage for short side
        try:
            lev_short = await connector.set_leverage(
                symbol=pair.symbol,
                margin_coin=pair.margin_coin,
                leverage=pair.leverage,
                hold_side="short",
                product_type=pair.product_type,
            )
            pair_result["leverage_short"] = {"status": "success", "leverage": pair.leverage, "result": lev_short}
        except Exception as e:
            pair_result["leverage_short"] = {"status": "error", "error": str(e)}
            pair_result["errors"].append(f"Leverage short: {e}")

        pair_result["success"] = len(pair_result["errors"]) == 0
        results.append(pair_result)

    all_success = all(r["success"] for r in results)
    return {
        "exchange": "bitget",
        "success": all_success,
        "results": results,
    }


@router.get("/bitget/available-pairs")
async def get_bitget_available_pairs(
    quote: str = Query("USDT", description="Quote currency filter"),
):
    """
    Get ALL available trading pairs from Bitget (spot + futures) with
    status, delisting dates, leverage limits, and adjustment info.
    """
    connector = _get_bitget_connector()
    pairs: list[dict] = []
    seen: set[str] = set()

    # 1. Spot symbols
    try:
        spot_res = await connector.native_client.get("/api/v2/spot/public/symbols", {})
        for s in spot_res.get("data", []):
            if s.get("quoteCoin") != quote:
                continue
            symbol = f"{s['baseCoin']}/{s['quoteCoin']}"
            status = s.get("status", "online")
            off_time = s.get("offTime", "")
            delisting_ts = int(off_time) if off_time and off_time not in ("", "-1", "0") else None
            entry = {
                "symbol": symbol,
                "baseCoin": s["baseCoin"],
                "quoteCoin": s["quoteCoin"],
                "market": "spot",
                "status": status,
                "delisting_ts": delisting_ts,
                "delisting_date": _ts_to_iso(delisting_ts),
                "minLever": None,
                "maxLever": None,
                "symbolType": "spot",
            }
            pairs.append(entry)
            seen.add(symbol)
    except Exception as e:
        logger.warning(f"Failed to fetch spot symbols: {e}")

    # 2. Futures contracts 
    try:
        futures_res = await connector.native_client.get_futures_contracts(product_type=f"{quote}-FUTURES")
        for c in futures_res.get("data", []):
            base = c.get("baseCoin", "")
            symbol = f"{base}/{quote}"
            status = c.get("symbolStatus", "normal")
            off_time = c.get("offTime", "")
            delisting_ts = int(off_time) if off_time and off_time not in ("", "-1", "0") else None
            min_lever = int(c.get("minLever", 1) or 1)
            max_lever = int(c.get("maxLever", 125) or 125)
            maintain_time = c.get("maintainTime", "")
            limit_open_time = c.get("limitOpenTime", "")

            # If symbol already added from spot, merge futures info
            existing = next((p for p in pairs if p["symbol"] == symbol), None)
            if existing:
                existing["futures_status"] = status
                existing["minLever"] = min_lever
                existing["maxLever"] = max_lever
                existing["market"] = "both"
                if delisting_ts and not existing.get("delisting_ts"):
                    existing["delisting_ts"] = delisting_ts
                    existing["delisting_date"] = _ts_to_iso(delisting_ts)
                if status != "normal":
                    existing["futures_adjustment"] = status
                if maintain_time and maintain_time not in ("", "-1", "0"):
                    existing["maintain_time"] = _ts_to_iso(int(maintain_time))
                if limit_open_time and limit_open_time not in ("", "-1", "0"):
                    existing["limit_open_time"] = _ts_to_iso(int(limit_open_time))
            else:
                entry = {
                    "symbol": symbol,
                    "baseCoin": base,
                    "quoteCoin": quote,
                    "market": "futures",
                    "status": status,
                    "delisting_ts": delisting_ts,
                    "delisting_date": _ts_to_iso(delisting_ts),
                    "minLever": min_lever,
                    "maxLever": max_lever,
                    "symbolType": c.get("symbolType", "perpetual"),
                }
                if status != "normal":
                    entry["futures_adjustment"] = status
                if maintain_time and maintain_time not in ("", "-1", "0"):
                    entry["maintain_time"] = _ts_to_iso(int(maintain_time))
                if limit_open_time and limit_open_time not in ("", "-1", "0"):
                    entry["limit_open_time"] = _ts_to_iso(int(limit_open_time))
                pairs.append(entry)
                seen.add(symbol)
    except Exception as e:
        logger.warning(f"Failed to fetch futures contracts: {e}")

    # Sort: active first, then by baseCoin
    def sort_key(p):
        is_active = p["status"] in ("online", "normal") and not p.get("delisting_ts")
        return (0 if is_active else 1, p["baseCoin"])

    pairs.sort(key=sort_key)

    # Summary of delisting / adjustment warnings
    warnings = [
        {
            "symbol": p["symbol"],
            "status": p.get("futures_adjustment") or p["status"],
            "delisting_date": p.get("delisting_date"),
            "maintain_time": p.get("maintain_time"),
            "limit_open_time": p.get("limit_open_time"),
        }
        for p in pairs
        if p.get("delisting_ts") or p["status"] not in ("online", "normal")
        or p.get("futures_adjustment") or p.get("maintain_time") or p.get("limit_open_time")
    ]

    return {
        "exchange": "bitget",
        "total": len(pairs),
        "pairs": pairs,
        "warnings": warnings,
        "warnings_count": len(warnings),
    }


def _ts_to_iso(ts: int | None) -> str | None:
    """Convert millisecond timestamp to ISO date string in SAST."""
    if not ts or ts <= 0:
        return None
    from datetime import datetime, timezone, timedelta
    _SAST = timezone(timedelta(hours=2))
    return datetime.fromtimestamp(ts / 1000, tz=_SAST).strftime("%Y-%m-%d %H:%M SAST")


@router.get("/bitget/futures/account-summary")
async def get_bitget_futures_account_summary(
    product_type: str = Query("USDT-FUTURES"),
    db: AsyncSession = Depends(get_db),
):
    """
    Aggregated live futures account stats — mirrors sim dashboard:
    balance, equity, unrealized PnL, open positions count, total PnL.
    """
    connector = _get_bitget_connector()
    try:
        # Aggregate across ALL futures product types so a unified
        # (multi-assets) account's USDC- and COIN-margined balances and
        # positions are included, not just USDT-FUTURES.
        balance_data = await connector.get_all_futures_balances()
        positions_data = await connector.get_all_futures_positions()
    except Exception as e:
        # Return empty account on transient exchange errors instead of 500
        return {
            "exchange": "bitget",
            "balance": 0, "equity": 0, "unrealized_pnl": 0,
            "open_positions": [], "open_positions_count": 0,
            "total_pnl": 0, "total_pnl_pct": 0,
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0, "win_rate": 0,
            "asset_mode": None, "unified": False,
            "error": str(e),
        }

    # Detect asset mode (union = unified/multi-assets, single = classic).
    asset_mode: Optional[str] = None
    for b in (balance_data or []):
        if b.get("assetMode"):
            asset_mode = b.get("assetMode")
            break

    # Parse balance. Per-margin-coin fields (available, usdtEquity,
    # unrealizedPL) are summed across rows/product types. Union account-wide
    # fields (crossedRiskRate, unionMm) repeat on every row in multi-assets
    # mode, so take the max instead of summing to avoid double-counting.
    available = 0.0
    equity = 0.0
    unrealized_pl = 0.0
    crossed_risk_rate = 0.0
    maintenance_margin = 0.0
    for b in (balance_data or []):
        available += float(b.get("available", 0) or 0)
        equity += float(b.get("usdtEquity") or b.get("equity") or b.get("accountEquity") or 0)
        unrealized_pl += float(b.get("unrealizedPL", 0) or 0)
        crossed_risk_rate = max(crossed_risk_rate, float(b.get("crossedRiskRate", 0) or 0))
        maintenance_margin = max(maintenance_margin, float(b.get("unionMm", 0) or 0))

    # Parse positions
    open_positions = []
    total_position_unrealized = 0.0
    for p in (positions_data or []):
        total_amt = float(p.get("total", 0))
        if total_amt <= 0:
            continue
        pos_pnl = float(p.get("unrealizedPL", 0))
        # openPriceAvg = actual average entry price (what user paid)
        # breakEvenPrice = entry adjusted for fees (not the real entry)
        entry_price = float(p.get("openPriceAvg", 0))
        break_even_price = float(p.get("breakEvenPrice") or entry_price)
        mark_price = float(p.get("markPrice", 0))
        leverage_val = float(p.get("leverage", 1))
        margin_size = float(p.get("marginSize", 0))

        # Bitget unrealizedPL formula uses breakEvenPrice (includes fees):
        # Long: (markPrice - breakEvenPrice) * total
        # Short: (breakEvenPrice - markPrice) * total
        # If Bitget returns 0, calculate locally as fallback
        hold_side = p.get("holdSide", "")
        if pos_pnl == 0 and break_even_price > 0 and mark_price > 0:
            if hold_side == "long":
                pos_pnl = (mark_price - break_even_price) * total_amt
            elif hold_side == "short":
                pos_pnl = (break_even_price - mark_price) * total_amt

        # ROE% = unrealizedPL / initialMargin * 100
        # initialMargin = notionalValue / leverage = entry_price * amount / leverage
        notional = entry_price * total_amt
        initial_margin = margin_size if margin_size > 0 else (notional / leverage_val if leverage_val > 0 else notional)
        roe_pct = (pos_pnl / initial_margin * 100) if initial_margin > 0 else 0.0

        total_position_unrealized += pos_pnl
        open_positions.append({
            "symbol": p.get("symbol", ""),
            "side": hold_side,
            "amount": total_amt,
            "entry_price": entry_price,
            "break_even_price": break_even_price,
            "current_price": mark_price,
            "unrealized_pnl": round(pos_pnl, 4),
            "unrealized_roe_pct": round(roe_pct, 2),
            "leverage": p.get("leverage", "1"),
            "margin_mode": p.get("marginMode", "crossed"),
            "margin_size": margin_size,
            "initial_margin": round(initial_margin, 2),
            "liquidation_price": float(p.get("liquidationPrice", 0)),
            "product_type": p.get("product_type", "USDT-FUTURES"),
            "margin_coin": p.get("marginCoin", ""),
        })

    # Cross-reference Trade DB for SL/TP on open positions
    try:
        result = await db.execute(
            select(Trade).where(
                Trade.exchange == "bitget",
                Trade.status == "open",
                Trade.trade_side == "open",
            )
        )
        tracked_trades = result.scalars().all()
        # Build lookup: (bitget_symbol, side) → (stop_loss, take_profit)
        sl_tp_map = {}
        for t in tracked_trades:
            bitget_sym = t.symbol.replace("/", "")
            side_map = {"buy": "long", "sell": "short"}
            hold = side_map.get(t.side, t.side)
            sl_tp_map[(bitget_sym, hold)] = (t.stop_loss, t.take_profit)

        for pos in open_positions:
            key = (pos["symbol"], pos["side"])
            if key in sl_tp_map:
                pos["stop_loss"] = sl_tp_map[key][0]
                pos["take_profit"] = sl_tp_map[key][1]
            else:
                pos["stop_loss"] = None
                pos["take_profit"] = None
    except Exception as e:
        logger.warning(f"Could not fetch SL/TP from Trade DB: {e}")
        for pos in open_positions:
            pos["stop_loss"] = None
            pos["take_profit"] = None

    # Fallback: query Bitget pending TPSL plan orders for positions
    # without SL/TP from the Trade DB
    positions_needing_tpsl = [
        p for p in open_positions
        if p.get("stop_loss") is None or p.get("take_profit") is None
    ]
    if positions_needing_tpsl:
        try:
            all_tpsl = await connector.get_pending_tpsl_orders()
            # Build lookup: (symbol, holdSide) → {"sl": price, "tp": price}
            tpsl_map: Dict[tuple, Dict[str, float]] = {}
            for order in (all_tpsl or []):
                sym = (order.get("symbol") or "").upper()
                side = (order.get("posSide") or order.get("holdSide") or "").lower()
                plan_type = (order.get("planType") or "").lower()
                trigger = float(order.get("triggerPrice") or 0)
                if not sym or trigger <= 0:
                    continue
                k = (sym, side)
                if k not in tpsl_map:
                    tpsl_map[k] = {}
                # Prefer pos_loss/pos_profit (position-level) over loss_plan/profit_plan
                if plan_type in ("loss_plan", "pos_loss"):
                    existing_type = tpsl_map[k].get("sl_type", "")
                    if existing_type != "pos_loss" or plan_type == "pos_loss":
                        tpsl_map[k]["sl"] = trigger
                        tpsl_map[k]["sl_type"] = plan_type
                elif plan_type in ("profit_plan", "pos_profit"):
                    existing_type = tpsl_map[k].get("tp_type", "")
                    if existing_type != "pos_profit" or plan_type == "pos_profit":
                        tpsl_map[k]["tp"] = trigger
                        tpsl_map[k]["tp_type"] = plan_type

            for pos in positions_needing_tpsl:
                key = (pos["symbol"], pos["side"])
                if key in tpsl_map:
                    if pos.get("stop_loss") is None:
                        pos["stop_loss"] = tpsl_map[key].get("sl")
                    if pos.get("take_profit") is None:
                        pos["take_profit"] = tpsl_map[key].get("tp")
        except Exception as e:
            logger.warning(f"Could not fetch TPSL plan orders from Bitget: {e}")

    # ── Auto-reconcile: close DB trades whose Bitget positions no longer exist ──
    try:
        # Build set of currently open positions on Bitget: (bitget_symbol, holdSide)
        live_position_keys = set()
        for pos in open_positions:
            live_position_keys.add((pos["symbol"], pos["side"]))

        reconcile_result = await db.execute(
            select(Trade).where(
                Trade.exchange == "bitget",
                Trade.status == "open",
                Trade.trade_side == "open",
            )
        )
        stale_reconciled = 0
        for t in reconcile_result.scalars().all():
            bitget_sym = t.symbol.replace("/", "")
            side_map = {"buy": "long", "sell": "short"}
            hold = side_map.get(t.side, t.side)
            if (bitget_sym, hold) not in live_position_keys:
                # Position no longer exists on exchange — look up actual close fill
                since_ts = int(t.created_at.timestamp() * 1000) if t.created_at else None
                fill = await connector.lookup_close_fill(
                    symbol=t.symbol, hold_side=hold, since_ts_ms=since_ts,
                )
                t.status = "closed"
                t.closed_at = t.closed_at or _utcnow()
                if fill:
                    t.pnl = fill["pnl"]
                    t.average_price = fill["exit_price"] or t.average_price
                    t.filled_amount = fill["fill_size"] or t.filled_amount
                    logger.info(
                        f"[Reconcile] {t.symbol} {hold} closed — "
                        f"PnL: {fill['pnl']:.4f}, exit: {fill['exit_price']}, "
                        f"source: {fill['order_source']}"
                    )
                elif t.pnl is None:
                    t.pnl = 0.0
                    logger.warning(f"[Reconcile] {t.symbol} {hold} no fill found, PnL set to 0")
                stale_reconciled += 1
        if stale_reconciled > 0:
            await db.commit()
            logger.info(f"[Reconcile] Auto-closed {stale_reconciled} stale DB trade(s)")
    except Exception as e:
        logger.warning(f"Could not reconcile stale trades: {e}")

    # ── Compute reserved margin (sum of margin on open positions) ──
    reserved_margin = sum(p["margin_size"] for p in open_positions)

    # ── Fetch settings ──
    settings_data = {}
    try:
        settings_result = await db.execute(select(LiveTradeSettings).limit(1))
        settings_row = settings_result.scalars().first()
        if settings_row:
            settings_data = {
                "is_active": settings_row.is_active,
                "auto_trade": settings_row.auto_trade,
                "dry_run": settings_row.dry_run,
                "auto_trade_pairs": settings_row.auto_trade_pairs,
                "auto_trade_timeframe": settings_row.auto_trade_timeframe or "1h",
                "auto_trade_max_positions": settings_row.auto_trade_max_positions or 3,
                "auto_trade_risk_pct": settings_row.auto_trade_risk_pct or 1.0,
                "auto_trade_mode": settings_row.auto_trade_mode or "futures",
                "auto_trade_leverage": settings_row.auto_trade_leverage or 10,
                "auto_trade_margin_mode": settings_row.auto_trade_margin_mode or "crossed",
            }
    except Exception as e:
        logger.warning(f"Could not fetch LiveTradeSettings: {e}")

    # ── Compute trade stats from closed trades in DB ──
    total_trades = 0
    winning_trades = 0
    losing_trades = 0
    total_pnl = 0.0
    try:
        closed_q = select(Trade).where(
            Trade.exchange == "bitget",
            Trade.status == "closed",
            Trade.trade_side == "open",  # only original entries, skip duplicate close records
        )
        closed_result = await db.execute(closed_q)
        closed_trades = closed_result.scalars().all()
        for ct in closed_trades:
            pnl_val = float(ct.pnl or 0)
            total_pnl += pnl_val
            total_trades += 1
            if pnl_val > 0:
                winning_trades += 1
            elif pnl_val < 0:
                losing_trades += 1
    except Exception as e:
        logger.warning(f"Could not compute trade stats from DB: {e}")

    return {
        "balance": available,
        "equity": equity,
        "unrealized_pnl": total_position_unrealized,
        "open_positions_count": len(open_positions),
        "open_positions": open_positions,
        "reserved_margin": round(reserved_margin, 2),
        "mmr": round(crossed_risk_rate * 100, 2),
        "maintenance_margin": round(maintenance_margin, 2),
        "total_pnl": round(total_pnl, 2),
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "settings": settings_data,
        "product_type": product_type,
        "asset_mode": asset_mode,
        "unified": asset_mode == "union",
    }


# ─── Live Close Position ──────────────────────────────────────

class ClosePositionRequest(BaseModel):
    symbol: str
    side: str  # "long" or "short"
    amount: Optional[str] = None  # if None, close entire position
    product_type: str = "USDT-FUTURES"
    margin_coin: str = "USDT"


@router.post("/bitget/futures/close-position")
async def close_live_position(
    req: ClosePositionRequest,
    db: AsyncSession = Depends(get_db),
):
    """Close a specific live futures position by placing a close order."""
    connector = _get_bitget_connector()
    try:
        # Get current position to determine amount
        positions = await connector.get_futures_positions(product_type=req.product_type)
        target = None
        for p in (positions or []):
            if (
                p.get("symbol", "") == req.symbol
                and p.get("holdSide", "") == req.side
                and float(p.get("total", 0)) > 0
            ):
                target = p
                break

        if not target:
            raise HTTPException(
                status_code=404,
                detail=f"No open {req.side} position for {req.symbol}",
            )

        close_amount = req.amount or str(target.get("total", "0"))
        close_side = "sell" if req.side == "long" else "buy"

        result = await connector.create_futures_order(
            symbol=req.symbol,
            margin_coin=req.margin_coin,
            side=close_side,
            order_type="market",
            size=close_amount,
            margin_mode=target.get("marginMode", "crossed"),
            leverage=int(float(target.get("leverage", 1))),
            trade_side="close",
            product_type=req.product_type,
        )

        # Use unrealizedPL as initial estimate, then try to get actual fill data
        pnl = float(target.get("unrealizedPL", 0))
        exit_price = float(target.get("markPrice") or target.get("marketPrice") or 0)

        # Look up actual fill from exchange for real PnL and exit price
        try:
            import asyncio
            await asyncio.sleep(0.5)  # brief delay for exchange to process
            fill = await connector.lookup_close_fill(
                symbol=req.symbol.replace("USDT", "/USDT").replace("USDC", "/USDC"),
                hold_side=req.side,
                product_type=req.product_type,
            )
            if fill and fill["pnl"] != 0:
                pnl = fill["pnl"]
                exit_price = fill["exit_price"] or exit_price
        except Exception as e:
            logger.warning(f"Could not look up close fill for {req.symbol}: {e}")

        # Mark Trade DB record as closed + update stats
        try:
            bitget_sym_clean = req.symbol.replace("USDT", "/USDT").replace("USDC", "/USDC")
            trade_side_map = {"long": "buy", "short": "sell"}
            db_result = await db.execute(
                select(Trade).where(
                    Trade.exchange == "bitget",
                    Trade.symbol == bitget_sym_clean,
                    Trade.side == trade_side_map.get(req.side, req.side),
                    Trade.status == "open",
                ).order_by(Trade.created_at.desc())
            )
            open_trades = db_result.scalars().all()
            per_trade_pnl = pnl / len(open_trades) if open_trades else pnl
            for t in open_trades:
                t.status = "closed"
                t.closed_at = _utcnow()
                t.pnl = per_trade_pnl
                t.average_price = exit_price if exit_price > 0 else None

            # Update LiveTradeSettings counters
            settings_res = await db.execute(select(LiveTradeSettings).limit(1))
            settings_row = settings_res.scalars().first()
            if settings_row and open_trades:
                settings_row.total_trades = (settings_row.total_trades or 0) + 1
                if pnl > 0:
                    settings_row.winning_trades = (settings_row.winning_trades or 0) + 1
                elif pnl < 0:
                    settings_row.losing_trades = (settings_row.losing_trades or 0) + 1
            await db.commit()
        except Exception as e:
            logger.warning(f"Failed to update Trade DB on close: {e}")

        return {
            "success": True,
            "symbol": req.symbol,
            "side": req.side,
            "amount": close_amount,
            "pnl": round(pnl, 4),
            "order": result,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bitget/futures/close-all-positions")
async def close_all_live_positions(
    product_type: str = Query("USDT-FUTURES"),
    db: AsyncSession = Depends(get_db),
):
    """Close all open live futures positions."""
    connector = _get_bitget_connector()
    try:
        positions = await connector.get_futures_positions(product_type=product_type)
        open_positions = [
            p for p in (positions or [])
            if float(p.get("total", 0)) > 0
        ]

        if not open_positions:
            return {"success": True, "closed": 0, "message": "No open positions"}

        results = []
        total_pnl = 0.0
        for p in open_positions:
            symbol = p.get("symbol", "")
            hold_side = p.get("holdSide", "")
            amount = str(p.get("total", "0"))
            close_side = "sell" if hold_side == "long" else "buy"
            pnl = float(p.get("unrealizedPL", 0))

            try:
                result = await connector.create_futures_order(
                    symbol=symbol,
                    margin_coin="USDT",
                    side=close_side,
                    order_type="market",
                    size=amount,
                    margin_mode=p.get("marginMode", "crossed"),
                    leverage=int(float(p.get("leverage", 1))),
                    trade_side="close",
                    product_type=product_type,
                )
                results.append({
                    "symbol": symbol,
                    "side": hold_side,
                    "pnl": round(pnl, 4),
                    "order": result,
                })
                total_pnl += pnl
            except Exception as e:
                results.append({
                    "symbol": symbol,
                    "side": hold_side,
                    "error": str(e),
                })

        # Mark all Trade DB records as closed + update stats
        try:
            # Brief delay for exchange to process fills
            import asyncio
            await asyncio.sleep(0.5)

            # Build a pnl/exit lookup from exchange close fills
            pnl_lookup: dict[tuple, dict] = {}
            for r in results:
                if "order" not in r:
                    continue
                sym = r["symbol"]
                side = r["side"]
                ccxt_sym = sym.replace("USDT", "/USDT").replace("USDC", "/USDC")
                fill = await connector.lookup_close_fill(
                    symbol=ccxt_sym, hold_side=side, product_type=product_type,
                )
                if fill and fill["pnl"] != 0:
                    pnl_lookup[(sym, side)] = {"pnl": fill["pnl"], "exit_price": fill["exit_price"]}
                else:
                    pnl_lookup[(sym, side)] = {"pnl": r.get("pnl", 0), "exit_price": 0}

            db_result = await db.execute(
                select(Trade).where(
                    Trade.exchange == "bitget",
                    Trade.status == "open",
                )
            )
            closed_count = 0
            win_count = 0
            loss_count = 0
            total_pnl = 0.0  # recalculate from actual fills
            for t in db_result.scalars().all():
                t.status = "closed"
                t.closed_at = _utcnow()
                bitget_sym = t.symbol.replace("/", "")
                side_map = {"buy": "long", "sell": "short"}
                hold = side_map.get(t.side, t.side)
                matched = pnl_lookup.get((bitget_sym, hold), {"pnl": 0, "exit_price": 0})
                t.pnl = matched["pnl"]
                if matched["exit_price"]:
                    t.average_price = matched["exit_price"]
                total_pnl += matched["pnl"]
                closed_count += 1
                if matched["pnl"] > 0:
                    win_count += 1
                elif matched["pnl"] < 0:
                    loss_count += 1

            # Update LiveTradeSettings counters
            if closed_count > 0:
                settings_res = await db.execute(select(LiveTradeSettings).limit(1))
                s = settings_res.scalars().first()
                if s:
                    s.total_trades = (s.total_trades or 0) + closed_count
                    s.winning_trades = (s.winning_trades or 0) + win_count
                    s.losing_trades = (s.losing_trades or 0) + loss_count

            await db.commit()
        except Exception as e:
            logger.warning(f"Failed to update Trade DB on close-all: {e}")

        return {
            "success": True,
            "closed": len([r for r in results if "order" in r]),
            "total_pnl": round(total_pnl, 4),
            "results": results,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Live Trade History ───────────────────────────────────────

@router.get("/bitget/futures/trade-history")
async def get_live_trade_history(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
):
    """Get closed trade history from our Trade DB."""
    result = await db.execute(
        select(Trade).where(
            Trade.exchange == "bitget",
            Trade.status == "closed",
            Trade.trade_side == "open",  # only original entries, not duplicate close records
        ).order_by(Trade.closed_at.desc()).limit(limit)
    )
    trades = result.scalars().all()
    return {
        "trades": [
            {
                "id": t.id,
                "symbol": t.symbol,
                "side": t.side,
                "trade_side": t.trade_side,
                "order_type": t.order_type,
                "amount": t.amount,
                "price": t.price,
                "average_price": t.average_price,
                "stop_loss": t.stop_loss,
                "take_profit": t.take_profit,
                "margin_mode": t.margin_mode,
                "leverage": t.leverage,
                "pnl": t.pnl,
                "pnl_percentage": t.pnl_percentage,
                "status": t.status,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            }
            for t in trades
        ],
        "count": len(trades),
    }


@router.get("/bitget/futures/order-history")
async def get_bitget_futures_order_history(
    product_type: str = Query("USDT-FUTURES"),
    symbol: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """Get historical (filled/cancelled) futures orders from Bitget."""
    connector = _get_bitget_connector()
    try:
        orders = await connector.get_futures_history_orders(
            product_type=product_type,
            symbol=symbol,
            limit=limit,
        )
        return {"orders": orders, "count": len(orders)}
    except Exception as e:
        logger.warning(f"Failed to get Bitget futures order history: {e}")
        return {"orders": [], "count": 0, "error": str(e)}


@router.post("/bitget/futures/backfill-closed-pnl")
async def backfill_closed_pnl(
    force: bool = Query(False, description="Re-backfill all trades, not just zero-PnL ones"),
    db: AsyncSession = Depends(get_db),
):
    """One-time backfill: look up real PnL from Bitget order history for
    closed trades that have pnl=0 or missing exit price."""
    connector = _get_bitget_connector()

    # Get all closed trades with zero/null PnL or missing exit price
    result = await db.execute(
        select(Trade).where(
            Trade.exchange == "bitget",
            Trade.status == "closed",
            Trade.trade_side == "open",
        ).order_by(Trade.created_at.desc())
    )
    trades = result.scalars().all()
    if not trades:
        return {"message": "No closed trades to backfill", "updated": 0}

    updated = 0
    errors = 0
    details = []

    # Group trades by (symbol, side, closed_at date) so DCA entries sharing the
    # same close fill split the PnL correctly instead of each getting the full amount
    from collections import defaultdict
    groups: dict[tuple, list] = defaultdict(list)
    for t in trades:
        needs_backfill = force or (t.pnl is None or t.pnl == 0.0 or t.average_price is None)
        if not needs_backfill:
            continue
        close_date = t.closed_at.isoformat()[:10] if t.closed_at else ""
        groups[(t.symbol, t.side, close_date)].append(t)

    for (symbol, side, _), group in groups.items():
        side_map = {"buy": "long", "sell": "short"}
        hold = side_map.get(side, side)
        earliest = min(t.created_at for t in group if t.created_at)
        since_ts = int(earliest.timestamp() * 1000) if earliest else None

        try:
            fill = await connector.lookup_close_fill(
                symbol=symbol, hold_side=hold, since_ts_ms=since_ts,
            )
            if fill and (fill["pnl"] != 0 or fill["exit_price"] > 0):
                # Split PnL across DCA entries in the same close group
                per_trade_pnl = fill["pnl"] / len(group)
                for t in group:
                    old_pnl = t.pnl
                    t.pnl = per_trade_pnl
                    t.average_price = fill["exit_price"] or t.average_price
                    t.filled_amount = fill["fill_size"] or t.filled_amount
                    updated += 1
                details.append({
                    "symbol": symbol,
                    "side": side,
                    "old_pnl": 0.0,
                    "new_pnl": fill["pnl"],
                    "per_trade_pnl": per_trade_pnl,
                    "entries_in_group": len(group),
                    "exit_price": fill["exit_price"],
                    "source": fill["order_source"],
                })
            else:
                details.append({
                    "symbol": symbol,
                    "side": side,
                    "old_pnl": 0.0,
                    "status": "no_fill_found",
                })
        except Exception as e:
            errors += 1
            details.append({"symbol": symbol, "error": str(e)})

    if updated > 0:
        await db.commit()

    return {
        "message": f"Backfilled {updated} trade(s), {errors} error(s)",
        "updated": updated,
        "errors": errors,
        "details": details,
    }
