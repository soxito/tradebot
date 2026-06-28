"""
Bitget Exchange Connector
Uses official Bitget v2 API signing for authenticated endpoints (balance, orders)
and ccxt for public data (OHLCV, tickers, markets).
"""
import time
import ccxt.async_support as ccxt
from typing import Dict, Any, Optional, List, Tuple
from app.exchanges.base import ExchangeConnector, OrderSide, OrderType
from app.exchanges.bitget_sdk import BitgetClient, BitgetAPIError
from loguru import logger


class BitgetConnector(ExchangeConnector):
    """Bitget exchange connector with native v2 API support"""

    # Class-level cache for leverage limits: {symbol: (min_lever, max_lever)}
    _leverage_cache: Dict[str, Tuple[int, int]] = {}
    _leverage_cache_ts: float = 0
    _LEVERAGE_CACHE_TTL = 3600  # 1 hour

    # Class-level cache for contract precision: {symbol: {pricePlace, volumePlace, priceEndStep, sizeMultiplier, minTradeNum}}
    _precision_cache: Dict[str, Dict[str, Any]] = {}
    _precision_cache_ts: float = 0

    def __init__(self, api_key: str, api_secret: str, passphrase: Optional[str] = None, testnet: bool = True):
        self.native_client: Optional[BitgetClient] = None
        super().__init__(api_key, api_secret, passphrase, testnet)

    @property
    def exchange_name(self) -> str:
        return "Bitget"

    def _initialize_exchange(self) -> None:
        """Initialize both ccxt (public data) and native SDK (authenticated)"""
        # ccxt for public endpoints — use swap (futures) since the platform trades futures
        config = {
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "password": self.passphrase,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
        self.exchange = ccxt.bitget(config)

        # Native SDK client for authenticated endpoints (balance, orders)
        if self.passphrase:
            self.native_client = BitgetClient(
                api_key=self.api_key,
                api_secret=self.api_secret,
                passphrase=self.passphrase,
            )
            logger.info(f"[{self.exchange_name}] Native v2 API client initialized")

        logger.info(f"[{self.exchange_name}] Connector initialized (ccxt + native SDK)")

    @staticmethod
    def _to_swap_symbol(symbol: str) -> str:
        """Convert BTC/USDT -> BTC/USDT:USDT for ccxt swap market lookups."""
        if ":" in symbol:
            return symbol
        for quote in ("USDT", "USDC"):
            if symbol.endswith(f"/{quote}"):
                return f"{symbol}:{quote}"
        return symbol

    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """Get ticker — auto-converts to swap symbol format."""
        swap_sym = self._to_swap_symbol(symbol)
        try:
            ticker = await self.exchange.fetch_ticker(swap_sym)
            return ticker
        except Exception as e:
            logger.error(f"[{self.exchange_name}] Error fetching ticker for {swap_sym}: {e}")
            raise

    async def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 100, since=None):
        """Get OHLCV — auto-converts to swap symbol format."""
        swap_sym = self._to_swap_symbol(symbol)
        try:
            return await self.exchange.fetch_ohlcv(
                symbol=swap_sym, timeframe=timeframe, limit=limit, since=since,
            )
        except Exception as e:
            logger.error(f"[{self.exchange_name}] Error fetching OHLCV for {swap_sym}: {e}")
            raise

    async def get_balance(self, currency: Optional[str] = None) -> Dict[str, Any]:
        """Get account balance using native Bitget v2 API"""
        if not self.native_client:
            return await super().get_balance(currency)

        try:
            result = await self.native_client.get_account_assets(coin=currency)
            assets = result.get("data", [])

            # Convert to ccxt-compatible format for UI compatibility
            balance: Dict[str, Any] = {"info": assets, "timestamp": None, "datetime": None}
            for asset in assets:
                coin = asset.get("coin", "")
                available = float(asset.get("available", 0))
                frozen = float(asset.get("frozen", 0))
                locked = float(asset.get("locked", 0))
                total = available + frozen + locked
                balance[coin] = {
                    "free": available,
                    "used": frozen + locked,
                    "total": total,
                }

            if currency:
                coin_data = balance.get(currency, {"free": 0, "used": 0, "total": 0})
                return {
                    "currency": currency,
                    "free": coin_data["free"],
                    "used": coin_data["used"],
                    "total": coin_data["total"],
                }

            return balance

        except BitgetAPIError as e:
            logger.error(f"[{self.exchange_name}] Native API balance error: {e}")
            raise
        except Exception as e:
            logger.error(f"[{self.exchange_name}] Balance error, trying ccxt fallback: {e}")
            return await super().get_balance(currency)

    async def create_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        amount: float,
        price: Optional[float] = None,
        params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Create order using native Bitget v2 API"""
        if not self.native_client:
            return await super().create_order(symbol, side, order_type, amount, price, params)

        try:
            # Convert symbol format: BTC/USDT -> BTCUSDT
            bitget_symbol = symbol.replace("/", "")

            logger.info(
                f"[{self.exchange_name}] Creating {side.value} {order_type.value} "
                f"order: {amount} {symbol} @ {price or 'market'}"
            )

            result = await self.native_client.place_order(
                symbol=bitget_symbol,
                side=side.value,
                order_type="limit" if order_type == OrderType.LIMIT else "market",
                size=str(amount),
                price=str(price) if price else None,
            )

            order_data = result.get("data", {})
            logger.info(f"[{self.exchange_name}] Order created: {order_data.get('orderId', 'unknown')}")
            return order_data

        except BitgetAPIError as e:
            logger.error(f"[{self.exchange_name}] Native API order error: {e}")
            raise
        except Exception as e:
            logger.error(f"[{self.exchange_name}] Order error: {e}")
            raise

    async def cancel_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        """Cancel order using native Bitget v2 API"""
        if not self.native_client:
            return await super().cancel_order(order_id, symbol)

        try:
            bitget_symbol = symbol.replace("/", "")
            result = await self.native_client.cancel_order(
                symbol=bitget_symbol,
                order_id=order_id,
            )
            logger.info(f"[{self.exchange_name}] Order {order_id} cancelled")
            return result.get("data", {})

        except BitgetAPIError as e:
            logger.error(f"[{self.exchange_name}] Cancel order error: {e}")
            raise

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get open orders using native Bitget v2 API"""
        if not self.native_client:
            return await super().get_open_orders(symbol)

        try:
            bitget_symbol = symbol.replace("/", "") if symbol else None
            result = await self.native_client.get_unfilled_orders(symbol=bitget_symbol)
            return result.get("data", [])

        except BitgetAPIError as e:
            logger.error(f"[{self.exchange_name}] Open orders error: {e}")
            raise

    async def get_account_info(self) -> Dict[str, Any]:
        """Get Bitget account info (native SDK only)"""
        if not self.native_client:
            raise RuntimeError("Native client not initialized")
        result = await self.native_client.get_account_info()
        return result.get("data", {})

    async def get_trade_fills(self, symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Get trade fills/history (native SDK only)"""
        if not self.native_client:
            raise RuntimeError("Native client not initialized")
        bitget_symbol = symbol.replace("/", "")
        result = await self.native_client.get_fills(symbol=bitget_symbol, limit=limit)
        return result.get("data", [])

    async def get_order_history(self, symbol: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Get order history (native SDK only)"""
        if not self.native_client:
            raise RuntimeError("Native client not initialized")
        bitget_symbol = symbol.replace("/", "")
        result = await self.native_client.get_history_orders(symbol=bitget_symbol, limit=limit)
        return result.get("data", [])

    async def close(self) -> None:
        """Close all connections"""
        if self.native_client:
            await self.native_client.close()
        if self.exchange:
            await self.exchange.close()

    async def lookup_close_fill(
        self,
        symbol: str,
        hold_side: str,
        since_ts_ms: Optional[int] = None,
        product_type: str = "USDT-FUTURES",
    ) -> Optional[Dict[str, Any]]:
        """Look up the most recent close fill from Bitget order history.

        Returns dict with keys: pnl, exit_price, fill_size, order_source, order_id
        or None if no close fill found.
        """
        if not self.native_client:
            return None
        try:
            bitget_sym = symbol.replace("/", "")
            params: dict = {"productType": product_type, "symbol": bitget_sym, "limit": "50"}
            if since_ts_ms:
                params["startTime"] = str(since_ts_ms)
            result = await self.native_client.get("/api/v2/mix/order/orders-history", params)
            orders = result.get("data", {}).get("entrustedList", [])
            # Find most recent filled close order for the matching side
            for o in orders:
                trade_side = (o.get("tradeSide") or "").lower()
                pos_side = (o.get("posSide") or "").lower()
                status = (o.get("status") or "").lower()
                if status != "filled":
                    continue
                # Match close orders for the correct position side
                is_close = trade_side in ("close", "reduce_close_long", "reduce_close_short",
                                          "burst_close_long", "burst_close_short",
                                          "reduce_buy_single", "reduce_sell_single")
                if not is_close:
                    continue
                if pos_side and pos_side != hold_side:
                    continue
                pnl = float(o.get("totalProfits") or 0)
                avg_price = float(o.get("priceAvg") or 0)
                size = float(o.get("baseVolume") or o.get("size") or 0)
                return {
                    "pnl": pnl,
                    "exit_price": avg_price,
                    "fill_size": size,
                    "order_source": o.get("orderSource", ""),
                    "order_id": o.get("orderId", ""),
                }
        except Exception as e:
            logger.warning(f"[{self.exchange_name}] lookup_close_fill error for {symbol}: {e}")
        return None

    # ─── Futures Methods ─────────────────────────────────────────
    async def get_futures_balance(self, product_type: str = "USDT-FUTURES") -> Dict[str, Any]:
        """Get futures account balances"""
        if not self.native_client:
            raise RuntimeError("Native client not initialized")
        result = await self.native_client.get_futures_accounts(product_type=product_type)
        return result.get("data", [])

    async def get_futures_positions(
        self,
        product_type: str = "USDT-FUTURES",
        margin_coin: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get all open futures positions"""
        if not self.native_client:
            raise RuntimeError("Native client not initialized")
        result = await self.native_client.get_futures_positions(
            product_type=product_type,
            margin_coin=margin_coin,
        )
        return result.get("data", [])

    async def create_futures_order(
        self,
        symbol: str,
        margin_coin: str,
        side: str,
        order_type: str,
        size: str,
        price: Optional[str] = None,
        margin_mode: str = "crossed",
        leverage: Optional[int] = None,
        trade_side: str = "open",
        product_type: str = "USDT-FUTURES",
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Place a futures order with optional preset SL/TP."""
        if not self.native_client:
            raise RuntimeError("Native client not initialized")

        logger.info(
            f"[{self.exchange_name}] Creating futures {side} {order_type} order: "
            f"{size} {symbol} @ {price or 'market'} (trade_side: {trade_side})"
            f"{f' SL={stop_loss}' if stop_loss else ''}"
            f"{f' TP={take_profit}' if take_profit else ''}"
        )

        if trade_side == "open":
            await self.set_margin_mode(
                symbol=symbol,
                margin_coin=margin_coin,
                margin_mode=margin_mode,
                product_type=product_type,
            )
            if leverage is not None:
                await self.set_leverage(
                    symbol=symbol,
                    margin_coin=margin_coin,
                    leverage=leverage,
                    hold_side="long" if side == "buy" else "short",
                    product_type=product_type,
                )

        # Round price and size to contract precision
        prec = BitgetConnector._precision_cache.get(symbol) or BitgetConnector._precision_cache.get(symbol.replace("USDT", "/USDT"))
        price_place = prec["pricePlace"] if prec else 2
        vol_place = prec.get("volumePlace", 4) if prec else 4

        if price and order_type == "limit":
            price = str(round(float(price), price_place))
        if size:
            size = str(round(float(size), vol_place))

        sl_str = None
        tp_str = None
        if trade_side == "open":
            if stop_loss is not None:
                sl_str = str(round(stop_loss, price_place))
            if take_profit is not None:
                tp_str = str(round(take_profit, price_place))

        result = await self.native_client.place_futures_order(
            symbol=symbol,
            margin_coin=margin_coin,
            side=side,
            order_type=order_type,
            size=size,
            price=price,
            margin_mode=margin_mode,
            trade_side=trade_side,
            product_type=product_type,
            preset_stop_loss_price=sl_str,
            preset_stop_surplus_price=tp_str,
        )
        order_data = result.get("data", {})
        logger.info(f"[{self.exchange_name}] Futures order created: {order_data.get('orderId', 'unknown')}")
        return order_data

    async def cancel_futures_order(
        self,
        symbol: str,
        margin_coin: str,
        order_id: str,
        product_type: str = "USDT-FUTURES",
    ) -> Dict[str, Any]:
        """Cancel a futures order"""
        if not self.native_client:
            raise RuntimeError("Native client not initialized")

        result = await self.native_client.cancel_futures_order(
            symbol=symbol,
            margin_coin=margin_coin,
            order_id=order_id,
            product_type=product_type,
        )
        logger.info(f"[{self.exchange_name}] Futures order {order_id} cancelled")
        return result.get("data", {})

    async def modify_futures_order_tpsl(
        self,
        symbol: str,
        order_id: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        product_type: str = "USDT-FUTURES",
    ) -> Dict[str, Any]:
        """Modify an existing pending limit order to add/update preset SL/TP."""
        if not self.native_client:
            raise RuntimeError("Native client not initialized")

        # Round to contract precision
        prec = BitgetConnector._precision_cache.get(symbol) or BitgetConnector._precision_cache.get(
            symbol.replace("USDT", "/USDT")
        )
        price_place = prec["pricePlace"] if prec else 2

        sl_str = str(round(stop_loss, price_place)) if stop_loss is not None else None
        tp_str = str(round(take_profit, price_place)) if take_profit is not None else None

        logger.info(
            f"[{self.exchange_name}] Modifying order {order_id} for {symbol} "
            f"SL={sl_str} TP={tp_str}"
        )

        result = await self.native_client.modify_futures_order(
            symbol=symbol,
            product_type=product_type,
            order_id=order_id,
            new_preset_stop_loss_price=sl_str,
            new_preset_stop_surplus_price=tp_str,
        )
        order_data = result.get("data", {})
        logger.info(
            f"[{self.exchange_name}] Order {order_id} modified with SL/TP: "
            f"{order_data.get('orderId', 'unknown')}"
        )
        return order_data

    async def modify_limit_order_price(
        self,
        symbol: str,
        order_id: str,
        new_price: float,
        size: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        product_type: str = "USDT-FUTURES",
    ) -> Dict[str, Any]:
        """
        Modify a pending limit order's entry price (and optionally SL/TP).
        Bitget requires both newPrice + newSize when changing price.
        Note: This cancels the old order and creates a new one async.
        """
        if not self.native_client:
            raise RuntimeError("Native client not initialized")

        prec = BitgetConnector._precision_cache.get(symbol) or BitgetConnector._precision_cache.get(
            symbol.replace("USDT", "/USDT")
        )
        price_place = prec["pricePlace"] if prec else 2

        price_str = str(round(new_price, price_place))
        sl_str = str(round(stop_loss, price_place)) if stop_loss is not None else None
        tp_str = str(round(take_profit, price_place)) if take_profit is not None else None

        logger.info(
            f"[{self.exchange_name}] Modifying limit order {order_id} for {symbol}: "
            f"price={price_str} size={size} SL={sl_str} TP={tp_str}"
        )

        result = await self.native_client.modify_futures_order(
            symbol=symbol,
            product_type=product_type,
            order_id=order_id,
            new_price=price_str,
            new_size=size,
            new_preset_stop_loss_price=sl_str,
            new_preset_stop_surplus_price=tp_str,
        )
        order_data = result.get("data", {})
        logger.info(
            f"[{self.exchange_name}] Limit order {order_id} modified → "
            f"new orderId={order_data.get('orderId', 'unknown')} price={price_str}"
        )
        return order_data

    async def place_tpsl_order(
        self,
        symbol: str,
        margin_coin: str,
        plan_type: str,
        trigger_price: float,
        hold_side: str,
        size: Optional[str] = None,
        product_type: str = "USDT-FUTURES",
    ) -> Dict[str, Any]:
        """
        Place a TP/SL plan order on an existing position.
        plan_type: profit_plan (TP), loss_plan (SL), pos_profit, pos_loss
        """
        if not self.native_client:
            raise RuntimeError("Native client not initialized")

        # Round price to contract precision
        prec = BitgetConnector._precision_cache.get(symbol) or BitgetConnector._precision_cache.get(
            symbol.replace("USDT", "/USDT")
        )
        price_place = prec["pricePlace"] if prec else 2
        rounded_price = str(round(trigger_price, price_place))

        logger.info(
            f"[{self.exchange_name}] Placing TPSL {plan_type} for {symbol} "
            f"{hold_side} @ {rounded_price}"
        )

        result = await self.native_client.place_tpsl_order(
            symbol=symbol,
            margin_coin=margin_coin,
            plan_type=plan_type,
            trigger_price=rounded_price,
            hold_side=hold_side,
            size=size,
            product_type=product_type,
        )
        order_data = result.get("data", {})
        logger.info(
            f"[{self.exchange_name}] TPSL {plan_type} placed: {order_data.get('orderId', 'unknown')}"
        )
        return order_data

    async def set_leverage(
        self,
        symbol: str,
        margin_coin: str,
        leverage: int,
        hold_side: str = "long",
        product_type: str = "USDT-FUTURES",
    ) -> Dict[str, Any]:
        """Set leverage for a futures symbol"""
        if not self.native_client:
            raise RuntimeError("Native client not initialized")

        logger.info(
            f"[{self.exchange_name}] Setting leverage for {symbol}: {leverage}x ({hold_side})"
        )

        result = await self.native_client.set_leverage(
            symbol=symbol,
            margin_coin=margin_coin,
            leverage=str(leverage),
            hold_side=hold_side,
            product_type=product_type,
        )
        return result.get("data", {})

    async def _refresh_leverage_cache(self, product_type: str = "USDT-FUTURES") -> None:
        """Fetch all contracts and cache their leverage limits + precision."""
        if not self.native_client:
            return
        try:
            result = await self.native_client.get_futures_contracts(product_type=product_type)
            contracts = result.get("data", [])
            for c in contracts:
                sym = c.get("symbol", "")
                base = c.get("baseCoin", "")
                min_l = int(c.get("minLever", 1) or 1)
                max_l = int(c.get("maxLever", 125) or 125)
                BitgetConnector._leverage_cache[sym] = (min_l, max_l)
                # Also store with "BASE/USDT" key for easy lookup from ccxt symbols
                if base:
                    BitgetConnector._leverage_cache[f"{base}/USDT"] = (min_l, max_l)

                # Cache precision info
                precision = {
                    "pricePlace": int(c.get("pricePlace", 2) or 2),
                    "volumePlace": int(c.get("volumePlace", 4) or 4),
                    "priceEndStep": str(c.get("priceEndStep", "1") or "1"),
                    "sizeMultiplier": str(c.get("sizeMultiplier", "0.001") or "0.001"),
                    "minTradeNum": str(c.get("minTradeNum", "0.001") or "0.001"),
                }
                BitgetConnector._precision_cache[sym] = precision
                if base:
                    BitgetConnector._precision_cache[f"{base}/USDT"] = precision

            BitgetConnector._leverage_cache_ts = time.time()
            BitgetConnector._precision_cache_ts = time.time()
            logger.info(f"[{self.exchange_name}] Cached leverage limits for {len(contracts)} contracts")
        except Exception as e:
            logger.warning(f"[{self.exchange_name}] Failed to fetch leverage limits: {e}")

    async def get_max_leverage(self, symbol: str, product_type: str = "USDT-FUTURES") -> Tuple[int, int]:
        """
        Get (min_leverage, max_leverage) for a symbol.
        Refreshes cache if stale (>1h) or empty.
        Returns (1, 125) as safe default if lookup fails.
        """
        now = time.time()
        if not BitgetConnector._leverage_cache or (now - BitgetConnector._leverage_cache_ts) > self._LEVERAGE_CACHE_TTL:
            await self._refresh_leverage_cache(product_type)

        # Try exact symbol, then ccxt format
        if symbol in BitgetConnector._leverage_cache:
            return BitgetConnector._leverage_cache[symbol]

        # Try converting "BTC/USDT" -> "BTCUSDT"
        compact = symbol.replace("/", "")
        if compact in BitgetConnector._leverage_cache:
            return BitgetConnector._leverage_cache[compact]

        return (1, 125)  # safe default

    @staticmethod
    def clamp_leverage(desired: int, max_lever: int, default: int = 10) -> int:
        """
        Clamp leverage: if desired exceeds max, use min(default, max).
        Always ensure result is >= 1 and <= max_lever.
        """
        if desired <= max_lever:
            return max(1, desired)
        # Desired exceeds max — use default or max, whichever is smaller
        return max(1, min(default, max_lever))

    async def set_margin_mode(
        self,
        symbol: str,
        margin_coin: str,
        margin_mode: str,
        product_type: str = "USDT-FUTURES",
    ) -> Dict[str, Any]:
        """
        Set margin mode for futures trading
        margin_mode: 'crossed' for cross margin, 'isolated' for isolated margin
        """
        if not self.native_client:
            raise RuntimeError("Native client not initialized")

        logger.info(
            f"[{self.exchange_name}] Setting margin mode for {symbol}: {margin_mode}"
        )

        result = await self.native_client.set_margin_mode(
            symbol=symbol,
            margin_coin=margin_coin,
            margin_mode=margin_mode,
            product_type=product_type,
        )
        return result.get("data", {})

    async def get_futures_open_orders(
        self,
        product_type: str = "USDT-FUTURES",
        symbol: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get open futures orders"""
        if not self.native_client:
            raise RuntimeError("Native client not initialized")

        result = await self.native_client.get_futures_open_orders(
            product_type=product_type,
            symbol=symbol,
        )
        return result.get("data", {}).get("entrustedList", [])

    async def get_pending_tpsl_orders(
        self,
        product_type: str = "USDT-FUTURES",
        symbol: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get pending TP/SL trigger orders"""
        if not self.native_client:
            raise RuntimeError("Native client not initialized")

        result = await self.native_client.get_pending_tpsl_orders(
            product_type=product_type,
            plan_type="profit_loss",
            symbol=symbol,
        )
        data = result.get("data") or {}
        return data.get("entrustedList") or []

    async def cancel_tpsl_order(
        self,
        order_id: str,
        symbol: str,
        margin_coin: str = "USDT",
        product_type: str = "USDT-FUTURES",
        plan_type: str = "profit_loss",
    ) -> Dict[str, Any]:
        """Cancel a pending TP/SL trigger order"""
        if not self.native_client:
            raise RuntimeError("Native client not initialized")
        result = await self.native_client.cancel_tpsl_order(
            order_id=order_id,
            symbol=symbol,
            margin_coin=margin_coin,
            product_type=product_type,
            plan_type=plan_type,
        )
        logger.info(f"[{self.exchange_name}] TPSL order {order_id} cancelled for {symbol} (planType={plan_type})")
        return result.get("data", {})

    async def replace_tpsl_orders(
        self,
        symbol: str,
        hold_side: str,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None,
        margin_coin: str = "USDT",
        size: Optional[str] = None,
        product_type: str = "USDT-FUTURES",
    ) -> Dict[str, Any]:
        """Cancel existing SL/TP for a position and place new ones."""
        if not self.native_client:
            raise RuntimeError("Native client not initialized")

        bitget_sym = symbol.replace("/", "").upper()
        # Fetch current pending TPSL orders for this symbol
        pending = await self.get_pending_tpsl_orders(product_type=product_type, symbol=bitget_sym)
        if not pending:
            pending = []

        cancelled = []
        for order in pending:
            order_hold = (order.get("posSide") or order.get("holdSide") or "").lower()
            if order_hold and order_hold != hold_side:
                continue
            plan_type = (order.get("planType", "") or "").lower()
            oid = order.get("orderId", "")
            if not oid:
                continue
            # Cancel SL if we're replacing SL, cancel TP if replacing TP
            if (plan_type in ("loss_plan", "pos_loss") and new_sl) or \
               (plan_type in ("profit_plan", "pos_profit") and new_tp):
                try:
                    await self.cancel_tpsl_order(
                        order_id=oid, symbol=bitget_sym,
                        margin_coin=margin_coin, product_type=product_type,
                        plan_type=plan_type,
                    )
                    cancelled.append({"order_id": oid, "plan_type": plan_type})
                except Exception as e:
                    logger.warning(f"[{self.exchange_name}] Failed to cancel TPSL {oid}: {e}")

        # Place new orders
        placed = []
        if new_sl and new_sl > 0:
            try:
                sl_result = await self.place_tpsl_order(
                    symbol=bitget_sym, margin_coin=margin_coin,
                    plan_type="pos_loss", trigger_price=new_sl,
                    hold_side=hold_side, size=size, product_type=product_type,
                )
                placed.append({"plan_type": "loss_plan", "price": new_sl, "order_id": sl_result.get("orderId")})
            except Exception as e:
                logger.error(f"[{self.exchange_name}] Failed to place new SL for {symbol}: {e}")

        if new_tp and new_tp > 0:
            try:
                tp_result = await self.place_tpsl_order(
                    symbol=bitget_sym, margin_coin=margin_coin,
                    plan_type="pos_profit", trigger_price=new_tp,
                    hold_side=hold_side, size=size, product_type=product_type,
                )
                placed.append({"plan_type": "profit_plan", "price": new_tp, "order_id": tp_result.get("orderId")})
            except Exception as e:
                logger.error(f"[{self.exchange_name}] Failed to place new TP for {symbol}: {e}")

        logger.info(
            f"[{self.exchange_name}] TPSL replaced for {symbol} {hold_side}: "
            f"cancelled={len(cancelled)}, placed={len(placed)}"
        )
        return {"cancelled": cancelled, "placed": placed}

    async def get_futures_history_orders(
        self,
        product_type: str = "USDT-FUTURES",
        symbol: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get historical (filled/cancelled) futures orders"""
        if not self.native_client:
            raise RuntimeError("Native client not initialized")

        result = await self.native_client.get_futures_history_orders(
            product_type=product_type,
            symbol=symbol,
            limit=limit,
        )
        return result.get("data", {}).get("entrustedList", [])
