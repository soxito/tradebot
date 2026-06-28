"""Curated free-tier AI provider presets.

Each preset is an OpenAI-compatible (or native) chat endpoint. Users add an
account by picking a preset and pasting their API key. Free models are chosen
to last a long time on free tiers.
"""
from __future__ import annotations

from typing import Any

# Per-model capability catalog. Keyed by the exact model id used in API calls.
# Fields: label, context (token window), params (size), speed (1-5),
# strengths (list), best_for, vision (bool), reasoning (bool),
# json_mode (bool), cost ("free"/"cheap"/"paid"), notes.
MODEL_CATALOG: dict[str, dict[str, Any]] = {
    # ── Groq ─────────────────────────────────────────────
    "llama-3.3-70b-versatile": {
        "label": "Llama 3.3 70B Versatile",
        "context": 128000, "params": "70B", "speed": 5,
        "strengths": ["Strong reasoning", "Structured JSON", "General analysis"],
        "best_for": "Best all-round default for signal analysis & sniper entries.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Great balance of quality + Groq's extreme speed.",
    },
    "llama-3.1-8b-instant": {
        "label": "Llama 3.1 8B Instant",
        "context": 128000, "params": "8B", "speed": 5,
        "strengths": ["Ultra-fast", "Low latency", "High volume"],
        "best_for": "High-frequency lightweight checks where speed beats depth.",
        "vision": False, "reasoning": False, "json_mode": True, "cost": "free",
        "notes": "Fastest option; use for quick re-checks, not deep reasoning.",
    },
    "moonshotai/kimi-k2-instruct": {
        "label": "Kimi K2 Instruct",
        "context": 128000, "params": "MoE", "speed": 4,
        "strengths": ["Long context", "Tool use", "Reasoning"],
        "best_for": "Longer multi-message context analysis.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Strong agentic/tool-use model on Groq.",
    },
    "qwen/qwen3-32b": {
        "label": "Qwen3 32B",
        "context": 128000, "params": "32B", "speed": 5,
        "strengths": ["Strong reasoning", "Structured JSON", "Multilingual"],
        "best_for": "High-quality reasoning at Groq speed.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Excellent reasoning/structured output on Groq.",
    },
    "openai/gpt-oss-120b": {
        "label": "GPT-OSS 120B",
        "context": 128000, "params": "120B MoE", "speed": 4,
        "strengths": ["Frontier-class reasoning", "Coding", "Analysis"],
        "best_for": "Deepest analysis available on Groq's free tier.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "OpenAI open-weights flagship; top quality.",
    },
    # ── OpenRouter (:free) ───────────────────────────────
    "google/gemma-4-31b-it:free": {
        "label": "Gemma 4 31B (free)",
        "context": 128000, "params": "31B", "speed": 4,
        "strengths": ["Reasoning", "Structured output", "Reliable uptime"],
        "best_for": "Most reliable OpenRouter free model for analysis.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Google Gemma 4 on OpenRouter — usually available when others are busy.",
    },
    "google/gemma-4-26b-a4b-it:free": {
        "label": "Gemma 4 26B A4B (free)",
        "context": 128000, "params": "26B MoE", "speed": 4,
        "strengths": ["Efficient", "Reasoning", "Fast"],
        "best_for": "Lightweight free reasoning via OpenRouter.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Mixture-of-experts Gemma; efficient and capable.",
    },
    "qwen/qwen3-next-80b-a3b-instruct:free": {
        "label": "Qwen3-Next 80B (free)",
        "context": 256000, "params": "80B MoE", "speed": 3,
        "strengths": ["Strong reasoning", "256K context", "Multilingual"],
        "best_for": "Long-context, high-quality free analysis (when not busy).",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Large context; popular so can be rate-limited upstream.",
    },
    "nousresearch/hermes-3-llama-3.1-405b:free": {
        "label": "Hermes 3 405B (free)",
        "context": 128000, "params": "405B", "speed": 2,
        "strengths": ["Deepest reasoning", "Massive model", "Steerable"],
        "best_for": "Hardest free analytical calls (when available).",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Huge 405B model; slower and often busy on the free tier.",
    },
    "qwen/qwen3-coder:free": {
        "label": "Qwen3 Coder (free)",
        "context": 256000, "params": "MoE", "speed": 3,
        "strengths": ["Coding", "Reasoning", "Long context"],
        "best_for": "Code-heavy or structured-data reasoning.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Coder-tuned Qwen3; strong structured output.",
    },
    "meta-llama/llama-3.3-70b-instruct:free": {
        "label": "Llama 3.3 70B (free)",
        "context": 128000, "params": "70B", "speed": 3,
        "strengths": ["Strong reasoning", "Structured output"],
        "best_for": "Free high-quality reasoning via OpenRouter.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Popular ':free' model — frequently rate-limited upstream.",
    },
    # ── Google Gemini (AI Studio) ────────────────────────
    "gemini-2.5-flash-lite": {
        "label": "Gemini 2.5 Flash-Lite",
        "context": 1000000, "params": "—", "speed": 5,
        "strengths": ["1M context", "Vision", "Highest free RPD", "Cheap"],
        "best_for": "Best free workhorse — huge context + vision + top free quota.",
        "vision": True, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Highest requests/day on Gemini free tier — least likely to hit 429.",
    },
    "gemini-2.5-flash": {
        "label": "Gemini 2.5 Flash",
        "context": 1000000, "params": "—", "speed": 4,
        "strengths": ["1M context", "Vision", "Strong reasoning"],
        "best_for": "Higher-quality Gemini analysis with large context.",
        "vision": True, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Better quality than Lite; lower free request quota.",
    },
    "gemini-2.0-flash": {
        "label": "Gemini 2.0 Flash",
        "context": 1000000, "params": "—", "speed": 5,
        "strengths": ["1M context", "Vision", "Very fast", "Cheap/free"],
        "best_for": "Best free workhorse — huge context + vision + speed.",
        "vision": True, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "~1500 requests/day free via AI Studio.",
    },
    "gemini-2.0-flash-lite": {
        "label": "Gemini 2.0 Flash-Lite",
        "context": 1000000, "params": "—", "speed": 5,
        "strengths": ["Cheapest", "Fast", "1M context"],
        "best_for": "Highest-volume low-cost checks.",
        "vision": True, "reasoning": False, "json_mode": True, "cost": "free",
        "notes": "Most cost-efficient Gemini tier.",
    },
    "gemini-1.5-flash": {
        "label": "Gemini 1.5 Flash",
        "context": 1000000, "params": "—", "speed": 4,
        "strengths": ["1M context", "Vision", "Stable"],
        "best_for": "Stable fallback with large context.",
        "vision": True, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Previous-gen but very dependable.",
    },
    # ── Mistral ──────────────────────────────────────────
    "mistral-small-latest": {
        "label": "Mistral Small",
        "context": 32000, "params": "24B", "speed": 4,
        "strengths": ["Efficient", "Structured output", "Reasoning"],
        "best_for": "Balanced quality/speed on Mistral's free tier.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Good default Mistral choice.",
    },
    "open-mistral-nemo": {
        "label": "Mistral Nemo 12B",
        "context": 128000, "params": "12B", "speed": 4,
        "strengths": ["Long context", "Multilingual", "Fast"],
        "best_for": "Long-context tasks on a small fast model.",
        "vision": False, "reasoning": False, "json_mode": True, "cost": "free",
        "notes": "128K context on a lightweight model.",
    },
    "ministral-8b-latest": {
        "label": "Ministral 8B",
        "context": 128000, "params": "8B", "speed": 5,
        "strengths": ["Very fast", "Edge-efficient"],
        "best_for": "Fast low-cost checks.",
        "vision": False, "reasoning": False, "json_mode": True, "cost": "free",
        "notes": "Compact, speedy model.",
    },
    # ── Cerebras ─────────────────────────────────────────
    "gpt-oss-120b": {
        "label": "GPT-OSS 120B (Cerebras)",
        "context": 128000, "params": "120B MoE", "speed": 5,
        "strengths": ["Frontier reasoning", "Record speed", "Coding"],
        "best_for": "Top-quality analysis at Cerebras' record latency.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "OpenAI open-weights flagship on wafer-scale hardware.",
    },
    "zai-glm-4.7": {
        "label": "GLM 4.7 (Cerebras)",
        "context": 128000, "params": "MoE", "speed": 5,
        "strengths": ["Reasoning", "Agentic", "Very fast"],
        "best_for": "Fast agentic reasoning alternative on Cerebras.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Zhipu GLM 4.7 served at Cerebras speed.",
    },
    "llama-3.3-70b": {
        "label": "Llama 3.3 70B (Cerebras)",
        "context": 128000, "params": "70B", "speed": 5,
        "strengths": ["Fastest 70B anywhere", "Strong reasoning"],
        "best_for": "Top quality at record inference speed.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Cerebras wafer-scale = extremely low latency.",
    },
    "llama3.1-8b": {
        "label": "Llama 3.1 8B (Cerebras)",
        "context": 128000, "params": "8B", "speed": 5,
        "strengths": ["Blazing fast", "Lightweight"],
        "best_for": "Instant lightweight responses.",
        "vision": False, "reasoning": False, "json_mode": True, "cost": "free",
        "notes": "Near-instant small model.",
    },
    # ── DeepSeek (paid, cheap) ───────────────────────────
    "deepseek-chat": {
        "label": "DeepSeek Chat (V3)",
        "context": 64000, "params": "MoE 671B", "speed": 3,
        "strengths": ["Frontier reasoning", "Coding", "Math"],
        "best_for": "High-accuracy decisions when budget allows.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "cheap",
        "notes": "Very low price per token.",
    },
    "deepseek-reasoner": {
        "label": "DeepSeek Reasoner (R1)",
        "context": 64000, "params": "MoE 671B", "speed": 2,
        "strengths": ["Chain-of-thought", "Deep reasoning", "Math"],
        "best_for": "Hardest analytical calls needing step-by-step logic.",
        "vision": False, "reasoning": True, "json_mode": False, "cost": "cheap",
        "notes": "Thinks before answering; slower but deepest.",
    },
    # ── Together ─────────────────────────────────────────
    "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free": {
        "label": "Llama 3.3 70B Turbo (free)",
        "context": 128000, "params": "70B", "speed": 4,
        "strengths": ["Strong reasoning", "Free endpoint"],
        "best_for": "Free 70B-class analysis via Together.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Together's free Llama 3.3 endpoint.",
    },
    # ── OpenAI (paid) ────────────────────────────────────
    "gpt-4o-mini": {
        "label": "GPT-4o mini",
        "context": 128000, "params": "—", "speed": 5,
        "strengths": ["Cheap", "Fast", "Vision", "Reliable JSON"],
        "best_for": "Cheap reliable premium fallback.",
        "vision": True, "reasoning": True, "json_mode": True, "cost": "paid",
        "notes": "Low-cost OpenAI option.",
    },
    "gpt-4o": {
        "label": "GPT-4o",
        "context": 128000, "params": "—", "speed": 4,
        "strengths": ["Top quality", "Vision", "Robust reasoning"],
        "best_for": "Highest-quality decisions (premium).",
        "vision": True, "reasoning": True, "json_mode": True, "cost": "paid",
        "notes": "Flagship multimodal model.",
    },
    "o4-mini": {
        "label": "o4-mini (reasoning)",
        "context": 128000, "params": "—", "speed": 3,
        "strengths": ["Deep reasoning", "Math", "Planning"],
        "best_for": "Complex multi-step reasoning (premium).",
        "vision": True, "reasoning": True, "json_mode": True, "cost": "paid",
        "notes": "Reasoning-optimized; thinks longer.",
    },
}


