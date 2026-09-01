"""TradingAgents run execution.

Bridges the HTTP contract to ``tradingagents.graph.TradingAgentsGraph``,
translating per-run config into a ``TradingAgentsConfig``, mapping
tradebot crypto symbols to Yahoo Finance tickers, and streaming progress
out through the run's event log (phase snapshots + agent dialogue).
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

from loguru import logger

from .store import Run, store

CRYPTO_BASES = {
    "BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK",
    "DOT", "MATIC", "LTC", "BCH", "UNI", "ATOM", "XLM", "NEAR",
    "APT", "ARB", "OP", "INJ", "SUI", "PEPE", "SHIB", "TRX",
}


def map_ticker(raw: str) -> str:
    """Map a tradebot symbol to a Yahoo Finance ticker TradingAgents accepts.

    Crypto pairs (`BTC/USDT`, `BTC-USD`, `BTCUSDT`) become `BTC-USD`;
    everything else passes through with quote-currency suffixes stripped
    so equities/FX keep their exchange suffixes (`.HK`, `.T`, `.NS`...).
    """
    symbol = str(raw or "").strip().upper()
    if not symbol:
        return symbol
    if "-" in symbol and symbol.split("-", 1)[1] in {"USD", "USDT"}:
        return f"{symbol.split('-', 1)[0]}-USD"
    base = re.split(r"[/:\-]", symbol)[0]
    if len(symbol.split("/")) == 2 or symbol.endswith("USDT") or symbol.endswith("USD"):
        if base in CRYPTO_BASES:
            return f"{base}-USD"
    return symbol


PROVIDER_KEY_VARS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "xai": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# Keys whose format identifies them; a mismatched prefix means the key belongs
# to another vendor (e.g. an nvapi- NVIDIA key or an sk-or- OpenRouter key
# parked in OPENAI_API_KEY) and would be rejected at call time.
KEY_PREFIX_EXPECTATIONS = {
    "openai": lambda k: k.startswith("sk-") and not k.startswith("sk-or-"),
    "openrouter": lambda k: k.startswith("sk-or-"),
}

DEFAULT_DEEP_MODELS = {
    "openai": "gpt-5.4",
    "openrouter": "openai/gpt-5.4",
}
DEFAULT_QUICK_MODELS = {
    "openai": "gpt-5.4-mini",
    "openrouter": "openai/gpt-5.4-mini",
}

VALID_PROVIDERS = set(PROVIDER_KEY_VARS) | {"ollama", "litellm", "huggingface"}
VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def build_config(req: dict[str, Any]) -> dict[str, Any]:
    """Normalise + validate the request config into a plain dict."""
    provider = str(req.get("llm_provider") or os.getenv("TRADINGAGENTS_LLM_PROVIDER") or "openai").strip().lower()
    if provider not in VALID_PROVIDERS:
        provider = "openai"

    deep = str(
        req.get("deep_think_llm")
        or req.get("model")
        or DEFAULT_DEEP_MODELS.get(provider, "gpt-5.4")
    ).strip()
    quick = str(
        req.get("quick_think_llm")
        or DEFAULT_QUICK_MODELS.get(provider, deep)
    ).strip() or deep

    effort = str(req.get("reasoning_effort") or "medium").strip().lower()
    if effort not in VALID_EFFORTS:
        effort = "medium"

    language = str(req.get("response_language") or "en-US")

    api_key = str(req.get("api_key") or "").strip() or None

    return {
        "llm_provider": provider,
        "deep_think_llm": deep,
        "quick_think_llm": quick,
        "reasoning_effort": effort,
        "response_language": language,
        "max_debate_rounds": _clamp(req.get("max_debate_rounds"), 1, 6, 2),
        "max_risk_discuss_rounds": _clamp(req.get("max_risk_discuss_rounds"), 1, 6, 2),
        "max_recur_limit": _clamp(req.get("max_recur_limit"), 30, 150, 60),
        "api_key": api_key,
    }


PHASES = [
    ("analysts", ("market_report", "sentiment_report", "news_report", "fundamentals_report")),
    ("research_debate", None),
    ("trader", None),
    ("risk_debate", None),
]


def detect_phase(state: dict[str, Any]) -> str:
    """Best-effort pipeline phase from an AgentState snapshot."""
    invest = state.get("investment_debate_state") or {}
    risk = state.get("risk_debate_state") or {}
    if state.get("final_trade_decision"):
        return "portfolio_manager"
    if risk.get("judge_decision"):
        return "portfolio_manager"
    if risk.get("history"):
        return "risk_debate"
    if state.get("trader_investment_plan"):
        return "trader"
    if invest.get("judge_decision"):
        return "research_manager"
    if invest.get("history"):
        return "research_debate"
    reports = [
        state.get(k) for k in
        ("market_report", "sentiment_report", "news_report", "fundamentals_report")
    ]
    if any(reports):
        return "analysts"
    return "starting"


def compact_state(state: dict[str, Any]) -> dict[str, Any]:
    """Extract the UI-relevant subset of an AgentState snapshot."""
    invest = state.get("investment_debate_state") or {}
    risk = state.get("risk_debate_state") or {}
    return {
        "ticker": state.get("company_of_interest"),
        "trade_date": state.get("trade_date"),
        "market_report": bool(state.get("market_report")),
        "sentiment_report": bool(state.get("sentiment_report")),
        "news_report": bool(state.get("news_report")),
        "fundamentals_report": bool(state.get("fundamentals_report")),
        "situation_summary": bool(state.get("situation_summary")),
        "debate_turns": invest.get("count") or 0,
        "invest_judge_done": bool(invest.get("judge_decision")),
        "risk_turns": risk.get("count") or 0,
        "risk_judge_done": bool(risk.get("judge_decision")),
        "trader_plan_done": bool(state.get("trader_investment_plan")),
        "final_decision": state.get("final_trade_decision") or None,
    }


def message_payload(msg: Any) -> dict[str, Any] | None:
    """Serialise one LangChain message for SSE transport."""
    try:
        role = getattr(msg, "type", None) or getattr(msg, "__class__", type("X", (), {})).__name__
        content = getattr(msg, "content", "")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        name = getattr(msg, "name", None) or getattr(msg, "response_metadata", {}).get("model_name")
        text = str(content or "")
        if not text.strip():
            return None
        return {
            "role": role,
            "agent": name,
            "preview": text[-4000:] if len(text) > 4000 else text,
            "length": len(text),
        }
    except Exception:  # noqa: BLE001 - never let a bad message kill a paid run
        return None


def extract_result(state: Any, recommendation: Any) -> dict[str, Any]:
    """Full final payload: every report, debate transcript and the decision."""
    def _d(obj: Any) -> Any:
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        return obj

    s = _d(state) or {}
    rec = _d(recommendation) or {}
    invest = s.get("investment_debate_state") or {}
    risk = s.get("risk_debate_state") or {}

    decision_text = ""
    for candidate in (
        rec.get("action"),
        rec.get("decision"),
        (rec.get("recommendation") if isinstance(rec.get("recommendation"), dict) else None),
        s.get("final_trade_recommendation"),
    ):
        if isinstance(candidate, dict):
            decision_text = json.dumps(candidate, default=str)[:2000]
            break
        if isinstance(candidate, str) and candidate:
            decision_text = candidate[:2000]
            break

    final_decision = s.get("final_trade_decision") or ""
    return {
        "ticker": s.get("company_of_interest"),
        "trade_date": s.get("trade_date"),
        "reports": {
            "market": s.get("market_report") or "",
            "sentiment": s.get("sentiment_report") or "",
            "news": s.get("news_report") or "",
            "fundamentals": s.get("fundamentals_report") or "",
        },
        "situation_summary": s.get("situation_summary") or "",
        "investment_debate": {
            "bull_history": invest.get("bull_history") or "",
            "bear_history": invest.get("bear_history") or "",
            "judge_decision": invest.get("judge_decision") or "",
            "turns": invest.get("count") or 0,
        },
        "trader_plan": s.get("trader_investment_plan") or "",
        "risk_debate": {
            "aggressive_history": risk.get("aggressive_history") or "",
            "conservative_history": risk.get("conservative_history") or "",
            "neutral_history": risk.get("neutral_history") or "",
            "judge_decision": risk.get("judge_decision") or "",
            "turns": risk.get("count") or 0,
        },
        "final_trade_decision": final_decision,
        "decision_summary": decision_text,
        "recommendation": rec,
        "message_count": len(s.get("messages") or []),
    }


MAX_CONCURRENT_RUNS = 2


def start_run(ticker: str, trade_date: str, config: dict[str, Any], raw_req: dict[str, Any]) -> Run:
    """Create a run and execute it on a daemon thread."""
    active = store.active_count()
    if active >= MAX_CONCURRENT_RUNS:
        raise RuntimeError(f"{active} runs already in progress (max {MAX_CONCURRENT_RUNS})")

    run = store.create(ticker, trade_date, {k: v for k, v in config.items() if k != "api_key"})
    thread = threading.Thread(target=_execute, args=(run,), daemon=True, name=f"ta-run-{run.id}")
    thread.start()
    return run


def _execute(run: Run) -> None:
    ticker = map_ticker(run.ticker)
    cfg = dict(run.config)

    provider = cfg["llm_provider"]
    key_var = PROVIDER_KEY_VARS.get(provider)
    request_key = (run.config.get("api_key") or "").strip()
    env_key = os.getenv(key_var or "", "").strip()

    if provider not in ("ollama",) and key_var and not (request_key or env_key):
        run.status = "error"
        run.error = f"Missing API key: set {key_var} in .env (or pass it in the request)"
        run.phase = "failed"
        run.finished_at = _now_iso_safe()
        run.append_event("error", {"error": run.error})
        logger.warning(f"[TA:{run.id}] aborted — {run.error}")
        return

    if request_key and key_var:
        os.environ[key_var] = request_key

    try:
        from tradingagents.config import TradingAgentsConfig
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        ta_config = TradingAgentsConfig(
            results_dir=os.getenv("TRADINGAGENTS_RESULTS_DIR") or "./results",
            llm_provider=provider,
            deep_think_llm=cfg["deep_think_llm"],
            quick_think_llm=cfg["quick_think_llm"],
            reasoning_effort=cfg["reasoning_effort"],
            response_language=cfg.get("response_language") or "en-US",
            max_debate_rounds=cfg["max_debate_rounds"],
            max_risk_discuss_rounds=cfg["max_risk_discuss_rounds"],
            max_recur_limit=cfg["max_recur_limit"],
        )

        graph = TradingAgentsGraph(debug=False, config=ta_config)

        run.append_event("start", {"ticker": ticker, "trade_date": run.trade_date})

        def on_message(message: Any) -> None:
            payload = message_payload(message)
            if payload is not None:
                run.append_event("message", payload)

        def on_state(snapshot: Any) -> None:
            data = snapshot.model_dump() if hasattr(snapshot, "model_dump") else snapshot
            if not isinstance(data, dict):
                return
            run.phase = detect_phase(data)
            run.append_event("state", {"phase": run.phase, **compact_state(data)})
            if data.get("final_trade_decision"):
                run._last_full_state = data  # noqa: SLF001 - same-module use only

        final_state, recommendation = graph.propagate(
            ticker,
            run.trade_date,
            on_message=on_message,
            on_state=on_state,
        )

        run.result = extract_result(final_state, recommendation)
        run.status = "done"
        run.phase = "done"
        run.finished_at = _now_iso_safe()
        run.append_event("result", run.result)
        # Compact terminal event so SSE clients can stop early.
        run.append_event("done", {
            "status": "done",
            "decision_summary": run.result.get("decision_summary"),
            "ticker": run.result.get("ticker"),
        })
        logger.info(f"[TA:{run.id}] completed {ticker} @ {run.trade_date}")

    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[TA:{run.id}] run failed")
        run.status = "error"
        run.error = str(exc)[:2000]
        run.phase = "failed"
        run.finished_at = _now_iso_safe()
        run.append_event("error", {"error": run.error})


def _now_iso_safe() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
