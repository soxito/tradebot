"""
KuCoin Exchange Connector
"""
import ccxt.async_support as ccxt
from app.exchanges.base import ExchangeConnector
from loguru import logger


class KuCoinConnector(ExchangeConnector):
    """KuCoin exchange connector"""
    
    @property
    def exchange_name(self) -> str:
        return "KuCoin"
    
    def _initialize_exchange(self) -> None:
        """Initialize KuCoin exchange"""
        config = {
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "password": self.passphrase,  # KuCoin requires passphrase
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
            },
        }
        
        if self.testnet:
            config["urls"] = {
                "api": {
                    "public": "https://openapi-sandbox.kucoin.com",
                    "private": "https://openapi-sandbox.kucoin.com",
                }
            }
            logger.info(f"[{self.exchange_name}] Initializing in SANDBOX mode")
        
        self.exchange = ccxt.kucoin(config)
        logger.info(f"[{self.exchange_name}] Connector initialized")
