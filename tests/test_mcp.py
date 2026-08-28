"""
MCP 模块单元测试

覆盖：
- 配置解析（config.py）
- 工具名映射（adapter.py）
- 参数转换（adapter.py）
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from ftre_agent.tool import ToolDefinition

from ftre.plugins.builtin.mcp.adapter import (
    _convert_parameters,
    _parse_tool_name,
    mcp_tool_id,
)
from ftre.plugins.builtin.mcp.config import parse_mcp_config
from ftre.plugins.builtin.mcp.connection import McpManager
from ftre.plugins.builtin.mcp.service import McpService
from ftre.services.tools import ToolService

# ============================================================
# 配置解析
# ============================================================

class TestParseMcpConfig:

    def test_empty_input(self):
        assert parse_mcp_config({}) == []
        assert parse_mcp_config(None) == []

    def test_local_server_minimal(self):
        raw = {
            "filesystem": {
                "type": "local",
                "command": ["npx", "-y", "@mcp/server-fs", "/tmp"],
            }
        }
        result = parse_mcp_config(raw)
        assert len(result) == 1
        assert result[0].name == "filesystem"
        assert result[0].type == "local"
        assert result[0].command == ["npx", "-y", "@mcp/server-fs", "/tmp"]
        assert result[0].disabled is False
        assert result[0].timeout == 30_000

    def test_local_server_with_env_and_timeout(self):
        raw = {
            "my-server": {
                "type": "local",
                "command": ["python", "server.py"],
                "environment": {"API_KEY": "xxx"},
                "timeout": 60000,
            }
        }
        result = parse_mcp_config(raw)
        assert len(result) == 1
        assert result[0].environment == {"API_KEY": "xxx"}
        assert result[0].timeout == 60000

    def test_local_command_inferring_type(self):
        """有 command 但没 type 时按 local 处理"""
        raw = {
            "implicit-local": {
                "command": ["npx", "-y", "some-server"],
            }
        }
        result = parse_mcp_config(raw)
        assert len(result) == 1
        assert result[0].type == "local"

    def test_disabled_server_skipped(self):
        raw = {
            "disabled1": {"type": "local", "command": ["a"], "disabled": True},
            "disabled2": {"type": "local", "command": ["b"], "enabled": False},
            "enabled": {"type": "local", "command": ["c"]},
        }
        result = parse_mcp_config(raw)
        assert len(result) == 1
        assert result[0].name == "enabled"

    def test_remote_server(self):
        raw = {
            "remote-api": {
                "type": "remote",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer xxx"},
            }
        }
        result = parse_mcp_config(raw)
        assert len(result) == 1
        assert result[0].type == "remote"
        assert result[0].url == "https://example.com/mcp"
        assert result[0].headers == {"Authorization": "Bearer xxx"}

    def test_remote_missing_url_skipped(self):
        raw = {"bad": {"type": "remote"}}
        result = parse_mcp_config(raw)
        assert result == []

    def test_local_missing_command_skipped(self):
        raw = {"bad": {"type": "local"}}
        result = parse_mcp_config(raw)
        assert result == []

    def test_unknown_type_no_command_skipped(self):
        raw = {"bad": {"type": "websocket", "url": "ws://..."}}
        result = parse_mcp_config(raw)
        assert result == []

    def test_invalid_entry_skipped(self):
        raw = {
            "not-dict": "just a string",
            "valid": {"type": "local", "command": ["a"]},
        }
        result = parse_mcp_config(raw)
        assert len(result) == 1
        assert result[0].name == "valid"


# ============================================================
# 工具名映射
# ============================================================

class TestToolNameMapping:

    def test_mcp_tool_id(self):
        assert mcp_tool_id("filesystem", "read_file") == "mcp__filesystem__read_file"

    def test_parse_tool_name(self):
        result = _parse_tool_name("mcp__filesystem__read_file")
        assert result == ("filesystem", "read_file")

    def test_parse_tool_name_not_mcp(self):
        assert _parse_tool_name("bash") is None

    def test_parse_tool_name_malformed(self):
        assert _parse_tool_name("mcp__onlyonepart") is None

    def test_roundtrip(self):
        server, tool = "my-server", "search"
        tool_id = mcp_tool_id(server, tool)
        parsed = _parse_tool_name(tool_id)
        assert parsed == (server, tool)


# ============================================================
# 参数转换
# ============================================================

class TestConvertParameters:

    def test_basic_types(self):
        """模拟 MCP tool 的 inputSchema"""
        from mcp import Tool as McpToolDef

        mcp_tool = McpToolDef(
            name="test_tool",
            description="A test tool",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "count": {"type": "integer", "description": "Number"},
                    "verbose": {"type": "boolean", "description": "Verbose mode"},
                    "ratio": {"type": "number", "description": "A ratio"},
                },
                "required": ["path", "count"],
            },
        )
        params = _convert_parameters(mcp_tool)
        assert len(params) == 4

        # string → string
        assert params[0].name == "path"
        assert params[0].type == "string"
        assert params[0].required is True

        # integer → number
        assert params[1].name == "count"
        assert params[1].type == "number"
        assert params[1].required is True

        # boolean → boolean
        assert params[2].name == "verbose"
        assert params[2].type == "boolean"
        assert params[2].required is False

        # number → number
        assert params[3].name == "ratio"
        assert params[3].type == "number"

    def test_array_and_object_types(self):
        """array / object 类型映射到 string + JSON 提示"""
        from mcp import Tool as McpToolDef

        mcp_tool = McpToolDef(
            name="test_tool",
            description="test",
            inputSchema={
                "type": "object",
                "properties": {
                    "items": {"type": "array"},
                    "config": {"type": "object", "description": "Config dict"},
                },
                "required": [],
            },
        )
        params = _convert_parameters(mcp_tool)
        assert len(params) == 2

        # array → string（JSON 提示）
        assert params[0].type == "string"
        assert "JSON" in params[0].description

        # object → string（JSON 提示）
        assert params[1].type == "string"
        assert "JSON" in params[1].description

    def test_enum_passthrough(self):
        """enum 属性透传"""
        from mcp import Tool as McpToolDef

        mcp_tool = McpToolDef(
            name="test_tool",
            description="test",
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["read", "write"]},
                },
                "required": [],
            },
        )
        params = _convert_parameters(mcp_tool)
        assert params[0].enum == ["read", "write"]

    def test_empty_schema(self):
        """无 inputSchema 的工具"""
        from mcp import Tool as McpToolDef

        mcp_tool = McpToolDef(
            name="no_params",
            description="No params",
            inputSchema={},
        )
        params = _convert_parameters(mcp_tool)
        assert params == []


@pytest.mark.asyncio
async def test_private_agent_mcp_is_loaded_into_agent_scope(monkeypatch):
    """Merged profile config must activate private MCP and expose scoped tools."""
    global_tool = ToolDefinition(name="mcp__global__search", description="search", func=lambda: "ok")

    class _GlobalManager:
        attachment_service = None
        registered_tool_names = frozenset({global_tool.name})

        async def start_and_register(self, _raw):
            return None

        async def reload_and_register(self, _raw, source="unknown"):
            del source

        async def stop(self):
            return None

        async def list_tools_for_servers(self, _names):
            return []

    global_manager = _GlobalManager()
    tools = ToolService()
    tools.register(global_tool, owner="mcp", source="mcp")
    service = McpService(global_manager, tool_service=tools)
    await service.start_and_register({
        "global": {"type": "local", "command": ["global-server"]},
    })

    private_calls = []

    async def fake_private_start(manager, raw):
        private_calls.append(raw)
        manager._connections["private"] = SimpleNamespace(
            is_connected=True,
            disconnect=lambda: _done(),
        )

    async def _done():
        return None

    async def no_global_tools(*_args):
        return []

    monkeypatch.setattr("ftre.plugins.builtin.mcp.service.build_mcp_tools_for_servers", no_global_tools)
    monkeypatch.setattr("ftre.plugins.builtin.mcp.connection.McpManager.start_and_register", fake_private_start)

    await service.prepare_agent("worker", {
        "global": {"type": "local", "command": ["global-server"]},
        "private": {"type": "local", "command": ["private-server"]},
    })

    assert private_calls == [
        {"private": {"type": "local", "command": ["private-server"]}}
    ]
    assert tools.snapshot("worker")
    await service.prepare_agent("worker-2", {
        "global": {"type": "local", "command": ["global-server"]},
        "private": {"type": "local", "command": ["private-server"]},
    })
    assert len(private_calls) == 1
    await service.stop()


@pytest.mark.asyncio
async def test_agent_mcp_scope_can_hide_disabled_global_server():
    global_tool = ToolDefinition(name="mcp__global__search", description="search", func=lambda: "ok")

    class _GlobalManager:
        attachment_service = None
        registered_tool_names = frozenset({global_tool.name})

        async def start_and_register(self, _raw):
            return None

        async def stop(self):
            return None

        async def list_tools_for_servers(self, _names):
            return []

    tools = ToolService()
    tools.register(global_tool, owner="mcp", source="mcp")
    service = McpService(_GlobalManager(), tool_service=tools)
    await service.start_and_register({
        "global": {"type": "local", "command": ["global-server"]},
    })
    await service.prepare_agent("worker", {
        "global": {"type": "local", "command": ["global-server"], "disabled": True},
    })

    assert global_tool.name not in {item.name for item in tools.snapshot("worker")}
    await service.stop()


@pytest.mark.asyncio
async def test_mcp_manager_registers_and_disposes_scoped_tools(monkeypatch):
    tool = ToolDefinition(name="mcp__private__search", description="search", func=lambda: "ok")
    tools = ToolService()
    manager = McpManager(
        tool_service=tools,
        tool_scope="agent:worker",
        tool_owner="mcp:worker",
    )

    async def fake_build(_manager):
        return [tool]

    monkeypatch.setattr("ftre.plugins.builtin.mcp.adapter.build_mcp_tools", fake_build)
    await manager._register_tools()
    assert [item.name for item in tools.snapshot("worker")] == [tool.name]

    await manager.stop()
    assert tools.snapshot("worker") == ()
