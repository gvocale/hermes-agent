"""Dashboard model cards must not invent catalog capacity for unknown routes."""


def test_model_capabilities_empty_provider_is_unknown():
    from hermes_cli.web_routers.analytics import _model_capabilities

    assert _model_capabilities("", "gpt-5.4") == {}


def test_model_capabilities_allows_network_catalog_refresh(monkeypatch):
    from hermes_cli.web_routers import analytics as analytics_mod

    seen = {}

    class Caps:
        supports_tools = True
        supports_vision = False
        supports_reasoning = False
        context_window = 128000
        max_output_tokens = 8192
        model_family = "test"

    def fake_get(*, provider, model, allow_network=False):
        seen["allow_network"] = allow_network
        seen["provider"] = provider
        seen["model"] = model
        return Caps()

    monkeypatch.setattr(
        "agent.models_dev.get_model_capabilities",
        fake_get,
    )
    out = analytics_mod._model_capabilities("openai-codex", "gpt-5.4")
    assert seen["allow_network"] is True
    assert out["context_window"] == 128000
