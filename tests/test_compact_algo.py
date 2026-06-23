"""
compact_handler 模块级算法工具的单测。

只测纯函数，不依赖 db / channel / bus。
"""
from __future__ import annotations

import pytest

from ftre.agent.compact_handler import (
    get_cursor_index,
    get_pending_compact_index,
    get_previous_summary,
    _serialize_events,
    _build_prompt,
    _compact_enabled,
    SUMMARY_TEMPLATE,
)


# ─── get_cursor_index / get_previous_summary ──────────────────────────


def test_cursor_no_compact():
    events = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "hi"}},
        {"type": "assistant_message_complete", "data": {"content": "yes"}},
    ]
    assert get_cursor_index(events) == 0
    assert get_previous_summary(events) is None


def test_cursor_with_one_compact():
    events = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "a"}},
        {"type": "context_compact", "data": {"summary": "## summary v1"}},
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "b"}},
    ]
    # context_compact 在 idx 1，cursor 应指向 idx 2
    assert get_cursor_index(events) == 2
    assert get_previous_summary(events) == "## summary v1"


def test_cursor_with_multiple_compacts_takes_latest():
    events = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "a"}},
        {"type": "context_compact", "data": {"summary": "v1"}},
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "b"}},
        {"type": "context_compact", "data": {"summary": "v2"}},
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "c"}},
    ]
    assert get_cursor_index(events) == 4
    assert get_previous_summary(events) == "v2"


def test_pending_compact_does_not_advance_cursor():
    events = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "a"}},
        {"type": "context_compact", "data": {"summary": "v1", "enabled": True}},
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "b"}},
        {"type": "context_compact", "data": {"summary": "pending", "enabled": False}},
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "c"}},
    ]
    assert get_cursor_index(events) == 2
    assert get_previous_summary(events) == "v1"
    assert get_pending_compact_index(events) == 3


def test_compact_enabled_default_true():
    # 旧事件缺少 enabled → 按已启用处理
    assert _compact_enabled({"type": "context_compact", "data": {"summary": "v1"}}) is True
    assert _compact_enabled({"type": "context_compact", "data": {"summary": "v1", "enabled": True}}) is True
    assert _compact_enabled({"type": "context_compact", "data": {"summary": "v1", "enabled": False}}) is False


def test_pending_compact_index_no_pending():
    events = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "a"}},
        {"type": "context_compact", "data": {"summary": "v1", "enabled": True}},
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "b"}},
    ]
    assert get_pending_compact_index(events) is None


# ─── _serialize_events ────────────────────────────────────────────


def test_serialize_empty():
    assert _serialize_events([]) == ""


def test_serialize_preserves_user_full_text():
    chunk = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "请按文档第 3 节实现压缩算法，注意游标只进不退"}},
        {"type": "assistant_message_complete", "data": {"content": "好的"}},
    ]
    out = _serialize_events(chunk)
    assert "请按文档第 3 节实现压缩算法，注意游标只进不退" in out
    assert "[User]:" in out
    assert "[Assistant]:" in out


def test_serialize_truncates_long_tool_result():
    big = "x" * 3000
    chunk = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "go"}},
        {"type": "tool_call", "data": {"id": "c1", "name": "bash", "arguments": {}}},
        {"type": "tool_result", "data": {"id": "c1", "result": big}},
    ]
    out = _serialize_events(chunk)
    # 默认 tool_output_max_chars=2000，所以 3000 字符的结果会被截断
    assert "x" * 1500 in out  # 前 2000 字符在
    assert "[truncated]" in out
    assert "x" * 2500 not in out  # 超出 2000 的部分不在


def test_serialize_handles_multimodal_user_content():
    chunk = [
        {
            "type": "user_message",
        "data": {"metadata": {"hide": False}, "content": [{"type": "text", "data": "看下这张图"}]},
        },
    ]
    out = _serialize_events(chunk)
    assert "看下这张图" in out


