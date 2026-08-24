"""Synthetic chart screenshots that reproduce what the axis detector must read.

Modelled on the two shapes users actually send: a phone screenshot with a wide
axis strip, and a desktop capture with a narrow one. Only the properties the
detector depends on are reproduced — a right-hand strip holding evenly spaced
grey price labels, a coloured live-quote chip among them, and candles that must
not be mistaken for either.
"""
from __future__ import annotations

import io

_BG = (19, 23, 34)
_TEXT = (177, 180, 190)
_CHIP = (34, 197, 94)
_UP = (38, 166, 154)
_DOWN = (239, 83, 80)


def chart_png(
    *,
    width: int = 590,
    height: int = 1280,
    strip_px: int = 106,
    top_price: float = 4460.0,
    step: float = 10.0,
    n_labels: int = 8,
    first_label_y: int = 160,
    label_gap: int = 106,
    quote_price: float | None = 4395.2,
    light: bool = False,
) -> tuple[bytes, dict[float, int]]:
    """A chart image plus the truth: {price: y_pixel} for each printed label."""
    from PIL import Image, ImageDraw, ImageFont

    bg = (255, 255, 255) if light else _BG
    text = (60, 60, 70) if light else _TEXT
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    # Axis text is sized for reading, not scaled to the canvas: a desktop
    # capture is three times as wide as a phone's but its labels are not.
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", max(11, height // 60))
    except OSError:
        font = ImageFont.load_default()

    # Candles, so the detector has real chart content to ignore.
    x = 10
    price_at = lambda y: top_price - (y - first_label_y) / label_gap * step  # noqa: E731
    while x < width - strip_px - 8:
        top = first_label_y + (x * 7) % max(1, (height - first_label_y - 300))
        draw.rectangle([x, top, x + 5, top + 60], fill=_UP if (x // 9) % 2 else _DOWN)
        x += 9

    chip_y = (
        first_label_y + (top_price - quote_price) / step * label_gap
        if quote_price is not None else None
    )

    truth: dict[float, int] = {}
    axis_x = width - strip_px + 6
    for i in range(n_labels):
        y = first_label_y + i * label_gap
        if y > height - 40:
            break
        # A label the live-quote chip covers is not drawn — the charting
        # library hides it, and a detector cannot be asked to find it.
        if chip_y is not None and abs(y - chip_y) < 16:
            continue
        price = top_price - i * step
        label = f"{price:,.3f}"
        draw.text((axis_x, y - font.size // 2), label, fill=text, font=font)
        truth[price] = y

    if quote_price is not None:
        y = chip_y
        draw.rectangle(
            [width - strip_px, y - 13, width - 1, y + 13], fill=_CHIP
        )
        draw.text(
            (axis_x, y - font.size // 2), f"{quote_price:,.3f}",
            fill=(255, 255, 255), font=font,
        )

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue(), truth
