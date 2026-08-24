"""Shared image understanding — chart/screenshot reads for every surface.

One implementation behind the Telegram bot, Paul chat and the browser
extension, so a screenshot gets the same treatment wherever it is sent from.

Two outputs per call:

* ``narrative`` — the prose a human reads.
* ``findings`` — the same read as structured data (levels, regions, bias) that
  :mod:`chart_annotate` draws back onto the original screenshot.

The annotation is drawn on the user's real pixels rather than generated. A
diffusion model asked to "mark support and resistance" would return a plausible
*new* chart with invented prices, which for someone about to place a trade is
worse than no picture at all.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

#: The vision chain leads with a 952B reasoning model that thinks its way
#: through an image; measured runs land well past the router's 40s default, and
#: timing out there also trips the provider breaker for everything behind it.
DEFAULT_VISION_TIMEOUT_S = 120.0

DEFAULT_CHART_PROMPT = (
    "Read this trading chart. Identify the instrument and timeframe if they are "
    "visible, then describe the market structure, the key support and resistance "
    "levels with their price values, any pattern or indicator you can see, and "
    "the directional bias the chart supports."
)

_SYSTEM_PROMPT = (
    "You are a technical analyst reading a chart image for a trader.\n"
    "Report only what is actually visible in the image. If the image is not a "
    "chart, describe what it shows and say it is not a chart.\n"
    "\n"
    "Price levels: quote a number ONLY if you can read it off the chart's price "
    "axis. If the axis is missing, cropped or unreadable, say so and describe "
    "levels by position instead — 'resistance at the highs of the recent range', "
    "not a made-up figure. A fabricated price is worse than no price, because "
    "the trader may act on it. The same goes for the instrument and the "
    "timeframe: if the label is not visible, say it is not visible rather than "
    "guessing from the shape of the candles.\n"
    "\n"
    "Be concise and concrete — no hedging boilerplate, no risk disclaimers.\n"
    "\n"
    "After your prose answer, append a fenced ```json block with this shape so "
    "the findings can be drawn onto the image:\n"
    '{"instrument": str|null, "timeframe": str|null, '
    '"bias": "bullish"|"bearish"|"neutral"|null, '
    '"axis_labels": [number, ...], "last_price": number|null, '
    '"axis": {"top_price": number, "top_y_pct": number, '
    '"bottom_price": number, "bottom_y_pct": number}|null, '
    '"levels": [{"label": str, "price": str|null, "y_pct": number, '
    '"kind": "support"|"resistance"|"other"}], '
    '"regions": [{"label": str, "x_pct": number, "y_pct": number, '
    '"w_pct": number, "h_pct": number}]}\n'
    "y_pct/x_pct/w_pct/h_pct are percentages (0-100) of the image's height and "
    "width, measured from the top-left, locating what you described. Include a "
    "level only if you can actually see it on the chart; an empty list is a "
    "valid and honest answer. Omit the block entirely for a non-chart image.\n"
    "\n"
    '"instrument" must be the trading symbol, not the chart\'s title: a chart '
    'headed "Gold Spot / U.S. Dollar" is XAUUSD, "Bitcoin / TetherUS" is '
    "BTCUSDT, \"Euro / U.S. Dollar\" is EURUSD. A live quote gets looked up from "
    "it, so a title cannot be used. Use null if no instrument is identifiable.\n"
    "\n"
    '"timeframe" is the candle size. On phone screenshots it is usually NOT '
    "next to the title — look for it in the toolbar strip at the bottom or top "
    'of the screen, printed right beside the symbol code, e.g. "XAUUSD 4H", '
    '"XAUUSD 1H", "XAUUSD 30m", "XAUUSD 15m", "XAUUSD 1D", "XAUUSD 1W". That '
    "strip may also list other timeframes above or below the active one — "
    "report the one on the SAME line as the symbol being charted, which is the "
    "active chart. Copy it exactly as written (4H, 30m, 1W); do not translate "
    "it, and do not infer it from candle spacing. Use null if it is not "
    "visible anywhere.\n"
    "\n"
    '"axis_labels" is how the price scale gets calibrated, so the levels can be '
    "drawn onto this exact image at the right height. List EVERY price printed "
    "on the price axis (the column of numbers down the right-hand edge), in the "
    "order they appear from top to bottom, as plain numbers: "
    "[4460, 4450, 4440, 4430]. Do not skip any, do not reorder them, and do not "
    "add prices that are not printed there — the list is matched against the "
    "labels found in the image, and a missing or invented entry throws the "
    "whole scale out. Skip the coloured price tags (the live quote, bid/ask); "
    "those are not part of the regular scale. Use an empty list if the axis is "
    "not legible.\n"
    "\n"
    '"last_price" is the number printed inside the coloured price tag on the '
    "axis — the live quote, usually a filled green or red box. It is what ties "
    "the prices to the image: its row is measured directly, so it settles which "
    "labels are which when only some of them were listed. Use null if no such "
    "tag is visible.\n"
    "\n"
    '"axis" is a coarser fallback used only when the list above cannot be '
    "matched. Pick two price labels far apart on the axis — ideally the highest "
    "and lowest you can read — and give each one's value and its vertical "
    "position as a percentage of image height. Read the numbers off the axis; "
    "if the axis is not legible, set axis to null rather than estimating, "
    "because a wrong scale would put every level in the wrong place."
)


#: Restated on the user turn — see :func:`read_image` for why the system prompt
#: alone is not enough for the smaller vision models.
_FINDINGS_REMINDER = (
    "End your answer with the required ```json findings block, including the "
    "axis calibration."
)


@dataclass
class VisionRead:
    """Result of looking at one image."""

    narrative: str
    findings: dict[str, Any] = field(default_factory=dict)
    model: str | None = None

    @property
    def is_chart(self) -> bool:
        return bool(self.findings.get("levels") or self.findings.get("regions"))


def _data_uri(image_bytes: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"


def _split_findings(raw: str) -> tuple[str, dict[str, Any]]:
    """Peel the trailing ```json block off the model's answer.

    The prose is returned without it — the block is machinery for the annotator,
    not something to show the user.
    """
    marker = raw.rfind("```json")
    if marker == -1:
        return raw.strip(), {}
    end = raw.find("```", marker + 7)
    block = raw[marker + 7: end if end != -1 else len(raw)]
    prose = (raw[:marker] + (raw[end + 3:] if end != -1 else "")).strip()
    try:
        parsed = json.loads(block.strip())
    except json.JSONDecodeError:
        logger.debug("[Vision] trailing json block was not parseable, ignoring")
        return prose or raw.strip(), {}
    return prose or raw.strip(), parsed if isinstance(parsed, dict) else {}


