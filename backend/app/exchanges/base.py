"""
Base Exchange Connector
Provides unified interface for all supported exchanges
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from enum import Enum
import ccxt
from loguru import logger


class OrderSide(str, Enum):
    """Order side enumeration"""
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    """Order type enumeration"""
    MARKET = "market"
    LIMIT = "limit"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"


class ExchangeConnector(ABC):
    """
    Base class for exchange connectors
    All exchange implementations must inherit from this
    """
    
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: Optional[str] = None,
        testnet: bool = True,
    ):
        """
        Initialize exchange connector
        
        Args:
            api_key: Exchange API key
            api_secret: Exchange API secret
            passphrase: Exchange passphrase (required for some exchanges)
            testnet: Use testnet/sandbox mode
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.testnet = testnet
        self.exchange: Optional[ccxt.Exchange] = None
        self._initialize_exchange()
    
    @abstractmethod
    def _initialize_exchange(self) -> None:
        """Initialize the ccxt exchange instance"""
        pass
    
    @property
    @abstractmethod
    def exchange_name(self) -> str:
        """Return the exchange name"""
        pass
    
    async def get_balance(self, currency: Optional[str] = None) -> Dict[str, Any]:
        """
        Get account balance
        
        Args:
            currency: Specific currency to query (e.g., 'USDT', 'BTC')
                     If None, returns all balances
        
        Returns:
            Dictionary with balance information
        """
        try:
            balance = await self.exchange.fetch_balance()
            
            if currency:
                return {
                    "currency": currency,
                    "free": balance.get(currency, {}).get("free", 0),
                    "used": balance.get(currency, {}).get("used", 0),
                    "total": balance.get(currency, {}).get("total", 0),
                }
            
            return balance
        except Exception as e:
            logger.error(f"[{self.exchange_name}] Error fetching balance: {e}")
            raise
    
    async def create_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        amount: float,
        price: Optional[float] = None,
        params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Create an order
        
        Args:
            symbol: Trading pair (e.g., 'BTC/USDT')
            side: Order side (buy/sell)
            order_type: Order type (market/limit)
            amount: Order amount in base currency
            price: Order price (required for limit orders)
            params: Additional exchange-specific parameters
        
        Returns:
            Order information
        """
        try:
            if order_type == OrderType.LIMIT and price is None:
                raise ValueError("Price is required for limit orders")
            
            logger.info(
                f"[{self.exchange_name}] Creating {side.value} {order_type.value} "
                f"order: {amount} {symbol} @ {price or 'market'}"
            )
            
            order = await self.exchange.create_order(
                symbol=symbol,
                type=order_type.value,
                side=side.value,
                amount=amount,
                price=price,
                params=params or {},
            )
            
            logger.info(f"[{self.exchange_name}] Order created: {order['id']}")
            return order
        
        except Exception as e:
            logger.error(f"[{self.exchange_name}] Error creating order: {e}")
            raise
    
    async def cancel_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        """
        Cancel an order
        
        Args:
            order_id: Order ID to cancel
            symbol: Trading pair
        
        Returns:
            Cancellation result
        """
        try:
            logger.info(f"[{self.exchange_name}] Canceling order {order_id}")
            result = await self.exchange.cancel_order(order_id, symbol)
            logger.info(f"[{self.exchange_name}] Order canceled: {order_id}")
            return result
        except Exception as e:
            logger.error(f"[{self.exchange_name}] Error canceling order: {e}")
            raise
    
    async def get_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        """
        Get order information
        
        Args:
            order_id: Order ID
            symbol: Trading pair
        
        Returns:
            Order information
        """
        try:
            order = await self.exchange.fetch_order(order_id, symbol)
            return order
        except Exception as e:
            logger.error(f"[{self.exchange_name}] Error fetching order: {e}")
            raise
    
    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all open orders
        
        Args:
            symbol: Filter by trading pair (optional)
        
        Returns:
            List of open orders
        """
        try:
            orders = await self.exchange.fetch_open_orders(symbol)
            return orders
        except Exception as e:
            logger.error(f"[{self.exchange_name}] Error fetching open orders: {e}")
            raise
    
    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """
        Get current ticker information
        
        Args:
            symbol: Trading pair
        
        Returns:
            Ticker data
        """
        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            return ticker
        except Exception as e:
            logger.error(f"[{self.exchange_name}] Error fetching ticker: {e}")
            raise
    
    async def get_markets(self) -> List[Dict[str, Any]]:
        """
        Get all available markets
        
        Returns:
            List of market information
        """
        try:
            await self.exchange.load_markets()
            return list(self.exchange.markets.values())
        except Exception as e:
            logger.error(f"[{self.exchange_name}] Error fetching markets: {e}")
            raise
    
    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 100,
        since: Optional[int] = None,
    ) -> List[List]:
        """
        Get OHLCV (candlestick) data
        
        Args:
            symbol: Trading pair (e.g., 'BTC/USDT')
            timeframe: Timeframe (1m, 5m, 15m, 1h, 4h, 1d, etc.)
            limit: Number of candles to fetch
            since: Timestamp in milliseconds to fetch from
        
        Returns:
            List of OHLCV data: [timestamp, open, high, low, close, volume]
        """
        try:
            ohlcv = await self.exchange.fetch_ohlcv(
                symbol=symbol,
                timeframe=timeframe,
                limit=limit,
                since=since,
            )
            return ohlcv
        except Exception as e:
            logger.error(f"[{self.exchange_name}] Error fetching OHLCV for {symbol}: {e}")
            raise
    
    def is_healthy(self) -> bool:
        """
        Check if exchange connection is healthy
        
        Returns:
            True if exchange is accessible
        """
        try:
            # Simple health check - try to fetch markets
            self.exchange.fetch_markets()
            return True
        except Exception as e:
            logger.warning(f"[{self.exchange_name}] Health check failed: {e}")
            return False
