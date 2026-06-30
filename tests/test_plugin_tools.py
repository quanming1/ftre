from types import SimpleNamespace

import pytest

from ftre_agent_core.agent.event import UserMessageEvent
from ftre_agent_core.tool import Tool

from ftre.agent.loop import AgentLoop
from ftre.config import AgentConfig, LLMConfig
from ftre.plugin import BEFORE_AGENT_RUN, HookManager, Plugin, PluginManager
from ftre.tools import ToolRegistry, build_default_tools
from ftre.tools._workspace import WorkspaceAccessor
from ftre.tools.read import create_read_tool


def _dummy_tool(name: str = "dummy") -> Tool:
    def dummy() -> str:
        return "ok"

    return Tool(
        name=name,
        description="dummy tool",
        parameters=[],
        func=dummy,
    )


def test_tool_registry_rejects_duplicate_names():
    registry = ToolRegistry()
    registry.register(_dummy_tool("dup"))

    with pytest.raises(ValueError, match="tool already registered"):
        registry.register(_dummy_tool("dup"))


def test_build_default_tools_includes_registry_tools():
    registry = ToolRegistry()
    registry.register(_dummy_tool("extra"))

    names = [tool.name for tool in build_default_tools(tool_registry=registry)]

    assert "extra" in names


def test_build_default_tools_omits_see_img_without_vision():
    names = [
        tool.name
        for tool in build_default_tools(llm_config=SimpleNamespace(vision=False))
    ]

    assert "see_img" not in names


def test_build_default_tools_omits_see_img_with_vision():
    names = [
        tool.name
        for tool in build_default_tools(llm_config=SimpleNamespace(vision=True))
    ]

    assert "see_img" not in names


def test_read_tool_reads_relative_image_path(tmp_path):
    import os

    class FakeWorkspace(WorkspaceAccessor):
        def __init__(self, cwd: str):
            self.cwd = cwd

        def get(self) -> str:
            return self.cwd

    image = tmp_path / "screen.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00"
        b"\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    result = create_read_tool().func(
        "screen.png",
        ws=FakeWorkspace(str(tmp_path)),
        llm_config=SimpleNamespace(vision=True),
    )

    assert isinstance(result, UserMessageEvent)
    assert result.metadata["hide"] is True
    assert result.metadata["path"] == str(image.resolve())
    assert result.content[0]["type"] == "image_file"
    assert "path" in result.content[0]
    assert os.path.exists(result.content[0]["path"])
    assert result.content[0]["mime_type"] == "image/png"


def test_read_tool_rejects_image_without_vision(tmp_path):
    class FakeWorkspace(WorkspaceAccessor):
        def __init__(self, cwd: str):
            self.cwd = cwd

        def get(self) -> str:
            return self.cwd

    image = tmp_path / "screen.png"
    image.write_bytes(b"not actually decoded because vision is disabled")

    result = create_read_tool().func(
        "screen.png",
        ws=FakeWorkspace(str(tmp_path)),
        llm_config=SimpleNamespace(vision=False),
    )

    assert "当前模型不支持视觉输入" in result


def test_plugin_manager_rolls_back_tools_when_setup_fails():
    class FailingToolPlugin(Plugin):
        name = "failing_tool"

        def setup(self) -> None:
            self.api.tool_registry.register(_dummy_tool("leaked"))
            raise RuntimeError("boom")

    registry = ToolRegistry()
    manager = PluginManager(
        bus=None,
        channel_manager=None,
        session_manager=None,
        hook_manager=HookManager(),
        tool_registry=registry,
    )

    manager._load(FailingToolPlugin(), {})

    assert "leaked" not in [tool.name for tool in registry.snapshot()]


