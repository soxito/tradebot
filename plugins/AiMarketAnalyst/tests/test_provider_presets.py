from plugins.AiMarketAnalyst.backend.services.provider_presets import get_preset


def test_freellmapi_preset_uses_tradebot_safe_port():
    preset = get_preset("freellmapi")

    assert preset is not None
    assert preset["editable_endpoint"] is True
    assert preset["base_url"] == "http://localhost:3002/v1"
    assert preset["default_model"] == "auto"
    assert preset["models"] == ["auto"]
    assert "PORT=3002" in preset["notes"]
