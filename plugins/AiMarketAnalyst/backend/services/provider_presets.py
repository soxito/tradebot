"""Curated free-tier AI provider presets.

Each preset is an OpenAI-compatible (or native) chat endpoint. Users add an
account by picking a preset and pasting their API key. Free models are chosen
to last a long time on free tiers.
"""
from __future__ import annotations

import os
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
    # ── Groq (newer additions) ───────────────────────────
    "qwen/qwen3.6-27b": {
        "label": "Qwen3.6 27B (Groq)",
        "context": 128000, "params": "27B", "speed": 5,
        "strengths": ["Strong reasoning", "Structured JSON", "Multilingual"],
        "best_for": "High-quality reasoning at Groq's extreme speed.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Newest Qwen on Groq; excellent structured output.",
    },
    "openai/gpt-oss-20b": {
        "label": "GPT-OSS 20B (Groq)",
        "context": 128000, "params": "20B MoE", "speed": 5,
        "strengths": ["Fast reasoning", "Coding", "Analysis"],
        "best_for": "Fast, capable reasoning when 120B is rate-limited.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Smaller OpenAI open-weights model; very fast on Groq.",
    },
    "meta-llama/llama-4-scout-17b-16e-instruct": {
        "label": "Llama 4 Scout 17B (Groq)",
        "context": 128000, "params": "17B MoE", "speed": 5,
        "strengths": ["Multimodal-capable", "Fast", "Reasoning"],
        "best_for": "Next-gen Llama at Groq speed for signal analysis.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Llama 4 Scout — larger daily quota (1000/day) on Groq.",
    },
    # ── Google Gemini 3.x (AI Studio) ────────────────────
    "gemini-3.1-flash-lite": {
        "label": "Gemini 3.1 Flash-Lite",
        "context": 1000000, "params": "—", "speed": 5,
        "strengths": ["1M context", "Vision", "500 req/day free", "Cheap"],
        "best_for": "Best free workhorse — highest free quota (500 RPD) + vision.",
        "vision": True, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Highest requests/day on Gemini free tier (500/day) — least likely to hit 429.",
    },
    "gemini-3-flash": {
        "label": "Gemini 3 Flash",
        "context": 1000000, "params": "—", "speed": 4,
        "strengths": ["1M context", "Vision", "Strong reasoning"],
        "best_for": "Higher-quality Gemini 3 analysis with large context.",
        "vision": True, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Newest-gen quality; 20 req/day free quota.",
    },
    "gemini-3.1-flash": {
        "label": "Gemini 3.1 Flash",
        "context": 1000000, "params": "—", "speed": 4,
        "strengths": ["1M context", "Vision", "Best reasoning"],
        "best_for": "Top free Gemini quality for the hardest analysis.",
        "vision": True, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Best free Gemini reasoning; lower quota (20/day) — use as quality fallback.",
    },
    # ── NVIDIA NIM (build.nvidia.com) ────────────────────
    # The four below are the task-routed models (ai_router.TASK_MODEL_CHAINS).
    # Every figure here was checked against a live account on 2026-08-15 rather
    # than copied off the model card.
    "nvidia/nemotron-3.5-lightning-30b-a3b": {
        "label": "Nemotron 3.5 Lightning 30B (NVIDIA)",
        "context": 1000000, "params": "30B-A3B MoE", "speed": 5,
        "strengths": ["Fastest agentic", "Tool use", "1M context", "Long-running agents"],
        "best_for": "Default for everything interactive — bot replies, tool calls, "
                    "position checks. Fastest 30B MoE with switchable thinking.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Preferred NVIDIA default: answers in seconds where Ultra 550B "
                 "takes minutes, at the accuracy these tasks need. Emits "
                 "reasoning_content first, so give it >=2048 max_tokens.",
    },
    "z-ai/glm-5.2": {
        "label": "GLM-5.2 (NVIDIA)",
        "context": 1000000, "params": "753B", "speed": 2,
        "strengths": ["Long-horizon reasoning", "Agentic workflows", "Coding"],
        "best_for": "Deep market analysis, strategy synthesis and forecast narration.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Flagship reasoning model. Slow — allow ~120s — so reserve it "
                 "for analysis the user is willing to wait for.",
    },
    "thinkingmachines/inkling": {
        "label": "Inkling 952B (Thinking Machines)",
        "context": 128000, "params": "952B MoE (256 experts)", "speed": 1,
        "strengths": ["Vision", "Deepest chart reading", "Tool use", "Reasoning"],
        "best_for": "Primary chart/screenshot reader — reads instrument, timeframe "
                    "and structure off an image and reports levels honestly.",
        "vision": True, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Mamba-hybrid MoE. Regularly exceeds 60s on a chart, so it runs "
                 "on a 120s deadline; a short one times out and trips the breaker.",
    },
    "meta/muse-glimmer-30b": {
        "label": "Muse Glimmer 30B (Meta)",
        "context": 131000, "params": "30B", "speed": 4,
        "strengths": ["Vision", "Chart reading", "Reasoning", "Tool use"],
        "best_for": "Faster second opinion on images when Inkling is busy or slow.",
        "vision": True, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Multimodal reasoning model on vLLM with native tool-calling. "
                 "Read a test chart's instrument and timeframe correctly.",
    },
    "nvidia/nemotron-3-ultra-550b-a55b": {
        "label": "Nemotron 3 Ultra 550B (NVIDIA)",
        "context": 128000, "params": "550B-A55B MoE", "speed": 1,
        "strengths": ["Largest free model", "Deep reasoning"],
        "best_for": "One-off deep analysis where latency genuinely does not matter.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Too slow to be a default: it cannot answer inside the standard "
                 "40s deadline, which is why every call used to time out and take "
                 "the whole provider down with it. Prefer Nemotron 3.5 Lightning.",
    },
    "nvidia/nemotron-3-super-120b-a12b": {
        "label": "Nemotron 3 Super 120B (NVIDIA)",
        "context": 128000, "params": "120B MoE", "speed": 3,
        "strengths": ["Frontier reasoning", "Analysis", "Steerable"],
        "best_for": "Deepest free analysis available on NVIDIA NIM.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "NVIDIA's flagship Nemotron; strong analytical quality.",
    },
    "meta/llama-3.3-70b-instruct": {
        "label": "Llama 3.3 70B (NVIDIA)",
        "context": 128000, "params": "70B", "speed": 4,
        "strengths": ["Strong reasoning", "Structured output"],
        "best_for": "Reliable 70B-class free analysis via NVIDIA NIM.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Well-supported default on NVIDIA's free endpoint.",
    },
    "deepseek-ai/deepseek-v4-flash": {
        "label": "DeepSeek V4 Flash (NVIDIA)",
        "context": 128000, "params": "MoE", "speed": 4,
        "strengths": ["Fast reasoning", "Coding", "Math"],
        "best_for": "Fast high-accuracy reasoning free on NVIDIA NIM.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Latest DeepSeek generation on NVIDIA's free endpoint.",
    },
    "nvidia/nemotron-nano-12b-v2-vl": {
        "label": "Nemotron Nano 12B VL (NVIDIA)",
        "context": 128000, "params": "12B", "speed": 4,
        "strengths": ["Vision", "Chart reading", "Fast"],
        "best_for": "Reading chart screenshots and other images sent to the bot.",
        "vision": True, "reasoning": False, "json_mode": True, "cost": "free",
        "notes": "Default for Telegram image analysis. Verified against a live "
                 "account: read instrument, timeframe and trend off a candlestick "
                 "chart correctly.",
    },
    "meta/llama-3.2-11b-vision-instruct": {
        "label": "Llama 3.2 11B Vision (NVIDIA)",
        "context": 128000, "params": "11B", "speed": 4,
        "strengths": ["Vision", "Chart reading"],
        "best_for": "Vision fallback when the Nemotron VL function is unavailable.",
        "vision": True, "reasoning": False, "json_mode": False, "cost": "free",
        "notes": "Verified serving. The 90B sibling is deployed but too slow for "
                 "a chat round trip (>90s on a 900x520 chart).",
    },
    # ── SambaNova Cloud (trial credits, record speed) ────
    "Meta-Llama-3.3-70B-Instruct": {
        "label": "Llama 3.3 70B (SambaNova)",
        "context": 128000, "params": "70B", "speed": 5,
        "strengths": ["Fastest 70B", "Strong reasoning", "Structured JSON"],
        "best_for": "Top quality at SambaNova's record inference speed.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "SambaNova RDU hardware = extremely low latency.",
    },
    "DeepSeek-V3.1": {
        "label": "DeepSeek V3.1 (SambaNova)",
        "context": 64000, "params": "MoE 671B", "speed": 4,
        "strengths": ["Frontier reasoning", "Coding", "Math"],
        "best_for": "Deepest analysis at SambaNova speed.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Frontier DeepSeek served fast on SambaNova.",
    },
    "Llama-4-Maverick-17B-128E-Instruct": {
        "label": "Llama 4 Maverick 17B (SambaNova)",
        "context": 128000, "params": "17B MoE", "speed": 5,
        "strengths": ["Next-gen Llama", "Very fast", "Reasoning"],
        "best_for": "Fast next-gen Llama analysis on SambaNova.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Llama 4 Maverick at SambaNova's record speed.",
    },
    # ── Cohere (compatibility endpoint) ──────────────────
    "command-a-03-2025": {
        "label": "Command A (Cohere)",
        "context": 256000, "params": "111B", "speed": 4,
        "strengths": ["Strong reasoning", "256K context", "Enterprise-grade"],
        "best_for": "High-quality free reasoning with huge context.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Cohere's flagship; 1000 req/month free via compatibility API.",
    },
    "command-r-plus-08-2024": {
        "label": "Command R+ (Cohere)",
        "context": 128000, "params": "104B", "speed": 4,
        "strengths": ["RAG-optimized", "Tool use", "Reasoning"],
        "best_for": "Retrieval/tool-heavy analysis on Cohere free tier.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Strong RAG + tool-use model; shares monthly quota.",
    },
    "c4ai-aya-expanse-32b": {
        "label": "Aya Expanse 32B (Cohere)",
        "context": 128000, "params": "32B", "speed": 4,
        "strengths": ["Multilingual", "Reasoning", "Efficient"],
        "best_for": "Multilingual free analysis via Cohere.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Best multilingual coverage on Cohere's free tier.",
    },
    # ── GitHub Models (free with GitHub token) ───────────
    "o3-mini": {
        "label": "o3-mini (GitHub Models)",
        "context": 128000, "params": "—", "speed": 3,
        "strengths": ["Deep reasoning", "Math", "Planning"],
        "best_for": "Free reasoning model for complex multi-step calls.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "OpenAI reasoning model, free via GitHub Models.",
    },
    "DeepSeek-R1": {
        "label": "DeepSeek R1 (GitHub Models)",
        "context": 64000, "params": "MoE 671B", "speed": 2,
        "strengths": ["Chain-of-thought", "Deep reasoning", "Math"],
        "best_for": "Hardest analytical calls needing step-by-step logic.",
        "vision": False, "reasoning": True, "json_mode": False, "cost": "free",
        "notes": "Free R1 reasoning via GitHub Models; slower but deep.",
    },
    "Llama-4-Scout-17B-16E-Instruct": {
        "label": "Llama 4 Scout 17B (GitHub Models)",
        "context": 128000, "params": "17B MoE", "speed": 4,
        "strengths": ["Next-gen Llama", "Reasoning", "Fast"],
        "best_for": "Free next-gen Llama analysis via GitHub Models.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "free",
        "notes": "Llama 4 Scout, free with a GitHub token.",
    },
    # ── Fireworks AI ($1 signup credit) ──────────────────
    "accounts/fireworks/models/llama-v3p3-70b-instruct": {
        "label": "Llama 3.3 70B (Fireworks)",
        "context": 128000, "params": "70B", "speed": 5,
        "strengths": ["Very fast", "Strong reasoning", "Structured JSON"],
        "best_for": "Fast 70B-class analysis on Fireworks.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "cheap",
        "notes": "Fireworks' fast serving; $1 signup credit then pay-per-token.",
    },
    "accounts/fireworks/models/qwen3-30b-a3b": {
        "label": "Qwen3 30B A3B (Fireworks)",
        "context": 128000, "params": "30B MoE", "speed": 5,
        "strengths": ["Efficient", "Reasoning", "Fast"],
        "best_for": "Efficient reasoning at Fireworks speed.",
        "vision": False, "reasoning": True, "json_mode": True, "cost": "cheap",
        "notes": "Mixture-of-experts Qwen3; cheap + fast on Fireworks.",
    },
}

