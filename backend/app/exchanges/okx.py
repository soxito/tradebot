"""
OKX Exchange Connector
"""
import ccxt.async_support as ccxt
from app.exchanges.base import ExchangeConnector
from loguru import logger


class OKXConnector(ExchangeConnector):
    """OKX exchange connector"""
    
    @property
    def exchange_name(self) -> str:
        return "OKX"
    
    def _initialize_exchange(self) -> None:
        """Initialize OKX exchange"""
        config = {
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "password": self.passphrase,  # OKX requires passphrase
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
            },
        }
        
        if self.testnet:
            # OKX uses demo trading
            config["options"]["demo"] = True
            logger.info(f"[{self.exchange_name}] Initializing in DEMO mode")
        
        self.exchange = ccxt.okx(config)
        logger.info(f"[{self.exchange_name}] Connector initialized")
