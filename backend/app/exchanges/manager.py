"""
Exchange Manager
Manages multiple exchange connectors and provides unified access
"""
from typing import Dict, Optional, List
from enum import Enum

from app.core.config import settings
from app.exchanges.base import ExchangeConnector, OrderSide, OrderType
from app.exchanges.binance import BinanceConnector
from app.exchanges.bitget import BitgetConnector
from app.exchanges.bybit import BybitConnector
from app.exchanges.okx import OKXConnector
from app.exchanges.kucoin import KuCoinConnector
from app.exchanges.coinbase import CoinbaseConnector
from loguru import logger


class SupportedExchange(str, Enum):
    """Supported exchange enumeration"""
    BINANCE = "binance"
    BITGET = "bitget"
    BYBIT = "bybit"
    OKX = "okx"
    KUCOIN = "kucoin"
    COINBASE = "coinbase"


class ExchangeManager:
    """
    Centralized exchange manager
    Handles initialization and access to all exchange connectors
    """
    
    def __init__(self, testnet: bool = True):
        """
        Initialize exchange manager
        
        Args:
            testnet: Use testnet/sandbox mode for all exchanges
        """
        self.testnet = testnet
        self.exchanges: Dict[SupportedExchange, ExchangeConnector] = {}
        self._initialize_exchanges()
    
    def _initialize_exchanges(self) -> None:
        """Initialize all configured exchanges"""
        exchange_configs = {
            SupportedExchange.BINANCE: {
                "connector": BinanceConnector,
                "api_key": settings.BINANCE_API_KEY,
                "api_secret": settings.BINANCE_API_SECRET,
                "passphrase": None,
            },
            SupportedExchange.BITGET: {
                "connector": BitgetConnector,
                "api_key": settings.BITGET_API_KEY,
                "api_secret": settings.BITGET_API_SECRET,
                "passphrase": settings.BITGET_PASSPHRASE,
            },
            SupportedExchange.BYBIT: {
                "connector": BybitConnector,
                "api_key": settings.BYBIT_API_KEY,
                "api_secret": settings.BYBIT_API_SECRET,
                "passphrase": None,
            },
            SupportedExchange.OKX: {
                "connector": OKXConnector,
                "api_key": settings.OKX_API_KEY,
                "api_secret": settings.OKX_API_SECRET,
                "passphrase": settings.OKX_PASSPHRASE,
            },
            SupportedExchange.KUCOIN: {
                "connector": KuCoinConnector,
                "api_key": settings.KUCOIN_API_KEY,
                "api_secret": settings.KUCOIN_API_SECRET,
                "passphrase": settings.KUCOIN_PASSPHRASE,
            },
            SupportedExchange.COINBASE: {
                "connector": CoinbaseConnector,
                "api_key": settings.COINBASE_API_KEY,
                "api_secret": settings.COINBASE_API_SECRET,
                "passphrase": None,
            },
        }
        
        for exchange_name, config in exchange_configs.items():
            # Only initialize if API credentials are provided
            if config["api_key"] and config["api_secret"]:
                try:
                    connector = config["connector"](
                        api_key=config["api_key"],
                        api_secret=config["api_secret"],
                        passphrase=config["passphrase"],
                        testnet=self.testnet,
                    )
                    self.exchanges[exchange_name] = connector
                    logger.info(f"✅ {exchange_name.value.upper()} connector initialized")
                except Exception as e:
                    logger.error(f"❌ Failed to initialize {exchange_name.value}: {e}")
            else:
                logger.warning(f"⚠️  {exchange_name.value.upper()} credentials not configured - skipping")
    
    def get_exchange(self, exchange: SupportedExchange) -> Optional[ExchangeConnector]:
        """
        Get specific exchange connector
        
        Args:
            exchange: Exchange to retrieve
        
        Returns:
            Exchange connector or None if not initialized
        """
        return self.exchanges.get(exchange)
    
    def get_all_exchanges(self) -> List[SupportedExchange]:
        """
        Get list of all initialized exchanges
        
        Returns:
            List of exchange names
        """
        return list(self.exchanges.keys())
    
    def get_exchange_status(self) -> Dict[str, Dict[str, any]]:
        """
        Get status of all exchanges
        
        Returns:
            Dictionary with exchange status information
        """
        status = {}
        for exchange_name, connector in self.exchanges.items():
            status[exchange_name.value] = {
                "initialized": True,
                "testnet": self.testnet,
                "healthy": connector.is_healthy(),
            }
        
        # Add uninitiali zed exchanges
        all_exchanges = list(SupportedExchange)
        for exchange in all_exchanges:
            if exchange not in self.exchanges:
                status[exchange.value] = {
                    "initialized": False,
                    "testnet": self.testnet,
                    "healthy": False,
                    "reason": "No API credentials configured",
                }
        
        return status


# Global exchange manager instance
exchange_manager = ExchangeManager(testnet=True)
