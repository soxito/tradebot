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
from typing import Dict, Any, Optional
from loguru import logger

from app.utils.headroom_compress import compress_messages
from app.core.ai_key_routing import (
    build_async_client,
    is_openai_key,
    resolve_base_url,
)

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore

# ── Circuit Breaker ──────────────────────────────────────────
# When OpenAI returns 429/quota/auth errors, suppress all calls for this period.
CIRCUIT_BREAKER_COOLDOWN = 300  # 5 minutes

# ── Per-symbol AI decision cache ─────────────────────────────
# Prevents repeated provider calls for the same (role, symbol) within the TTL.
# The room worker already cooldowns at 30 min; this guard covers every code
# path that calls analyze() directly (e.g. signal research, self-improve).
AI_CALL_TTL_S = 3600  # 1 hour

_AI_DECISION_CACHE: Dict[str, tuple] = {}  # cache_key → (timestamp, decision_dict)


def _cache_key(role: str, symbol: str) -> str:
    return f"{role}:{symbol.upper()}"


def _get_cached_decision(role: str, symbol: str) -> "Optional[Dict[str, Any]]":
    """Return a cached decision if it exists and is within TTL, else None."""
    key = _cache_key(role, symbol)
    entry = _AI_DECISION_CACHE.get(key)
    if entry is None:
        return None
    ts, decision = entry
    if time.time() - ts > AI_CALL_TTL_S:
        del _AI_DECISION_CACHE[key]
        return None
    return decision


def _store_cached_decision(role: str, symbol: str, decision: "Dict[str, Any]") -> None:
    """Store a decision in the TTL cache (only meaningful decisions, not safe holds)."""
    action = (decision.get("action") or "").lower()
    if action == "hold" and not decision.get("ai_called"):
        return  # don't cache a safe hold — it's a circuit-breaker placeholder
    _AI_DECISION_CACHE[_cache_key(role, symbol)] = (time.time(), decision)

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


# Budget for models that emit reasoning before their answer.
#
# A ceiling, not a cost: billing is on tokens produced, and given room these
# models mostly answer well inside it. What varies is how far they wander first
# — measured on nemotron-3.5-lightning with a real room prompt, the same
# question landed in 1403 tokens at this ceiling and never landed at all at
# 4000, where it was still deliberating when the budget ran out. Raising the
# ceiling does not slow the good answers down; it stops the wandering ones from
# costing the whole turn.
_REASONING_BUDGET = 8000

# Floor for every agent, reasoning model or not.
#
# The per-agent ``max_tokens`` seeds at 2000, which is enough for the JSON but
# not for a model that writes a paragraph of analysis on the way there — and
# the room publishes that reasoning verbatim, so running out of budget shows up
# to the user as a sentence that simply stops. Tokens are billed on what is
# produced, so a ceiling nobody reaches costs nothing; a ceiling that clips a
# published answer costs the whole turn.
_MIN_AGENT_BUDGET = 4000


