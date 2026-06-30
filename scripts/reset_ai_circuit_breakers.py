#!/usr/bin/env python3
"""
Reset AI Provider Circuit Breakers
===================================
Clears all tripped circuit breakers and forces provider registry reload.
Use this after fixing API keys or when providers are stuck in "circuit open" state.
"""
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from plugins.AiMarketAnalyst.backend.services import llm_gateway, llm_registry
from loguru import logger


def main():
    """Reset all circuit breakers and reload provider registry."""
    logger.info("[Circuit Breaker Reset] Starting...")
    
    # Clear all circuit breakers
    circuit_count = len(llm_gateway._circuits)
    llm_gateway._circuits.clear()
    logger.info(f"[Circuit Breaker Reset] Cleared {circuit_count} circuit breakers")
    
    # Force reload provider registry from disk
    providers = llm_registry.get_providers(force_reload=True)
    logger.info(f"[Circuit Breaker Reset] Reloaded {len(providers)} providers from registry")
    
    # Show enabled providers
    enabled = [p for p in providers if p.enabled]
    logger.info(f"[Circuit Breaker Reset] Enabled providers ({len(enabled)}):")
    for provider in enabled:
        logger.info(f"  - {provider.label} ({provider.id}): {provider.type}")
        if provider.models and "*" not in provider.models:
            logger.info(f"    Models: {', '.join(provider.models[:3])}")
    
    # Show disabled providers
    disabled = [p for p in providers if not p.enabled]
    if disabled:
        logger.warning(f"[Circuit Breaker Reset] Disabled providers ({len(disabled)}):")
        for provider in disabled:
            logger.warning(f"  - {provider.label} ({provider.id}): missing {provider.api_key_env}")
    
    logger.info("[Circuit Breaker Reset] Complete! Providers ready to use.")


if __name__ == "__main__":
    main()
