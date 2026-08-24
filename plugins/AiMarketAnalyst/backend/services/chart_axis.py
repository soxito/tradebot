"""Find the price axis in a chart screenshot, in pixels.

A vision model reads the *numbers* on a chart's axis reliably — they are printed
text. Where those numbers sit is a different skill, and it is the one the models
are weak at: asked for a label's height as a percentage of the image, a small
vision model is routinely off by several percent. On a 1280px screenshot of gold
that is tens of dollars, so every level drawn from its estimate lands somewhere
the chart never said, which is worse than drawing nothing at all.

The positions, though, are in the image. Axis labels are the only text in the
right-hand strip, they are evenly spaced, and they are the same colour. So this
module measures the positions here and takes only the values from the model:
each side does the half it is actually good at.

Nothing here guesses. Every function returns None when the image does not
clearly show what it is looking for.
"""
from __future__ import annotations

import io
from typing import Any

from loguru import logger

#: Fraction of the width searched for the price axis, from the right edge.
#: Wide enough for a phone screenshot's chunky axis (~18%), bounded so candles
#: never enter the search.
_STRIP_FRACTION = 0.22

#: A text row is a thin band. Anything taller is a chip, a candle body poking
#: into the strip, or the toolbar — not a price label.
_MAX_BAND_FRACTION = 0.035

#: How much of a band's rows must carry ink before it counts as text.
_INK_THRESHOLD = 0.06

#: Even spacing tolerance, as a fraction of the median gap. Axis labels are laid
#: out by the charting library, so real gaps vary only by rounding.
_SPACING_TOLERANCE = 0.18


def _pixels(image_bytes: bytes):
    """The image as a float array, or None if it cannot be read."""
    try:
        import numpy as np
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        return np.asarray(img).astype("float32"), img.size
    except Exception as exc:  # noqa: BLE001 — a bad upload must not raise here
        logger.debug("[ChartAxis] could not read image: {}", exc)
        return None, None


def _ink_mask(strip):
    """Pixels in the strip that look like axis text.

    Only near-grey pixels count. The search window is a fixed slice of the
    width, so on a chart whose candles run close to the scale some of them fall
    inside it — and a candle is far taller than a line of text, so one leaking
    in swallows the label bands next to it. Candles are coloured and axis text
    is not, which separates them without having to know where the plot ends.
    """
    import numpy as np

    grey = strip.mean(axis=2)
    sat = _saturation_map(strip)
    # The background is whatever most of the strip is — taken over the whole
    # strip, not per row, so a row full of text cannot become its own baseline.
    baseline = float(np.median(grey))
    background_sat = float(np.median(sat))
    # Axis text is the same hue as its background (grey on a dark navy panel,
    # dark grey on a white one); a candle is not. Judging colour relative to the
    # background rather than against a fixed number is what makes that true on
    # both themes — the popular dark theme's own navy is quite saturated.
    plain = sat <= background_sat + 0.15
    spread = float(np.median(np.abs(grey - baseline))) or 1.0
    return (np.abs(grey - baseline) > max(spread * 4.0, 24.0)) & plain


def _label_strip(strip):
    """Narrow the search window to the columns the axis labels occupy.

    Every measurement below is a *share* of the window's width, so the window
    has to be the labels' own columns. A phone's axis is a fifth of the screen
    and a desktop capture's is a twentieth: measured over one fixed window, the
    desktop's text dilutes below any useful threshold, and — worse — so does the
    live-quote chip, which then reads as ordinary text and joins the ladder.
    """
    mask = _ink_mask(strip)
    height, width = mask.shape
    per_column = mask.sum(axis=0)
    live = [x for x in range(width) if per_column[x] >= max(2, height * 0.005)]
    if not live:
        return None

    # Glyphs and the gaps between them belong to one label; the space between
    # the labels and the plot does not.
    gap = max(4, int(width * 0.03))
    groups: list[list[int]] = [[live[0]]]
    for x in live[1:]:
        if x - groups[-1][-1] <= gap:
            groups[-1].append(x)
        else:
            groups.append([x])
    best = max(groups, key=lambda g: int(per_column[g[0]:g[-1] + 1].sum()))
    return strip[:, best[0]:best[-1] + 1, :]


def _ink_rows(strip) -> list[float]:
    """Per-row share of inked pixels in the given window."""
    return _ink_mask(strip).mean(axis=1)


def _bands(row_ink, height: int) -> list[tuple[int, int]]:
    """Contiguous runs of inked rows, as (start, end) row indices."""
    max_band = max(2, int(height * _MAX_BAND_FRACTION))
    bands: list[tuple[int, int]] = []
    start: int | None = None
    for y, value in enumerate(row_ink):
        if value >= _INK_THRESHOLD:
            if start is None:
                start = y
        elif start is not None:
            bands.append((start, y - 1))
            start = None
    if start is not None:
        bands.append((start, len(row_ink) - 1))
    return [b for b in bands if 0 < (b[1] - b[0] + 1) <= max_band]