_FREELLMAPI_BASE_URL = os.getenv("AI_ANALYST_FREELLMAPI_BASE_URL", "http://localhost:3002/v1").strip() or "http://localhost:3002/v1"
_FREELLMAPI_DEFAULT_MODEL = os.getenv("AI_ANALYST_FREELLMAPI_DEFAULT_MODEL", "auto").strip() or "auto"


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
        # Verified against the live API. Removed: qwen/qwen3-32b and
        # meta-llama/llama-4-scout-17b-16e-instruct — both 404 "does not exist".
        "models": [
            "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
            "openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.6-27b",
        ],
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
        # Verified against the live API. Removed four ids that now answer
        # 404 "This model is unavailable for free. The paid version is
        # available" — OpenRouter moves models off the free tier over time.
        "models": [
            "google/gemma-4-31b-it:free",
            "google/gemma-4-26b-a4b-it:free",
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
        "default_model": "gemini-3.1-flash-lite",
        # Verified against the live API. Removed gemini-3.1-flash and
        # gemini-3-flash — both 404 "is not found for API version v1beta".
        # The 2.x entries could only be seen rate-limited, not absent, so they
        # stay: a 429 says nothing about whether a model exists.
        "models": [
            "gemini-3.1-flash-lite",
            "gemini-2.5-flash-lite", "gemini-2.5-flash",
            "gemini-2.0-flash", "gemini-2.0-flash-lite",
        ],
        "free_tier": True,
        "daily_limit": 500,
        "monthly_limit": 10000,
        "signup_url": "https://aistudio.google.com/apikey",
        "notes": "Generous free tier via Google AI Studio (Gemini 3.1 Flash-Lite has the highest free RPD at 500/day).",
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
        "models": [
            "deepseek-chat",
            "deepseek-reasoner",
            "deepseek-coder",
        ],
        "free_tier": False,
        "daily_limit": None,
        "monthly_limit": None,
        "signup_url": "https://platform.deepseek.com/api_keys",
        "notes": "DeepSeek-V3 (deepseek-chat): best cost-performance ratio. DeepSeek-R1 (deepseek-reasoner): top reasoning, rivals o1. Very cheap ($0.14/1M input tokens). Add API key from platform.deepseek.com.",
    },
    {
        "key": "together",
        "label": "Together AI",
        "type": "openai_compatible",
        "base_url": "https://api.together.xyz/v1",
        "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
        "models": [
            "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "deepseek-ai/DeepSeek-R1",
            "deepseek-ai/DeepSeek-V3",
            "Qwen/Qwen3-235B-A22B",
            "meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo",
            "mistralai/Mixtral-8x22B-Instruct-v0.1",
            "google/gemma-2-27b-it",
        ],
        "free_tier": True,
        "daily_limit": 100,
        "monthly_limit": 3000,
        "signup_url": "https://api.together.ai/settings/api-keys",
        "notes": "Free Llama 3.3 70B endpoint. Paid access to DeepSeek-R1/V3, Qwen3-235B, Llama 405B and more. $1 starting credit.",
    },
    {
        "key": "nvidia",
        "label": "NVIDIA NIM",
        "type": "openai_compatible",
        "base_url": "https://integrate.api.nvidia.com/v1",
        # NVIDIA lists models in /v1/models that have no inference function
        # deployed: they answer 404 "Function '<uuid>': Not found" on
        # /chat/completions while looking perfectly available in the catalog.
        # Every id below was verified against a live account by calling
        # /chat/completions, and the first two additionally return parseable
        # output under `response_format: json_object`, which is what the
        # research path needs. Undeployed ids that used to be listed here
        # (llama-3.1-nemotron-ultra-253b-v1, -70b-instruct, -51b-instruct,
        # nemotron-4-340b-instruct) are removed — a catalog listing is not
        # evidence that a model will serve.
        # Lightning leads: it is the newest generation and answers in seconds,
        # where Ultra 550B cannot finish inside a normal request deadline at all.
        # A default nobody can wait for is what made this provider look broken.
        "default_model": "nvidia/nemotron-3.5-lightning-30b-a3b",
        "models": [
            # Task-routed models (see ai_router.TASK_MODEL_CHAINS). All four
            # verified serving against a live account on 2026-08-15.
            # Text-only — do not send image blocks to these two.
            "nvidia/nemotron-3.5-lightning-30b-a3b",
            "z-ai/glm-5.2",
            # Vision-capable (text+image in, text out), verified with a chart
            # image. Both emit `reasoning_content` before `content`, so they
            # need max_tokens >= 2048 or `content` comes back empty.
            "thinkingmachines/inkling",
            "meta/muse-glimmer-30b",
            "nvidia/nemotron-3-super-120b-a12b",
            "nvidia/nemotron-3-ultra-550b-a55b",
            "deepseek-ai/deepseek-v4-flash",
            "meta/llama-3.3-70b-instruct",
            "nvidia/llama-3.3-nemotron-super-49b-v1.5",
            "nvidia/llama-3.3-nemotron-super-49b-v1",
            "nvidia/nemotron-3-nano-30b-a3b",
            "nvidia/nvidia-nemotron-nano-9b-v2",
            "nvidia/nemotron-mini-4b-instruct",
            # Vision — verified serving with an image payload. Of the vision ids
            # NVIDIA lists, only these two and llama-3.1-nemotron-nano-vl-8b-v1
            # have a function deployed; gemma-3, phi-3-vision and neva-22b all
            # 404, and llama-3.2-90b-vision times out.
            "nvidia/nemotron-nano-12b-v2-vl",
            "meta/llama-3.2-11b-vision-instruct",
        ],
        "free_tier": True,
        "daily_limit": None,
        "monthly_limit": None,
        "signup_url": "https://build.nvidia.com/",
        "notes": "Free access to frontier models (40 req/min). Requires phone verification. Best-in-class: Nemotron Super 120B, Nemotron Ultra 253B (largest free model), Meta Llama 405B, DeepSeek-R1 distill.",
    },
    {
        "key": "sambanova",
        "label": "SambaNova Cloud",
        "type": "openai_compatible",
        "base_url": "https://api.sambanova.ai/v1",
        "default_model": "Meta-Llama-3.3-70B-Instruct",
        # Verified against the live API: only this one still serves. The other
        # seven answer 410 "not available on SambaNova Cloud" (or 404), which is
        # the platform having retired them, not a credential problem.
        "models": [
            "Meta-Llama-3.3-70B-Instruct",
        ],
        "free_tier": True,
        "daily_limit": None,
        "monthly_limit": None,
        "signup_url": "https://cloud.sambanova.ai/",
        "notes": "World-record inference speed on RDU hardware. Free tier with rate limits. $5 trial credits for new accounts. Best latency for any model size.",
    },
    {
        "key": "xai",
        "label": "xAI (Grok)",
        "type": "openai_compatible",
        "base_url": "https://api.x.ai/v1",
        "default_model": "grok-3-fast",
        "models": [
            "grok-3",
            "grok-3-fast",
            "grok-3-mini",
            "grok-3-mini-fast",
            "grok-2-1212",
            "grok-2-vision-1212",
        ],
        "free_tier": False,
        "daily_limit": None,
        "monthly_limit": None,
        "signup_url": "https://console.x.ai/",
        "notes": "Grok-3 and Grok-3-Fast: xAI's flagship models with real-time X/Twitter knowledge. Grok-3-Fast is fastest; Grok-3-Mini for cost efficiency. Add API key from console.x.ai.",
    },
    {
        "key": "perplexity",
        "label": "Perplexity AI",
        "type": "openai_compatible",
        "base_url": "https://api.perplexity.ai",
        "default_model": "sonar-pro",
        "models": [
            "sonar-reasoning-pro",
            "sonar-pro",
            "sonar-reasoning",
            "sonar",
            "r1-1776",
        ],
        "free_tier": False,
        "daily_limit": None,
        "monthly_limit": None,
        "signup_url": "https://www.perplexity.ai/settings/api",
        "notes": "Online models with real-time web search. sonar-reasoning-pro = best quality + reasoning + web. r1-1776 = uncensored DeepSeek-R1 variant. Excellent for news context with fresh web data.",
    },
    {
        "key": "cohere",
        "label": "Cohere",
        "type": "openai_compatible",
        "base_url": "https://api.cohere.ai/compatibility/v1",
        "default_model": "command-a-03-2025",
        "models": [
            "command-a-03-2025",
            "command-r-plus-08-2024",
            "c4ai-aya-expanse-32b",
        ],
        "free_tier": True,
        "daily_limit": None,
        "monthly_limit": 1000,
        "signup_url": "https://dashboard.cohere.com/api-keys",
        "notes": "1000 requests/month free (shared across models). Command A has a 256K context window. Uses Cohere's OpenAI-compatibility endpoint.",
    },
    {
        "key": "github_models",
        "label": "GitHub Models",
        "type": "openai_compatible",
        # `models.inference.ai.azure.com` is the retired preview host and now
        # answers 401 regardless of how good the token is. The current endpoint
        # is models.github.ai, and it requires the `publisher/model` form —
        # sending a bare `gpt-4o` there 404s.
        "base_url": "https://models.github.ai/inference",
        "default_model": "openai/gpt-4o",
        "models": [
            "openai/o3",
            "openai/o3-mini",
            "openai/gpt-4o",
            "openai/gpt-4o-mini",
            "meta/Llama-4-Scout-17B-16E-Instruct",
            "deepseek/DeepSeek-R1",
            "mistral-ai/Ministral-3B",
        ],
        "free_tier": False,
        "daily_limit": None,
        "monthly_limit": None,
        "signup_url": "https://github.com/settings/tokens",
        "notes": "RETIRED BY GITHUB — every endpoint, including the model catalog, answers HTTP 410 `github_models_retirement_brownout`. No API key, base URL or model id restores it, so none of the models below can be verified or used. Kept only so an existing install shows why it stopped rather than silently losing its configuration. The same models are available elsewhere: GPT-4o/o3 via OpenAI, DeepSeek-R1 via NVIDIA NIM, Llama via Groq or NVIDIA NIM.",
    },
    {
        "key": "hyperbolic",
        "label": "Hyperbolic",
        "type": "openai_compatible",
        "base_url": "https://api.hyperbolic.xyz/v1",
        "default_model": "Qwen/Qwen3-235B-A22B",
        "models": [
            "Qwen/Qwen3-235B-A22B",
            "deepseek-ai/DeepSeek-V3-0324",
            "deepseek-ai/DeepSeek-R1",
            "meta-llama/Meta-Llama-3.1-405B-Instruct",
            "meta-llama/Llama-3.3-70B-Instruct",
            "Qwen/Qwen2.5-72B-Instruct",
        ],
        "free_tier": False,
        "daily_limit": None,
        "monthly_limit": None,
        "signup_url": "https://app.hyperbolic.xyz/settings",
        "notes": "Fast GPU inference. Qwen3-235B, DeepSeek-V3, DeepSeek-R1, Llama 405B. Competitive pricing. Add API key from app.hyperbolic.xyz.",
    },
    {
        "key": "fireworks",
        "label": "Fireworks AI",
        "type": "openai_compatible",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "default_model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
        "models": [
            "accounts/fireworks/models/llama-v3p3-70b-instruct",
            "accounts/fireworks/models/llama-v3p1-405b-instruct",
            "accounts/fireworks/models/qwen3-30b-a3b",
            "accounts/fireworks/models/deepseek-r1",
            "accounts/fireworks/models/deepseek-v3",
            "accounts/fireworks/models/mixtral-8x22b-instruct-hf",
        ],
        "free_tier": False,
        "daily_limit": None,
        "monthly_limit": None,
        "signup_url": "https://fireworks.ai/account/api-keys",
        "notes": "Fast open-weight serving. $1 signup credit, then cheap pay-per-token. Model ids use 'accounts/fireworks/models/...' prefix. Specializes in speed.",
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
    {
        # FreeLLMAPI — a self-hosted OpenAI-compatible proxy that stacks the
        # free tiers of 16+ providers behind one /v1 endpoint with model="auto"
        # routing + automatic failover. Run it yourself (docker) and point this
        # super-provider at it; it handles per-upstream rate limits internally.
        "key": "freellmapi",
        "label": "FreeLLMAPI (self-hosted proxy)",
        "type": "openai_compatible",
        "base_url": _FREELLMAPI_BASE_URL,
        "default_model": _FREELLMAPI_DEFAULT_MODEL,
        "models": [_FREELLMAPI_DEFAULT_MODEL],
        "free_tier": True,
        "daily_limit": None,   # the proxy enforces upstream free-tier caps itself
        "monthly_limit": None,
        "signup_url": "https://freellmapi.co/",
        "notes": "Self-host FreeLLMAPI (docker) — one endpoint that aggregates 16+ free providers. In Tradebot, run it on PORT=3002 so it does not collide with the frontend on 3001, then use your unified freellmapi-… key with model 'auto'. Edit the Base URL if it runs elsewhere.",
        "editable_endpoint": True,
    },
    {
        # Generic custom OpenAI-compatible endpoint (LM Studio, Ollama, vLLM,
        # llama.cpp, a remote gateway, etc.). User supplies base_url + model.
        "key": "custom",
        "label": "Custom OpenAI-compatible endpoint",
        "type": "openai_compatible",
        "base_url": "",
        "default_model": "",
        "models": [],
        "free_tier": True,
        "daily_limit": None,
        "monthly_limit": None,
        "signup_url": "https://platform.openai.com/docs/api-reference/chat",
        "notes": "Point at any OpenAI-compatible /v1 endpoint — LM Studio, Ollama (http://localhost:11434/v1), vLLM, llama.cpp, or a remote gateway. Enter the Base URL and model id.",
        "editable_endpoint": True,
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
