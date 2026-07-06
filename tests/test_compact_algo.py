"""
compact_manager 模块级算法工具的单测。

只测纯函数，不依赖 db / channel / bus。
"""
from __future__ import annotations

import pytest

from ftre.utils.image_store import save_image

from ftre.agent.compact_manager import (
    get_cursor_index,
    get_pending_compact_index,
    get_previous_summary,
    _serialize_events,
    _build_prompt,
    _compact_enabled,
    SUMMARY_TEMPLATE,
)


def _make_test_image() -> str:
    """创建一个真实的 temp 图片文件，返回路径。"""
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    return save_image(raw, "image/png", "compact_test.png")


# ─── get_cursor_index / get_previous_summary ──────────────────────────


def test_cursor_no_compact():
    events = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "hi"}},
        {"type": "assistant_message_complete", "data": {"content": [{"type": "text", "text": "yes"}], "metadata": {}}},
    ]
    assert get_cursor_index(events) == 0
    assert get_previous_summary(events) is None


def test_cursor_with_one_compact():
    events = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "a"}},
        {"type": "context_compact", "data": {"summary": "## summary v1"}},
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "b"}},
    ]
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
        {"type": "assistant_message_complete", "data": {"content": [{"type": "text", "text": "好的"}], "metadata": {}}},
    ]
    out = _serialize_events(chunk)
    assert "请按文档第 3 节实现压缩算法，注意游标只进不退" in out
    assert "[User]:" in out
    assert "[Assistant]:" in out


def test_serialize_truncates_long_tool_result():
    big = "x" * 3000
    chunk = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "go"}},
        {"type": "tool_result", "data": {"id": "c1", "result": big}},
    ]
    out = _serialize_events(chunk)
    assert "x" * 1500 in out
    assert "[truncated]" in out
    assert "x" * 2500 not in out


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
        {"type": "assistant_message_complete", "data": {
            "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}}],
            "metadata": {},
        }},
        {"type": "tool_result", "data": {"id": "c1", "result": "file1\nfile2"}},
    ]
    out = _serialize_events(chunk)
    assert "[Assistant tool call]: bash(" in out
    assert "[Tool result]:" in out


def test_serialize_reasoning():
    chunk = [
        {"type": "assistant_message_complete", "data": {
            "content": [{"type": "thinking", "thinking": "我在想这个问题..."}],
            "metadata": {},
        }},
    ]
    out = _serialize_events(chunk)
    assert "[Assistant reasoning]: 我在想这个问题..." in out


# ─── _build_prompt ────────────────────────────────────────────


def test_build_prompt_first_time():
    prompt = _build_prompt(context=["[User]: hello\n\n[Assistant]: hi"])
    joined = "\n".join(prompt) if isinstance(prompt, list) else prompt
    assert "创建一份新的锚定摘要" in joined
    assert SUMMARY_TEMPLATE in joined
    assert "[User]: hello" in joined


def test_build_prompt_incremental():
    prompt = _build_prompt(
        previous_summary="## 目标\n- 做某事",
        context=["[User]: hello\n\n[Assistant]: hi"],
    )
    joined = "\n".join(prompt) if isinstance(prompt, list) else prompt
    assert "更新锚定摘要" in joined
    assert "<previous-summary>" in joined
    assert "## 目标\n- 做某事" in joined
    assert "</previous-summary>" in joined
    assert SUMMARY_TEMPLATE in joined


# ─── L1 prune 修剪测试 ────────────────────────────────────────────