def _saturation_map(strip):
    """Per-pixel colour saturation, 0 (grey) to 1 (fully coloured)."""
    import numpy as np

    mx = strip.max(axis=2)
    mn = strip.min(axis=2)
    return np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)


def _coloured_share(strip):
    """Per-row share of pixels more coloured than the strip's background.

    A filled chip covers the strip, so nearly every pixel in its rows is
    coloured; a row of grey text has almost none. Measuring the share rather
    than the peak is what separates them — on the common dark theme the panel's
    own navy is saturated enough that a peak test calls every row a chip.
    """
    sat = _saturation_map(strip)
    import numpy as np

    return (sat > float(np.median(sat)) + 0.15).mean(axis=1)


def label_rows(image_bytes: bytes) -> list[float] | None:
    """Vertical centres (in pixels) of the price labels on the right axis.

    Coloured chips — the live-quote tag, bid/ask tags — are excluded: they are
    part of the axis but they are not the regularly spaced scale, and letting
    one in shifts every pairing by a row. Returns None when the strip shows no
    usable ladder.
    """
    import numpy as np

    pixels, size = _pixels(image_bytes)
    if pixels is None:
        return None
    height, width = pixels.shape[0], pixels.shape[1]
    if height < 80 or width < 80:
        return None

    strip = _label_strip(pixels[:, int(width * (1 - _STRIP_FRACTION)):, :])
    if strip is None:
        return None
    bands = _bands(_ink_rows(strip), height)
    if len(bands) < 2:
        return None

    coloured = _coloured_share(strip)
    plain: list[float] = []
    for top, bottom in bands:
        # A filled chip is coloured across nearly its whole width; grey text is
        # not. Chips are part of the axis but not part of its regular ladder,
        # and one let in shifts every pairing below it by a row.
        if float(np.median(coloured[top:bottom + 1])) > 0.35:
            continue
        plain.append((top + bottom) / 2.0)
    return plain or None


def price_tag_row(image_bytes: bytes) -> float | None:
    """Vertical centre of the live-quote chip on the axis, if one is visible.

    Used as an independent check: the chip sits exactly at the last price, so a
    calibration that puts that price somewhere else is wrong regardless of how
    neat its own numbers look.
    """
    pixels, _ = _pixels(image_bytes)
    if pixels is None:
        return None
    height, width = pixels.shape[0], pixels.shape[1]
    strip = _label_strip(pixels[:, int(width * (1 - _STRIP_FRACTION)):, :])
    if strip is None:
        return None
    bands = _bands(_coloured_share(strip), height)
    if not bands:
        return None
    # The tallest saturated band is the chip; thin coloured lines are not chips.
    top, bottom = max(bands, key=lambda b: b[1] - b[0])
    if bottom - top < 3:
        return None
    return (top + bottom) / 2.0