async def read_image(
    image_bytes: bytes,
    mime: str,
    question: str,
    db: AsyncSession,
    *,
    source: str = "chat",
    agent_name: str = "vision",
    timeout: float | None = DEFAULT_VISION_TIMEOUT_S,
) -> VisionRead | None:
    """Look at an image and answer ``question`` about it.

    Returns None when no vision model could serve, so callers can say so plainly
    instead of showing a broken-looking silence.
    """
    from plugins.AiMarketAnalyst.backend.services.ai_router import chat_for_task

    # The dedicated vision models are small enough that a requirement stated
    # only in the system prompt gets dropped — they answer the question in prose
    # and skip the findings block, which silently costs the annotated overlay.
    # Restating it on the user turn is what actually gets it emitted.
    asked = f"{question or DEFAULT_CHART_PROMPT}\n\n{_FINDINGS_REMINDER}"

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": asked},
                {"type": "image_url", "image_url": {"url": _data_uri(image_bytes, mime)}},
            ],
        },
    ]

    try:
        resp = await chat_for_task(
            db,
            messages,
            task="vision_analysis",
            temperature=0.3,
            max_tokens=2400,
            # The OpenManus MCP hook runs ahead of the provider loop for
            # non-tool calls and cannot parse multimodal content.
            bypass_openmanus=True,
            agent_name=agent_name,
            source=source,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Vision] read failed: {}", exc)
        return None

    if not (resp.get("ok") and resp.get("content")):
        logger.warning("[Vision] no model served the image: {}", resp.get("error"))
        return None

    narrative, findings = _split_findings(str(resp["content"]))
    logger.info("[Vision] image read by {}", resp.get("model"))
    return VisionRead(narrative=narrative, findings=findings, model=resp.get("model"))
