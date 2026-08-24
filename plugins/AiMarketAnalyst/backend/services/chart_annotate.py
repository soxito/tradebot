"""Draw a vision model's findings back onto the screenshot it read.

Deterministic overlay on the user's real pixels: the levels and regions the
model reported, positioned where it said they were. Nothing is regenerated, so
the chart under the markup stays exactly the chart the trader is looking at.

Positions come from a language model estimating percentages, so they are
approximate — the overlay is labelled as a model read for that reason. The
*prices* in the labels are what the model claimed to read off the axis; the
prompt in :mod:`vision` is explicit that inventing one is not acceptable.
"""
from __future__ import annotations

import io
from typing import Any

from loguru import logger

_SUPPORT = (34, 197, 94)
_RESISTANCE = (239, 68, 68)
_OTHER = (250, 204, 21)
_REGION = (96, 165, 250)
_BANNER_BG = (17, 24, 39)
_WHITE = (255, 255, 255)


def _colour(kind: str | None) -> tuple[int, int, int]:
    return {"support": _SUPPORT, "resistance": _RESISTANCE}.get(
        (kind or "").lower(), _OTHER
    )


_ENTRY = (56, 189, 248)
_STOP = (239, 68, 68)
_TARGET = (34, 197, 94)


def _pct(value: Any, limit: int) -> int | None:
    """Turn a 0-100 percentage into a pixel offset, rejecting nonsense."""
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    if not 0 <= pct <= 100:
        return None
    return int(round(limit * pct / 100))


def _num(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError, AttributeError):
        return None


def price_to_y(
    findings: dict[str, Any],
    height: int,
    *,
    axis_only: bool = False,
    image_bytes: bytes | None = None,
    anchor_price: float | None = None,
):
    """Build a price→pixel mapper from the chart's own price axis.

    Returns None when the scale cannot be established. That matters: without a
    calibration, a plan level would have to be placed by guesswork, and a stop
    drawn at the wrong height on a real chart is worse than no line at all.

    Three sources, in order of trust:

    1. The axis measured in the image itself, paired with the label values the
       model read off it (:mod:`chart_axis`). Positions come from pixels rather
       than from a model's estimate of a percentage, which is the difference
       between a level landing on its price and landing near it.
    2. The two calibration points the model reported.
    3. A least-squares fit over any levels it gave both a price and a position.

    ``axis_only`` keeps the first two — used when the result is applied back to
    those same levels, where fitting to them and then moving them would be
    circular.
    """
    if image_bytes:
        measured = _measured_axis(findings, image_bytes, height, anchor_price)
        if measured is not None:
            return measured

    pairs: list[tuple[float, float]] = []

    axis = findings.get("axis")
    if isinstance(axis, dict):
        top_p, top_y = _num(axis.get("top_price")), _num(axis.get("top_y_pct"))
        bot_p, bot_y = _num(axis.get("bottom_price")), _num(axis.get("bottom_y_pct"))
        if None not in (top_p, top_y, bot_p, bot_y):
            pairs = [(top_p, top_y), (bot_p, bot_y)]

    if axis_only:
        if len(pairs) < 2:
            return None
    elif not pairs:
        for lv in findings.get("levels") or []:
            if not isinstance(lv, dict):
                continue
            price, y = _num(lv.get("price")), _num(lv.get("y_pct"))
            if price is not None and y is not None:
                pairs.append((price, y))

    # Need two genuinely different prices at two different heights to fix a scale.
    prices = {p for p, _ in pairs}
    ys = {y for _, y in pairs}
    if len(pairs) < 2 or len(prices) < 2 or len(ys) < 2:
        return None

    n = len(pairs)
    mean_p = sum(p for p, _ in pairs) / n
    mean_y = sum(y for _, y in pairs) / n
    denom = sum((p - mean_p) ** 2 for p, _ in pairs)
    if denom == 0:
        return None
    slope = sum((p - mean_p) * (y - mean_y) for p, y in pairs) / denom
    if slope == 0:
        return None
    intercept = mean_y - slope * mean_p

    def mapper(price: float) -> int | None:
        y_pct = slope * price + intercept
        if not -5 <= y_pct <= 105:          # off-chart: refuse rather than clamp
            return None
        return int(round(height * min(max(y_pct, 0.0), 100.0) / 100))

    return mapper


