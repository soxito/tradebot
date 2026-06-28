"""
AI Market Analyst — Chart Overlay Service

Publishes proposed entry / SL / TP lines for TradingView Lightweight Charts overlay.
"""
from typing import Optional
from plugins.AiMarketAnalyst.backend.schemas import AIOverlayLine, AIOverlayResponse


def build_overlay(
    *,
    direction: str,
    entry_price: Optional[float],
    sl_price: Optional[float],
    tp_price: Optional[float],
    confidence: Optional[float],
    status: str = "drafted",
) -> AIOverlayResponse:
    """Build chart overlay data from a decision."""
    entry_line = None
    sl_line = None
    tp_line = None

    if entry_price:
        entry_color = "#22c55e" if direction == "buy" else "#ef4444"
        entry_line = AIOverlayLine(
            price=entry_price,
            color=entry_color,
            lineWidth=2,
            lineStyle=2,  # dashed
            title=f"AI Entry ({direction.upper()})",
        )

    if sl_price:
        sl_line = AIOverlayLine(
            price=sl_price,
            color="#ef4444",
            lineWidth=1,
            lineStyle=1,  # dotted
            title="AI SL",
        )

    if tp_price:
        tp_line = AIOverlayLine(
            price=tp_price,
            color="#22c55e",
            lineWidth=1,
            lineStyle=1,
            title="AI TP",
        )

    return AIOverlayResponse(
        proposed_entry=entry_line,
        sl_line=sl_line,
        tp_line=tp_line,
        status=status,
        direction=direction,
        confidence=confidence,
    )
