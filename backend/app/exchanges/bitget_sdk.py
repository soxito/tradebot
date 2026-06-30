"""
Bitget Official SDK Integration
Direct integration with Bitget v2 REST API using their official signing method.
This provides proper authenticated access for account, balance, orders, and market data.
"""
import hmac
import base64
import time
import json
from typing import Dict, Any, Optional, List
from urllib.parse import urlencode

import httpx
from loguru import logger


# Bitget API Constants
BITGET_API_URL = "https://api.bitget.com"
CONTENT_TYPE = "Content-Type"
ACCESS_KEY = "ACCESS-KEY"
ACCESS_SIGN = "ACCESS-SIGN"
ACCESS_TIMESTAMP = "ACCESS-TIMESTAMP"
ACCESS_PASSPHRASE = "ACCESS-PASSPHRASE"
APPLICATION_JSON = "application/json"
LOCALE = "locale"


def _sign(message: str, secret_key: str) -> str:
    """Create HMAC-SHA256 signature"""
    mac = hmac.new(
        bytes(secret_key, encoding="utf8"),
        bytes(message, encoding="utf-8"),
        digestmod="sha256",
    )
    return str(base64.b64encode(mac.digest()), "utf8")


def _pre_hash(timestamp: str, method: str, request_path: str, body: str = "") -> str:
    """Create pre-hash string for signing"""
    return str(timestamp) + str.upper(method) + request_path + body


def _get_header(api_key: str, sign: str, timestamp: str, passphrase: str) -> Dict[str, str]:
    """Build authenticated request headers"""
    return {
        CONTENT_TYPE: APPLICATION_JSON,
        ACCESS_KEY: api_key,
        ACCESS_SIGN: sign,
        ACCESS_TIMESTAMP: timestamp,
        ACCESS_PASSPHRASE: passphrase,
        LOCALE: "en-US",
    }


def _parse_params_to_str(params: Dict) -> str:
    """Convert params dict to sorted query string"""
    if not params:
        return ""
    sorted_params = sorted(params.items(), key=lambda x: x[0])
    query = "&".join(f"{k}={v}" for k, v in sorted_params)
    return "?" + query if query else ""


