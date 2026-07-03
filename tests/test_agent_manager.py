"""
Tests for AgentManager: config merging, tool filtering, prompt loading.
"""
import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def tmp_agents_dir(tmp_path):
    """Create a temporary ~/.ftre/agents/ directory with a default agent."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    # Create default agent with llm config
    default_dir = agents_dir / "default"
    default_dir.mkdir()
    (default_dir / "agent.config.json").write_text(json.dumps({
        "id": "default",
        "name": "Ftre",
        "llm": {"provider": "openai", "model": "gpt-4o"},
        "workspace": "/tmp",
    }), encoding="utf-8")
    return agents_dir


@pytest.fixture
def fake_global_config():
    """A minimal global config dict simulating config.json contents."""
    return {
        "providers": {
            "openai": {
                "api_key": "sk-global",
                "api_base": "https://api.openai.com/v1",
                "api_protocol": "openai",
                "models": [
                    {"id": "gpt-4o", "name": "GPT-4o", "context_window": 128000, "max_output": 16384, "vision": True},
                    {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "context_window": 128000, "max_output": 16384, "vision": False},
                ],
            },
            "anthropic": {
                "api_key": "sk-ant-global",
                "api_base": "https://api.anthropic.com",
                "api_protocol": "anthropic",
                "models": [
                    {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet", "context_window": 200000, "max_output": 16384, "vision": True},
                ],
            },
        },
        "agents": {},
        "mcp": {
            "playwright": {
                "type": "local",
                "command": ["npx", "@playwright/mcp@latest"],
                "disabled": False,
                "timeout": 60000,
            },
        },
        "plugins": [
            {"name": "octo_channel", "enabled": True, "config": {"bot_token": "xxx"}},
        ],
        "disabled_skills": ["mcp-guide"],
    }


def test_load_default_agent_uses_global_config(tmp_agents_dir, fake_global_config):
    """Loading 'default' with empty agent.config.json inherits everything from global."""
    from ftre.agent.agent_manager import AgentManager

    mgr = AgentManager(agents_dir=tmp_agents_dir, global_config_data=fake_global_config)
    profile = mgr.load("default")

    assert profile.agent_id == "default"
    assert profile.llm.model == "gpt-4o"
    assert profile.llm.api_key == "sk-global"
    assert profile.workspace == "/tmp"
    assert profile.tools_config is None  # no tools key → all available
    assert "playwright" in profile.mcp_config
    assert len(profile.plugins_config) == 1
    assert profile.plugins_config[0]["name"] == "octo_channel"
    assert "mcp-guide" in profile.disabled_skills
    assert profile.soul_prompt == ""  # no SOUL.md
    assert profile.user_prompt_md == ""
    assert profile.agents_md == ""


# ─── Task 2: Tool filtering and per-agent overrides ──────────────────

def test_tool_filter_allow_deny():
    """filter_tools respects allow and deny lists."""
    from ftre.tools import filter_tools
    from ftre_agent_core.tool import Tool

    def _noop(**kw):
        return ""

    tools = [
        Tool(name="bash", description="", parameters=[], func=_noop),
        Tool(name="read", description="", parameters=[], func=_noop),
        Tool(name="write", description="", parameters=[], func=_noop),
        Tool(name="cron", description="", parameters=[], func=_noop),
        Tool(name="mcp__playwright__browser_navigate", description="", parameters=[], func=_noop),
    ]

    # No config → all tools
    assert len(filter_tools(tools, None)) == 5

    # Allow only bash and read
    result = filter_tools(tools, {"allow": ["bash", "read"]})
    assert [t.name for t in result] == ["bash", "read"]

    # Deny cron
    result = filter_tools(tools, {"deny": ["cron"]})
    assert "cron" not in [t.name for t in result]
    assert len(result) == 4

    # Allow + Deny combined
    result = filter_tools(tools, {"allow": ["bash", "read", "cron"], "deny": ["cron"]})
    assert [t.name for t in result] == ["bash", "read"]

    # Empty allow = no whitelist restriction, only deny applies
    result = filter_tools(tools, {"allow": [], "deny": ["write"]})
    assert "write" not in [t.name for t in result]
    assert len(result) == 4


def test_load_agent_with_tool_overrides(tmp_agents_dir, fake_global_config):
    """Agent with tools config gets tools_config set on profile."""
    from ftre.agent.agent_manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(json.dumps({
        "llm": {"provider": "openai", "model": "gpt-4o-mini"},
        "tools": {
            "allow": ["bash", "read", "write", "edit"],
            "deny": ["cron", "task", "send_message"],
        },
        "workspace": "/custom/workspace",
    }), encoding="utf-8")

    mgr = AgentManager(agents_dir=tmp_agents_dir, global_config_data=fake_global_config)
    profile = mgr.load("coder")

    assert profile.llm.model == "gpt-4o-mini"
    assert profile.llm.api_key == "sk-global"
    assert profile.workspace == "/custom/workspace"
    assert profile.tools_config == {"allow": ["bash", "read", "write", "edit"], "deny": ["cron", "task", "send_message"]}


def test_load_agent_with_mcp_merge(tmp_agents_dir, fake_global_config):
    """Agent MCP config deep-merges with global."""
    from ftre.agent.agent_manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(json.dumps({
        "mcp": {
            "playwright": {"disabled": True},
            "extra-server": {"type": "local", "command": ["node", "server.js"]},
        },
    }), encoding="utf-8")

    mgr = AgentManager(agents_dir=tmp_agents_dir, global_config_data=fake_global_config)
    profile = mgr.load("coder")

    assert "playwright" in profile.mcp_config
    assert profile.mcp_config["playwright"]["disabled"] is True
    assert "extra-server" in profile.mcp_config
    assert profile.mcp_config["extra-server"]["command"] == ["node", "server.js"]


def test_load_agent_with_plugins_merge(tmp_agents_dir, fake_global_config):
    """Agent plugins merge by name with global."""
    from ftre.agent.agent_manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(json.dumps({
        "plugins": [
            {"name": "octo_channel", "enabled": False},
            {"name": "my-plugin", "module": "my_plugin.MyPlugin", "config": {}},
        ],
    }), encoding="utf-8")

    mgr = AgentManager(agents_dir=tmp_agents_dir, global_config_data=fake_global_config)
    profile = mgr.load("coder")

    plugin_names = [p["name"] for p in profile.plugins_config]
    assert "octo_channel" in plugin_names
    assert "my-plugin" in plugin_names
    octo = [p for p in profile.plugins_config if p["name"] == "octo_channel"][0]
    assert octo["enabled"] is False


def test_load_agent_with_disabled_skills_override(tmp_agents_dir, fake_global_config):
    """Agent disabled_skills replaces global."""
    from ftre.agent.agent_manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(json.dumps({
        "disabled_skills": ["playwright-mcp", "brainstorming"],
    }), encoding="utf-8")

    mgr = AgentManager(agents_dir=tmp_agents_dir, global_config_data=fake_global_config)
    profile = mgr.load("coder")

    assert profile.disabled_skills == ["playwright-mcp", "brainstorming"]
    assert "mcp-guide" not in profile.disabled_skills


def test_load_nonexistent_agent_falls_back_to_default(tmp_agents_dir, fake_global_config):
    """Loading a non-existent agent_id falls back to 'default'."""
    from ftre.agent.agent_manager import AgentManager

    mgr = AgentManager(agents_dir=tmp_agents_dir, global_config_data=fake_global_config)
    profile = mgr.load("nonexistent")

    assert profile.agent_id == "default"


def test_load_agent_reads_md_files(tmp_agents_dir, fake_global_config):
    """Agent loads SOUL.md, AGENTS.md, USER.md from its directory."""
    from ftre.agent.agent_manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text("{}", encoding="utf-8")
    (coder_dir / "SOUL.md").write_text("You are a coding expert.", encoding="utf-8")
    (coder_dir / "AGENTS.md").write_text("# Coding Rules\n\nAlways test.", encoding="utf-8")
    (coder_dir / "USER.md").write_text("Call me boss.", encoding="utf-8")

    mgr = AgentManager(agents_dir=tmp_agents_dir, global_config_data=fake_global_config)
    profile = mgr.load("coder")

    assert profile.soul_prompt == "You are a coding expert."
    assert "Coding Rules" in profile.agents_md
    assert profile.user_prompt_md == "Call me boss."


def test_list_agents(tmp_agents_dir, fake_global_config):
    """list_agents returns all agent directories with metadata."""
    from ftre.agent.agent_manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(json.dumps({
        "llm": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
    }), encoding="utf-8")
    (coder_dir / "SOUL.md").write_text("expert", encoding="utf-8")

    mgr = AgentManager(agents_dir=tmp_agents_dir, global_config_data=fake_global_config)
    agents = mgr.list_agents()

    assert len(agents) == 2
    ids = [a["id"] for a in agents]
    assert "default" in ids
    assert "coder" in ids

    coder = [a for a in agents if a["id"] == "coder"][0]
    assert coder["model"] == "claude-sonnet-4-20250514"
    assert coder["provider"] == "anthropic"
    assert coder["has_soul"] is True


# ─── Task 4: context_govern AGENTS.md per-agent injection ────────────

def test_context_govern_injects_agent_dir_agents_md(tmp_path):
    """context_govern reads AGENTS.md from agent_dir, not just workspace."""
    from ftre.plugin.builtin.context_govern import ContextGovernPlugin
    from ftre.plugin.hook_manager import MessagesBuildContext
    from ftre.config import AgentConfig

    agent_dir = tmp_path / "agents" / "coder"
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENTS.md").write_text("# Agent Rules\n\nUse Python 3.12.", encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("# Workspace Rules\n\nUse TypeScript.", encoding="utf-8")

    plugin = ContextGovernPlugin()
    config = AgentConfig(system_prompt="base prompt")
    ctx = MessagesBuildContext(
        session_id="test",
        channel_id="ws",
        inbound_data={},
        workspace=str(workspace),
        agent_dir=str(agent_dir),
        config=config,
        events=[],
    )

    plugin._inject_agents_md(ctx)

    assert "Agent Rules" in ctx.config.system_prompt
    assert "Python 3.12" in ctx.config.system_prompt
    assert "Workspace Rules" not in ctx.config.system_prompt


def test_context_govern_falls_back_to_workspace_agents_md(tmp_path):
    """When agent_dir has no AGENTS.md, fall back to workspace."""
    from ftre.plugin.builtin.context_govern import ContextGovernPlugin
    from ftre.plugin.hook_manager import MessagesBuildContext
    from ftre.config import AgentConfig

    agent_dir = tmp_path / "agents" / "coder"
    agent_dir.mkdir(parents=True)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("# Workspace Rules", encoding="utf-8")

    plugin = ContextGovernPlugin()
    config = AgentConfig(system_prompt="base prompt")
    ctx = MessagesBuildContext(
        session_id="test",
        channel_id="ws",
        inbound_data={},
        workspace=str(workspace),
        agent_dir=str(agent_dir),
        config=config,
        events=[],
    )

    plugin._inject_agents_md(ctx)

    assert "Workspace Rules" in ctx.config.system_prompt


# ─── Task 6: Integration tests ───────────────────────────────────────

def test_ensure_default_creates_agent_dir(tmp_path):
    """ensure_default() creates default/ with agent.config.json and md templates."""
    from ftre.agent.agent_manager import AgentManager

    agents_dir = tmp_path / "agents"
    mgr = AgentManager(agents_dir=agents_dir, global_config_data={
        "providers": {
            "openai": {
                "api_key": "sk-test",
                "api_base": "https://api.openai.com/v1",
                "api_protocol": "openai",
                "models": [
                    {"id": "gpt-4o", "name": "GPT-4o", "context_window": 128000, "max_output": 16384, "vision": True},
                ],
            },
        },
        "agents": {},
    })

    mgr.ensure_default()

    default_dir = agents_dir / "default"
    assert default_dir.is_dir()
    assert (default_dir / "agent.config.json").is_file()
    assert (default_dir / "SOUL.md").is_file()
    assert (default_dir / "AGENTS.md").is_file()
    assert (default_dir / "USER.md").is_file()

    cfg = json.loads((default_dir / "agent.config.json").read_text(encoding="utf-8"))
    assert cfg["llm"]["provider"] == "openai"
    assert cfg["llm"]["model"] == "gpt-4o"


def test_ensure_default_idempotent(tmp_path):
    """ensure_default() does not overwrite existing default agent."""
    from ftre.agent.agent_manager import AgentManager

    agents_dir = tmp_path / "agents"
    default_dir = agents_dir / "default"
    default_dir.mkdir(parents=True)
    (default_dir / "agent.config.json").write_text(
        json.dumps({"llm": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"}}),
        encoding="utf-8",
    )

    mgr = AgentManager(agents_dir=agents_dir, global_config_data={
        "agents": {},
    })

    mgr.ensure_default()

    cfg = json.loads((default_dir / "agent.config.json").read_text(encoding="utf-8"))
    assert cfg["llm"]["provider"] == "anthropic"


def test_agent_profile_llm_uses_agent_provider_model(tmp_agents_dir, fake_global_config):
    """Agent with different provider/model gets correct LLMConfig from global providers."""
    from ftre.agent.agent_manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(json.dumps({
        "llm": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
    }), encoding="utf-8")

    mgr = AgentManager(agents_dir=tmp_agents_dir, global_config_data=fake_global_config)
    profile = mgr.load("coder")

    assert profile.llm.model == "claude-sonnet-4-20250514"
    assert profile.llm.api_key == "sk-ant-global"
    assert profile.llm.vision is True
    assert profile.llm.context_window == 200000


def test_agent_profile_llm_fallback_on_invalid_provider(tmp_agents_dir, fake_global_config):
    """Agent specifying an invalid provider falls back to global default."""
    from ftre.agent.agent_manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(json.dumps({
        "llm": {"provider": "nonexistent", "model": "fake-model"},
    }), encoding="utf-8")

    mgr = AgentManager(agents_dir=tmp_agents_dir, global_config_data=fake_global_config)
    profile = mgr.load("coder")

    assert profile.llm.model == "gpt-4o"
    assert profile.llm.api_key == "sk-global"


def test_default_agent_config_as_global_fallback(tmp_path):
    """Without agents.defaults, default agent's config is the global fallback."""
    from ftre.agent.agent_manager import AgentManager

    agents_dir = tmp_path / "agents"
    # default agent with llm config
    default_dir = agents_dir / "default"
    default_dir.mkdir(parents=True)
    (default_dir / "agent.config.json").write_text(json.dumps({
        "id": "default",
        "name": "Ftre",
        "llm": {"provider": "openai", "model": "gpt-4o"},
        "workspace": "/global/ws",
    }), encoding="utf-8")

    # coder agent with empty config — should inherit from default agent
    coder_dir = agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text("{}", encoding="utf-8")

    # No agents.defaults in global config
    global_data = {
        "providers": {
            "openai": {
                "api_key": "sk-global",
                "api_base": "https://api.openai.com/v1",
                "api_protocol": "openai",
                "models": [
                    {"id": "gpt-4o", "name": "GPT-4o", "context_window": 128000, "max_output": 16384, "vision": True},
                ],
            },
        },
        # No agents.defaults — new structure
        "agents": {},
    }

    mgr = AgentManager(agents_dir=agents_dir, global_config_data=global_data)
    profile = mgr.load("coder")

    # coder has no llm → falls back to default agent's llm
    assert profile.llm.model == "gpt-4o"
    assert profile.llm.api_key == "sk-global"
    # coder has no workspace → falls back to default agent's workspace
    assert profile.workspace == "/global/ws"


def test_ensure_default_picks_first_provider(tmp_path):
    """ensure_default() picks first provider/model when agents.defaults is absent."""
    from ftre.agent.agent_manager import AgentManager

    agents_dir = tmp_path / "agents"
    global_data = {
        "providers": {
            "anthropic": {
                "api_key": "sk-ant",
                "api_base": "https://api.anthropic.com",
                "api_protocol": "anthropic",
                "models": [
                    {"id": "claude-sonnet-4", "name": "Claude", "context_window": 200000, "max_output": 16384, "vision": True},
                ],
            },
        },
        "agents": {},  # no defaults
    }

    mgr = AgentManager(agents_dir=agents_dir, global_config_data=global_data)
    mgr.ensure_default()

    cfg = json.loads((agents_dir / "default" / "agent.config.json").read_text(encoding="utf-8"))
    assert cfg["llm"]["provider"] == "anthropic"
    assert cfg["llm"]["model"] == "claude-sonnet-4"
