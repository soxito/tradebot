from __future__ import annotations
"""Plugins package for TradeBot."""

# Import WhatsApp plugin router if available
try:
    from .WhatsAppSignalNewsPlugin.backend import router as whatsapp_router
except ImportError:
    whatsapp_router = None

__all__ = ["whatsapp_router"]