def _measured_axis(
    findings: dict[str, Any],
    image_bytes: bytes,
    height: int,
    anchor_price: float | None = None,
):
    """A mapper built from the image's own axis, or None if it cannot be read.

    Needs the axis label *values* in printed order, which is the one part of
    this a language model does reliably. Everything positional is measured.
    """
    labels = [
        v for v in (
            _num(x) for x in (findings.get("axis_labels") or [])
        ) if v is not None
    ]
    if len(labels) < 2:
        return None

    try:
        from plugins.AiMarketAnalyst.backend.services.chart_axis import calibrate
    except Exception:  # noqa: BLE001 — annotation must survive a missing dep
        return None

    # The model rarely lists every label, and a partial list fits a regular
    # ladder in several places at once. The live quote is the tie-breaker: its
    # row is measured, so the alignment that puts it where the chart draws it is
    # the right one.
    read_price = _num(findings.get("last_price"))
    anchor = anchor_price if anchor_price is not None else read_price
    if anchor_price is not None:
        labels = _rescaled(labels, anchor_price)
    fit = calibrate(image_bytes, labels, anchor_price=anchor)
    if fit is None:
        return None
    slope, intercept = fit
    logger.info("[Annotate] price axis measured from the image ({} labels)", len(labels))

    def mapper(price: float) -> int | None:
        y_pct = slope * price + intercept
        if not -5 <= y_pct <= 105:
            return None
        return int(round(height * min(max(y_pct, 0.0), 100.0) / 100))

    return mapper


def _rescaled(labels: list[float], anchor: float) -> list[float]:
    """Put the label values back on the anchor's scale if a factor of ten was lost.

    Measured on llama-3.2-11b: "4,460.000" comes back as 4460000. Every label is
    wrong by the same factor, so the fit still looks perfect and the levels are
    then drawn a thousandfold out — the failure that hides best. A live price we
    trust says which decade the axis is really in.
    """
    if not labels or anchor <= 0:
        return labels
    low, high = min(labels), max(labels)
    span = max(high - low, 1e-9)
    if low - span <= anchor <= high + span:
        return labels                       # already on the same scale

    for exponent in (-6, -5, -4, -3, -2, -1, 1, 2, 3, 4, 5, 6):
        factor = 10.0 ** exponent
        scaled = [v * factor for v in labels]
        if min(scaled) - span * factor <= anchor <= max(scaled) + span * factor:
            logger.info(
                "[Annotate] axis labels rescaled by 1e{} to match the live price "
                "({} → {})", exponent, labels[0], scaled[0],
            )
            return scaled
    return labels


def _agrees_with_quote_chip(mapper, current_price, image_bytes, height: int) -> bool:
    """Check the calibration against the one price the image places for us.

    The live-quote chip on the axis sits exactly at the last price. If the
    scale we are about to draw with puts that price somewhere else, the scale
    is wrong — no matter how tidy its own arithmetic looked — and the plan
    lines it would produce are prices the chart never showed. Missing chip or
    missing quote means no opinion, so drawing goes ahead.
    """
    if current_price is None or not image_bytes:
        return True
    try:
        from plugins.AiMarketAnalyst.backend.services.chart_axis import price_tag_row
    except Exception:  # noqa: BLE001
        return True

    chip_y = price_tag_row(image_bytes)
    if chip_y is None:
        return True
    predicted = mapper(current_price)
    if predicted is None:
        logger.info("[Annotate] live price falls outside the calibrated scale")
        return False

    slack = max(12.0, height * 0.02)
    if abs(predicted - chip_y) > slack:
        logger.info(
            "[Annotate] scale rejected: {} maps to y={} but the chart's own "
            "quote chip is at y={}", current_price, predicted, round(chip_y),
        )
        return False
    return True


def _load_font(size: int):
    from PIL import ImageFont

    for name in ("DejaVuSans.ttf", "Arial.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _label(draw, x: int, y: int, text: str, colour, font, max_x: int,
           taken: list[tuple[int, int, int, int]] | None = None,
           max_y: int | None = None, above: bool = False) -> None:
    """Draw text on a filled chip so it stays readable over any chart.

    ``max_x`` is the image width: a chip anchored near the right edge is pulled
    back inside it rather than being clipped mid-word.

    ``above`` treats ``y`` as the line the chip belongs to and seats the chip
    just clear of it, so a level's own line never strikes through its price.

    ``taken`` collects the chips already placed. Levels that sit close together
    — and levels near the top edge, whose labels all clamp to y=0 — would
    otherwise stack into an unreadable pile, so a chip that would collide is
    nudged until it finds clear space. Two overlapping labels are two prices
    the user cannot check.
    """
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    w, h = right - left, bottom - top
    x = max(0, min(x, max_x - w - 9))
    box_h = h + 6

    if above:
        # Prefer sitting above the line; drop below it when there is no room.
        y = y - box_h - 4 if y - box_h - 4 >= 0 else y + 5

    if taken is not None:
        limit = (max_y - box_h) if max_y is not None else None
        for _ in range(24):
            box = (x, y, x + w + 8, y + box_h)
            if not any(
                box[0] < t[2] and t[0] < box[2] and box[1] < t[3] and t[1] < box[3]
                for t in taken
            ):
                break
            y += box_h + 2
            if limit is not None and y > limit:
                y = max(0, limit)
                break
        taken.append((x, y, x + w + 8, y + box_h))

    draw.rectangle([x, y, x + w + 8, y + box_h], fill=colour)
    draw.text((x + 4, y + 3 - top), text, fill=_WHITE, font=font)


