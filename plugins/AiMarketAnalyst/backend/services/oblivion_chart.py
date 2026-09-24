"""OBLIVION AI v5.0 — the dedicated analysis image for Telegram.

Draws, from real OHLCV and the engine's own result
(:func:`app.signals.oblivion.analyze_oblivion`), exactly what the Pine study
shows on TradingView: session shading, FVG / order-block boxes extended to
the right, HH / HL / LH / LL swing tags, BOS / CHOCH, LIQ sweeps, the auto
fib 0.382 / 0.5 / 0.618, the last signal's Entry / SL / TP1-3 and the BUY /
SELL diamonds — with the Pine dashboard rebuilt as a proper panel: bull /
bear score bars against the required minimum, the components that earned
each point, the read grid and the trade plan.

Nothing here is inferred: every number comes from the engine result, every
shape from :func:`oblivion_to_overlays`, so the image cannot disagree with
the text the bot sends alongside it.
"""
from __future__ import annotations

import io
import math
from typing import Any, Sequence

from loguru import logger

# ── Palette (matches the app's dark charts) ──────────────────────────────────
_BG = (11, 15, 25)
_PANEL = (17, 23, 38)
_PANEL_EDGE = (36, 46, 66)
_GRID = (28, 36, 52)
_TEXT = (226, 232, 240)
_MUTED = (128, 142, 168)
_DIM = (84, 96, 120)
_UP = (38, 166, 154)
_DOWN = (239, 83, 80)
_LIME = (132, 204, 22)
_GREEN = (34, 197, 94)
_RED = (239, 68, 68)
_AQUA = (34, 211, 238)
_ORANGE = (249, 115, 22)
_YELLOW = (234, 179, 8)
_WHITE = (248, 250, 252)
_GRAY = (148, 163, 184)
_BLUE = (59, 130, 246)

_SESSION_FILL = {
    "ASIA": (59, 130, 246, 16),
    "LONDON": (234, 179, 8, 16),
    "NEW YORK": (239, 68, 68, 16),
    "LONDON + NY": (249, 115, 22, 22),
}

W, H = 1400, 880
_HEADER_H = 66
_DASH_H = 236
_PAD_L, _PAD_R, _PAD_B = 16, 96, 26
#: Share of the plot kept free on the right so boxes / lines can extend past
#: the last bar the way Pine draws them (bar_index + N).
_RIGHT_EXT = 0.14


