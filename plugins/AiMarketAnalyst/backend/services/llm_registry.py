"""AI Market Analyst - LLM Provider Registry"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
import json

from loguru import logger

from plugins.AiMarketAnalyst.backend.config import ai_analyst_config


SUPPORTED_TYPES = {"openai", "openai_compatible", "anthropic", "google"}


@dataclass(frozen=True)
class LLMProvider:
    id: str
    label: str
    type: str
    api_key_env: str
    base_url: Optional[str] = None
    models: List[str] = field(default_factory=list)
    rate_limits: Dict[str, int] = field(default_factory=dict)
    weight: float = 1.0
    enabled: bool = True

    def supports_model(self, model: str) -> bool:
        if not self.models or "*" in self.models:
            return True
        return model in self.models


def _default_providers() -> List[LLMProvider]:
    return [
        LLMProvider(
            id="openai",
            label="OpenAI",
            type="openai",
            api_key_env="OPENAI_API_KEY",
            base_url=None,
            models=["*"],
            rate_limits={"minute": ai_analyst_config.openai_rpm_limit},
            weight=1.0,
            enabled=True,
        )
    ]


def _normalize_provider(item: Dict) -> Optional[LLMProvider]:
    provider_id = str(item.get("id", "")).strip()
    if not provider_id:
        return None

    api_key_env = str(item.get("api_key_env", "")).strip()
    if not api_key_env:
        logger.warning(f"[AI-Analyst] Provider '{provider_id}' missing api_key_env")
        return None

    label = str(item.get("label", provider_id)).strip()
    provider_type = str(item.get("type", "openai_compatible")).strip()
    base_url = item.get("base_url")
    models = item.get("models", []) or []
    rate_limits = item.get("rate_limits", item.get("limits", {})) or {}
    weight = float(item.get("weight", 1.0))
    enabled = bool(item.get("enabled", True))

    return LLMProvider(
        id=provider_id,
        label=label,
        type=provider_type,
        api_key_env=api_key_env,
        base_url=base_url,
        models=[str(m) for m in models],
        rate_limits={str(k): int(v) for k, v in rate_limits.items()},
        weight=weight,
        enabled=enabled,
    )


def _load_from_json(payload: str) -> List[LLMProvider]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        logger.warning(f"[AI-Analyst] Provider JSON invalid: {exc}")
        return []

    if isinstance(data, dict):
        items = data.get("providers", [])
    else:
        items = data

    providers: List[LLMProvider] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        provider = _normalize_provider(item)
        if provider:
            providers.append(provider)
    return providers


def _load_from_file(path_str: str) -> List[LLMProvider]:
    path = Path(path_str)
    if not path.is_file():
        return []
    try:
        return _load_from_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.warning(f"[AI-Analyst] Provider file read failed: {exc}")
        return []


_providers_cache: Optional[List[LLMProvider]] = None


def get_providers(force_reload: bool = False) -> List[LLMProvider]:
    global _providers_cache
    if _providers_cache is not None and not force_reload:
        return _providers_cache

    providers: List[LLMProvider] = []

    if ai_analyst_config.providers_json:
        providers = _load_from_json(ai_analyst_config.providers_json)
    elif ai_analyst_config.providers_file:
        providers = _load_from_file(ai_analyst_config.providers_file)

    if not providers:
        providers = _default_providers()

    _providers_cache = providers
    return providers


def get_enabled_providers() -> List[LLMProvider]:
    return [p for p in get_providers() if p.enabled]
