"""
AI Market Analyst — MT5 Order Gateway

Adapter that delegates limit order placement to the MT5 plugin's client.
If MT5 plugin is not available, raises a clear error.
"""
from typing import Dict, Optional
from loguru import logger


async def place_limit_order(
    *,
    mt5_account_id: int,
    symbol: str,
    direction: str,
    lot_size: float,
    entry_price: float,
    sl_price: Optional[float] = None,
    tp_price: Optional[float] = None,
) -> Dict:
    """
    Place a limit order via the MT5 plugin's REST client.

    Returns: {"success": bool, "mt5_ticket": str|None, "error": str|None}
    """
    try:
        from plugins.MT5TradingPlugin.backend.services.mt5_client import mt5_client

        if mt5_client is None:
            return {"success": False, "mt5_ticket": None, "error": "MT5 client not configured"}

        order_type = "buy_limit" if direction == "buy" else "sell_limit"

        result = await mt5_client.place_order(
            account_id=mt5_account_id,
            symbol=symbol,
            order_type=order_type,
            volume=lot_size,
            price=entry_price,
            sl=sl_price,
            tp=tp_price,
        )

        if result.get("error"):
            return {"success": False, "mt5_ticket": None, "error": result["error"]}

        return {
            "success": True,
            "mt5_ticket": str(result.get("ticket", result.get("order", ""))),
            "error": None,
        }

    except ImportError:
        logger.warning("[AI-Gateway] MT5 plugin not available — cannot place order")
        return {"success": False, "mt5_ticket": None, "error": "MT5 plugin not installed"}
    except Exception as exc:
        logger.error(f"[AI-Gateway] Order placement failed: {exc}")
        return {"success": False, "mt5_ticket": None, "error": str(exc)}