def test_plugin_api_tool_registry_adds_to_shared_registry():
    class ToolPlugin(Plugin):
        name = "tool_plugin"

        def setup(self) -> None:
            self.api.tool_registry.register(_dummy_tool("from_plugin"))

    registry = ToolRegistry()
    manager = PluginManager(
        bus=None,
        channel_manager=None,
        session_manager=None,
        hook_manager=HookManager(),
        tool_registry=registry,
    )

    manager._load(ToolPlugin(), {})

    assert [tool.name for tool in registry.snapshot()] == ["from_plugin"]


def test_plugin_api_register_router():
    from fastapi import APIRouter

    class RouterPlugin(Plugin):
        name = "router_plugin"

        def setup(self) -> None:
            router = APIRouter()

            @router.get("/ping")
            def ping():
                return {"pong": True}

            self.api.register_router(router)

    registry = ToolRegistry()
    manager = PluginManager(
        bus=None,
        channel_manager=None,
        session_manager=None,
        hook_manager=HookManager(),
        tool_registry=registry,
    )

    manager._load(RouterPlugin(), {})

    assert len(manager.routers) == 1
    routes = [r.path for r in manager.routers[0].routes]
    assert "/ping" in routes


def test_before_agent_run_hook_can_insert_messages():
    """before_agent_run hook 可以自由插入任意 role 的消息。"""
    hooks = HookManager()

    def rewrite(ctx):
        # 模拟 OpenClaw 双轨注入
        ctx.messages.insert(0, {"role": "system", "content": "persona: act as Alice"})
        ctx.messages.insert(1, {"role": "user", "content": "[GROUP CONTEXT]\n群规: 禁止骂人\n[/GROUP CONTEXT]"})
        ctx.messages.insert(2, {"role": "user", "content": "成员列表:\n- Alice\n- Bob"})
        return ctx

    hooks.register(BEFORE_AGENT_RUN, rewrite)

    from ftre.plugin import AgentRunContext
    ctx = AgentRunContext(
        session_id="sess_1",
        channel_id="ws",
        messages=[
            {"role": "system", "content": "base system prompt"},
            {"role": "user", "content": "你好"},
        ],
        config=AgentConfig(),
    )
    ctx = hooks.trigger_sync(BEFORE_AGENT_RUN, ctx)

    assert ctx.messages[0]["role"] == "system"
    assert ctx.messages[0]["content"] == "persona: act as Alice"
    assert ctx.messages[1]["role"] == "user"
    assert "群规" in ctx.messages[1]["content"]
    assert ctx.messages[2]["role"] == "user"
    assert "Alice" in ctx.messages[2]["content"]
    # 原始消息还在
    assert ctx.messages[3]["role"] == "system"
    assert "base system prompt" in ctx.messages[3]["content"]
    assert ctx.messages[4]["role"] == "user"
    assert ctx.messages[4]["content"] == "你好"


def test_builtin_prompt_injection_uses_before_agent_run(tmp_path):
    from ftre.plugin.builtin.mcp_plugin import McpPlugin
    from ftre.plugin.builtin.skill_plugin import SkillPlugin

    hooks = HookManager()
    registry = ToolRegistry()
    manager = PluginManager(
        bus=None,
        channel_manager=None,
        session_manager=None,
        hook_manager=hooks,
        tool_registry=registry,
    )
    manager._load(McpPlugin(), {})
    manager._load(SkillPlugin(), {"skills_dir": str(tmp_path)})

    from ftre.plugin import AgentRunContext
    ctx = AgentRunContext(
        session_id="sess_1",
        channel_id="ws",
        messages=[
            {"role": "system", "content": "base system prompt"},
            {"role": "user", "content": "你好"},
        ],
        config=AgentConfig(),
    )
    ctx = hooks.trigger_sync(BEFORE_AGENT_RUN, ctx)

    # mcp 和 skill 插件注入的内容应在第一条 system 消息中
    system_msg = ctx.messages[0]
    assert system_msg["role"] == "system"
    assert "## MCP 工具" in system_msg["content"]
    assert "Skill 是 ~/.ftre/skills 下的本地能力说明" in system_msg["content"]
    assert "base system prompt" in system_msg["content"]


