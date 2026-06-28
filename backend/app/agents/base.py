"""
Base Agent — foundation for all AI trading agents.
Each agent wraps an OpenAI chat completion with a specialized system prompt,
receives market context, and returns a structured decision.

Supports memory-aware analysis:
  1. Checks past decisions (memory) for local pattern matching
  2. If confident from memory → returns local decision (no OpenAI call)
  3. Otherwise calls OpenAI with memory context injected into the prompt
  4. Gracefully returns safe "hold" if OpenAI is unavailable or quota exhausted

Circuit breaker: On 429/quota errors, ALL OpenAI calls are suppressed
for CIRCUIT_BREAKER_COOLDOWN seconds to avoid noisy error logs and wasted latency.
"""
import os
import json
import time
from typing import Dict, Any, Optional, List
from loguru import logger

from app.utils.headroom_compress import compress_messages

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore

# ── Circuit Breaker ──────────────────────────────────────────
# When OpenAI returns 429/quota/auth errors, suppress all calls for this period.
CIRCUIT_BREAKER_COOLDOWN = 300  # 5 minutes

_circuit_open_until: float = 0.0  # timestamp when the breaker can be retried
_circuit_reason: str = ""


def _trip_circuit(reason: str, cooldown: int = CIRCUIT_BREAKER_COOLDOWN):
    """Trip the circuit breaker — suppress OpenAI calls for `cooldown` seconds."""
    global _circuit_open_until, _circuit_reason
    _circuit_open_until = time.time() + cooldown
    _circuit_reason = reason
    logger.warning(
        f"[AI Circuit Breaker] OPEN — suppressing OpenAI calls for {cooldown}s. "
        f"Reason: {reason}"
    )


def _circuit_is_open() -> bool:
    """Check if the circuit breaker is currently tripped."""
    return time.time() < _circuit_open_until


def get_ai_status() -> dict:
    """Return the current AI availability status (for API/dashboard)."""
    available = _openai_available() and not _circuit_is_open()
    result = {
        "available": available,
        "openai_configured": _openai_available(),
        "circuit_breaker_open": _circuit_is_open(),
    }
    if _circuit_is_open():
        remaining = max(0, _circuit_open_until - time.time())
        result["circuit_breaker_reason"] = _circuit_reason
        result["circuit_breaker_remaining_s"] = round(remaining)
    return result


def _is_quota_or_auth_error(error) -> bool:
    """Detect 429, quota exhaustion, or auth errors that should trip the breaker."""
    err_str = str(error).lower()
    trigger_phrases = [
        "429", "rate limit", "quota", "insufficient_quota",
        "exceeded your current quota", "billing",
        "invalid_api_key", "authentication",
    ]
    return any(phrase in err_str for phrase in trigger_phrases)


def _openai_available() -> bool:
    """Check if OpenAI is usable without raising."""
    if AsyncOpenAI is None:
        return False
    return bool(os.getenv("OPENAI_API_KEY", ""))


def _get_client() -> "AsyncOpenAI":
    if AsyncOpenAI is None:
        raise RuntimeError("openai package not installed — run: pip install openai")
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    return AsyncOpenAI(api_key=api_key)