def _repair_truncated_json(text: str) -> "Optional[Dict[str, Any]]":
    """Close whatever a token ceiling cut and parse the remainder.

    Strategy: track the open string/escape state character by character, cut
    back past any dangling partial value, drop trailing commas, then append
    exactly the closers the surviving text needs. Returns None when the text
    never opened a JSON object at all.
    """

    def _scan(s: str) -> "tuple[list[str], bool]":
        """(open-bracket stack for ``s``, whether s ends inside a string)."""
        stack: list[str] = []
        in_string = False
        escaped = False
        for ch in s:
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if stack:
                    stack.pop()
        return stack, in_string

    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return None

    candidate = text
    stack, in_string = _scan(candidate)

    # Dangling partial string: trim back to its last COMPLETE sentence so the
    # salvaged reasoning is published whole — "…momentum is sideways." not
    # "…momentum is side" — then close the quote and rescan.
    if in_string:
        cut = max(
            candidate.rfind(". "),
            candidate.rfind(".\n"),
            candidate.rfind("! "),
            candidate.rfind("? "),
        )
        opening = candidate.rfind('"')
        if opening <= 0:
            return None
        candidate = candidate[: cut + 1] if cut > opening else candidate[:opening]
        stack, in_string = _scan(candidate)

    # Drop any trailing comma left before the cut.
    while candidate.rstrip().endswith(","):
        candidate = candidate.rstrip()[:-1]
        stack, _ = _scan(candidate)

    # A value cut mid-number/mid-word parses fine once closed. A dangling key
    # ("name": or just "name") cannot survive — cut it and whatever led it.
    trimmed = candidate.rstrip()
    while trimmed.endswith((":", ",")):
        trimmed = trimmed[:-1].rstrip()
    if trimmed.endswith('"'):
        open_quote = trimmed.rfind('"', 0, len(trimmed) - 1)
        if open_quote > 0:
            trimmed = trimmed[:open_quote].rstrip()
            while trimmed.endswith((":", ",")):
                trimmed = trimmed[:-1].rstrip()

    closers = ["}" if o == "{" else "]" for o in reversed(_scan(trimmed)[0])]
    repaired = trimmed + "".join(closers)
    try:
        parsed = json.loads(repaired)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _budget_for(model: str, configured: int) -> int:
    """The token ceiling this call should carry."""
    floor = _REASONING_BUDGET if _is_reasoning_model(model) else _MIN_AGENT_BUDGET
    return max(int(configured or 0), floor)


def _is_reasoning_model(model: str) -> bool:
    """Whether this model thinks before answering (router's list is canonical)."""
    try:
        from plugins.AiMarketAnalyst.backend.services.ai_router import is_reasoning_model
    except Exception:
        return model.startswith(("o1", "o3", "o4"))
    return is_reasoning_model(model)


def _key_provider_label() -> str:
    """Name the endpoint the local key talks to, for decision provenance."""
    base = resolve_base_url(os.getenv("OPENAI_API_KEY", "")) or ""
    for marker, label in (
        ("nvidia", "nvidia"), ("openrouter", "openrouter"),
        ("groq", "groq"), ("cerebras", "cerebras"), ("openai.com", "openai"),
    ):
        if marker in base:
            return label
    return "local-key"