def get_model_info(model: str) -> dict[str, Any] | None:
    return MODEL_CATALOG.get(model)


def _model_info_map(models: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for m in models:
        info = MODEL_CATALOG.get(m)
        if info:
            out[m] = info
    return out


# type: "openai_compatible" works for the vast majority (Bearer key + /chat/completions)
PROVIDER_PRESETS: list[dict[str, Any]] = [
    {
        "key": "groq",
        "label": "Groq",
        "type": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "qwen/qwen3-32b", "openai/gpt-oss-120b"],
        "free_tier": True,
        "daily_limit": 1000,
        "monthly_limit": 14000,
        "signup_url": "https://console.groq.com/keys",
        "notes": "Very fast, generous free tier. Great default for signals/sniper.",
    },
    {
        "key": "openrouter",
        "label": "OpenRouter",
        "type": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "google/gemma-4-31b-it:free",
        "models": [
            "google/gemma-4-31b-it:free",
            "google/gemma-4-26b-a4b-it:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "qwen/qwen3-next-80b-a3b-instruct:free",
            "nousresearch/hermes-3-llama-3.1-405b:free",
            "qwen/qwen3-coder:free",
        ],
        "free_tier": True,
        "daily_limit": 50,
        "monthly_limit": 1000,
        "signup_url": "https://openrouter.ai/keys",
        "notes": "Aggregates many models; ':free' models share upstream quota (can be busy). ~50/day free.",
    },
    {
        "key": "gemini",
        "label": "Google Gemini",
        "type": "openai_compatible",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.5-flash-lite",
        "models": ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite"],
        "free_tier": True,
        "daily_limit": 200,
        "monthly_limit": 4000,
        "signup_url": "https://aistudio.google.com/apikey",
        "notes": "Generous free tier via Google AI Studio (Flash-Lite has the highest free RPD).",
    },
    {
        "key": "mistral",
        "label": "Mistral",
        "type": "openai_compatible",
        "base_url": "https://api.mistral.ai/v1",
        "default_model": "mistral-small-latest",
        "models": ["mistral-small-latest", "open-mistral-nemo", "ministral-8b-latest"],
        "free_tier": True,
        "daily_limit": 500,
        "monthly_limit": 14000,
        "signup_url": "https://console.mistral.ai/api-keys/",
        "notes": "Free experimental tier on small models.",
    },
    {
        "key": "cerebras",
        "label": "Cerebras",
        "type": "openai_compatible",
        "base_url": "https://api.cerebras.ai/v1",
        "default_model": "gpt-oss-120b",
        "models": ["gpt-oss-120b", "zai-glm-4.7"],
        "free_tier": True,
        "daily_limit": 500,
        "monthly_limit": 14000,
        "signup_url": "https://cloud.cerebras.ai/",
        "notes": "Extremely fast inference, free tier.",
    },
    {
        "key": "deepseek",
        "label": "DeepSeek",
        "type": "openai_compatible",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "free_tier": False,
        "daily_limit": None,
        "monthly_limit": None,
        "signup_url": "https://platform.deepseek.com/api_keys",
        "notes": "Very cheap; strong reasoning model.",
    },
    {
        "key": "together",
        "label": "Together AI",
        "type": "openai_compatible",
        "base_url": "https://api.together.xyz/v1",
        "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
        "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"],
        "free_tier": True,
        "daily_limit": 100,
        "monthly_limit": 3000,
        "signup_url": "https://api.together.ai/settings/api-keys",
        "notes": "Has a free Llama 3.3 70B endpoint.",
    },
    {
        "key": "openai",
        "label": "OpenAI",
        "type": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o", "o4-mini"],
        "free_tier": False,
        "daily_limit": None,
        "monthly_limit": None,
        "signup_url": "https://platform.openai.com/api-keys",
        "notes": "Paid; highest quality. Use as a premium fallback.",
    },
]


def get_preset(key: str) -> dict[str, Any] | None:
    for p in PROVIDER_PRESETS:
        if p["key"] == key:
            return p
    return None


# Attach per-model capability info to every preset (kept in sync with the catalog)
for _preset in PROVIDER_PRESETS:
    _preset["model_info"] = _model_info_map(_preset["models"])
