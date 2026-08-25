from dataclasses import replace

from ftre_llm_recovery.config import parse_config
from ftre_llm_recovery.policy import decide

from ftre.services.llm.hooks import LLMErrorPayload


def _payload(code: str) -> LLMErrorPayload:
    import asyncio

    return LLMErrorPayload(
        session_id="s",
        turn_id="t",
        iteration=1,
        model="primary",
        error_code=code,
        error_message="failed",
        attempt=1,
        max_attempts=3,
        cancellation=asyncio.Event(),
    )


def test_policy_returns_retry_rule_without_owning_attempt_limit():
    config = parse_config({"rules": {"timeout": {"action": "retry", "delay": 2}}})
    result = decide(_payload("timeout"), config)
    assert result is not None
    assert result.action == "retry"
    assert result.delay == 2.0


def test_unknown_and_excluded_errors_return_to_core_default():
    config = parse_config(
        {
            "rules": {"overflow": {"action": "retry"}},
            "exclude_codes": ["overflow"],
        }
    )
    assert decide(_payload("overflow"), config) is None
    assert decide(_payload("unknown"), config) is None


def test_context_length_text_is_excluded_even_when_provider_uses_bad_request():
    config = parse_config({"rules": {"bad_request": {"action": "stop"}}})
    payload = _payload("bad_request")
    payload = replace(payload, error_message="context length exceeded")
    assert decide(payload, config) is None