def _ladder(values: list[float]) -> list[float]:
    """The values that sit on one regular grid, in order.

    An axis is a grid: every label is a whole number of steps from the one
    above it. Missing rungs are normal — the live-quote chip covers the label
    behind it, and the charting library simply does not draw that one — so a
    gap of two steps still belongs to the ladder. What does not belong is a
    value off the grid entirely: an indicator's own readout, a stray glyph, or
    (measured, repeatedly) the quote chip's price listed among the labels
    despite the prompt asking for it separately. One of those sets the step to
    a fraction of the real one and every rung after it lands wrong, so the
    grid is taken from the typical gap and outliers are dropped against it.
    """
    ordered = sorted(values)
    if len(ordered) < 3:
        return ordered
    gaps = sorted(b - a for a, b in zip(ordered, ordered[1:]) if b > a)
    if not gaps:
        return ordered
    step = gaps[len(gaps) // 2]           # the typical gap, not the smallest
    if step <= 0:
        return ordered

    best: list[float] = []
    for origin in ordered:
        kept = [
            v for v in ordered
            if abs((v - origin) / step - round((v - origin) / step)) <= _SPACING_TOLERANCE
        ]
        if len(kept) > len(best):
            best = kept
    return best or ordered


def _is_even(values: list[float], tolerance: float = _SPACING_TOLERANCE) -> bool:
    """Whether these values sit on a regular grid, missing rungs allowed."""
    ordered = sorted(abs(v) for v in values)
    return len(values) >= 2 and len(_ladder(ordered)) == len(ordered)


def _slots(sequence: list[float]) -> list[int] | None:
    """Each entry's rung number on its own regular grid, counted from the first.

    ``sequence`` must already be in display order — top of the image first, so
    highest price first. Both the printed prices and the measured rows are
    ladders with the same rhythm, and counting rungs from the same end is what
    lets a partial list of labels meet the rows it belongs to, including when
    either side is missing one in the middle (the live-quote chip hides the
    label behind it, so that rung is absent from the image but present in the
    price sequence, or the other way round).
    """
    if len(sequence) < 2:
        return None
    gaps = [abs(b - a) for a, b in zip(sequence, sequence[1:]) if b != a]
    if not gaps:
        return None
    step = min(gaps)
    if step <= 0:
        return None
    slots = [round(abs(v - sequence[0]) / step) for v in sequence]
    if len(set(slots)) != len(slots) or slots != sorted(slots):
        return None
    return slots


def _fit(pairs: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """Least-squares price → y_pct, returned with its worst residual."""
    n = len(pairs)
    mean_p = sum(p for p, _ in pairs) / n
    mean_y = sum(y for _, y in pairs) / n
    denom = sum((p - mean_p) ** 2 for p, _ in pairs)
    if denom == 0:
        return None
    slope = sum((p - mean_p) * (y - mean_y) for p, y in pairs) / denom
    # Price rises up the image, so y must fall as price rises. A non-negative
    # slope means the pairing is upside down, i.e. wrong.
    if slope >= 0:
        return None
    intercept = mean_y - slope * mean_p
    residual = max(abs(slope * p + intercept - y) for p, y in pairs)
    return slope, intercept, residual


def calibrate(
    image_bytes: bytes,
    prices: list[float],
    *,
    anchor_price: float | None = None,
) -> tuple[float, float] | None:
    """Fit price → y_pct from the axis in the image and the values read off it.

    ``prices`` are axis labels the model read, in any order. They do not have to
    be all of them: vision models routinely report the first few and stop, so a
    partial list is the normal case rather than the exception.

    A partial list is ambiguous on its own. Both ladders are regular, so sliding
    the reported values up or down by a rung fits the measured rows exactly as
    well — same slope, different intercept, and a scale off by one rung puts
    every level a rung out. ``anchor_price`` resolves it: the live-quote chip is
    a price whose row is measured, not estimated, so the alignment that puts it
    where the chart draws it is the right one. Without an anchor, an ambiguous
    list is refused rather than guessed at.

    Returns ``(slope, intercept)`` in percent-of-height, or None.
    """
    # Drop anything off the printed grid before pairing. Models list the quote
    # chip's price among the labels often enough that this is the normal case,
    # and one off-grid value halves the apparent step.
    values = sorted(_ladder(sorted(
        {float(p) for p in prices if isinstance(p, (int, float))}
    )), reverse=True)
    if len(values) < 2:
        return None
    rows = label_rows(image_bytes)
    if not rows or len(rows) < 2:
        return None
    ladder = _ladder(rows)
    if len(ladder) < 2 or len(values) > len(ladder):
        logger.debug(
            "[ChartAxis] {} rungs measured for {} reported labels — not pairing",
            len(ladder), len(values),
        )
        return None

    rungs = sorted(ladder)                 # top of the image first
    price_slots = _slots(values)           # highest price first
    row_slots = _slots(rungs)
    if price_slots is None or row_slots is None:
        return None
    if price_slots[-1] > row_slots[-1]:
        logger.debug("[ChartAxis] reported labels span more rungs than the axis has")
        return None

    pixels, _ = _pixels(image_bytes)
    if pixels is None:
        return None
    height = pixels.shape[0]
    # Values run high→low down the image; rows run top→bottom. Both are indexed
    # from their own first entry, so slot k of one meets slot k+offset of the
    # other.
    rows_by_slot = {slot: y / height * 100 for slot, y in zip(row_slots, rungs)}

    candidates: list[tuple[float, float, float]] = []
    for offset in range(row_slots[-1] - price_slots[-1] + 1):
        pairs = []
        for value, slot in zip(values, price_slots):
            y = rows_by_slot.get(slot + offset)
            if y is None:
                break
            pairs.append((value, y))
        if len(pairs) != len(values):
            continue
        fit = _fit(pairs)
        if fit and fit[2] <= 1.5:  # percent of height
            candidates.append(fit)

    if not candidates:
        logger.debug("[ChartAxis] no alignment of the labels fits the measured axis")
        return None
    if len(candidates) == 1:
        slope, intercept, _ = candidates[0]
        return slope, intercept

    if anchor_price is None:
        logger.debug(
            "[ChartAxis] {} alignments fit equally well and there is no anchor "
            "price to choose between them", len(candidates),
        )
        return None

    chip = price_tag_row(image_bytes)
    if chip is None:
        logger.debug("[ChartAxis] no quote chip to anchor an ambiguous alignment")
        return None
    chip_pct = chip / height * 100
    slack = max(1.5, 100 * 12 / height)
    anchored = [
        c for c in candidates
        if abs(c[0] * anchor_price + c[1] - chip_pct) <= slack
    ]
    if len(anchored) != 1:
        logger.debug(
            "[ChartAxis] the anchor matched {} alignments — refusing", len(anchored),
        )
        return None
    slope, intercept, _ = anchored[0]
    return slope, intercept
