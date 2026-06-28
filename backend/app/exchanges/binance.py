"""
Binance Exchange Connector
"""
import ccxt.async_support as ccxt
from app.exchanges.base import ExchangeConnector
from loguru import logger


class BinanceConnector(ExchangeConnector):
    """Binance exchange connector"""
    
    @property
    def exchange_name(self) -> str:
        return "Binance"
    
    def _initialize_exchange(self) -> None:
        """Initialize Binance exchange"""
        config = {
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",  # spot, margin, future
            },
        }
        
        if self.testnet:
            config["urls"] = {
                "api": {
                    "public": "https://testnet.binance.vision/api",
                    "private": "https://testnet.binance.vision/api",
                }
            }
            logger.info(f"[{self.exchange_name}] Initializing in TESTNET mode")
        
        self.exchange = ccxt.binance(config)
        logger.info(f"[{self.exchange_name}] Connector initialized")
