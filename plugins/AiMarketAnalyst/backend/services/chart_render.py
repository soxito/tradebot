"""Render a trade plan as a candlestick chart image.

Unlike :mod:`chart_annotate`, which marks up a screenshot the user sent, this
draws a chart from scratch out of real OHLCV and overlays the levels the room's
agents decided on — entry, stop, targets, order blocks, and where they expect
price to go next.

Everything drawn here comes from data the caller supplies. Nothing is inferred
or invented at this layer: if the agents produced no stop, no stop line is
drawn. The projection is explicitly styled as a forecast (dashed, into empty
space to the right of the last candle) so it can never be mistaken for price
that has actually printed.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any, Sequence

from loguru import logger

# Dark theme, so it sits well next to the app's own charts in Telegram.
_BG = (11, 15, 25)
_GRID = (30, 38, 54)
_TEXT = (226, 232, 240)
_MUTED = (110, 124, 148)
_UP = (34, 197, 94)
_DOWN = (239, 68, 68)
_ENTRY = (56, 189, 248)
_STOP = (239, 68, 68)
_TARGET = (34, 197, 94)
_OB_BULL = (34, 197, 94)
_OB_BEAR = (239, 68, 68)
_PROJECTION = (250, 204, 21)
_EMA_COLOURS = {20: (96, 165, 250), 50: (232, 121, 249), 200: (251, 146, 60)}
_FIB_COLOUR = (168, 162, 158)
_GOLDEN_ZONE = (250, 204, 21)
_SUPPORT_ZONE = (34, 197, 94)
_RESISTANCE_ZONE = (239, 68, 68)

_W, _H = 1280, 720
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 12, 104, 44, 30
#: Share of the plot reserved to the right for the projected path.
_FUTURE_FRAC = 0.18


@dataclass
class PlanOverlay:
    """The levels to draw. Every field is optional — draw what is known."""

    direction: str | None = None          # "long" | "short"
    entry: float | None = None
    stop_loss: float | None = None
    take_profits: list[float] = field(default_factory=list)
    #: {"low": float, "high": float, "kind": "bullish"|"bearish", "label": str}
    order_blocks: list[dict[str, Any]] = field(default_factory=list)
    #: Prices the agents expect ahead, in order; drawn as a dashed path.
    projection: list[float] = field(default_factory=list)
    #: {period: per-candle values, aligned to the candle list} — None where the
    #: average has no value yet (the first `period - 1` bars).
    ema: dict[int, list[float | None]] = field(default_factory=dict)
    #: {"ratio": float, "price": float, "label": str} from auto_fib_retracement.
    fib_levels: list[dict[str, Any]] = field(default_factory=list)
    #: {"low": float, "high": float} — the 0.5-0.618 retracement band.
    fib_golden_zone: dict[str, Any] | None = None
    #: {"low": float, "high": float} bands from support_resistance_mtf.
    support_zones: list[dict[str, Any]] = field(default_factory=list)
    resistance_zones: list[dict[str, Any]] = field(default_factory=list)


def _load_font(size: int):
    from PIL import ImageFont

    for name in ("DejaVuSans.ttf", "Arial.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fmt(price: float) -> str:
    """Format a price without inventing or losing precision."""
    a = abs(price)
    if a >= 1000:
        return f"{price:,.2f}"
    if a >= 1:
        return f"{price:.4f}".rstrip("0").rstrip(".")
    return f"{price:.8f}".rstrip("0").rstrip(".")


def render_plan_chart(
    candles: Sequence[Sequence[float]],
    *,
    symbol: str,
    timeframe: str,
    overlay: PlanOverlay | None = None,
    subtitle: str = "",
) -> bytes | None:
    """Draw ``candles`` with ``overlay`` on top. Returns PNG bytes, or None.

    ``candles`` are ccxt-style ``[ts, open, high, low, close, volume]``.
    """
    rows = [c for c in (candles or []) if c and len(c) >= 5]
    if len(rows) < 5:
        logger.warning("[ChartRender] need at least 5 candles, got {}", len(rows))
        return None

    overlay = overlay or PlanOverlay()

    try:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (_W, _H), _BG)
        draw = ImageDraw.Draw(img, "RGBA")
        f_sm, f_md, f_lg = _load_font(15), _load_font(17), _load_font(23)

        plot_l, plot_r = _PAD_L, _W - _PAD_R
        plot_t, plot_b = _PAD_T, _H - _PAD_B
        # Only hold back space for a forecast when there is one to draw; a HOLD
        # verdict has no projection and should use the full width for price.
        candle_r = (
            plot_l + int((plot_r - plot_l) * (1 - _FUTURE_FRAC))
            if overlay.projection else plot_r
        )

        # ── Price range: candles plus every level we intend to draw, so nothing
        # lands off-canvas (a stop drawn outside the frame is worse than none).
        lows = [float(c[3]) for c in rows]
        highs = [float(c[2]) for c in rows]
        zone_bounds = [
            b.get(edge)
            for b in (
                overlay.order_blocks + overlay.support_zones + overlay.resistance_zones
                + ([overlay.fib_golden_zone] if overlay.fib_golden_zone else [])
            )
            for edge in ("low", "high")
        ]
        extra = [
            v for v in (
                [overlay.entry, overlay.stop_loss]
                + list(overlay.take_profits)
                + list(overlay.projection)
                + zone_bounds
                + [lv.get("price") for lv in overlay.fib_levels]
                + [v for series in overlay.ema.values() for v in series]
            ) if isinstance(v, (int, float))
        ]
        lo, hi = min(lows + extra), max(highs + extra)
        if hi <= lo:
            hi = lo + max(abs(lo) * 0.001, 1e-8)
        span = hi - lo
        lo -= span * 0.06
        hi += span * 0.06
        span = hi - lo

        def y_of(price: float) -> int:
            return int(plot_b - (price - lo) / span * (plot_b - plot_t))

        step = (candle_r - plot_l) / max(1, len(rows))
        body_w = max(1, int(step * 0.62))

        # ── Grid + price axis ────────────────────────────────────────────────
        for i in range(6):
            price = lo + span * i / 5
            y = y_of(price)
            draw.line([(plot_l, y), (plot_r, y)], fill=_GRID, width=1)
            draw.text((plot_r + 8, y - 8), _fmt(price), fill=_MUTED, font=f_sm)

        def band(low: Any, high: Any, colour, alpha: int, label: str = "") -> None:
            """Shade a price band across the plot. Ignores incomplete bounds."""
            if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
                return
            if high < low:
                low, high = high, low
            draw.rectangle([plot_l, y_of(high), plot_r, y_of(low)], fill=(*colour, alpha))
            if label:
                draw.text((plot_l + 8, y_of(high) + 3), label, fill=(*colour, 235), font=f_sm)

        # ── Context bands: drawn first so candles and levels stay readable ───
        for zone in overlay.support_zones:
            band(zone.get("low"), zone.get("high"), _SUPPORT_ZONE, 26)
        for zone in overlay.resistance_zones:
            band(zone.get("low"), zone.get("high"), _RESISTANCE_ZONE, 26)
        if overlay.fib_golden_zone:
            band(
                overlay.fib_golden_zone.get("low"), overlay.fib_golden_zone.get("high"),
                _GOLDEN_ZONE, 30, "golden zone",
            )

        # ── Order blocks: zones behind price, so candles stay readable ───────
        for block in overlay.order_blocks:
            b_lo, b_hi = block.get("low"), block.get("high")
            if not isinstance(b_lo, (int, float)) or not isinstance(b_hi, (int, float)):
                continue
            if b_hi < b_lo:
                b_lo, b_hi = b_hi, b_lo
            colour = _OB_BULL if str(block.get("kind", "")).lower().startswith("bull") else _OB_BEAR
            draw.rectangle(
                [plot_l, y_of(b_hi), plot_r, y_of(b_lo)], fill=(*colour, 38), outline=(*colour, 120)
            )
            if label := str(block.get("label") or "").strip():
                draw.text((plot_l + 8, y_of(b_hi) + 4), label, fill=(*colour, 235), font=f_sm)

        # ── Candles ──────────────────────────────────────────────────────────
        for i, c in enumerate(rows):
            o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
            x = int(plot_l + i * step + step / 2)
            colour = _UP if cl >= o else _DOWN
            draw.line([(x, y_of(h)), (x, y_of(l))], fill=colour, width=1)
            y_o, y_c = y_of(o), y_of(cl)
            if y_o == y_c:
                y_c = y_o + 1  # doji still needs a visible mark
            draw.rectangle([x - body_w // 2, min(y_o, y_c), x + body_w // 2, max(y_o, y_c)], fill=colour)

        # ── Moving averages, over the candles they describe ──────────────────
        legend_x = plot_l + 8
        for period in sorted(overlay.ema):
            series = overlay.ema[period]
            colour = _EMA_COLOURS.get(period, _MUTED)
            pts = [
                (int(plot_l + i * step + step / 2), y_of(float(v)))
                for i, v in enumerate(series[:len(rows)])
                if isinstance(v, (int, float))
            ]
            if len(pts) < 2:
                continue
            draw.line(pts, fill=colour, width=2)
            text = f"EMA{period}"
            draw.text((legend_x, plot_t + 4), text, fill=colour, font=f_sm)
            legend_x += draw.textbbox((0, 0), text, font=f_sm)[2] + 12

        # ── Fib retracement levels ───────────────────────────────────────────
        for lv in overlay.fib_levels:
            price = lv.get("price")
            if not isinstance(price, (int, float)):
                continue
            y = y_of(float(price))
            for x in range(plot_l, plot_r, 12):
                draw.line([(x, y), (min(x + 6, plot_r), y)], fill=(*_FIB_COLOUR, 170), width=1)
            label = str(lv.get("label") or "")
            if label:
                tw = draw.textbbox((0, 0), label, font=f_sm)[2]
                draw.text((plot_r - tw - 4, y - 15), label, fill=(*_FIB_COLOUR, 220), font=f_sm)

        last_close = float(rows[-1][4])

        # ── Projection: dashed, only in the empty space to the right ─────────
        if overlay.projection:
            pts = [(candle_r, y_of(last_close))]
            fstep = (plot_r - candle_r) / max(1, len(overlay.projection))
            for i, price in enumerate(overlay.projection):
                if isinstance(price, (int, float)):
                    pts.append((int(candle_r + (i + 1) * fstep), y_of(float(price))))
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                # Manual dashes — PIL has no dashed line.
                segments = max(1, int(abs(x2 - x1) / 9))
                for s in range(0, segments, 2):
                    t1, t2 = s / segments, min((s + 1) / segments, 1.0)
                    draw.line(
                        [(x1 + (x2 - x1) * t1, y1 + (y2 - y1) * t1),
                         (x1 + (x2 - x1) * t2, y1 + (y2 - y1) * t2)],
                        fill=_PROJECTION, width=2,
                    )
            draw.text((candle_r + 6, plot_t + 6), "projected", fill=_PROJECTION, font=f_sm)

        # ── Plan levels ──────────────────────────────────────────────────────
        def level(price: float | None, colour, label: str) -> None:
            if not isinstance(price, (int, float)):
                return
            y = y_of(float(price))
            draw.line([(plot_l, y), (plot_r, y)], fill=colour, width=2)
            text = f"{label} {_fmt(float(price))}"
            tw = draw.textbbox((0, 0), text, font=f_md)[2]
            draw.rectangle([plot_l, y - 20, plot_l + tw + 12, y - 1], fill=colour)
            draw.text((plot_l + 6, y - 19), text, fill=(11, 15, 25), font=f_md)

        level(overlay.entry, _ENTRY, "ENTRY")
        level(overlay.stop_loss, _STOP, "SL")
        for i, tp in enumerate(overlay.take_profits, start=1):
            level(tp, _TARGET, f"TP{i}")

        # ── Header ───────────────────────────────────────────────────────────
        head = f"{symbol}  {timeframe}"
        if overlay.direction:
            head += f"   {overlay.direction.upper()}"
        draw.text((_PAD_L, 10), head, fill=_TEXT, font=f_lg)
        if subtitle:
            draw.text((_PAD_L + draw.textbbox((0, 0), head, font=f_lg)[2] + 18, 16),
                      subtitle[:90], fill=_MUTED, font=f_md)
        draw.text((_PAD_L, _H - 22),
                  "Generated by the trading room — levels are the agents' plan, not advice. "
                  "Verify before trading.", fill=_MUTED, font=f_sm)

        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ChartRender] could not draw chart: {}", exc)
        return None