def _get_client() -> "AsyncOpenAI":
    if AsyncOpenAI is None:
        raise RuntimeError("openai package not installed — run: pip install openai")
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    # The key prefix decides the endpoint — never send a key to a provider it
    # doesn't belong to. HEADROOM_OPENAI_BASE_URL is honoured only when it names
    # the same provider as the key.
    base_url = resolve_base_url(api_key, os.getenv("HEADROOM_OPENAI_BASE_URL", "") or None)

    # The headroom proxy is an OpenAI-shaped front for api.openai.com: it picks
    # its own upstream and ignores what we think it forwards to. So it may only
    # carry a real OpenAI key. Routing an nvapi-/gsk_/csk- key through it is how
    # a key the user never gave OpenAI ended up being rejected *by* OpenAI —
    # 401, breaker tripped, every agent on the board downgraded to a local read.
    headroom_proxy = os.getenv("HEADROOM_PROXY_URL")
    if headroom_proxy and is_openai_key(api_key):
        proxy_base = f"{headroom_proxy.rstrip('/')}/p/tradebot/v1"
        logger.info(f"[BaseAgent] Routing calls through headroom proxy: {proxy_base}")
        return build_async_client(api_key, proxy_base)

    logger.info(f"[BaseAgent] Direct provider call → {base_url or 'SDK default'}")
    return build_async_client(api_key, base_url)


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
        model: str = "fable-5-high",
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

    def _routed_model(self) -> Optional[str]:
        """The model this agent should use for its kind of work.

        A model the user actually chose in the agents UI always wins. "o3" is
        the historical seed default rather than a real choice — no provider in
        the catalog serves it — so it is treated as unset and the role's task
        category picks the model instead.
        """
        configured = (self.model or "").strip()
        if configured and configured != "o3":
            return configured
        try:
            from app.agents.specialists import ROLE_TASKS
            from plugins.AiMarketAnalyst.backend.services.ai_router import (
                resolve_model_for_task,
            )
        except Exception:
            return configured or None
        chain = resolve_model_for_task(ROLE_TASKS.get(self.role, ""))
        return chain[0] if chain else (configured or None)

    # ── Subclass hooks ───────────────────────────────────────

    def build_user_prompt(self, context: Dict[str, Any]) -> str:
        """Build the user message from market context. Override in subclass."""
        return json.dumps(context, default=str)

    def parse_response(self, raw: str) -> Dict[str, Any]:
        """Parse raw LLM response into a structured decision.

        A model that hit its token ceiling mid-JSON used to lose everything to
        ``{"raw": ...}`` — the decision, the levels, all of it — because one
        unclosed brace made strict parsing give up. The repair pass closes what
        the ceiling cut (strings, objects, arrays) and parses again; most
        truncated decisions survive with every field the model did emit.
        """
        if not raw or not isinstance(raw, str):
            return {"raw": raw}
        text = raw.strip()
        # Strip markdown fences some models wrap JSON in.
        if text.startswith("```"):
            first_nl = text.find("\n")
            if first_nl != -1:
                text = text[first_nl + 1:]
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        repaired = _repair_truncated_json(text)
        if repaired is not None:
            return repaired
        return {"raw": raw}

    # ── Memory-aware analysis ────────────────────────────────

    async def analyze(
        self,
        context: Dict[str, Any],
        memory_prompt: str = "",
        local_decision: Optional[Dict[str, Any]] = None,
        db: Any = None,
        live: bool = False,
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
            live: This run is one a person asked for and is waiting on. The
                answer cache is skipped so the model is actually called, and the
                providers' backoff is stepped over rather than waited out — a
                cooldown is there to protect a budget from the scanner, and it
                should not be what decides that a person's question goes
                unanswered.

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

        # ── Symbol key used for cache lookups below ──
        _sym = str(context.get("symbol") or "").strip().upper()

        # ── Option A½: Return cached AI decision if still within TTL ──
        if _sym and not live:
            _hit = _get_cached_decision(self.role, _sym)
            if _hit is not None:
                _age = round(time.time() - _AI_DECISION_CACHE.get(_cache_key(self.role, _sym), (0,))[0])
                logger.info(
                    f"[Agent:{self.name}] ↩ cache hit for {_sym} "
                    f"(age {_age}s / TTL {AI_CALL_TTL_S}s) — no provider call"
                )
                return {**_hit, "from_cache": True, "ai_called": False,
                        "agent_name": self.name, "agent_role": self.role}

        # ── Option B: Route through connected providers (telegram-signals) ──
        pool_active = False
        if db is not None:
            provider_decision, pool_active = await self._try_provider_chat(
                db, context, memory_prompt, live=live,
            )
            if provider_decision is not None:
                if _sym:
                    _store_cached_decision(self.role, _sym, provider_decision)
                return provider_decision

        # ── Option C: Fall back to the local OpenAI key ──
        # Only when the user has no connected providers at all. If the pool is
        # configured and simply didn't answer this turn, the raw key is not a
        # second chance at the same models — it is a different, unconfigured
        # vendor, billed and rate-limited outside every cap the pool enforces.
        # Reporting the local read is the honest outcome.
        if pool_active:
            return self._safe_hold(
                "Connected AI providers returned no usable answer",
                context, memory_prompt,
            )

        if not _openai_available():
            logger.warning(f"[Agent:{self.name}] No connected providers and OpenAI not configured, returning safe hold")
            return self._safe_hold(
                "No AI providers connected and OpenAI not configured",
                context, memory_prompt,
            )

        if _circuit_is_open():
            return self._safe_hold(
                f"AI circuit breaker open: {_circuit_reason}", context, memory_prompt,
            )

        client = _get_client()
        user_msg = self.build_user_prompt(context)
        if memory_prompt:
            user_msg += memory_prompt

        # The seat's own model is a seed default as often as a choice ("o3" is
        # served by nobody here), and this client is pointed at whatever
        # provider owns the key — so ask for the model that provider actually
        # serves for this role's kind of work.
        model = self._routed_model() or self.model
        logger.info(
            f"[Agent:{self.name}] Calling {model} for {context.get('symbol', '?')}…"
        )

        try:
            # Reasoning models (o1, o3, o4) use max_completion_tokens and don't accept temperature
            is_reasoning = model.startswith(("o1", "o3", "o4"))
            extra_params = {}
            if is_reasoning:
                extra_params["max_completion_tokens"] = _budget_for(model, self.max_tokens)
            else:
                # A model that thinks before it answers spends the first ~1-2k
                # tokens on reasoning; too tight a budget truncates the JSON
                # mid-sentence and the decision is lost.
                extra_params["max_tokens"] = _budget_for(model, self.max_tokens)
                extra_params["temperature"] = self.temperature

            messages = compress_messages(
                [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                caller=self.name,
            )
            resp = await client.chat.completions.create(
                model=model,
                **extra_params,
                response_format={"type": "json_object"},
                messages=messages,
            )
            raw = resp.choices[0].message.content or "{}"

            # A "length" finish means the ceiling cut the answer mid-object.
            # One retry at 1.5× budget with a finish-your-answer nudge — the
            # parse repair below usually rescues the first attempt, so this
            # only fires on genuinely clipped decisions.
            finish = getattr(resp.choices[0], "finish_reason", None)
            if finish == "length":
                logger.warning(
                    f"[Agent:{self.name}] hit token ceiling for "
                    f"{context.get('symbol', '?')} — retrying with 1.5x budget"
                )
                try:
                    retry_messages = list(messages)
                    retry_messages[-1] = {
                        **retry_messages[-1],
                        "content": (
                            str(retry_messages[-1]["content"])
                            + "\n\nYour previous answer was cut off. Emit ONE complete JSON object; keep every field short and finished."
                        ),
                    }
                    bumped = int(_budget_for(model, self.max_tokens) * 1.5)
                    resp = await client.chat.completions.create(
                        model=model,
                        max_completion_tokens=bumped if is_reasoning else None,
                        max_tokens=None if is_reasoning else bumped,
                        temperature=None if is_reasoning else self.temperature,
                        response_format={"type": "json_object"},
                        messages=retry_messages,
                    )
                    raw = resp.choices[0].message.content or raw
                except Exception as retry_exc:  # noqa: BLE001
                    logger.debug(f"[Agent:{self.name}] length-retry failed: {retry_exc}")

            decision = self.parse_response(raw)
            decision.setdefault("agent_name", self.name)
            decision.setdefault("agent_role", self.role)
            decision["ai_called"] = True
            decision["provider"] = _key_provider_label()
            decision["model_used"] = model
            if _sym:
                _store_cached_decision(self.role, _sym, decision)
            logger.info(
                f"[Agent:{self.name}] → {decision.get('action', '?')} "
                f"(confidence={decision.get('confidence', '?')})"
            )
            return decision
        except Exception as e:
            logger.error(f"[Agent:{self.name}] LLM call failed: {e}")
            if _is_quota_or_auth_error(e):
                _trip_circuit(str(e)[:200])
            return self._safe_hold(f"Agent error: {str(e)}", context, memory_prompt)

    async def _try_provider_chat(
        self,
        db: Any,
        context: Dict[str, Any],
        memory_prompt: str,
        live: bool = False,
    ) -> "tuple[Optional[Dict[str, Any]], bool]":
        """Route this agent's call through the connected telegram-signals providers.

        Returns ``(decision, pool_active)``. ``pool_active`` says whether the
        connected-provider pool was the thing that ran — the caller uses it to
        decide whether falling back to the raw local key is legitimate or would
        be reaching for a vendor the user never connected. The plugin is
        imported lazily/guarded so the core still works standalone.
        """
        try:
            from plugins.AiMarketAnalyst.backend.services.ai_router import (
                agent_chat,
                get_router_settings,
                has_enabled_providers,
            )
        except Exception:
            return None, False

        try:
            settings = await get_router_settings(db)
            if not settings.agents_use_providers:
                return None, False
            if not await has_enabled_providers(db):
                return None, False

            user_msg = self.build_user_prompt(context)

            routed_model = self._routed_model()
            # Reasoning models spend tokens thinking before they emit the JSON
            # decision; too tight a budget returns a truncated answer, which this
            # path reads as "no usable result" and downgrades to the heuristic.
            # The router honours this figure for reasoning models rather than
            # clamping it to the per-agent ceiling.
            budget = _budget_for(routed_model or self.model, self.max_tokens)

            logger.info(f"[Agent:{self.name}] Routing through connected providers for {context.get('symbol', '?')}…")
            res = await agent_chat(
                db,
                system_prompt=self.system_prompt,
                user_prompt=user_msg,
                reference_context=memory_prompt or None,
                max_tokens=budget,
                model_override=routed_model,
                agent_name=self.name,
                agent_role=self.role,
                source="agent",
                bypass_circuits=live,
            )
            if not res.get("ok") or not isinstance(res.get("content"), dict):
                # Pool ran but produced nothing usable — the caller reports the
                # local read rather than reaching for an unconnected vendor.
                logger.warning(
                    f"[Agent:{self.name}] provider route returned no usable result: {res.get('error')}"
                )
                return None, True

            decision = res["content"]
            if decision.pop("_truncated", False):
                # The answer was salvaged from a cut-off response: the decision
                # and its levels are real, the prose was trimmed back to its
                # last finished sentence. Recorded so the UI can say so rather
                # than presenting a shortened read as a complete one.
                decision["reasoning_trimmed"] = True
                logger.warning(
                    f"[Agent:{self.name}] answer was cut off by the token budget "
                    f"and recovered — reasoning trimmed to complete sentences"
                )
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
            return decision, True
        except Exception as e:
            logger.warning(f"[Agent:{self.name}] provider chat failed: {e}")
            return None, True

    def _safe_hold(
        self,
        reason: str,
        context: Optional[Dict[str, Any]] = None,
        memory_prompt: str = "",
    ) -> Dict[str, Any]:
        """Decide from the local technical read when no provider answered.

        This used to return ``hold`` at zero confidence with the provider error
        as its only reasoning. Providers are rate-limited and the shared ones
        are consumed hourly, so that state is common — and it made the agents
        look like they had stopped reporting, filling the dashboard with blank
        holds that said nothing about the market.

        The context already carries a full local indicator read that costs no
        tokens, so it is analysed here instead. The agent still reports, with
        real numbers behind it; ``ai_called`` stays False so the provenance is
        never misrepresented.
        """
        if context:
            try:
                from app.agents.local_analysis import analyze_locally
                decision = analyze_locally(
                    role=self.role,
                    context={**context, "agent_name": self.name},
                    memory_prompt=memory_prompt,
                    reason=reason,
                )
                logger.info(
                    f"[Agent:{self.name}] local read for "
                    f"{context.get('symbol', '?')}: {decision['action']} "
                    f"(confidence={decision['confidence']}) — {reason}"
                )
                return decision
            except Exception as exc:  # noqa: BLE001 — never block on the fallback
                logger.warning(f"[Agent:{self.name}] local analysis failed: {exc}")
        return {
            "agent_name": self.name,
            "agent_role": self.role,
            "action": "hold",
            "confidence": 0,
            "reasoning": reason,
            "ai_called": False,
            "error": True,
        }
