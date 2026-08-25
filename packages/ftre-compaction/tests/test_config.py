from __future__ import annotations

from ftre_compaction.config import CompactionConfig, parse_compaction_config


def test_parse_compaction_config_reads_package_owned_fields():
    raw = {
        "agents": {
            "context": {
                "precompactThreshold": 0.6,
                "compactThreshold": 0.85,
                "safetyBuffer": 2048,
            }
        }
    }

    result = parse_compaction_config(raw)

    assert result == CompactionConfig(
        precompact_threshold=0.6,
        compact_threshold=0.85,
        safety_buffer=2048,
    )


def test_parse_compaction_config_accepts_legacy_threshold_alias_without_core_owner():
    result = parse_compaction_config({"agents": {"context": {"threshold": 0.9}}})

    assert result.compact_threshold == 0.9
    assert result.precompact_threshold == 0.7


def test_parse_compaction_config_uses_defaults_for_invalid_values():
    defaults = CompactionConfig(
        precompact_threshold=0.55,
        compact_threshold=0.75,
        safety_buffer=512,
    )

    result = parse_compaction_config(
        {"agents": {"context": {"compactThreshold": "bad", "safetyBuffer": -10}}},
        defaults=defaults,
    )

    assert result.precompact_threshold == defaults.precompact_threshold
    assert result.compact_threshold == defaults.compact_threshold
    assert result.safety_buffer == 0


def test_parse_compaction_config_resolves_optional_summary_model():
    raw = {
        "providers": {
            "cheap": {
                "api_key": "key",
                "models": [{"id": "summary", "context_window": 4096}],
            }
        },
        "agents": {"compact_generation": {"provider": "cheap", "model": "summary"}},
    }

    result = parse_compaction_config(raw)

    assert result.llm is not None
    assert result.llm.model == "summary"
    assert result.llm.api_key == "key"


def test_parse_compaction_config_does_not_mutate_input():
    raw = {"agents": {"context": {"compact_threshold": 0.8}}}

    parse_compaction_config(raw)

    assert raw == {"agents": {"context": {"compact_threshold": 0.8}}}


def test_parse_compaction_config_reads_parallel_limits():
    result = parse_compaction_config({
        "agents": {
            "context": {
                "parallelWorkers": 2,
                "parallelTimeoutSeconds": 30,
                "parallelRetryAttempts": 0,
            }
        }
    })

    assert result.parallel_workers == 2
    assert result.parallel_timeout_seconds == 30
    assert result.parallel_retry_attempts == 0


def test_parse_compaction_config_clamps_parallel_limits():
    result = parse_compaction_config({
        "agents": {
            "context": {
                "parallel_workers": 99,
                "parallel_timeout_seconds": 9999,
                "parallel_retry_attempts": -1,
            }
        }
    })

    assert result.parallel_workers == 3
    assert result.parallel_timeout_seconds == 300
    assert result.parallel_retry_attempts == 0
