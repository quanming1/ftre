"""共享 golden fixture 回放测试（PRD-F41 AC1 / F43 AC5）。

fixture（``fixtures/session_events_golden.json``）是跨语言对拍的唯一事实源：
服务端 ``derive_messages`` 与 desktop ``ConversationAssembler``（经
``scripts/gen_wire_types.py`` 同步该 fixture 到 desktop types 目录）对同一
事件序列 fold，输出必须逐字段一致。本文件锁定服务端侧：derive 输出 ==
fixture.expected_messages（防止 fixture 与实现漂移）。
"""
from __future__ import annotations

import json
from pathlib import Path

from ftre_agent.session import derive_messages

FIXTURE = Path(__file__).parent / "fixtures" / "session_events_golden.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_replay_matches_expected_messages():
    fixture = _load()
    events = fixture["events"]
    messages = derive_messages(events)
    dumped = [m.model_dump(mode="json", exclude_none=True) for m in messages]
    assert dumped == fixture["expected_messages"]


def test_fixture_covers_all_surface_event_types():
    """fixture 至少覆盖每种表面事件与关键生命周期分支。"""
    fixture = _load()
    types = {event["type"] for event in fixture["events"]}
    assert {
        "user/message", "assistant/message", "tool/result",
        "hint/message", "compact/message",
        "assistant/chunk", "tool/call-start", "tool/result-start",
        "approval/asked", "turn/start", "turn/end",
    } <= types
    outcomes = {
        event["data"]["outcome"]
        for event in fixture["events"] if event["type"] == "turn/end"
    }
    assert {"completed", "paused", "error"} <= outcomes


def test_fixture_derive_is_deterministic():
    """同一事件序列两次 derive 输出完全一致（确定性 id/时间戳，无随机生成）。"""
    fixture = _load()
    events = fixture["events"]
    first = [m.model_dump(mode="json") for m in derive_messages(events)]
    second = [m.model_dump(mode="json") for m in derive_messages(events)]
    assert first == second
