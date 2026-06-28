from plugins.AiMarketAnalyst.backend.services.llm_registry import get_providers


def test_default_providers_present():
    providers = get_providers(force_reload=True)
    assert providers
    assert any(p.id == "openai" for p in providers)
