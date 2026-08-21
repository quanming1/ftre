"""config api_type 三级回退测试（PRD-A1 FR6/AC4）。

model 条目 api_type > provider 级 api_type > 默认 "completions"。
"""


from ftre.services.agent.config import _build_llm_config


def _provider_data(model_api_type=None, provider_api_type=None):
    model_entry = {"name": "Test Model", "id": "test-model"}
    if model_api_type is not None:
        model_entry["api_type"] = model_api_type
    provider = {
        "api_key": "k",
        "api_base": "http://x",
        "models": [model_entry],
    }
    if provider_api_type is not None:
        provider["api_type"] = provider_api_type
    return {"providers": {"p1": provider}}


class TestApiTypeFallback:
    def test_model_level_wins(self):
        cfg = _build_llm_config(_provider_data(model_api_type="responses"), "p1", "test-model")
        assert cfg.api_type == "responses"

    def test_provider_level_fallback(self):
        cfg = _build_llm_config(_provider_data(provider_api_type="responses"), "p1", "test-model")
        assert cfg.api_type == "responses"

    def test_default_completions(self):
        cfg = _build_llm_config(_provider_data(), "p1", "test-model")
        assert cfg.api_type == "completions"

    def test_model_level_overrides_provider(self):
        cfg = _build_llm_config(
            _provider_data(model_api_type="responses", provider_api_type="completions"),
            "p1", "test-model",
        )
        assert cfg.api_type == "responses"

    def test_non_string_api_type_falls_back_to_default(self):
        cfg = _build_llm_config(_provider_data(model_api_type=123), "p1", "test-model")
        assert cfg.api_type == "completions"