class BitgetClient:
    """
    Async client for Bitget v2 API using official SDK signing method.
    Handles authentication, rate limiting, and error parsing.
    """

    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=BITGET_API_URL,
                timeout=15.0,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def _make_headers(self, method: str, request_path: str, body: str = "") -> Dict[str, str]:
        """Create signed headers for a request"""
        timestamp = str(int(time.time() * 1000))
        message = _pre_hash(timestamp, method, request_path, body)
        sign = _sign(message, self.api_secret)
        return _get_header(self.api_key, sign, timestamp, self.passphrase)

    async def get(self, path: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """Authenticated GET request"""
        query_string = _parse_params_to_str(params or {})
        full_path = path + query_string
        headers = self._make_headers("GET", full_path)
        client = await self._get_client()

        response = await client.get(full_path, headers=headers)
        return self._handle_response(response)

    async def post(self, path: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """Authenticated POST request"""
        body = json.dumps(params) if params else ""
        headers = self._make_headers("POST", path, body)
        client = await self._get_client()

        response = await client.post(path, content=body, headers=headers)
        return self._handle_response(response)

    def _handle_response(self, response: httpx.Response) -> Dict[str, Any]:
        """Parse response and raise on errors"""
        data = response.json()
        if not str(response.status_code).startswith("2"):
            code = data.get("code", response.status_code)
            msg = data.get("msg", response.text)
            raise BitgetAPIError(code=code, message=msg)
        if data.get("code") and str(data["code"]) != "00000":
            raise BitgetAPIError(code=data["code"], message=data.get("msg", "Unknown error"))
        return data

    # ─── Spot Account ────────────────────────────────────────────
    async def get_account_info(self) -> Dict[str, Any]:
        """GET /api/v2/spot/account/info"""
        return await self.get("/api/v2/spot/account/info")

    async def get_account_assets(self, coin: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/v2/spot/account/assets"""
        params = {}
        if coin:
            params["coin"] = coin
        return await self.get("/api/v2/spot/account/assets", params)

    async def get_account_bills(self, params: Optional[Dict] = None) -> Dict[str, Any]:
        """GET /api/v2/spot/account/bills"""
        return await self.get("/api/v2/spot/account/bills", params or {})

    # ─── Spot Market ─────────────────────────────────────────────
    async def get_tickers(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/v2/spot/market/tickers"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return await self.get("/api/v2/spot/market/tickers", params)

    async def get_candles(
        self,
        symbol: str,
        granularity: str = "1H",
        limit: int = 100,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /api/v2/spot/market/candles"""
        params = {"symbol": symbol, "granularity": granularity, "limit": str(limit)}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        return await self.get("/api/v2/spot/market/candles", params)

    async def get_orderbook(self, symbol: str, limit: int = 15) -> Dict[str, Any]:
        """GET /api/v2/spot/market/orderbook"""
        return await self.get("/api/v2/spot/market/orderbook", {"symbol": symbol, "limit": str(limit)})

    async def get_symbols(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/v2/spot/market/symbols"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return await self.get("/api/v2/spot/market/symbols", params)

    async def get_coins(self) -> Dict[str, Any]:
        """GET /api/v2/spot/market/coins"""
        return await self.get("/api/v2/spot/market/coins")

    async def get_market_fills(self, symbol: str, limit: int = 100) -> Dict[str, Any]:
        """GET /api/v2/spot/market/fills"""
        return await self.get("/api/v2/spot/market/fills", {"symbol": symbol, "limit": str(limit)})

    # ─── Spot Orders ─────────────────────────────────────────────
    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: str,
        price: Optional[str] = None,
        force: str = "gtc",
        client_oid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /api/v2/spot/trade/place-order"""
        params = {
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "size": size,
            "force": force,
        }
        if price:
            params["price"] = price
        if client_oid:
            params["clientOid"] = client_oid
        return await self.post("/api/v2/spot/trade/place-order", params)

    async def cancel_order(self, symbol: str, order_id: Optional[str] = None, client_oid: Optional[str] = None) -> Dict[str, Any]:
        """POST /api/v2/spot/trade/cancel-order"""
        params = {"symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if client_oid:
            params["clientOid"] = client_oid
        return await self.post("/api/v2/spot/trade/cancel-order", params)

    async def batch_orders(self, symbol: str, order_list: List[Dict]) -> Dict[str, Any]:
        """POST /api/v2/spot/trade/batch-orders"""
        return await self.post("/api/v2/spot/trade/batch-orders", {"symbol": symbol, "orderList": order_list})

    async def get_unfilled_orders(self, symbol: Optional[str] = None, start_time: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/v2/spot/trade/unfilled-orders"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        if start_time:
            params["startTime"] = start_time
        return await self.get("/api/v2/spot/trade/unfilled-orders", params)

    async def get_history_orders(self, symbol: str, start_time: Optional[str] = None, end_time: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        """GET /api/v2/spot/trade/history-orders"""
        params = {"symbol": symbol, "limit": str(limit)}
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        return await self.get("/api/v2/spot/trade/history-orders", params)

    async def get_fills(self, symbol: str, limit: int = 100) -> Dict[str, Any]:
        """GET /api/v2/spot/trade/fills"""
        return await self.get("/api/v2/spot/trade/fills", {"symbol": symbol, "limit": str(limit)})

    # ─── Plan Orders ─────────────────────────────────────────────
    async def place_plan_order(
        self,
        symbol: str,
        side: str,
        trigger_price: str,
        size: str,
        order_type: str = "market",
        execute_price: Optional[str] = None,
        trigger_type: str = "market_price",
    ) -> Dict[str, Any]:
        """POST /api/v2/spot/trade/place-plan-order"""
        params = {
            "symbol": symbol,
            "side": side,
            "triggerPrice": trigger_price,
            "size": size,
            "orderType": order_type,
            "triggerType": trigger_type,
        }
        if execute_price:
            params["executePrice"] = execute_price
        return await self.post("/api/v2/spot/trade/place-plan-order", params)

    async def cancel_plan_order(self, order_id: str) -> Dict[str, Any]:
        """POST /api/v2/spot/trade/cancel-plan-order"""
        return await self.post("/api/v2/spot/trade/cancel-plan-order", {"orderId": order_id})

    async def get_current_plan_orders(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/v2/spot/trade/current-plan-order"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        return await self.get("/api/v2/spot/trade/current-plan-order", params)

    # ─── Futures Account ─────────────────────────────────────────
    async def get_futures_accounts(self, product_type: str = "USDT-FUTURES") -> Dict[str, Any]:
        """
        GET /api/v2/mix/account/accounts
        Get futures account list
        product_type: USDT-FUTURES (default), COIN-FUTURES, USDC-FUTURES, SUSDT-FUTURES, SCOIN-FUTURES
        """
        return await self.get(f"/api/v2/mix/account/accounts", {"productType": product_type})

    async def get_futures_account(self, symbol: str, margin_coin: str, product_type: str = "USDT-FUTURES") -> Dict[str, Any]:
        """
        GET /api/v2/mix/account/account
        Get single futures account
        """
        params = {
            "symbol": symbol,
            "marginCoin": margin_coin,
            "productType": product_type,
        }
        return await self.get("/api/v2/mix/account/account", params)

    async def get_futures_positions(self, product_type: str = "USDT-FUTURES", margin_coin: Optional[str] = None) -> Dict[str, Any]:
        """
        GET /api/v2/mix/position/all-position
        Get all futures positions
        """
        params = {"productType": product_type}
        if margin_coin:
            params["marginCoin"] = margin_coin
        return await self.get("/api/v2/mix/position/all-position", params)

    async def get_futures_single_position(self, symbol: str, margin_coin: str, product_type: str = "USDT-FUTURES") -> Dict[str, Any]:
        """
        GET /api/v2/mix/position/single-position
        Get single futures position
        """
        params = {
            "symbol": symbol,
            "marginCoin": margin_coin,
            "productType": product_type,
        }
        return await self.get("/api/v2/mix/position/single-position", params)

    # ─── Futures Trading ─────────────────────────────────────────
    async def place_futures_order(
        self,
        symbol: str,
        margin_coin: str,
        side: str,
        order_type: str,
        size: str,
        price: Optional[str] = None,
        margin_mode: str = "crossed",
        trade_side: str = "open",
        product_type: str = "USDT-FUTURES",
        force: str = "GTC",
        client_oid: Optional[str] = None,
        preset_stop_loss_price: Optional[str] = None,
        preset_stop_surplus_price: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        POST /api/v2/mix/order/place-order
        Place futures order
        side: buy, sell
        order_type: limit, market
        trade_side: open, close
        preset_stop_loss_price: preset SL trigger price
        preset_stop_surplus_price: preset TP trigger price
        """
        params = {
            "symbol": symbol,
            "marginCoin": margin_coin,
            "productType": product_type,
            "marginMode": margin_mode,
            "side": side,
            "orderType": order_type,
            "size": size,
            "tradeSide": trade_side,
            "force": force,
        }
        if price:
            params["price"] = price
        if client_oid:
            params["clientOid"] = client_oid
        if preset_stop_loss_price:
            params["presetStopLossPrice"] = preset_stop_loss_price
        if preset_stop_surplus_price:
            params["presetStopSurplusPrice"] = preset_stop_surplus_price
        return await self.post("/api/v2/mix/order/place-order", params)

    async def cancel_futures_order(
        self,
        symbol: str,
        margin_coin: str,
        order_id: Optional[str] = None,
        client_oid: Optional[str] = None,
        product_type: str = "USDT-FUTURES",
    ) -> Dict[str, Any]:
        """POST /api/v2/mix/order/cancel-order"""
        params = {
            "symbol": symbol,
            "marginCoin": margin_coin,
            "productType": product_type,
        }
        if order_id:
            params["orderId"] = order_id
        if client_oid:
            params["clientOid"] = client_oid
        return await self.post("/api/v2/mix/order/cancel-order", params)

    async def modify_futures_order(
        self,
        symbol: str,
        product_type: str = "USDT-FUTURES",
        order_id: Optional[str] = None,
        client_oid: Optional[str] = None,
        new_client_oid: Optional[str] = None,
        new_price: Optional[str] = None,
        new_size: Optional[str] = None,
        new_preset_stop_loss_price: Optional[str] = None,
        new_preset_stop_surplus_price: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        POST /api/v2/mix/order/modify-order
        Modify an existing pending order's price, size, and/or preset TP/SL.
        Note: modifying price+size cancels old order and creates new one async.
        When modifying price+size, both must be provided together.
        If only modifying TP/SL, do NOT pass newPrice/newSize.
        """
        params: Dict[str, str] = {
            "symbol": symbol,
            "productType": product_type,
        }
        if order_id:
            params["orderId"] = order_id
        if client_oid:
            params["clientOid"] = client_oid
        if new_client_oid:
            params["newClientOid"] = new_client_oid
        else:
            # Bitget requires newClientOid; generate one if not provided
            import uuid
            params["newClientOid"] = uuid.uuid4().hex[:32]
        if new_price is not None:
            params["newPrice"] = new_price
        if new_size is not None:
            params["newSize"] = new_size
        if new_preset_stop_loss_price is not None:
            params["newPresetStopLossPrice"] = new_preset_stop_loss_price
        if new_preset_stop_surplus_price is not None:
            params["newPresetStopSurplusPrice"] = new_preset_stop_surplus_price
        return await self.post("/api/v2/mix/order/modify-order", params)

    async def place_futures_tpsl_order(
        self,
        symbol: str,
        margin_coin: str,
        plan_type: str,          # "profit_plan" (TP) | "loss_plan" (SL)
        trigger_price: str,
        size: str,
        side: str,               # "buy" to close short, "sell" to close long
        product_type: str = "USDT-FUTURES",
        trigger_type: str = "fill_price",
        hold_side: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        POST /api/v2/mix/order/place-tpsl-order
        Place a take-profit or stop-loss conditional order on an existing position.

        plan_type:
          • "profit_plan" → take-profit (TP)
          • "loss_plan"   → stop-loss   (SL)
          • "pos_profit"  → position-level TP (one-way mode, no holdSide needed)
          • "pos_loss"    → position-level SL (one-way mode, no holdSide needed)
        trigger_type: "fill_price" | "mark_price"
        """
        params: Dict[str, str] = {
            "symbol": symbol,
            "marginCoin": margin_coin,
            "productType": product_type,
            "planType": plan_type,
            "triggerPrice": trigger_price,
            "size": size,
            "side": side,
            "triggerType": trigger_type,
        }
        if hold_side:
            params["holdSide"] = hold_side
        return await self.post("/api/v2/mix/order/place-tpsl-order", params)

    async def get_futures_open_orders(
        self,
        product_type: str = "USDT-FUTURES",
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /api/v2/mix/order/orders-pending"""
        params = {"productType": product_type}
        if symbol:
            params["symbol"] = symbol
        return await self.get("/api/v2/mix/order/orders-pending", params)

    async def get_futures_history_orders(
        self,
        product_type: str = "USDT-FUTURES",
        symbol: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """GET /api/v2/mix/order/orders-history"""
        params = {"productType": product_type, "limit": str(limit)}
        if symbol:
            params["symbol"] = symbol
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        return await self.get("/api/v2/mix/order/orders-history", params)

    async def get_futures_fills(
        self,
        symbol: str,
        product_type: str = "USDT-FUTURES",
        limit: int = 100,
    ) -> Dict[str, Any]:
        """GET /api/v2/mix/order/fills"""
        params = {"symbol": symbol, "productType": product_type, "limit": str(limit)}
        return await self.get("/api/v2/mix/order/fills", params)

    # ─── Futures Account Config ──────────────────────────────────
    async def set_leverage(
        self,
        symbol: str,
        margin_coin: str,
        leverage: str,
        hold_side: str = "long",
        product_type: str = "USDT-FUTURES",
    ) -> Dict[str, Any]:
        """
        POST /api/v2/mix/account/set-leverage
        Set leverage for futures position
        hold_side: long, short
        leverage: "1" to "125" (depends on symbol)
        """
        params = {
            "symbol": symbol,
            "marginCoin": margin_coin,
            "leverage": leverage,
            "holdSide": hold_side,
            "productType": product_type,
        }
        return await self.post("/api/v2/mix/account/set-leverage", params)

    async def set_margin_mode(
        self,
        symbol: str,
        margin_coin: str,
        margin_mode: str,
        product_type: str = "USDT-FUTURES",
    ) -> Dict[str, Any]:
        """
        POST /api/v2/mix/account/set-margin-mode
        Set margin mode for futures
        margin_mode: crossed (cross margin), isolated (isolated margin)
        """
        params = {
            "symbol": symbol,
            "marginCoin": margin_coin,
            "marginMode": margin_mode,
            "productType": product_type,
        }
        return await self.post("/api/v2/mix/account/set-margin-mode", params)

    async def set_position_mode(
        self,
        product_type: str,
        position_mode: str,
    ) -> Dict[str, Any]:
        """
        POST /api/v2/mix/account/set-position-mode
        Set position mode
        position_mode: one_way_mode (one-way), hedge_mode (hedge)
        """
        params = {
            "productType": product_type,
            "posMode": position_mode,
        }
        return await self.post("/api/v2/mix/account/set-position-mode", params)

    # ─── Futures TPSL Orders ─────────────────────────────────────
    async def place_tpsl_order(
        self,
        symbol: str,
        margin_coin: str,
        plan_type: str,
        trigger_price: str,
        hold_side: str,
        size: Optional[str] = None,
        trigger_type: str = "mark_price",
        execute_price: str = "0",
        product_type: str = "USDT-FUTURES",
    ) -> Dict[str, Any]:
        """
        POST /api/v2/mix/order/place-tpsl-order
        Place a TP/SL plan order on an existing position.
        plan_type: profit_plan, loss_plan, pos_profit, pos_loss
        hold_side: long, short
        size: required for profit_plan/loss_plan; not required for pos_profit/pos_loss
        execute_price: "0" = market price execution
        """
        params: Dict[str, str] = {
            "symbol": symbol,
            "marginCoin": margin_coin,
            "productType": product_type,
            "planType": plan_type,
            "triggerPrice": trigger_price,
            "triggerType": trigger_type,
            "holdSide": hold_side,
        }
        if execute_price and execute_price != "0":
            params["executePrice"] = execute_price
        if size:
            params["size"] = size
        return await self.post("/api/v2/mix/order/place-tpsl-order", params)

    async def get_pending_tpsl_orders(
        self,
        product_type: str = "USDT-FUTURES",
        plan_type: str = "profit_loss",
        symbol: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /api/v2/mix/order/orders-plan-pending — fetch pending TP/SL trigger orders"""
        params: Dict[str, str] = {
            "productType": product_type,
            "planType": plan_type,
        }
        if symbol:
            params["symbol"] = symbol
        return await self.get("/api/v2/mix/order/orders-plan-pending", params)

    async def cancel_tpsl_order(
        self,
        order_id: str,
        symbol: str,
        margin_coin: str = "USDT",
        product_type: str = "USDT-FUTURES",
        plan_type: str = "profit_loss",
    ) -> Dict[str, Any]:
        """POST /api/v2/mix/order/cancel-plan-order — cancel a TP/SL trigger order"""
        params: Dict[str, str] = {
            "orderId": order_id,
            "symbol": symbol,
            "marginCoin": margin_coin,
            "productType": product_type,
            "planType": plan_type,
        }
        return await self.post("/api/v2/mix/order/cancel-plan-order", params)

    # ─── Futures Market Data ─────────────────────────────────────
    async def get_futures_contracts(self, product_type: str = "USDT-FUTURES") -> Dict[str, Any]:
        """GET /api/v2/mix/market/contracts"""
        return await self.get("/api/v2/mix/market/contracts", {"productType": product_type})

    async def get_futures_ticker(self, symbol: str, product_type: str = "USDT-FUTURES") -> Dict[str, Any]:
        """GET /api/v2/mix/market/ticker"""
        return await self.get("/api/v2/mix/market/ticker", {"symbol": symbol, "productType": product_type})

    async def get_futures_candles(
        self,
        symbol: str,
        granularity: str = "1H",
        product_type: str = "USDT-FUTURES",
        limit: int = 100,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /api/v2/mix/market/candles"""
        params = {
            "symbol": symbol,
            "productType": product_type,
            "granularity": granularity,
            "limit": str(limit),
        }
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        return await self.get("/api/v2/mix/market/candles", params)


class BitgetAPIError(Exception):
    """Custom exception for Bitget API errors"""
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"Bitget API Error [{code}]: {message}")