def annotate(
    image_bytes: bytes,
    findings: dict[str, Any],
    plan: dict[str, Any] | None = None,
) -> bytes | None:
    """Overlay ``findings`` on the image. Returns PNG bytes, None if nothing to draw.

    ``plan`` is the live trade proposal (``proposed_entry``/``sl``/``tp1``/``tp2``
    as produced by the analysis route). Those are real prices rather than
    positions, so they are only drawn when the chart's own axis can be
    calibrated — see :func:`price_to_y`.
    """
    levels = [lv for lv in (findings.get("levels") or []) if isinstance(lv, dict)]
    regions = [rg for rg in (findings.get("regions") or []) if isinstance(rg, dict)]
    if not levels and not regions and not plan:
        return None

    try:
        from PIL import Image, ImageDraw

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        w, h = img.size
        font = _load_font(max(13, w // 55))

        drawn = 0
        # Chips already placed, so later ones can dodge rather than overlap.
        taken: list[tuple[int, int, int, int]] = []
        # Every line is drawn before any chip: a level added later spans the
        # full width and would otherwise strike through a price already
        # written, leaving the user a number they cannot read.
        pending: list[tuple[int, int, str, tuple[int, int, int]]] = []

        for region in regions:
            x = _pct(region.get("x_pct"), w)
            y = _pct(region.get("y_pct"), h)
            rw = _pct(region.get("w_pct"), w)
            rh = _pct(region.get("h_pct"), h)
            if None in (x, y, rw, rh) or rw <= 0 or rh <= 0:
                continue
            draw.rectangle(
                [x, y, min(x + rw, w - 1), min(y + rh, h - 1)], outline=_REGION, width=3
            )
            if label := str(region.get("label") or "").strip():
                pending.append((x, y, label, _REGION))
            drawn += 1

        # A level carrying a price it read off the axis belongs at that price.
        # The model eyeballs y_pct but *reads* the number, so when the axis is
        # calibrated the arithmetic beats the estimate — and it puts the level
        # lines in the same coordinate system as the plan lines below, so the
        # two can never contradict each other on the same image.
        anchor_price = _num((plan or {}).get("current_price"))
        level_axis = price_to_y(
            findings, h, axis_only=True, image_bytes=image_bytes,
            anchor_price=anchor_price,
        )
        # A scale the chart's own quote chip contradicts is worse than no scale:
        # the model's rough y_pct for each level at least came from looking at
        # the picture, while a wrong scale moves every level by the same error
        # and looks authoritative doing it.
        if level_axis is not None and not _agrees_with_quote_chip(
            level_axis, anchor_price or _num(findings.get("last_price")),
            image_bytes, h,
        ):
            level_axis = None

        for level in levels:
            y = _pct(level.get("y_pct"), h)
            if level_axis is not None and (priced := _num(level.get("price"))) is not None:
                y = level_axis(priced) if level_axis(priced) is not None else y
            if y is None:
                continue
            colour = _colour(level.get("kind"))
            draw.line([(0, y), (w, y)], fill=colour, width=3)
            parts = [str(level.get("label") or "").strip(), str(level.get("price") or "").strip()]
            if text := "  ".join(p for p in parts if p):
                pending.append((8, y, text, colour))
            drawn += 1

        # ── Trade plan, placed by real price via the chart's own axis ────────
        plan_drawn = False
        if plan:
            to_y = price_to_y(findings, h, image_bytes=image_bytes,
                              anchor_price=anchor_price)
            if to_y is not None and not _agrees_with_quote_chip(
                to_y, _num(plan.get("current_price")), image_bytes, h
            ):
                to_y = None
            if to_y is None:
                logger.info("[Annotate] price axis not readable — plan levels skipped")
            else:
                side = str(plan.get("side") or "").lower()
                rows = [
                    ("ENTRY", plan.get("proposed_entry") or plan.get("entry"), _ENTRY),
                    ("SL", plan.get("sl"), _STOP),
                    ("TP1", plan.get("tp1"), _TARGET),
                    ("TP2", plan.get("tp2"), _TARGET),
                ]
                for name, raw, colour in rows:
                    price = _num(raw)
                    if price is None:
                        continue
                    y = to_y(price)
                    if y is None:
                        continue
                    # Dashed, so a proposed level never reads as one the chart drew.
                    for x in range(0, w, 26):
                        draw.line([(x, y), (min(x + 15, w), y)], fill=colour, width=3)
                    text = f"{name} {price:g}"
                    if name == "ENTRY" and side:
                        text += f"  ({side.upper()})"
                    pending.append((w // 2, y, text, colour))
                    plan_drawn = True

        if not drawn and not plan_drawn:
            return None

        # Chips last, seated clear of the line each one belongs to.
        for x, line_y, text, colour in pending:
            _label(draw, x, line_y, text, colour, font, w, taken, h, above=True)

        banner = (
            "AI read + proposed plan — verify before trading"
            if plan_drawn
            else "AI read — levels are the model's estimate, verify before trading"
        )
        _label(draw, 8, h - font.size - 14, banner, _BANNER_BG, font, w)

        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Annotate] could not draw overlay: {}", exc)
        return None
