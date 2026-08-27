from __future__ import annotations

from ftre.services.config.service import ConfigService


def test_resolve_llm_returns_adapter_snapshot_without_exposing_internal_config(tmp_path):
    service = ConfigService(
        tmp_path / "config.json",
        {
            "providers": {
                "backup": {
                    "api_key": "secret",
                    "api_base": "https://example.test/v1",
                    "api_type": "completions",
                    "models": [
                        {
                            "id": "backup-model",
                            "api_type": "responses",
                            "context_window": 128000,
                            "max_output": 2048,
                            "vision": True,
                            "reasoning_effort": "low",
                            "reasoning_effort_values": ["low", "high"],
                        }
                    ],
                }
            }
        },
    )

    resolved = service.resolve_llm("backup", "backup-model")

    assert resolved == {
        "provider": "backup",
        "model": "backup-model",
        "api_key": "secret",
        "api_base": "https://example.test/v1",
        "api_type": "responses",
        "context_window": 128000,
        "vision": True,
        "reasoning_effort": "low",
        "reasoning_effort_values": ("low", "high"),
        "max_output": 2048,
    }
    resolved["model"] = "changed"
    assert service.resolve_llm("backup", "backup-model")["model"] == "backup-model"


def test_resolve_llm_returns_none_for_unknown_provider_or_model(tmp_path):
    service = ConfigService(tmp_path / "config.json", {"providers": {}})

    assert service.resolve_llm("missing", "model") is None
    assert service.resolve_llm("", "model") is None