def test_serialize_formats_tool_call():
    chunk = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "go"}},
        {"type": "tool_call", "data": {"id": "c1", "name": "bash", "arguments": {"command": "ls"}}},
        {"type": "tool_result", "data": {"id": "c1", "result": "file1\nfile2"}},
    ]
    out = _serialize_events(chunk)
    assert "[Assistant tool call]: bash(" in out
    assert "[Tool result]:" in out


def test_serialize_reasoning():
    chunk = [
        {"type": "reasoning_complete", "data": {"content": "我在想这个问题..."}},
    ]
    out = _serialize_events(chunk)
    assert "[Assistant reasoning]: 我在想这个问题..." in out


# ─── _build_prompt ────────────────────────────────────────────


def test_build_prompt_first_time():
    prompt = _build_prompt(context=["[User]: hello\n\n[Assistant]: hi"])
    assert "创建一份新的锚定摘要" in prompt
    assert SUMMARY_TEMPLATE in prompt
    assert "[User]: hello" in prompt


def test_build_prompt_incremental():
    prompt = _build_prompt(
        previous_summary="## 目标\n- 做某事",
        context=["[User]: hello\n\n[Assistant]: hi"],
    )
    assert "更新下方的锚定摘要" in prompt
    assert "<previous-summary>" in prompt
    assert "## 目标\n- 做某事" in prompt
    assert "</previous-summary>" in prompt
    assert SUMMARY_TEMPLATE in prompt


# ─── L1 prune 修剪测试 ────────────────────────────────────────────


def _events_with_long_tool(*, big_chars: int = 5000):
    """构造：3 轮，每轮含一个 tool_call/tool_result（result 很长）。"""
    big = "x" * big_chars
    out = []
    for i in range(3):
        out.append({"type": "user_message", "data": {"metadata": {"hide": False}, "content": f"q{i}"}})
        out.append({
            "type": "tool_call",
            "data": {"id": f"c{i}", "name": "bash", "arguments": {}},
        })
        out.append({
            "type": "tool_result",
            "data": {"id": f"c{i}", "result": big, "error": None},
        })
        out.append({"type": "assistant_message_complete", "data": {"content": "done"}})
    return out


def test_prune_protects_recent_turns():
    """最近 protect_turns 个 user_message 内的 tool_result 不截断。"""
    from ftre.session.manager import SessionManager
    events = _events_with_long_tool(big_chars=5000)
    msgs = SessionManager.to_openai_messages(
        events,
        prune={"protect_turns": 2, "max_chars": 2000, "head_chars": 1000, "tail_chars": 1000},
    )
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs[0]["content"]) < 5000
    assert "[L1 修剪" in tool_msgs[0]["content"]
    assert len(tool_msgs[1]["content"]) == 5000
    assert len(tool_msgs[2]["content"]) == 5000


def test_prune_no_action_when_below_max_chars():
    from ftre.session.manager import SessionManager
    events = _events_with_long_tool(big_chars=500)
    msgs = SessionManager.to_openai_messages(
        events,
        prune={"protect_turns": 2, "max_chars": 2000, "head_chars": 1000, "tail_chars": 1000},
    )
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    for m in tool_msgs:
        assert len(m["content"]) == 500
        assert "[L1 修剪" not in m["content"]


def test_prune_preserves_failed_results():
    from ftre.session.manager import SessionManager
    big = "x" * 5000
    events = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "q"}},
        {"type": "tool_call", "data": {"id": "c1", "name": "bash", "arguments": {}}},
        {"type": "tool_result", "data": {"id": "c1", "result": big, "error": "权限拒绝"}},
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "q2"}},
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "q3"}},
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "q4"}},
    ]
    msgs = SessionManager.to_openai_messages(
        events,
        prune={"protect_turns": 1, "max_chars": 2000, "head_chars": 500, "tail_chars": 500},
    )
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs[0]["content"]) == 5000
    assert "[L1 修剪" not in tool_msgs[0]["content"]


