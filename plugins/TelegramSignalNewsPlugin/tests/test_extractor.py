import pytest

from plugins.TelegramSignalNewsPlugin.backend.config import TelegramPluginConfig
from plugins.TelegramSignalNewsPlugin.backend.services.extractor import extract_message


@pytest.mark.asyncio
async def test_extract_signal_from_rule_patterns():
    cfg = TelegramPluginConfig(enable_llm_fallback=False, openai_api_key="")
    text = "BUY BTCUSDT entry 63125 sl 62200 tp1 64800"

    result = await extract_message(text=text, source_kind="signals", cfg=cfg)

    assert result.source_kind == "signals"
    assert result.direction == "buy"
    assert "BTCUSDT" in result.symbols
    assert result.levels["entry"] == 63125.0
    assert result.levels["sl"] == 62200.0
    assert result.is_signal is True


@pytest.mark.asyncio
async def test_extract_news_keywords_without_signal_levels():
    cfg = TelegramPluginConfig(enable_llm_fallback=False, openai_api_key="")
    text = "Breaking: SEC announced ETF update, market reaction expected before CPI release"

    result = await extract_message(text=text, source_kind="news", cfg=cfg)

    assert result.source_kind == "news"
    assert result.is_news is True
    assert result.is_signal is False
    assert result.confidence > 0
