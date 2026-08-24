"""Agent Hook 契约只覆盖 active Step/Turn，不携带 Inbox 队列模型。"""

from __future__ import annotations

import asyncio

from ftre.services.agent.config import AgentConfig
from ftre.services.agent.hooks import (
    AGENT_AFTER_RUN_SPEC,
    AGENT_BEFORE_RUN_SPEC,
    AgentSubject,
    AllowRun,
    BeforeRunPayload,
)


def test_before_turn_is_generic_and_has_no_queue_candidate() -> None:
    payload = BeforeRunPayload(
        agent=AgentSubject("default", object()),
        session_id="s1",
        turn_id="t1",
        cancellation=asyncio.Event(),
        config=AgentConfig(),
    )
    assert not hasattr(payload, "candidate")
    assert isinstance(asyncio.run(AGENT_BEFORE_RUN_SPEC.default(payload)), AllowRun)


def test_after_turn_spec_is_registered_as_agent_hook() -> None:
    assert AGENT_AFTER_RUN_SPEC.name == "agent/after-run"
    assert AGENT_AFTER_RUN_SPEC.scope.value == "agent"
