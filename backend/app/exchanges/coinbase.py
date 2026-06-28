"""
Coinbase Exchange Connector
"""
import ccxt.async_support as ccxt
from app.exchanges.base import ExchangeConnector
from loguru import logger


class CoinbaseConnector(ExchangeConnector):
    """Coinbase (Advanced Trade) exchange connector"""
    
    @property
    def exchange_name(self) -> str:
        return "Coinbase"
    
    def _initialize_exchange(self) -> None:
        """Initialize Coinbase exchange"""
        config = {
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,
        }
        
        if self.testnet:
            logger.warning(f"[{self.exchange_name}] No separate testnet - using production API")
        
        self.exchange = ccxt.coinbase(config)
        logger.info(f"[{self.exchange_name}] Connector initialized")
