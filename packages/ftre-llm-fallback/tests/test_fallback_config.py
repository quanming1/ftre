from ftre_llm_fallback.config import parse_config


def test_parse_config_normalizes_error_codes_without_accepting_credentials():
    config = parse_config(
        {
            "provider": " backup ",
            "model": " model ",
            "errors": [" TIMEOUT ", "rate_limit"],
            "api_key": "must-not-be-read",
        }
    )
    assert config.enabled
    assert config.provider == "backup"
    assert config.model == "model"
    assert config.errors == {"timeout", "rate_limit"}
    assert not hasattr(config, "api_key")
