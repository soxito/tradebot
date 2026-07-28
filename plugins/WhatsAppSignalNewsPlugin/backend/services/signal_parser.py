"""WhatsApp Signal Parser.

Extracts structured trading signals from WhatsApp message text.
Supports multiple common signal formats from Telegram/Whale groups.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum

from loguru import logger


class SignalDirection(str, Enum):
    BUY = "buy"
    SELL = "sell"
    LONG = "long"
    SHORT = "short"

    @classmethod
    def from_str(cls, value: str) -> "SignalDirection":
        v = value.strip().lower()
        if v in ("buy", "long", "l"):
            return cls.BUY
        if v in ("sell", "short", "s"):
            return cls.SELL
        raise ValueError(f"Unknown direction: {value}")


class MarketType(str, Enum):
    CRYPTO = "crypto"
    FOREX = "forex"
    FUTURES = "futures"
    SPOT = "spot"


@dataclass
class ParsedSignal:
    """Parsed trading signal from message text."""

    symbol: str
    direction: SignalDirection
    leverage: Optional[int] = None
    entry: Optional[float] = None
    entry_raw: Optional[str] = None
    stop_loss: Optional[float] = None
    stop_loss_raw: Optional[str] = None
    trailing_sl: Optional[float] = None
    take_profits: List[float] = field(default_factory=list)
    market_type: MarketType = MarketType.CRYPTO
    confidence: float = 0.8
    raw_text: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction.value,
            "leverage": self.leverage,
            "entry": self.entry,
            "entry_raw": self.entry_raw,
            "stop_loss": self.stop_loss,
            "stop_loss_raw": self.stop_loss_raw,
            "trailing_sl": self.trailing_sl,
            "take_profits": self.take_profits,
            "market_type": self.market_type.value,
            "confidence": self.confidence,
            "raw_text": self.raw_text,
            "metadata": self.metadata,
        }


class SignalParseError(Exception):
    """Signal parsing error."""

    pass


# ────────────────────────────────────────────────────────────────────
# Regex Patterns
# ────────────────────────────────────────────────────────────────────

# Symbol patterns
SYMBOL_PATTERN = r"([A-Z]{2,10}[-/]?(?:USDT|USDC|BUSD|BTC|ETH|BNB|USD|EUR|GBP|JPY))"
SYMBOL_ALT_PATTERN = r"([#$]?[A-Z]{2,10})"

# Direction patterns
DIRECTION_PATTERNS = [
    r"(?i)\b(buy|long|l)\b",
    r"(?i)\b(sell|short|s)\b",
    r"(?i)(🟢|🔵|📈)\s*(buy|long)",
    r"(?i)(🔴|🟠|📉)\s*(sell|short)",
]

# Entry patterns
ENTRY_PATTERNS = [
    r"(?i)(?:entry|enter|ep|price|@)\s*[:=]?\s*([\d.,]+(?:\s*[-~]\s*[\d.,]+)?)",
    r"(?i)(?:market|now)\s*[:=]?\s*(market|now)",
    r"(?i)💰\s*([\d.,]+)",
]

# Stop loss patterns
SL_PATTERNS = [
    r"(?i)(?:sl|stop\s*loss|stoploss|stop)\s*[:=]?\s*([\d.,]+)",
    r"(?i)🛑\s*([\d.,]+)",
    r"(?i)(?:invalid|void)\s*(?:below|under|<)\s*([\d.,]+)",
]

# Take profit patterns
TP_PATTERNS = [
    r"(?i)(?:tp|target|take\s*profit)(?:\s*\d+)?\s*[:=]?\s*([\d.,]+)",
    r"(?i)(?:tp|target)(?:\s*[1-5])?\s*[:=]?\s*([\d.,]+)",
    r"(?i)🎯\s*([\d.,]+)",
    r"(?i)(?:t1|t2|t3|t4|t5)\s*[:=]?\s*([\d.,]+)",
]

# Leverage pattern
LEVERAGE_PATTERN = r"(?i)(?:leverage|lev|x)\s*[:=]?\s*(\d{1,3})"

# Trailing stop pattern
TRAILING_SL_PATTERN = r"(?i)(?:trail|trailing)\s*(?:sl|stop)?\s*[:=]?\s*([\d.,]+)"

# Pair with exchange prefix (e.g., BINANCE:BTCUSDT)
EXCHANGE_SYMBOL_PATTERN = r"(?i)(?:binance|bybit|okx|kucoin|bitget|coinbase|mexc|gate)\s*[:]\s*" + SYMBOL_PATTERN


def _normalize_price(price_str: str) -> float:
    """Normalize price string to float."""
    cleaned = price_str.replace(",", "").replace(" ", "")
    # Handle ranges like "100-105" or "100~105" - take midpoint
    if "-" in cleaned or "~" in cleaned:
        parts = re.split(r"[-~]", cleaned)
        try:
            return (float(parts[0]) + float(parts[-1])) / 2
        except (ValueError, IndexError):
            pass
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _extract_symbol(text: str) -> Optional[str]:
    """Extract trading symbol from text."""
    # Try exchange-prefixed first
    for pattern in [EXCHANGE_SYMBOL_PATTERN, SYMBOL_PATTERN, SYMBOL_ALT_PATTERN]:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            symbol = matches[0].upper().replace("$", "").replace("#", "")
            # Normalize separators
            symbol = symbol.replace("/", "").replace("-", "")
            # Ensure USDT/USDC suffix
            if not any(symbol.endswith(s) for s in ("USDT", "USDC", "BUSD", "BTC", "ETH", "USD")):
                symbol += "USDT"
            return symbol
    return None


def _extract_direction(text: str) -> Optional[SignalDirection]:
    """Extract trade direction from text."""
    for pattern in DIRECTION_PATTERNS:
        match = re.search(pattern, text)
        if match:
            # Get the direction word (last group or whole match)
            groups = match.groups()
            direction_word = groups[-1] if groups else match.group(0)
            try:
                return SignalDirection.from_str(direction_word)
            except ValueError:
                continue
    return None


def _extract_entry(text: str) -> tuple[Optional[float], Optional[str]]:
    """Extract entry price."""
    for pattern in ENTRY_PATTERNS:
        match = re.search(pattern, text)
        if match:
            raw = match.group(1) if match.groups() else match.group(0)
            if raw.lower() in ("market", "now"):
                return None, "market"
            price = _normalize_price(raw)
            if price > 0:
                return price, raw
    return None, None


def _extract_stop_loss(text: str) -> tuple[Optional[float], Optional[str]]:
    """Extract stop loss price."""
    for pattern in SL_PATTERNS:
        match = re.search(pattern, text)
        if match:
            raw = match.group(1)
            price = _normalize_price(raw)
            if price > 0:
                return price, raw
    return None, None


def _extract_take_profits(text: str) -> List[float]:
    """Extract all take profit levels."""
    tps = []
    for pattern in TP_PATTERNS:
        matches = re.findall(pattern, text)
        for match in matches:
            price = _normalize_price(match)
            if price > 0 and price not in tps:
                tps.append(price)
    return sorted(tps)


def _extract_leverage(text: str) -> Optional[int]:
    """Extract leverage."""
    match = re.search(LEVERAGE_PATTERN, text)
    if match:
        try:
            lev = int(match.group(1))
            if 1 <= lev <= 125:
                return lev
        except ValueError:
            pass
    return None


def _extract_trailing_sl(text: str) -> Optional[float]:
    """Extract trailing stop loss."""
    match = re.search(TRAILING_SL_PATTERN, text)
    if match:
        price = _normalize_price(match.group(1))
        if price > 0:
            return price
    return None


def _detect_market_type(text: str, symbol: str) -> MarketType:
    """Detect market type from context."""
    text_lower = text.lower()
    if any(kw in text_lower for kw in ("future", "perp", "futures", "contract")):
        return MarketType.FUTURES
    if any(kw in text_lower for kw in ("spot", "cash")):
        return MarketType.SPOT
    if any(kw in text_lower for kw in ("forex", "fx", "eurusd", "gbpusd", "xauusd", "xagusd")):
        return MarketType.FOREX
    # Default to crypto futures for leveraged trades
    return MarketType.FUTURES


# ────────────────────────────────────────────────────────────────────
# Main Parser
# ────────────────────────────────────────────────────────────────────

def parse_signal(text: str, channel_name: str = "") -> Optional[ParsedSignal]:
    """Parse a trading signal from message text.

    Args:
        text: Raw message text
        channel_name: Source channel name (for context)

    Returns:
        ParsedSignal if valid signal found, None otherwise
    """
    if not text or len(text.strip()) < 10:
        return None

    text_clean = text.strip()

    # Quick filter - must have signal-like keywords
    signal_keywords = ["buy", "sell", "long", "short", "entry", "tp", "sl", "target", "stop", "leverage"]
    if not any(kw in text_clean.lower() for kw in signal_keywords):
        return None

    # Extract components
    symbol = _extract_symbol(text_clean)
    if not symbol:
        logger.debug(f"No symbol found in: {text_clean[:100]}")
        return None

    direction = _extract_direction(text_clean)
    if not direction:
        logger.debug(f"No direction found in: {text_clean[:100]}")
        return None

    entry, entry_raw = _extract_entry(text_clean)
    stop_loss, sl_raw = _extract_stop_loss(text_clean)
    take_profits = _extract_take_profits(text_clean)
    leverage = _extract_leverage(text_clean)
    trailing_sl = _extract_trailing_sl(text_clean)
    market_type = _detect_market_type(text_clean, symbol)

    # Calculate confidence based on completeness
    confidence = 0.5
    if entry is not None:
        confidence += 0.15
    if stop_loss is not None:
        confidence += 0.15
    if take_profits:
        confidence += 0.1
    if leverage:
        confidence += 0.05
    if trailing_sl:
        confidence += 0.05

    signal = ParsedSignal(
        symbol=symbol,
        direction=direction,
        leverage=leverage,
        entry=entry,
        entry_raw=entry_raw,
        stop_loss=stop_loss,
        stop_loss_raw=sl_raw,
        trailing_sl=trailing_sl,
        take_profits=take_profits,
        market_type=market_type,
        confidence=min(confidence, 1.0),
        raw_text=text_clean,
        metadata={
            "channel": channel_name,
            "has_entry": entry is not None,
            "has_sl": stop_loss is not None,
            "tp_count": len(take_profits),
        },
    )

    logger.info(
        f"Parsed signal: {signal.symbol} {signal.direction.value} "
        f"entry={signal.entry} sl={signal.stop_loss} tps={signal.take_profits} "
        f"lev={signal.leverage} conf={signal.confidence:.0%}"
    )
    return signal


def parse_signals_batch(texts: List[str], channel_name: str = "") -> List[ParsedSignal]:
    """Parse multiple messages for signals."""
    signals = []
    for text in texts:
        try:
            signal = parse_signal(text, channel_name)
            if signal:
                signals.append(signal)
        except Exception as e:
            logger.warning(f"Failed to parse signal: {e}")
    return signals


# ────────────────────────────────────────────────────────────────────
# Outcome Parser (for signal updates: TP hit, SL hit, closed, etc.)
# ────────────────────────────────────────────────────────────────────

@dataclass
class SignalOutcome:
    """Parsed signal outcome."""

    kind: str  # tp_hit, sl_hit, filled, closed, opposite_direction
    symbol: str
    detail: Optional[str] = None
    tp_number: Optional[int] = None


OUTCOME_PATTERNS = [
    # TP hit
    (r"(?i)(?:tp|target)\s*(\d+)?\s*(?:hit|reached|achieved|✅)", "tp_hit"),
    (r"(?i)take\s*profit\s*(\d+)?\s*(?:hit|reached)", "tp_hit"),
    # SL hit
    (r"(?i)(?:sl|stop\s*loss|stoploss)\s*(?:hit|triggered|❌)", "sl_hit"),
    (r"(?i)stopped\s*out", "sl_hit"),
    # Filled
    (r"(?i)(?:filled|entered|triggered|✅\s*(?:entry|filled))", "filled"),
    # Closed
    (r"(?i)(?:closed|close|exit|exited|✅\s*(?:closed|exit))", "closed"),
    # Opposite direction
    (r"(?i)(?:reverse|reversal|opposite|flip)\s*(?:to|direction)?\s*(buy|sell|long|short)", "opposite_direction"),
]


def parse_outcome(text: str) -> Optional[SignalOutcome]:
    """Parse signal outcome from message text."""
    text_clean = text.strip()
    symbol = _extract_symbol(text_clean)

    for pattern, kind in OUTCOME_PATTERNS:
        match = re.search(pattern, text_clean)
        if match:
            detail = match.group(0)
            tp_num = None
            if kind == "tp_hit" and match.groups():
                try:
                    tp_num = int(match.group(1))
                except (ValueError, TypeError):
                    pass

            return SignalOutcome(
                kind=kind,
                symbol=symbol or "UNKNOWN",
                detail=detail,
                tp_number=tp_num,
            )

    return None