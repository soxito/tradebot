"""
OpenHumanPlugin — MCP Server Manifest

Defines the MCP tool manifest that users paste into OpenHuman's
Settings → MCP Servers → Custom  (or into ~/.vibe-trading/agent.json).

The live SSE endpoint at /plugins/openhuman/mcp/sse can be used by
OpenHuman when TradeBot's backend is running.
"""
from __future__ import annotations
from typing import Any, Dict


# The MCP server manifest (served as JSON)
def build_manifest(base_url: str) -> Dict[str, Any]:
    return {
        "name": "tradebot",
        "description": "TradeBot trading intelligence — signals, forecasts, positions, JARVIS",
        "transport": {
            "type": "sse",
            "url": f"{base_url}/api/v1/plugins/openhuman/mcp/sse",
        },
        "tools": [
            {
                "name": "tradebot_get_signals",
                "description": "Get recent TradeBot trading signals with confidence scores.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "e.g. BTC/USDT"},
                        "limit": {"type": "integer", "default": 10},
                    },
                },
            },
            {
                "name": "tradebot_get_forecast",
                "description": "Get Kronos ML price forecast (direction, % change, confidence bands).",
                "inputSchema": {
                    "type": "object",
                    "required": ["symbol"],
                    "properties": {
                        "symbol": {"type": "string"},
                        "exchange": {"type": "string", "default": "bitget"},
                        "timeframe": {"type": "string", "default": "1h"},
                    },
                },
            },
            {
                "name": "tradebot_get_position",
                "description": "Get open Bitget futures position for a symbol (side, PnL, leverage, liquidation).",
                "inputSchema": {
                    "type": "object",
                    "required": ["symbol"],
                    "properties": {"symbol": {"type": "string"}},
                },
            },
            {
                "name": "tradebot_get_smc_analysis",
                "description": "Get Smart Money Concepts (SMC) bias — market structure, order blocks, entry quality.",
                "inputSchema": {
                    "type": "object",
                    "required": ["symbol"],
                    "properties": {
                        "symbol": {"type": "string"},
                        "timeframe": {"type": "string", "default": "1h"},
                    },
                },
            },
            {
                "name": "tradebot_ask_jarvis",
                "description": "Ask JARVIS to analyze a market or answer a trading question using live data.",
                "inputSchema": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                },
            },
        ],
    }
