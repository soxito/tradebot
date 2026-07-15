"""Signal/news extraction from Telegram message text."""
from __future__ import annotations

import json
import re
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from plugins.TelegramSignalNewsPlugin.backend.config import TelegramPluginConfig
from plugins.TelegramSignalNewsPlugin.backend.schemas import TelegramExtractionResult


DIRECTION_PATTERNS = {
    "buy": re.compile(r"\b(long|buy|bull(?:ish)?)\b", re.IGNORECASE),
    "sell": re.compile(r"\b(short|sell|bear(?:ish)?)\b", re.IGNORECASE),
}
SYMBOL_PATTERN = re.compile(r"\b([A-Z]{2,12}(?:USDT|USD|BTC|ETH|EUR|GBP|JPY)?)\b")
LEVEL_PATTERN = re.compile(r"\b(entry|sl|stop\s*loss|tp\d*|take\s*profit)\s*[:=@\-]?\s*(\d+(?:\.\d+)?)\b", re.IGNORECASE)

SIGNAL_KEYWORDS = ["entry", "sl", "stop loss", "tp", "target", "setup", "signal", "long", "short"]
NEWS_KEYWORDS = ["breaking", "announced", "report", "fed", "sec", "etf", "inflation", "cpi", "earnings"]

SYMBOL_STOPWORDS = {
    "BUY",
    "SELL",
    "LONG",
    "SHORT",
    "TP",
    "ENTRY",
    "SL",
    "USD",
    "USDT",
}


class LLMResult(BaseModel):
    direction: str = Field(default="unknown")
    symbols: list[str] = Field(default_factory=list)
    levels: dict[str, float] = Field(default_factory=dict)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    is_signal: bool = False
    is_news: bool = False
    summary: str = ""
    matched_keywords: list[str] = Field(default_factory=list)


async def extract_message(
    text: str,
    source_kind: str,
    cfg: TelegramPluginConfig,
) -> TelegramExtractionResult:
    normalized = _normalize_text(text)
    rule_result = _rule_extract(normalized, source_kind)

    if (
        cfg.enable_llm_fallback
        and cfg.openai_api_key
        and rule_result.confidence < 0.65
    ):
        llm_result = await _llm_extract(normalized, source_kind, cfg)
        if llm_result is not None:
            merged = _merge_results(rule_result, llm_result)
            merged.provider = "rules+llm" if rule_result.confidence > 0 else "llm"
            return merged

    return rule_result


def _rule_extract(text: str, source_kind: str) -> TelegramExtractionResult:
    source_kind = "news" if source_kind == "news" else "signals"
    direction = "unknown"
    for label, pattern in DIRECTION_PATTERNS.items():
        if pattern.search(text):
            direction = label
            break

    symbols = [
        token
        for token in sorted(set(SYMBOL_PATTERN.findall(text)))
        if token not in SYMBOL_STOPWORDS
    ]

    levels: dict[str, float] = {}
    for key, value in LEVEL_PATTERN.findall(text):
        normalized_key = key.lower().replace(" ", "")
        if normalized_key in {"stoploss", "sl"}:
            normalized_key = "sl"
        elif normalized_key.startswith("takeprofit"):
            normalized_key = normalized_key.replace("takeprofit", "tp")
        elif normalized_key == "entry":
            normalized_key = "entry"
        try:
            levels[normalized_key] = float(value)
        except ValueError:
            continue

    keywords = SIGNAL_KEYWORDS if source_kind == "signals" else NEWS_KEYWORDS
    matched_keywords = [word for word in keywords if word in text.lower()]

    is_signal = source_kind == "signals" and (bool(levels) or direction in {"buy", "sell"})
    is_news = source_kind == "news" and bool(matched_keywords)

    confidence = 0.15
    if matched_keywords:
        confidence += 0.25
    if direction in {"buy", "sell"}:
        confidence += 0.2
    if symbols:
        confidence += 0.2
    if levels:
        confidence += 0.2

    summary = _build_summary(direction=direction, symbols=symbols, levels=levels)

    return TelegramExtractionResult(
        source_kind=source_kind,
        direction=direction if direction in {"buy", "sell", "neutral"} else "unknown",
        symbols=symbols,
        levels=levels,
        confidence=min(confidence, 0.95),
        is_signal=is_signal,
        is_news=is_news,
        summary=summary,
        matched_keywords=matched_keywords,
        provider="rules",
    )


async def _llm_extract(
    text: str,
    source_kind: str,
    cfg: TelegramPluginConfig,
) -> TelegramExtractionResult | None:
    prompt = (
        "Extract structured trading signal or market news fields from Telegram text. "
        "Return JSON only with keys: direction, symbols, levels, confidence, is_signal, is_news, summary, matched_keywords."
    )

    user_msg = f"source_kind={source_kind}\ntext={text}"

    try:
        # Route through db_chat so every call goes via OpenManus → provider pool.
        from plugins.AiMarketAnalyst.backend.services.ai_router import db_chat
        content = await db_chat(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_msg},
            ],
            model=cfg.llm_model or None,
            temperature=0,
        )
    except Exception:
        return None

    if not content:
        return None

    try:
        parsed = json.loads(content)
        normalized = LLMResult.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError):
        return None

    return TelegramExtractionResult(
        source_kind="news" if source_kind == "news" else "signals",
        direction=_normalize_direction(normalized.direction),
        symbols=sorted(set(normalized.symbols)),
        levels=normalized.levels,
        confidence=normalized.confidence,
        is_signal=normalized.is_signal,
        is_news=normalized.is_news,
        summary=normalized.summary,
        matched_keywords=normalized.matched_keywords,
        provider="llm",
    )


def _merge_results(
    rules: TelegramExtractionResult,
    llm: TelegramExtractionResult,
) -> TelegramExtractionResult:
    return TelegramExtractionResult(
        source_kind=rules.source_kind,
        direction=llm.direction if llm.direction != "unknown" else rules.direction,
        symbols=sorted(set(rules.symbols + llm.symbols)),
        levels={**rules.levels, **llm.levels},
        confidence=max(rules.confidence, llm.confidence),
        is_signal=rules.is_signal or llm.is_signal,
        is_news=rules.is_news or llm.is_news,
        summary=llm.summary or rules.summary,
        matched_keywords=sorted(set(rules.matched_keywords + llm.matched_keywords)),
        provider="rules+llm",
    )


def _normalize_direction(value: str) -> str:
    lower = (value or "").lower().strip()
    if lower in {"buy", "sell", "neutral", "unknown"}:
        return lower
    return "unknown"


def _build_summary(direction: str, symbols: list[str], levels: dict[str, float]) -> str:
    chunks: list[str] = []
    if direction in {"buy", "sell"}:
        chunks.append(f"direction={direction}")
    if symbols:
        chunks.append("symbols=" + ",".join(symbols[:3]))
    if levels:
        chunks.append("levels=" + ",".join(f"{k}:{v}" for k, v in levels.items()))
    return " | ".join(chunks)


def _normalize_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", text or "")
    return compact.strip()
