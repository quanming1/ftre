"""
MCP 模块单元测试

覆盖：
- 配置解析（config.py）
- 工具名映射（adapter.py）
- 参数转换（adapter.py）
"""
import pytest
from ftre.mcp.config import parse_mcp_config, McpServerConfig
from ftre.mcp.adapter import mcp_tool_id, _parse_tool_name, _convert_parameters, MCP_TOOL_PREFIX


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