def _events_with_long_tool(*, big_chars: int = 5000):
    """构造：3 轮，每轮含一个 assistant(toolCall) + tool_result（result 很长）。"""
    big = "x" * big_chars
    out = []
    for i in range(3):
        out.append({"type": "user_message", "data": {"metadata": {"hide": False}, "content": f"q{i}"}})
        out.append({
            "type": "assistant_message_complete",
            "data": {
                "content": [{"type": "toolCall", "id": f"c{i}", "name": "bash", "arguments": {}}],
                "metadata": {"kind": "block"},
            },
        })
        out.append({
            "type": "tool_result",
            "data": {"id": f"c{i}", "result": big, "error": None},
        })
        out.append({"type": "assistant_message_complete", "data": {
            "content": [{"type": "text", "text": "done"}],
            "metadata": {"kind": "final"},
        }})
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
    """error 非空的 tool_result 不截断。"""
    from ftre.session.manager import SessionManager
    big = "x" * 5000
    events = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "go"}},
        {"type": "assistant_message_complete", "data": {
            "content": [{"type": "toolCall", "id": "c0", "name": "bash", "arguments": {}}],
            "metadata": {"kind": "block"},
        }},
        {"type": "tool_result", "data": {"id": "c0", "result": big, "error": "boom"}},
    ]
    msgs = SessionManager.to_openai_messages(
        events,
        prune={"protect_turns": 2, "max_chars": 2000, "head_chars": 1000, "tail_chars": 1000},
    )
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs[0]["content"]) == 5000
    assert "[L1 修剪" not in tool_msgs[0]["content"]


def test_prune_not_applied_without_prune_param():
    """不传 prune 时 tool_result 原样保留。"""
    from ftre.session.manager import SessionManager
    big = "x" * 5000
    events = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "go"}},
        {"type": "assistant_message_complete", "data": {
            "content": [{"type": "toolCall", "id": "c0", "name": "bash", "arguments": {}}],
            "metadata": {"kind": "block"},
        }},
        {"type": "tool_result", "data": {"id": "c0", "result": big, "error": None}},
    ]
    msgs = SessionManager.to_openai_messages(events)
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs[0]["content"]) == 5000


# ─── to_openai_messages 基础测试 ────────────────────────────────────


def test_to_openai_messages_simple_conversation():
    from ftre.session.manager import SessionManager
    events = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "hello"}},
        {"type": "assistant_message_complete", "data": {
            "content": [{"type": "text", "text": "Hi there!"}],
            "metadata": {"kind": "final"},
        }},
    ]
    msgs = SessionManager.to_openai_messages(events)
    assert len(msgs) == 2
    assert msgs[0] == {"role": "user", "content": "hello"}
    assert msgs[1]["role"] == "assistant"
    # format_assistant_message uses content_parts() which wraps text in list[dict]
    assert msgs[1]["content"] == [{"type": "text", "text": "Hi there!"}]


def test_to_openai_messages_tool_call_round():
    from ftre.session.manager import SessionManager
    events = [
        {"type": "user_message", "data": {"metadata": {"hide": False}, "content": "read file"}},
        {"type": "assistant_message_complete", "data": {
            "content": [
                {"type": "text", "text": "I'll read it"},
                {"type": "toolCall", "id": "c1", "name": "read", "arguments": {"path": "a.py"}},
            ],
            "metadata": {"kind": "block", "stopReason": "toolUse"},
        }},
        {"type": "tool_result", "data": {"id": "c1", "result": "file A content", "error": None}},
        {"type": "assistant_message_complete", "data": {
            "content": [{"type": "text", "text": "The file says A"}],
            "metadata": {"kind": "final"},
        }},
    ]
    msgs = SessionManager.to_openai_messages(events)
    assert len(msgs) == 4
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == [{"type": "text", "text": "I'll read it"}]
    assert msgs[1]["tool_calls"][0]["function"]["name"] == "read"
    assert msgs[2]["role"] == "tool"
    assert msgs[2]["tool_call_id"] == "c1"
    assert msgs[2]["content"] == "file A content"
    assert msgs[3]["role"] == "assistant"
    assert msgs[3]["content"] == [{"type": "text", "text": "The file says A"}]


def test_to_openai_messages_with_reasoning():
    from ftre.session.manager import SessionManager
    events = [
        {"type": "assistant_message_complete", "data": {
            "content": [
                {"type": "thinking", "thinking": "Let me analyze..."},
                {"type": "text", "text": "Here's my answer"},
            ],
            "metadata": {"kind": "final"},
        }},
    ]
    msgs = SessionManager.to_openai_messages(events)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["content"] == [{"type": "text", "text": "Here's my answer"}]
    assert msgs[0]["reasoning_content"] == "Let me analyze..."