def test_prune_disabled_when_not_passed():
    from ftre.session.manager import SessionManager
    events = _events_with_long_tool(big_chars=10000)
    msgs = SessionManager.to_openai_messages(events)
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    for m in tool_msgs:
        assert len(m["content"]) == 10000
        assert "[L1 修剪" not in m["content"]


def test_to_openai_messages_omits_images_by_default():
    from ftre.session.manager import SessionManager

    events = [{
        "type": "user_message",
        "data": {"metadata": {"hide": False}, 
            "content": "看图",
            "attachments": [{
                "type": "image",
                "mime_type": "image/png",
                "data": "abc",
            }],
        },
    }]

    msgs = SessionManager.to_openai_messages(events)

    assert isinstance(msgs[0]["content"], str)
    assert "image_url" not in str(msgs[0]["content"])
    assert "当前模型不支持视觉输入" in msgs[0]["content"]


def test_to_openai_messages_omits_images_when_vision_disabled():
    from ftre.session.manager import SessionManager

    events = [{
        "type": "user_message",
        "data": {"metadata": {"hide": False}, 
            "content": "看图",
            "attachments": [{
                "type": "image",
                "mime_type": "image/png",
                "data": "abc",
            }],
        },
    }]

    msgs = SessionManager.to_openai_messages(
        events,
        config={"llm": {"vision": False}},
    )

    assert isinstance(msgs[0]["content"], str)
    assert "image_url" not in str(msgs[0]["content"])
    assert "当前模型不支持视觉输入" in msgs[0]["content"]


def test_to_openai_messages_keeps_images_when_vision_enabled():
    from ftre.session.manager import SessionManager

    events = [{
        "type": "user_message",
        "data": {"metadata": {"hide": False}, 
            "content": "看图",
            "attachments": [{
                "type": "image",
                "mime_type": "image/png",
                "data": "abc",
            }],
        },
    }]

    msgs = SessionManager.to_openai_messages(
        events,
        config={"llm": {"vision": True}},
    )

    assert msgs[0]["content"][0] == {"type": "text", "text": "看图"}
    assert msgs[0]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,abc"},
    }


def test_to_openai_messages_omits_user_message_images_without_vision():
    from ftre.session.manager import SessionManager

    events = [{
        "type": "user_message",
        "data": {
            "content": [{
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,abc"},
            }],
            "metadata": {"hide": True},
        },
    }]

    msgs = SessionManager.to_openai_messages(
        events,
        config={"llm": {"vision": False}},
    )

    assert msgs == [{
        "role": "user",
        "content": "[图片附件已省略：当前模型不支持视觉输入]",
    }]


def test_to_openai_messages_keeps_user_message_images_with_vision():
    from ftre.session.manager import SessionManager

    content = [{
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,abc"},
    }]
    events = [{
        "type": "user_message",
        "data": {
            "content": content,
            "metadata": {"hide": True},
        },
    }]

    msgs = SessionManager.to_openai_messages(
        events,
        config={"llm": {"vision": True}},
    )

    assert msgs == [{"role": "user", "content": content}]


def test_to_openai_messages_ignores_disabled_compact():
    """pending compact 事件不影响上下文重建。"""
    from ftre.session.manager import SessionManager
    events = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "old"}},
        {"type": "assistant_message_complete", "data": {"content": "old answer"}},
        {"type": "context_compact", "data": {"summary": "## pending", "enabled": False}},
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "new"}},
    ]
    msgs = SessionManager.to_openai_messages(events)
    joined = "\n".join(str(m.get("content", "")) for m in msgs)
    assert "old" in joined
    assert "## pending" not in joined


def test_to_openai_messages_uses_enabled_compact():
    """enabled compact 事件启用 summary + tail 视图。"""
    from ftre.session.manager import SessionManager
    events = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "old"}},
        {"type": "assistant_message_complete", "data": {"content": "old answer"}},
        {"type": "context_compact", "data": {"summary": "## enabled", "enabled": True}},
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "new"}},
    ]
    msgs = SessionManager.to_openai_messages(events)
    joined = "\n".join(str(m.get("content", "")) for m in msgs)
    assert "## enabled" in joined
    assert "new" in joined
    assert "old answer" not in joined