class BaseAgent:
    """
    Base class for all trading agents.
    Subclasses override `build_user_prompt` and optionally `parse_response`.
    """

    def __init__(
        self,
        agent_id: int,
        name: str,
        role: str,
        system_prompt: str,
        model: str = "gpt-4o-mini",
        temperature: float = 0.3,
        max_tokens: int = 2000,
    ):
        self.agent_id = agent_id
        self.name = name
        self.role = role
        self.system_prompt = system_prompt
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    # ── Subclass hooks ───────────────────────────────────────

    def build_user_prompt(self, context: Dict[str, Any]) -> str:
        """Build the user message from market context. Override in subclass."""
        return json.dumps(context, default=str)

    def parse_response(self, raw: str) -> Dict[str, Any]:
        """Parse raw LLM response into structured decision. Override in subclass."""
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    # ── Memory-aware analysis ────────────────────────────────

    async def analyze(
        self,
        context: Dict[str, Any],
        memory_prompt: str = "",
        local_decision: Optional[Dict[str, Any]] = None,
        db: Any = None,
    ) -> Dict[str, Any]:
        """
        Analyze market context with memory awareness.

        Args:
            context: Market data dict
            memory_prompt: Past-decisions / knowledge / graph summary to inject
            local_decision: If provided, skip the LLM and return this (from memory)
            db: Optional AsyncSession. When provided and the AI Market Analyst
                plugin has connected providers, the call is routed through those
                shared providers (load-balanced, usage-capped) instead of the
                local OpenAI key.

        Returns: {action, confidence, reasoning, agent_name, agent_role, ai_called, ...}
        """
        # ── Option A: Use local memory decision (no LLM call) ──
        if local_decision is not None:
            logger.info(
                f"[Agent:{self.name}] LOCAL decision for {context.get('symbol', '?')}: "
                f"{local_decision.get('action')} (conf={local_decision.get('confidence')})"
            )
            local_decision["agent_name"] = self.name
            local_decision["agent_role"] = self.role
            local_decision["ai_called"] = False
            return local_decision

        # ── Option B: Route through connected providers (telegram-signals) ──
        if db is not None:
            provider_decision = await self._try_provider_chat(db, context, memory_prompt)
            if provider_decision is not None:
                return provider_decision

        # ── Option C: Fall back to the local OpenAI key ──
        if not _openai_available():
            logger.warning(f"[Agent:{self.name}] No connected providers and OpenAI not configured, returning safe hold")
            return self._safe_hold("No AI providers connected and OpenAI not configured")

        if _circuit_is_open():
            return self._safe_hold(f"AI circuit breaker open: {_circuit_reason}")

        client = _get_client()
        user_msg = self.build_user_prompt(context)
        if memory_prompt:
            user_msg += memory_prompt

        logger.info(f"[Agent:{self.name}] Calling OpenAI for {context.get('symbol', '?')}…")

        try:
            # Reasoning models (o1, o3, o4) use max_completion_tokens and don't accept temperature
            is_reasoning = self.model.startswith(("o1", "o3", "o4"))
            extra_params = {}
            if is_reasoning:
                extra_params["max_completion_tokens"] = self.max_tokens
            else:
                extra_params["max_tokens"] = self.max_tokens
                extra_params["temperature"] = self.temperature

            messages = compress_messages(
                [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                caller=self.name,
            )
            resp = await client.chat.completions.create(
                model=self.model,
                **extra_params,
                response_format={"type": "json_object"},
                messages=messages,
            )
            raw = resp.choices[0].message.content or "{}"
            decision = self.parse_response(raw)
            decision.setdefault("agent_name", self.name)
            decision.setdefault("agent_role", self.role)
            decision["ai_called"] = True
            decision["provider"] = "openai"
            logger.info(
                f"[Agent:{self.name}] → {decision.get('action', '?')} "
                f"(confidence={decision.get('confidence', '?')})"
            )
            return decision
        except Exception as e:
            logger.error(f"[Agent:{self.name}] LLM call failed: {e}")
            if _is_quota_or_auth_error(e):
                _trip_circuit(str(e)[:200])
            return self._safe_hold(f"Agent error: {str(e)}")

    async def _try_provider_chat(
        self,
        db: Any,
        context: Dict[str, Any],
        memory_prompt: str,
    ) -> Optional[Dict[str, Any]]:
        """Route this agent's call through the connected telegram-signals providers.

        Returns a decision dict on success, or None to fall back to OpenAI. The
        plugin is imported lazily/guarded so the core still works standalone.
        """
        try:
            from plugins.AiMarketAnalyst.backend.services.ai_router import (
                agent_chat,
                get_router_settings,
                has_enabled_providers,
            )
        except Exception:
            return None

        try:
            settings = await get_router_settings(db)
            if not settings.agents_use_providers:
                return None
            if not await has_enabled_providers(db):
                return None

            user_msg = self.build_user_prompt(context)

            logger.info(f"[Agent:{self.name}] Routing through connected providers for {context.get('symbol', '?')}…")
            res = await agent_chat(
                db,
                system_prompt=self.system_prompt,
                user_prompt=user_msg,
                reference_context=memory_prompt or None,
                max_tokens=self.max_tokens,
                agent_name=self.name,
                agent_role=self.role,
                source="agent",
            )
            if not res.get("ok") or not isinstance(res.get("content"), dict):
                # No working provider / unparsable → let caller fall back to OpenAI
                logger.debug(f"[Agent:{self.name}] provider route returned no usable result: {res.get('error')}")
                return None

            decision = res["content"]
            decision.setdefault("agent_name", self.name)
            decision.setdefault("agent_role", self.role)
            decision["ai_called"] = True
            decision["provider"] = res.get("provider")
            decision["model_used"] = res.get("model")
            usage = res.get("usage") or {}
            if usage.get("total_tokens"):
                decision["tokens"] = usage["total_tokens"]
            logger.info(
                f"[Agent:{self.name}] (provider={res.get('provider')}) → "
                f"{decision.get('action', '?')} (confidence={decision.get('confidence', '?')})"
            )
            return decision
        except Exception as e:
            logger.warning(f"[Agent:{self.name}] provider chat failed, falling back to OpenAI: {e}")
            return None

    def _safe_hold(self, reason: str) -> Dict[str, Any]:
        """Return a safe hold decision — used when OpenAI is unavailable or errors."""
        return {
            "agent_name": self.name,
            "agent_role": self.role,
            "action": "hold",
            "confidence": 0,
            "reasoning": reason,
            "ai_called": False,
            "error": True,
        }