def test_to_openai_messages_external_message():
    from ftre.session.manager import SessionManager
    events = [
        {"type": "external_message", "data": {
            "content": "Hello from another agent",
            "from_channel": "ws",
            "from_session": "sess_abc",
        }},
    ]
    msgs = SessionManager.to_openai_messages(events)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"
    assert "Hello from another agent" in msgs[0]["content"]
    assert "ws::sess_abc" in msgs[0]["content"]


def test_to_openai_messages_downgrades_images_without_vision():
    from ftre.session.manager import SessionManager

    events = [{
        "type": "user_message",
        "data": {"metadata": {"hide": False},
            "content": "看图",
            "attachments": [{
                "type": "image",
                "mime_type": "image/png",
                "path": _make_test_image(),
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
                "path": _make_test_image(),
            }],
        },
    }]

    msgs = SessionManager.to_openai_messages(
        events,
        config={"llm": {"vision": True}},
    )

    assert msgs[0]["content"][0] == {"type": "text", "text": "看图"}
    assert msgs[0]["content"][1]["type"] == "image_url"
    assert msgs[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


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
        {"type": "assistant_message_complete", "data": {"content": [{"type": "text", "text": "old answer"}], "metadata": {}}},
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
        {"type": "assistant_message_complete", "data": {"content": [{"type": "text", "text": "old answer"}], "metadata": {}}},
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
    import ftre.agent.compact_manager as compact_module

    class FakeLLMHandler:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def stream(self, messages):
            assert messages
            yield TextDelta(text="## 目标\n")
            yield TextDelta(text=("- 做某事" * 60))
            yield StepFinish(finish_reason="stop")

    handler = object.__new__(compact_module.CompactManager)
    handler.session_manager = None
    handler.channel_manager = None
    handler.bus = None
    handler._threshold = 0.6
    handler._last_llm_errors = {}
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
    import ftre.agent.compact_manager as compact_module

    class FakeLLMHandler:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def stream(self, messages):
            raise LLMError("Insufficient Balance", "bad_request")
            yield

    handler = object.__new__(compact_module.CompactManager)
    handler.session_manager = None
    handler.channel_manager = None
    handler.bus = None
    handler._threshold = 0.6
    handler._last_llm_errors = {}
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

    summary = await handler._run_compact_llm(events, config=config, session_id="test")

    assert summary is None
    assert handler._last_llm_errors.get("test") is not None
    assert handler._last_llm_errors["test"].code == "bad_request"


@pytest.mark.asyncio
async def test_idle_compact_unretryable_llm_error_enters_cooldown():
    import types

    from ftre.agent.compact_manager import CompactManager
    from ftre_agent_core.llm import LLMError

    handler = object.__new__(CompactManager)
    handler._compact_tasks = {}
    handler._compact_retry_after = {}
    handler._last_llm_errors = {}

    compact_calls = 0

    async def fake_should_compact(session_id, channel_id, config, *, threshold):
        return True

    async def fake_compact(session_id, channel_id, *, config, silent, enabled):
        nonlocal compact_calls
        compact_calls += 1
        handler._last_llm_errors[session_id] = LLMError("Insufficient Balance", "bad_request")
        return None

    handler.should_compact = fake_should_compact
    handler.compact = fake_compact

    config = types.SimpleNamespace(
        context=types.SimpleNamespace(
            idle_compaction=True,
            precompact_threshold=0.5,
            silent=True,
        )
    )

    await handler.maybe_schedule_idle_compact("ws::s1", "ws", config)
    task = handler._compact_tasks["ws::s1"]
    await task

    await handler.maybe_schedule_idle_compact("ws::s1", "ws", config)

    assert compact_calls == 1
    assert handler._compact_retry_after["ws::s1"] > 0
