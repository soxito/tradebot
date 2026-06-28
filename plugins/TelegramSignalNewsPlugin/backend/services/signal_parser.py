"""Robust parser that turns Telegram channel messages into structured signals.

Handles the common VIP-channel formats, e.g.::

    1000BONKUSDT
    🔴 SHORT
    Leverage- Cross (50X)
    Entry  Price:
    0.004373
    Take Profit ☄
    1) 0.00433
    2) 0.00429
    ⛔️ SL:
    0.00481

and::

    ⚡️⚡️ #NMR/USDT ⚡️⚡️
    Signal Type: Regular (Short)
    Leverage: Cross (20х)
    Entry Targets:
    9.314
    Take-Profit Targets:
    1) 9.17429
    Stop Targets:
    5-10%

and compact single-line formats::

    #H 🟢 LONG
    💰Entry: 0.104680
    🚫Stop Loss: 0.100493
    🎯 TP1: 0.105727
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(slots=True)
class ParsedSignal:
    symbol: str
    direction: str  # "long" | "short"
    leverage: str | None = None
    entry: float | None = None
    entry_raw: str | None = None
    stop_loss: float | None = None
    stop_loss_raw: str | None = None
    take_profits: list[float] = field(default_factory=list)
    confidence: float = 0.0


@dataclass(slots=True)
class SignalOutcome:
    """A status update referring to a previously-issued signal."""

    symbol: str
    kind: str  # "tp_hit" | "sl_hit" | "filled" | "closed"
    detail: str | None = None


_DIR_LONG = re.compile(r"\b(long|buy)\b|🟢", re.IGNORECASE)
_DIR_SHORT = re.compile(r"\b(short|sell)\b|🔴", re.IGNORECASE)
_SIGNAL_TYPE_LONG = re.compile(r"signal\s*type\s*:\s*regular\s*\(long\)", re.IGNORECASE)
_SIGNAL_TYPE_SHORT = re.compile(r"signal\s*type\s*:\s*regular\s*\(short\)", re.IGNORECASE)

# Symbol forms: #ABC/USDT, ABCUSDT, #ABC
_SYM_SLASH = re.compile(r"#?\b([A-Z0-9]{1,15})/(USDT|USD|BTC|ETH)\b")
_SYM_GLUED = re.compile(r"\b([A-Z0-9]{2,15})(USDT|USD)\b")
_SYM_HASH = re.compile(r"#([A-Z0-9]{1,15})\b")

# Forex pair symbols: EURUSD, GBPUSD, XAUUSD, USDJPY, GBPJPY, etc.
# Matches any 6-char combination of the major currency codes.
_FOREX_CCYS = (
    r"(?:EUR|GBP|AUD|NZD|USD|CAD|CHF|JPY|XAU|XAG|XPT|"
    r"NOK|SEK|DKK|SGD|HKD|MXN|ZAR|TRY|PLN|CZK|HUF|INR|"
    r"CNY|TWD|KRW|THB|MYR|IDR|PHP|VND)"
)
_SYM_FOREX = re.compile(
    rf"\b({_FOREX_CCYS})/?({_FOREX_CCYS})\b",
    re.IGNORECASE,
)

_LEVERAGE = re.compile(r"(?:leverage[\s\-:]*)?(?:cross|isolated)?\s*\(?\s*(\d{1,3})\s*[xх]\s*\)?", re.IGNORECASE)

_NUM = r"(\d+(?:\.\d+)?)"

# Outcome detectors
_TP_HIT = re.compile(r"take[-\s]?profit\s*target\s*(\d+).*?profit\s*[:=]?\s*([\d.]+%)", re.IGNORECASE | re.DOTALL)
_SL_HIT = re.compile(r"\bstop\s*loss\b|\bstoploss\b|closed at stoploss", re.IGNORECASE)
_ALL_ENTRIES = re.compile(r"all\s*entries\s*achieved", re.IGNORECASE)
_CLOSED = re.compile(r"\bclosed\b|\bcancel(?:led)?\b", re.IGNORECASE)
# Detects "Closed due to opposite direction signal" — the channel is telling us
# the direction has reversed and the old signal is invalid. The next message
# for the same symbol will contain the new entry in the reversed direction.
_OPPOSITE_DIR = re.compile(
    r"closed\s+due\s+to\s+opposite\s+direction"
    r"|opposite\s+direction\s+signal"
    r"|direction\s+(?:reversed|changed|flipped)"
    r"|contra(?:ry|dict)\s+signal",
    re.IGNORECASE,
)


def _detect_direction(text: str) -> str | None:
    if _SIGNAL_TYPE_LONG.search(text):
        return "long"
    if _SIGNAL_TYPE_SHORT.search(text):
        return "short"
    has_long = bool(_DIR_LONG.search(text))
    has_short = bool(_DIR_SHORT.search(text))
    if has_short and not has_long:
        return "short"
    if has_long and not has_short:
        return "long"
    if has_short and has_long:
        # Prefer the explicit word over the emoji
        if re.search(r"\bshort\b", text, re.IGNORECASE):
            return "short"
        if re.search(r"\blong\b", text, re.IGNORECASE):
            return "long"
    return None


def _detect_symbol(text: str) -> str | None:
    m = _SYM_SLASH.search(text)
    if m:
        return f"{m.group(1)}{m.group(2)}".upper()
    m = _SYM_GLUED.search(text)
    if m:
        return f"{m.group(1)}{m.group(2)}".upper()
    # Forex pair (e.g. EURUSD, EUR/USD, XAUUSD) — check before #HASH to avoid
    # misidentifying short keyword tokens.
    m = _SYM_FOREX.search(text)
    if m:
        return f"{m.group(1)}{m.group(2)}".upper().replace("/", "")
    m = _SYM_HASH.search(text)
    if m:
        token = m.group(1).upper()
        if token not in {"LONG", "SHORT", "BUY", "SELL", "TP", "SL", "VIP"}:
            return token
    return None


def _detect_leverage(text: str) -> str | None:
    m = _LEVERAGE.search(text)
    if m:
        return f"{m.group(1)}x"
    return None


def _detect_entry(text: str) -> tuple[float | None, str | None]:
    # ── Priority 1: "Entry Targets:" / "Entry Price:" followed by a numbered list
    # Many VIP channels format entries as:
    #   Entry Targets:
    #   1) 42.20
    #   2) 41.90
    # The old pattern captured the "1" from "1)" as the price. Fix: when a
    # numbered list follows, collect all entries and return their average (best
    # all-in price = what most traders use as the average fill).
    numbered_block = re.search(
        r"entry\s*(?:price|targets?)\s*[:=]?\s*\n((?:\s*\d+\)\s*[\d.]+\s*\n?)+)",
        text, re.IGNORECASE,
    )
    if numbered_block:
        vals: list[float] = []
        for mm in re.finditer(r"\d+\)\s*([\d.]+)", numbered_block.group(1)):
            try:
                vals.append(float(mm.group(1)))
            except ValueError:
                pass
        if vals:
            avg = round(sum(vals) / len(vals), 10)
            return avg, f"{vals[0]}"   # store first as raw, avg as float

    # ── Priority 2: Labelled entry, possibly value on the next line (no list) ──
    patterns = [
        rf"entry\s*price\s*[:=]?\s*\n?\s*{_NUM}",
        rf"entry\s*targets?\s*[:=]?\s*\n?\s*{_NUM}",
        rf"💰\s*entry\s*[:=]?\s*{_NUM}",
        rf"\bentry\b\s*[:=]?\s*\n?\s*{_NUM}",
        rf"average\s*entry\s*price\s*[:=]?\s*{_NUM}",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw = m.group(1)
            try:
                val = float(raw)
                # Sanity-check: a single digit (1, 2, …) is very unlikely to be a
                # real entry price for any live crypto pair — skip it and fall
                # through so the next pattern can find the actual price.
                if val < 0.00001:
                    continue
                return val, raw
            except ValueError:
                continue
    return None, None


def _detect_stop_loss(text: str) -> tuple[float | None, str | None]:
    # Percentage-based SL like "5-10%" or "5%" — check BEFORE numeric so the
    # leading digit of a percentage range isn't mistaken for a price.
    m = re.search(r"stop\s*targets?\s*[:=]?\s*\n?\s*(\d+\s*[-–]\s*\d+\s*%)", text, re.IGNORECASE)
    if m:
        return None, m.group(1).replace(" ", "")
    m = re.search(r"(?:stop\s*loss|sl)\s*[:=]?\s*\n?\s*(\d+\s*[-–]\s*\d+\s*%|\d+\s*%)", text, re.IGNORECASE)
    if m:
        return None, m.group(1).replace(" ", "")

    # Numeric SL on same or next line
    patterns = [
        rf"(?:⛔️?|🚫)?\s*(?:stop\s*loss|sl)\s*[:=]?\s*\n?\s*{_NUM}",
        rf"stop\s*targets?\s*[:=]?\s*\n?\s*{_NUM}",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw = m.group(1)
            try:
                return float(raw), raw
            except ValueError:
                continue
    return None, None


def _detect_take_profits(text: str) -> list[float]:
    tps: list[float] = []
    # Find the Take Profit block, then collect numbered lines
    block_match = re.search(
        r"(take[-\s]?profit[^\n]*|targets?\s*:?)(.*?)(?:⛔|stop|sl[:\s]|$)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    block = block_match.group(2) if block_match else text

    # Numbered list: "1) 0.00433"  — skip lines that are emojis-only (e.g. "7) 🚀🚀🚀")
    for m in re.finditer(rf"^\s*\d+\)\s*{_NUM}", block, re.MULTILINE):
        try:
            tps.append(float(m.group(1)))
        except ValueError:
            pass

    # Labelled: "TP1: 0.105727"
    if not tps:
        for m in re.finditer(rf"tp\s*\d*\s*[:=]?\s*{_NUM}", text, re.IGNORECASE):
            try:
                tps.append(float(m.group(1)))
            except ValueError:
                pass

    # Deduplicate while preserving order, cap at 8
    seen: set[float] = set()
    ordered: list[float] = []
    for v in tps:
        if v not in seen:
            seen.add(v)
            ordered.append(v)
    return ordered[:8]


def parse_entry_signal(text: str) -> ParsedSignal | None:
    """Return a ParsedSignal if the text is an actionable entry, else None."""
    if not text or not text.strip():
        return None

    direction = _detect_direction(text)
    symbol = _detect_symbol(text)
    if direction is None or symbol is None:
        return None

    entry, entry_raw = _detect_entry(text)
    sl, sl_raw = _detect_stop_loss(text)
    tps = _detect_take_profits(text)

    # Must have an entry and at least one of TP/SL to be actionable
    if entry is None and entry_raw is None:
        return None
    if not tps and sl is None and sl_raw is None:
        return None

    leverage = _detect_leverage(text)

    # ── Auto-correct direction from TP placement ──────────────────────────
    # If the majority of TPs are on the WRONG side of entry relative to the
    # detected direction, the parser likely caught a direction keyword from
    # context (channel name, previous message) rather than the actual signal.
    # Example: BUY at 4038 with TP1 4031, TP2 4024 → clearly a SELL.
    if entry is not None and entry > 0 and tps:
        tps_in_profit_long = sum(1 for tp in tps if tp > entry * 1.001)
        tps_in_profit_short = sum(1 for tp in tps if tp < entry * 0.999)
        if direction == "long" and tps_in_profit_short > tps_in_profit_long:
            direction = "short"
        elif direction == "short" and tps_in_profit_long > tps_in_profit_short:
            direction = "long"

    # ── After direction correction, also fix the SL if it's clearly on the
    # wrong side.  We prefer keeping the SL value; only swap direction
    # above when TPs clearly dictate it.  If the SL ends up on the wrong
    # side after correction, leave it as-is — the reconcile sanitisation
    # will handle it gracefully rather than guessing a new value.
    # ─────────────────────────────────────────────────────────────────────

    confidence = 0.4
    if entry is not None:
        confidence += 0.2
    if tps:
        confidence += 0.2
    if sl is not None or sl_raw is not None:
        confidence += 0.1
    if leverage:
        confidence += 0.1

    return ParsedSignal(
        symbol=symbol,
        direction=direction,
        leverage=leverage,
        entry=entry,
        entry_raw=entry_raw,
        stop_loss=sl,
        stop_loss_raw=sl_raw,
        take_profits=tps,
        confidence=min(confidence, 1.0),
    )


def parse_outcome(text: str) -> SignalOutcome | None:
    """Return a SignalOutcome if the text is a status update, else None."""
    if not text:
        return None

    symbol = _detect_symbol(text)
    if symbol is None:
        return None

    # ── Highest priority: opposite-direction close ───────────────────────
    # e.g. "#ONDO/USDT Closed due to opposite direction signal ⚠"
    # Must check BEFORE _CLOSED so this specific kind is set correctly.
    if _OPPOSITE_DIR.search(text):
        return SignalOutcome(symbol=symbol, kind="opposite_direction", detail="Direction reversed — new signal expected")

    if _TP_HIT.search(text):
        m = _TP_HIT.search(text)
        detail = f"TP{m.group(1)} +{m.group(2)}" if m else None
        return SignalOutcome(symbol=symbol, kind="tp_hit", detail=detail)
    if _SL_HIT.search(text):
        return SignalOutcome(symbol=symbol, kind="sl_hit", detail=None)
    if _ALL_ENTRIES.search(text):
        return SignalOutcome(symbol=symbol, kind="filled", detail=None)
    if _CLOSED.search(text):
        return SignalOutcome(symbol=symbol, kind="closed", detail=None)
    return None