@pytest.mark.asyncio
async def test_run_compact_llm_collects_stream(monkeypatch):
    """Regression: stream chunks must be collected before summary validation."""
    import types

    from ftre_agent_core.llm import TextDelta, StepFinish
    import ftre.agent.compact_handler as compact_module

    class FakeLLMHandler:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def stream(self, messages):
            assert messages
            yield TextDelta(text="## 目标\n")
            yield TextDelta(text=("- 做某事" * 60))
            yield StepFinish(finish_reason="stop")

    handler = object.__new__(compact_module.CompactHandler)
    handler.session_manager = None
    handler.channel_manager = None
    handler.bus = None
    handler._threshold = 0.6
    config = types.SimpleNamespace(
        llm=types.SimpleNamespace(
            model="fake-model",
            api_key="fake-key",
            api_base="https://example.test",
            api_type="openai",
        )
    )
    events = [{"type": "user_message", "data": {"metadata": {"hide": False}, "content": "hello"}}]

    monkeypatch.setattr(compact_module, "LLMHandler", FakeLLMHandler)

    summary = await handler._run_compact_llm(events, config=config)

    assert summary.startswith("## 目标")


@pytest.mark.asyncio
async def test_run_compact_llm_records_llm_error(monkeypatch):
    """Regression: compact callers need the LLM error code to suppress idle retry storms."""
    import types

    from ftre_agent_core.llm import LLMError
    import ftre.agent.compact_handler as compact_module

    class FakeLLMHandler:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def stream(self, messages):
            raise LLMError("Insufficient Balance", "bad_request")
            yield

    handler = object.__new__(compact_module.CompactHandler)
    handler.session_manager = None
    handler.channel_manager = None
    handler.bus = None
    handler._threshold = 0.6
    handler._last_llm_error = None
    config = types.SimpleNamespace(
        llm=types.SimpleNamespace(
            model="fake-model",
            api_key="fake-key",
            api_base="https://example.test",
            api_type="openai",
        )
    )
    events = [{"type": "user_message", "data": {"metadata": {"hide": False}, "content": "hello"}}]

    monkeypatch.setattr(compact_module, "LLMHandler", FakeLLMHandler)

    summary = await handler._run_compact_llm(events, config=config)

    assert summary is None
    assert handler._last_llm_error is not None
    assert handler._last_llm_error.code == "bad_request"


@pytest.mark.asyncio
async def test_idle_compact_unretryable_llm_error_enters_cooldown():
    import types

    from ftre.agent.loop import AgentLoop
    from ftre_agent_core.llm import LLMError

    class FakeCompactHandler:
        def __init__(self):
            self.compact_calls = 0
            self._last_llm_error = None

        async def should_compact(self, session_id, channel_id, config, *, threshold):
            return True

        async def compact(self, session_id, channel_id, *, config, silent, enabled):
            self.compact_calls += 1
            self._last_llm_error = LLMError("Insufficient Balance", "bad_request")
            return None

    config = types.SimpleNamespace(
        context=types.SimpleNamespace(
            idle_compaction=True,
            precompact_threshold=0.5,
            silent=True,
        )
    )
    compact_handler = FakeCompactHandler()
    loop = object.__new__(AgentLoop)
    loop._injected_config = config
    loop.compact_handler = compact_handler
    loop._compact_tasks = {}
    loop._compact_retry_after = {}

    await loop._schedule_idle_compact("ws::s1", "ws")
    task = loop._compact_tasks["ws::s1"]
    await task

    await loop._schedule_idle_compact("ws::s1", "ws")

    assert compact_handler.compact_calls == 1
    assert loop._compact_retry_after["ws::s1"] > 0