def _font(size: int, bold: bool = False, mono: bool = False):
    from PIL import ImageFont

    names = (
        ("Menlo.ttc", "Monaco.ttf", "DejaVuSansMono.ttf") if mono
        else ("Arial Bold.ttf", "Helvetica.ttc", "DejaVuSans-Bold.ttf") if bold
        else ("Helvetica.ttc", "Arial.ttf", "DejaVuSans.ttf")
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _rgb(hex_or_rgb: Any, fallback=_GRAY) -> tuple[int, int, int]:
    if isinstance(hex_or_rgb, (tuple, list)) and len(hex_or_rgb) >= 3:
        return tuple(int(v) for v in hex_or_rgb[:3])  # type: ignore[return-value]
    s = str(hex_or_rgb or "").strip()
    if s.startswith("rgba(") or s.startswith("rgb("):
        try:
            parts = [float(p) for p in s[s.index("(") + 1:s.rindex(")")].split(",")[:3]]
            return tuple(int(p) for p in parts)  # type: ignore[return-value]
        except Exception:  # noqa: BLE001
            return fallback
    s = s.lstrip("#")
    if len(s) == 6:
        try:
            return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            return fallback
    return fallback


def _rgba(value: Any, fallback=(148, 163, 184, 20)) -> tuple[int, int, int, int]:
    """``rgba(r,g,b,a)`` / ``#rrggbb`` / a tuple → an RGBA tuple Pillow can fill with.

    The alpha matters: these are washes behind the candles, so a colour that
    arrives without one is drawn faint rather than solid.
    """
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        vals = [int(v) for v in value[:4]]
        return (vals[0], vals[1], vals[2], vals[3] if len(vals) > 3 else fallback[3])
    s = str(value or "").strip()
    if s.startswith("rgba(") or s.startswith("rgb("):
        try:
            parts = [p.strip() for p in s[s.index("(") + 1:s.rindex(")")].split(",")]
            r, g, b = (int(float(p)) for p in parts[:3])
            a = int(round(float(parts[3]) * 255)) if len(parts) > 3 else fallback[3]
            return (r, g, b, max(0, min(255, a)))
        except Exception:  # noqa: BLE001
            return fallback
    rgb = _rgb(s, fallback[:3])
    return (*rgb, fallback[3])


def _fmt(price: float | None) -> str:
    if price is None or (isinstance(price, float) and (math.isnan(price) or math.isinf(price))):
        return "—"
    a = abs(price)
    if a >= 1000:
        return f"{price:,.2f}"
    if a >= 1:
        return f"{price:.4f}".rstrip("0").rstrip(".")
    return f"{price:.6f}".rstrip("0").rstrip(".")


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _wrap(draw, text: str, font, max_w: int, max_lines: int = 2) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textbbox((0, 0), trial, font=font)[2] <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and words and draw.textbbox((0, 0), lines[-1], font=font)[2] > max_w - 20:
        lines[-1] = lines[-1][:-1] + "…"
    return lines


def render_oblivion_chart(
    candles: Sequence[Sequence[float]],
    oblivion_result: dict[str, Any] | None,
    *,
    symbol: str,
    timeframe: str,
    candle_count: int | None = 120,
    source: str = "",
) -> bytes | None:
    """Render the OBLIVION analysis image. Returns PNG bytes, or None.

    ``candles`` are ccxt rows ``[ts_ms, o, h, l, c, v]`` — the *full* series
    (the engine needs ≥ 200 bars for EMA200); only the last ``candle_count``
    are drawn. ``oblivion_result`` is reused when given (so the picture
    matches the text built from the same dict) and computed otherwise.
    """
    rows = [c for c in (candles or []) if c and len(c) >= 5]
    if len(rows) < 5:
        logger.warning("[OblivionChart] need at least 5 candles, got {}", len(rows))
        return None

    try:
        from app.signals.oblivion import analyze_oblivion, oblivion_to_overlays, timeframe_seconds
    except Exception as exc:  # noqa: BLE001
        logger.warning("[OblivionChart] engine unavailable: {}", exc)
        return None

    obl = oblivion_result
    if not obl or obl.get("error"):
        obl = analyze_oblivion(rows, timeframe)
    if obl.get("error"):
        logger.warning("[OblivionChart] engine error: {}", obl.get("error"))
        return None
    bundle = oblivion_to_overlays(obl, timeframe)
    tf_sec = timeframe_seconds(timeframe)

    view = rows[-candle_count:] if candle_count and len(rows) > candle_count else rows
    n = len(view)
    t0 = int(view[0][0])
    if t0 < 1e11:  # seconds → ms
        t0 *= 1000

    try:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (W, H), _BG)
        draw = ImageDraw.Draw(img, "RGBA")
        f_xs, f_sm, f_md = _font(11), _font(13), _font(15)
        f_b_sm, f_b_md, f_b_lg, f_b_xl = _font(12, bold=True), _font(15, bold=True), _font(20, bold=True), _font(26, bold=True)
        f_mono, f_mono_sm = _font(13, mono=True), _font(11, mono=True)

        plot_l = _PAD_L
        plot_r = W - _PAD_R
        plot_t = _HEADER_H + 30
        plot_b = H - _DASH_H - _PAD_B
        bars_r = plot_l + int((plot_r - plot_l) * (1 - _RIGHT_EXT))
        step = (bars_r - plot_l) / max(1, n)
        body_w = max(2, int(step * 0.6))

        # ── time → x, extrapolated past the last bar; None when off-window ──
        def x_of(t_sec: float, *, clamp: bool = False) -> int | None:
            idx = (float(t_sec) * 1000 - t0) / (tf_sec * 1000)
            if not clamp and (idx < -0.5 or idx > n - 0.5):
                return None
            x = plot_l + (idx + 0.5) * step
            return int(max(plot_l, min(plot_r, x)))

        # ── price → y ──────────────────────────────────────────────────────
        lows = [float(c[3]) for c in view]
        highs = [float(c[2]) for c in view]
        c_lo, c_hi = min(lows), max(highs)
        c_span = max(c_hi - c_lo, 1e-9)
        # Levels widen the axis only within reach of the candles — a 3R target
        # far below the range is reported in the plan panel rather than
        # squashing the price action into a strip.
        reach_lo, reach_hi = c_lo - c_span * 0.45, c_hi + c_span * 0.45
        extra: list[float] = []
        for pl in bundle.get("price_lines") or []:
            if (v := _num(pl.get("price"))) is not None and reach_lo <= v <= reach_hi:
                extra.append(v)
        # Boxes still alive inside the window (Pine keeps up to 300; the eye
        # needs the recent ones): newest 8 FVGs and 6 order blocks.
        t_start_sec = t0 / 1000
        live_boxes = [
            b for b in (bundle.get("boxes") or [])
            if _num(b.get("end_time")) and float(b["end_time"]) >= t_start_sec
        ]
        fvg_boxes = sorted([b for b in live_boxes if str(b.get("kind", "")).startswith("fvg")], key=lambda b: b["time"])[-8:]
        ob_boxes = sorted([b for b in live_boxes if str(b.get("kind", "")).startswith("ob")], key=lambda b: b["time"])[-6:]
        boxes = fvg_boxes + ob_boxes
        for b in boxes:
            for k in ("top", "bottom"):
                if (v := _num(b.get(k))) is not None and reach_lo <= v <= reach_hi:
                    extra.append(v)
        lo, hi = min(lows + extra), max(highs + extra)
        span = max(hi - lo, 1e-9)
        lo -= span * 0.05
        hi += span * 0.05
        span = hi - lo

        def y_of(p: float) -> int:
            return int(max(plot_t, min(plot_b, plot_b - (p - lo) / span * (plot_b - plot_t))))

        def in_view(p: float) -> bool:
            return lo <= p <= hi

        # ── Header ────────────────────────────────────────────────────────
        draw.rectangle([0, 0, W, _HEADER_H], fill=_PANEL)
        draw.line([(0, _HEADER_H), (W, _HEADER_H)], fill=_PANEL_EDGE, width=1)
        draw.text((18, 14), "OBLIVION", fill=_LIME, font=f_b_xl)
        bw = draw.textbbox((0, 0), "OBLIVION", font=f_b_xl)[2]
        draw.text((18 + bw + 8, 22), "AI TRADING ENGINE v5.0", fill=_MUTED, font=f_b_sm)
        sym_x = 18 + bw + 8 + draw.textbbox((0, 0), "AI TRADING ENGINE v5.0", font=f_b_sm)[2] + 28
        draw.text((sym_x, 16), f"{symbol}", fill=_WHITE, font=f_b_lg)
        sx = sym_x + draw.textbbox((0, 0), symbol, font=f_b_lg)[2] + 10
        draw.text((sx, 21), timeframe, fill=_MUTED, font=f_b_md)
        if source:
            draw.text((sx + draw.textbbox((0, 0), timeframe, font=f_b_md)[2] + 12, 24), f"· {source}", fill=_DIM, font=f_sm)

        sig = obl.get("signal") or {}
        action = str(sig.get("action") or "wait").upper()
        kind = str(sig.get("kind") or "")
        mode = str(obl.get("mode") or "HYBRID")
        bias = str(obl.get("bias_label") or "NEUTRAL")
        sess = str((obl.get("session") or {}).get("name") or "OFF")
        news_blocked = bool((obl.get("news") or {}).get("blocked"))

        def pill(right_x: int, text: str, bg, fg=(11, 15, 25), font=f_b_sm, y0: int = 19, h: int = 28) -> int:
            tw = draw.textbbox((0, 0), text, font=font)[2]
            x0 = right_x - tw - 22
            draw.rounded_rectangle([x0, y0, right_x, y0 + h], radius=9, fill=bg)
            draw.text((x0 + 11, y0 + (h - 14) // 2), text, fill=fg, font=font)
            return x0 - 8

        rx = W - 18
        sig_bg = _LIME if action == "BUY" else _RED if action == "SELL" else (55, 65, 81)
        sig_fg = (11, 15, 25) if action != "WAIT" else _TEXT
        sig_txt = action + (f" {kind.upper()}" if kind else "") + ("" if sig.get("confirmed") or action == "WAIT" else " · forming")
        rx = pill(rx, sig_txt, sig_bg, sig_fg)
        rx = pill(rx, mode, (31, 41, 55), _TEXT)
        rx = pill(rx, f"NEWS {'BLOCKED' if news_blocked else 'CLEAR'}", (_ORANGE if news_blocked else (31, 41, 55)),
                  (11, 15, 25) if news_blocked else _GREEN)
        rx = pill(rx, sess, (31, 41, 55), _TEXT)
        pill(rx, bias, _GREEN if bias == "BULLISH" else _RED if bias == "BEARISH" else (55, 65, 81),
             (11, 15, 25) if bias != "NEUTRAL" else _TEXT)

        # ── Sub-header: legend ──────────────────────────────────────────────
        ly = _HEADER_H + 9
        lx = 18
        for label, col in (("FVG", _GREEN), ("OB", _BLUE), ("HH/HL", _GREEN), ("LH/LL", _RED),
                           ("BOS/CHOCH", _WHITE), ("LIQ", _AQUA), ("Fib .382/.5/.618", _YELLOW),
                           ("Entry", _WHITE), ("SL", _RED), ("TP", _GREEN), ("scalp triangle", _LIME), ("sniper diamond", _LIME)):
            draw.rectangle([lx, ly + 4, lx + 8, ly + 12], fill=col)
            draw.text((lx + 12, ly), label, fill=_MUTED, font=f_xs)
            lx += 12 + draw.textbbox((0, 0), label, font=f_xs)[2] + 16
        sess_lx = W - _PAD_R - 4
        for name in ("NEW YORK", "LONDON", "ASIA"):
            c = _SESSION_FILL[name]
            tw = draw.textbbox((0, 0), name, font=f_xs)[2]
            sess_lx -= tw + 26
            draw.rectangle([sess_lx, ly + 3, sess_lx + 10, ly + 13], fill=(*c[:3], 110))
            draw.text((sess_lx + 14, ly), name, fill=_MUTED, font=f_xs)

        # ── Premium / Discount bands (hbands) ───────────────────────────────
        # A wash behind the candles: the zone must be readable at a glance and
        # never compete with price, so the alpha stays low and the label sits
        # at the band's own edge.
        for hb in bundle.get("hbands") or []:
            top, bot = _num(hb.get("top")), _num(hb.get("bottom"))
            if top is None or bot is None:
                continue
            y1, y2 = y_of(max(top, bot)), y_of(min(top, bot))
            if y2 - y1 < 2:
                continue
            label = str(hb.get("label") or "")
            base = (239, 68, 68) if "PREMIUM" in label else (34, 197, 94)
            fill = _rgba(hb.get("color"), (*base, 18))
            fill = (*fill[:3], min(fill[3], 26))       # never heavier than a wash
            draw.rectangle([plot_l, y1, plot_r, y2], fill=fill)
            if label:
                draw.text((plot_l + 6, y1 + 3), label, fill=(*fill[:3], 150), font=f_xs)

        # ── VWAP / EMA lines ───────────────────────────────────────────────
        for line in bundle.get("lines") or []:
            pts = line.get("points") or []
            if len(pts) < 2:
                continue
            col = _rgb(line.get("color"))
            for i in range(1, len(pts)):
                x0 = x_of(pts[i-1].get("time", 0))
                x1 = x_of(pts[i].get("time", 0))
                y0 = y_of(float(pts[i-1].get("value", 0)))
                y1p = y_of(float(pts[i].get("value", 0)))
                if x0 is None or x1 is None:
                    continue
                draw.line([(x0, y0), (x1, y1p)], fill=col, width=int(line.get("width", 1)))

        # ── Session bands (behind everything) ───────────────────────────────
        for band in bundle.get("bands") or []:
            x1 = x_of(band["start"], clamp=True)
            x2 = x_of(band["end"], clamp=True)
            if x1 is None or x2 is None or x2 <= x1:
                continue
            if x_of(band["end"]) is None and x_of(band["start"]) is None:
                continue  # entirely outside the window
            fill = _SESSION_FILL.get(str(band.get("label")), (148, 163, 184, 10))
            draw.rectangle([x1, plot_t, x2, plot_b], fill=fill)

        # ── Grid + axis ─────────────────────────────────────────────────────
        for i in range(7):
            price = lo + span * i / 6
            y = y_of(price)
            draw.line([(plot_l, y), (plot_r, y)], fill=_GRID, width=1)
            draw.text((plot_r + 8, y - 7), _fmt(price), fill=_MUTED, font=f_mono_sm)
        # time ticks: ~8 evenly spaced bars
        from datetime import datetime, timezone
        tick_every = max(1, n // 8)
        for i in range(tick_every // 2, n, tick_every):
            ts = int(view[i][0]); ts = ts if ts > 1e11 else ts * 1000
            dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            lab = dt.strftime("%d %b %H:%M") if tf_sec < 86400 else dt.strftime("%d %b %y")
            x = int(plot_l + (i + 0.5) * step)
            draw.line([(x, plot_b), (x, plot_b + 4)], fill=_DIM, width=1)
            draw.text((x - draw.textbbox((0, 0), lab, font=f_xs)[2] // 2, plot_b + 6), lab, fill=_DIM, font=f_xs)
        draw.line([(bars_r, plot_t), (bars_r, plot_b)], fill=(*_PANEL_EDGE, 160), width=1)
        draw.text((bars_r + 6, plot_b - 14), "projected extension", fill=_DIM, font=f_xs)

        # ── Boxes: FVG / OB ─────────────────────────────────────────────────
        for box in boxes:
            top, bot = _num(box.get("top")), _num(box.get("bottom"))
            if top is None or bot is None:
                continue
            x0 = x_of(box["time"]) or plot_l          # formed before the window → from the left edge
            x1 = x_of(box["end_time"], clamp=True)
            if x1 is None or x1 <= x0:
                continue
            kind = str(box.get("kind") or "")
            is_fvg = kind.startswith("fvg")
            bull = kind.endswith("bull")
            base = (_GREEN if bull else _RED) if is_fvg else (_BLUE if bull else _ORANGE)
            y1, y2 = y_of(max(top, bot)), y_of(min(top, bot))
            if y2 - y1 < 2:
                y2 = y1 + 2
            draw.rectangle([x0, y1, x1, y2], fill=(*base, 26), outline=(*base, 120), width=1)
            if x1 - x0 > 26 and y2 - y1 >= 12:
                draw.text((x0 + 3, y1 + 1), "FVG" if is_fvg else "OB", fill=(*base, 220), font=f_xs)

        # ── Candles ─────────────────────────────────────────────────────────
        for i, c in enumerate(view):
            o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
            x = int(plot_l + (i + 0.5) * step)
            col = _UP if cl >= o else _DOWN
            draw.line([(x, y_of(h)), (x, y_of(l))], fill=col, width=1)
            y_o, y_c = y_of(o), y_of(cl)
            if y_o == y_c:
                y_c += 1
            draw.rectangle([x - body_w // 2, min(y_o, y_c), x + body_w // 2, max(y_o, y_c)], fill=col)

        # ── Price lines: fib + Entry / SL / TP ──────────────────────────────
        used_label_y: list[int] = []
        for pl in bundle.get("price_lines") or []:
            price = _num(pl.get("price"))
            if price is None or not in_view(price):
                continue
            y = y_of(price)
            col = _rgb(pl.get("color"))
            style = str(pl.get("style") or "solid")
            width = int(pl.get("width") or 1)
            title = str(pl.get("title") or "")
            xs = plot_l
            if pl.get("from_time"):
                xs = x_of(pl["from_time"], clamp=True) or plot_l
            if style == "dotted":
                for x in range(xs, plot_r, 8):
                    draw.line([(x, y), (min(x + 3, plot_r), y)], fill=(*col, 190), width=1)
            elif style == "dashed":
                for x in range(xs, plot_r, 14):
                    draw.line([(x, y), (min(x + 8, plot_r), y)], fill=(*col, 190), width=width)
            else:
                draw.line([(xs, y), (plot_r, y)], fill=(*col, 230), width=width)
            # right-edge tag: "TP2 84,312.5"
            tag = f"{title} {_fmt(price)}"
            tw = draw.textbbox((0, 0), tag, font=f_mono_sm)[2]
            ty = y - 8
            while any(abs(ty - u) < 16 for u in used_label_y):
                ty += 16
            used_label_y.append(ty)
            draw.rounded_rectangle([plot_r - tw - 12, ty, plot_r - 2, ty + 16], radius=4, fill=(*col, 235))
            draw.text((plot_r - tw - 7, ty + 2), tag, fill=(11, 15, 25), font=f_mono_sm)

        # ── Labels: swings, BOS/CHOCH, LIQ ──────────────────────────────────
        swing_labels = [lb for lb in (bundle.get("labels") or []) if lb.get("text") in ("HH", "HL", "LH", "LL")]
        other_labels = [lb for lb in (bundle.get("labels") or []) if lb.get("text") not in ("HH", "HL", "LH", "LL")]
        placed: list[tuple[int, int, int, int]] = []

        def draw_label(lb: dict, font, pad: int = 4) -> None:
            price = _num(lb.get("price"))
            x = x_of(lb["time"])
            if price is None or x is None or not in_view(price):
                return
            txt = str(lb.get("text") or "")
            bg, fg = _rgb(lb.get("bg"), _DIM), _rgb(lb.get("color"), _WHITE)
            tw, th = draw.textbbox((0, 0), txt, font=font)[2], 14
            above = str(lb.get("position") or "above") == "above"
            gap = 8
            by0 = y_of(price) - gap - th - 4 if above else y_of(price) + gap + 4
            bx0 = x - tw // 2 - pad
            bx1 = bx0 + tw + pad * 2
            by1 = by0 + th + 2
            # nudge away from already placed labels
            tries = 0
            while any(not (bx1 < px0 or bx0 > px1 or by1 < py0 or by0 > py1) for (px0, py0, px1, py1) in placed) and tries < 6:
                by0 += (-(th + 4)) if above else (th + 4)
                by1 = by0 + th + 2
                tries += 1
            if by0 < plot_t or by1 > plot_b:
                return
            placed.append((bx0, by0, bx1, by1))
            draw.rounded_rectangle([bx0, by0, bx1, by1], radius=3, fill=(*bg, 235))
            tip = 3
            if above:
                draw.polygon([(x - tip, by1), (x + tip, by1), (x, by1 + tip)], fill=(*bg, 235))
            else:
                draw.polygon([(x - tip, by0), (x + tip, by0), (x, by0 - tip)], fill=(*bg, 235))
            draw.text((bx0 + pad, by0 + 1), txt, fill=fg, font=font)

        for lb in other_labels:
            draw_label(lb, f_b_sm)
        for lb in swing_labels:
            draw_label(lb, f_xs, pad=3)

        # ── BUY / SELL diamonds ─────────────────────────────────────────────
        bar_by_time = {}
        for c in view:
            ts = int(c[0]); ts = ts if ts > 1e11 else ts * 1000
            bar_by_time[ts // 1000] = c
        for mk in bundle.get("markers") or []:
            x = x_of(mk["time"])
            bar = bar_by_time.get(int(mk["time"]))
            if x is None or bar is None:
                continue
            text = str(mk.get("text") or "")
            below = str(mk.get("position")) == "belowBar"
            anchor = float(bar[3]) if below else float(bar[2])

            # outcome ticks: a small circle where TP or SL was hit
            if mk.get("kind") == "outcome":
                y = y_of(anchor) + (14 if below else -14)
                y = max(plot_t + 8, min(plot_b - 8, y))
                col = _GREEN if text.startswith("TP") else _RED
                draw.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(*col, 210), outline=(11, 15, 25))
                # Helvetica has no ✓ / ✗ — they would print as tofu boxes here,
                # so the ring's colour carries the verdict and the label is plain.
                plain = text.replace("✓", "").replace("✗", "").strip()
                tw = draw.textbbox((0, 0), plain, font=f_xs)[2]
                draw.text((x - tw // 2, y + 7 if below else y - 18), plain, fill=col, font=f_xs)
                continue

            is_buy = text.upper().startswith("BUY")
            sniper = mk.get("kind") == "sniper"
            y = y_of(anchor) + (24 if is_buy else -24)
            y = max(plot_t + 14, min(plot_b - 14, y))
            col = _LIME if is_buy else _RED
            sz = 10 if sniper else 8
            if sniper:      # diamond — the high-conviction entry
                draw.polygon([(x, y - sz), (x + sz, y), (x, y + sz), (x - sz, y)], fill=col, outline=_WHITE)
            elif is_buy:    # triangle up — scalp
                draw.polygon([(x, y - sz), (x + sz, y + sz), (x - sz, y + sz)], fill=col, outline=(11, 15, 25))
            else:           # triangle down — scalp
                draw.polygon([(x, y + sz), (x + sz, y - sz), (x - sz, y - sz)], fill=col, outline=(11, 15, 25))
            label = ("BUY" if is_buy else "SELL") + (" *" if sniper else "")
            tw = draw.textbbox((0, 0), label, font=f_b_sm)[2]
            ty = y + sz + 3 if is_buy else y - sz - 17
            draw.text((x - tw // 2, ty), label, fill=col, font=f_b_sm)

        # ── Last / ATR / EQ strip ───────────────────────────────────────────
        info = [f"Last {_fmt(_num(obl.get('last_price')))}", f"ATR {_fmt(_num(obl.get('atr')))}",
                f"EQ {_fmt(_num(obl.get('equilibrium')))}",
                "DISCOUNT" if obl.get("discount") else "PREMIUM" if obl.get("premium") else "EQ"]
        txt = "   ·   ".join(info)
        tw = draw.textbbox((0, 0), txt, font=f_mono_sm)[2]
        draw.rounded_rectangle([plot_l + 6, plot_t + 6, plot_l + tw + 22, plot_t + 24], radius=4,
                               fill=(11, 15, 25, 200), outline=(*_PANEL_EDGE, 200))
        draw.text((plot_l + 14, plot_t + 9), txt, fill=_TEXT, font=f_mono_sm)

        # ── Dashboard panel ─────────────────────────────────────────────────
        dy = H - _DASH_H
        draw.rectangle([0, dy, W, H], fill=_PANEL)
        draw.line([(0, dy), (W, dy)], fill=_PANEL_EDGE, width=1)
        col1_x, col2_x, col3_x = 18, 520, 1010
        draw.line([(col2_x - 20, dy + 14), (col2_x - 20, H - 40)], fill=_PANEL_EDGE, width=1)
        draw.line([(col3_x - 20, dy + 14), (col3_x - 20, H - 40)], fill=_PANEL_EDGE, width=1)

        sc = obl.get("score") or {}
        buy = int(sc.get("buy", sc.get("bull")) or 0)
        sell = int(sc.get("sell", sc.get("bear")) or 0)
        scalp_min = int(sc.get("scalp_min") or sc.get("required") or 6)
        sniper_min = int(sc.get("sniper_min") or 9)
        comps = sc.get("components") or {}

        # column 1 — score bars
        draw.text((col1_x, dy + 12), "SIGNAL SCORE", fill=_MUTED, font=f_b_sm)
        draw.text((col1_x + 110, dy + 12), f"scalp {scalp_min} · sniper {sniper_min} / 12", fill=_DIM, font=f_xs)

        def score_bar(y0: int, label: str, val: int, col, comp: dict) -> None:
            bar_x, bar_w, bar_h = col1_x + 60, 330, 16
            draw.text((col1_x, y0 - 1), label, fill=col, font=f_b_md)
            draw.rounded_rectangle([bar_x, y0, bar_x + bar_w, y0 + bar_h], radius=4, fill=(31, 41, 55))
            fill_w = int(bar_w * min(12, val) / 12)
            if fill_w > 0:
                draw.rounded_rectangle([bar_x, y0, bar_x + fill_w, y0 + bar_h], radius=4, fill=(*col, 210))
            rq = bar_x + int(bar_w * scalp_min / 12)
            draw.line([(rq, y0 - 3), (rq, y0 + bar_h + 3)], fill=_YELLOW, width=2)
            rn = bar_x + int(bar_w * sniper_min / 12)
            draw.line([(rn, y0 - 3), (rn, y0 + bar_h + 3)], fill=(217, 70, 239), width=2)
            draw.text((bar_x + bar_w + 10, y0 - 1), f"{val}", fill=col, font=f_b_md)
            draw.text((bar_x + bar_w + 10 + draw.textbbox((0, 0), str(val), font=f_b_md)[2] + 2, y0 + 2), "/12", fill=_DIM, font=f_xs)
            # component chips
            cx, cy = bar_x, y0 + bar_h + 6
            items = list(comp.items()) if comp else []
            if not items:
                draw.text((cx, cy), "no confluence", fill=_DIM, font=f_xs)
            for k, v in items:
                chip = f"{k} +{v}"
                cw = draw.textbbox((0, 0), chip, font=f_xs)[2]
                if cx + cw + 10 > col2_x - 30:
                    break
                draw.rounded_rectangle([cx, cy, cx + cw + 10, cy + 15], radius=4, fill=(*col, 34), outline=(*col, 110))
                draw.text((cx + 5, cy + 1), chip, fill=col, font=f_xs)
                cx += cw + 16

        score_bar(dy + 40, "BUY", buy, _LIME, comps.get("buy") or comps.get("bull") or {})
        score_bar(dy + 104, "SELL", sell, _RED, comps.get("sell") or comps.get("bear") or {})
        draw.text((col1_x, dy + 168), "yellow = scalp minimum · magenta = sniper minimum · a side must clear one AND beat the other",
                  fill=_DIM, font=f_xs)
        oc = obl.get("outcomes") or {}
        if oc.get("count"):
            wr = f"{oc['win_rate'] * 100:.0f}%" if oc.get("win_rate") is not None else "n/a"
            draw.text((col1_x, dy + 186),
                      f"OUTCOMES  {oc['count']} signals ({oc.get('scalp', 0)} scalp / {oc.get('sniper', 0)} sniper)  ·  "
                      f"TP1 {oc.get('tp1', 0)}  TP2 {oc.get('tp2', 0)}  TP3 {oc.get('tp3', 0)}  ·  SL {oc.get('sl', 0)}  ·  "
                      f"open {oc.get('open', 0)}  ·  win rate {wr}",
                      fill=_LIME if (oc.get("win_rate") or 0) >= 0.5 else _ORANGE, font=f_xs)

        # column 2 — read grid
        draw.text((col2_x, dy + 12), "MARKET READ", fill=_MUTED, font=f_b_sm)
        dash = obl.get("dashboard") or {}
        colour_for = {row[0]: _rgb(row[2]) for row in ((bundle.get("dashboard") or {}).get("rows") or []) if len(row) > 2}
        fibd = obl.get("fib") or {}
        trend = obl.get("trend") or {}
        grid = [("Market Bias", dash.get("Market Bias")), ("Structure", dash.get("Structure")),
                ("Trend", dash.get("Trend")), ("HTF", dash.get("HTF")),
                ("VWAP", dash.get("VWAP")), ("Zone", dash.get("Zone")),
                ("OTE", dash.get("OTE")), ("FVG", dash.get("FVG")),
                ("Liquidity", dash.get("Liquidity")), ("Session", dash.get("Session")),
                ("Volume", dash.get("Volume")), ("News", dash.get("News")),
                ("Fib zone", "IN ZONE" if fibd.get("in_zone") else "outside"),
                ("Signal", dash.get("Signal"))]
        for i, (k, v) in enumerate(grid):
            gx = col2_x + (i % 2) * 235
            gy = dy + 40 + (i // 2) * 23
            draw.text((gx, gy + 2), k, fill=_MUTED, font=f_xs)
            val = str(v if v is not None else "—")
            vc = colour_for.get(k, _WHITE if v not in (None, "NONE", "WAIT", "outside", "mixed") else _GRAY)
            if k == "Fib zone" and fibd.get("in_zone"):
                vc = _YELLOW
            tw = draw.textbbox((0, 0), val, font=f_b_sm)[2]
            draw.rounded_rectangle([gx + 88, gy, gx + 88 + tw + 12, gy + 18], radius=4, fill=(*vc, 30), outline=(*vc, 120))
            draw.text((gx + 94, gy + 2), val, fill=vc, font=f_b_sm)
        f382, f500, f618 = _num(fibd.get("f382")), _num(fibd.get("f500")), _num(fibd.get("f618"))
        if f382 is not None:
            draw.text((col2_x, dy + 208), f"fib .382 {_fmt(f382)}  ·  .5 {_fmt(f500)}  ·  .618 {_fmt(f618)}", fill=_DIM, font=f_mono_sm)

        # column 3 — trade plan
        draw.text((col3_x, dy + 12), "TRADE PLAN", fill=_MUTED, font=f_b_sm)
        plan = sig if sig.get("entry") is not None else (obl.get("last_signal") or {})
        p_action = str(plan.get("action") or "wait").upper()
        if plan.get("entry") is not None:
            pc = _LIME if p_action == "BUY" else _RED
            head = f"{p_action}"
            if plan is sig:
                head += "  ·  " + ("confirmed" if sig.get("confirmed") else "forming bar")
            else:
                head += f"  ·  last signal {plan.get('bars_since', '?')} bars ago"
            draw.text((col3_x, dy + 36), head, fill=pc, font=f_b_md)
            entry, slp = _num(plan.get("entry")), _num(plan.get("stop_loss"))
            risk = abs(entry - slp) if entry is not None and slp is not None else None
            rows_plan = [("Entry", _fmt(entry), _WHITE), ("Stop", _fmt(slp), _RED),
                         ("TP1  1R", _fmt(_num(plan.get("tp1"))), _GREEN), ("TP2  2R", _fmt(_num(plan.get("tp2"))), _GREEN),
                         ("TP3  3R", _fmt(_num(plan.get("tp3"))), _GREEN),
                         ("Risk", (_fmt(risk) + (f"  ({risk / entry * 100:.2f}%)" if risk and entry else "")) if risk else "—", _MUTED)]
            for i, (k, v, c) in enumerate(rows_plan):
                py = dy + 62 + i * 21
                draw.text((col3_x, py), k, fill=_MUTED, font=f_xs)
                draw.text((col3_x + 78, py - 1), v, fill=c, font=f_mono)
            if sig.get("blocked_by_news"):
                draw.text((col3_x, dy + 192), "entry blocked by the news window", fill=_ORANGE, font=f_b_sm)
        else:
            draw.text((col3_x, dy + 36), "WAIT — no active signal", fill=_GRAY, font=f_b_md)
            lines = _wrap(draw, "The engine needs a side to reach the minimum score and beat the other side on a closed bar.",
                          f_xs, W - col3_x - 24, 3)
            for i, ln in enumerate(lines):
                draw.text((col3_x, dy + 62 + i * 16), ln, fill=_DIM, font=f_xs)

        # summary footer
        summary = str(obl.get("summary") or "")
        for i, ln in enumerate(_wrap(draw, summary, f_sm, W - 36, 2)):
            draw.text((18, H - 36 + i * 15), ln, fill=_TEXT if i == 0 else _MUTED, font=f_sm)

        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[OblivionChart] draw failed: {}", exc)
        return None
