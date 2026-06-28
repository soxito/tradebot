"""
Bybit Exchange Connector
"""
import ccxt.async_support as ccxt
from app.exchanges.base import ExchangeConnector
from loguru import logger


class BybitConnector(ExchangeConnector):
    """Bybit exchange connector"""
    
    @property
    def exchange_name(self) -> str:
        return "Bybit"
    
    def _initialize_exchange(self) -> None:
        """Initialize Bybit exchange"""
        config = {
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
            },
        }
        
        if self.testnet:
            config["urls"] = {
                "api": {
                    "public": "https://api-testnet.bybit.com",
                    "private": "https://api-testnet.bybit.com",
                }
            }
            logger.info(f"[{self.exchange_name}] Initializing in TESTNET mode")
        
        self.exchange = ccxt.bybit(config)
        logger.info(f"[{self.exchange_name}] Connector initialized")
