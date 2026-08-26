"""
Tests for AgentManager: config merging, tool filtering, prompt loading.
"""

import json
from unittest.mock import Mock

import pytest


@pytest.fixture
def tmp_agents_dir(tmp_path):
    """Create a temporary ~/.ftre/agents/ directory with a default agent."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    # Create default agent with llm config
    default_dir = agents_dir / "default"
    default_dir.mkdir()
    (default_dir / "agent.config.json").write_text(
        json.dumps(
            {
                "id": "default",
                "name": "Ftre",
                "llm": {"provider": "openai", "model": "gpt-4o"},
                "workspace": "/tmp",
            }
        ),
        encoding="utf-8",
    )
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
                    {
                        "id": "gpt-4o",
                        "name": "GPT-4o",
                        "context_window": 128000,
                        "max_output": 16384,
                        "vision": True,
                        "reasoning_effort": "high",
                    },
                    {
                        "id": "gpt-4o-mini",
                        "name": "GPT-4o Mini",
                        "context_window": 128000,
                        "max_output": 16384,
                        "vision": False,
                    },
                ],
            },
            "anthropic": {
                "api_key": "sk-ant-global",
                "api_base": "https://api.anthropic.com",
                "api_protocol": "anthropic",
                "models": [
                    {
                        "id": "claude-sonnet-4-20250514",
                        "name": "Claude Sonnet",
                        "context_window": 200000,
                        "max_output": 16384,
                        "vision": True,
                        "reasoning_effort": "high",
                    },
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


@pytest.fixture(autouse=True)
def _mock_load_config_file(monkeypatch, fake_global_config):
    """Patch load_config_file so AgentManager reads the fake config instead of the real file."""
    from ftre.services.agent.profile import manager as am

    monkeypatch.setattr(am, "load_config_file", lambda: fake_global_config)


def test_load_default_agent_uses_global_config(tmp_agents_dir, fake_global_config):
    """Loading 'default' with empty agent.config.json inherits everything from global."""
    from ftre.services.agent.profile.manager import AgentManager

    mgr = AgentManager(agents_dir=tmp_agents_dir)
    profile = mgr.load("default")

    assert profile.agent_id == "default"
    assert profile.llm.model == "gpt-4o"
    assert profile.llm.api_key == "sk-global"
    assert profile.llm.reasoning_effort == "high"
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
    from ftre_agent_core.tool import Tool, ToolRegistry

    from ftre.services.tools.builtin import filter_tools

    def _noop(**kw):
        return ""

    def _make_registry():
        r = ToolRegistry()
        for name in [
            "bash",
            "read",
            "write",
            "cron",
            "mcp__playwright__browser_navigate",
        ]:
            r.register(Tool(name=name, description="", parameters=[], func=_noop))
        return r

    # No config → all tools
    assert len(_make_registry()) == 5

    # Allow only bash and read
    r = _make_registry()
    filter_tools(r, {"allow": ["bash", "read"]})
    assert r.names == ["bash", "read"]

    # Deny cron
    r = _make_registry()
    filter_tools(r, {"deny": ["cron"]})
    assert "cron" not in r.names
    assert len(r) == 4

    # Allow + Deny combined
    r = _make_registry()
    filter_tools(r, {"allow": ["bash", "read", "cron"], "deny": ["cron"]})
    assert r.names == ["bash", "read"]

    # Empty allow = no whitelist restriction, only deny applies
    r = _make_registry()
    filter_tools(r, {"allow": [], "deny": ["write"]})
    assert "write" not in r.names
    assert len(r) == 4


def test_load_agent_with_tool_overrides(tmp_agents_dir, fake_global_config):
    """Agent with tools config gets tools_config set on profile."""
    from ftre.services.agent.profile.manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(
        json.dumps(
            {
                "llm": {"provider": "openai", "model": "gpt-4o-mini"},
                "tools": {
                    "allow": ["bash", "read", "write", "edit"],
                    "deny": ["cron", "task", "send_message"],
                },
                "workspace": "/custom/workspace",
            }
        ),
        encoding="utf-8",
    )

    mgr = AgentManager(agents_dir=tmp_agents_dir)
    profile = mgr.load("coder")

    assert profile.llm.model == "gpt-4o-mini"
    assert profile.llm.api_key == "sk-global"
    assert profile.workspace == "/custom/workspace"
    assert profile.tools_config == {
        "allow": ["bash", "read", "write", "edit"],
        "deny": ["cron", "task", "send_message"],
    }


def test_agent_reasoning_effort_overrides_model_default_including_empty_value(
    tmp_agents_dir, fake_global_config
):
    """An explicitly configured effort wins over the matching model's default."""
    from ftre.services.agent.profile.manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(
        json.dumps(
            {
                "llm": {"reasoning_effort": ""},
            }
        ),
        encoding="utf-8",
    )

    mgr = AgentManager(agents_dir=tmp_agents_dir)
    assert mgr.load("coder").llm.reasoning_effort == ""

    (coder_dir / "agent.config.json").write_text(
        json.dumps(
            {
                "llm": {"reasoning_effort": "none"},
            }
        ),
        encoding="utf-8",
    )
    assert mgr.load("coder").llm.reasoning_effort == "none"


def test_agent_reasoning_effort_dropped_for_model_without_support(
    tmp_agents_dir, fake_global_config
):
    """模型未声明任何推理强度配置（无默认值/无 values）→ agent 显式 effort 被清空。"""
    from ftre.services.agent.profile.manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "openai",
                    "model": "gpt-4o-mini",  # fake_global_config 中该模型无 reasoning 配置
                    "reasoning_effort": "max",
                },
            }
        ),
        encoding="utf-8",
    )

    mgr = AgentManager(agents_dir=tmp_agents_dir)
    assert mgr.load("coder").llm.reasoning_effort == ""


def test_agent_reasoning_effort_kept_for_model_with_support(
    tmp_agents_dir, fake_global_config
):
    """模型声明了 reasoning_effort（默认 high）→ agent 显式 effort 保留。"""
    from ftre.services.agent.profile.manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "openai",
                    "model": "gpt-4o",
                    "reasoning_effort": "max",
                },
            }
        ),
        encoding="utf-8",
    )

    mgr = AgentManager(agents_dir=tmp_agents_dir)
    assert mgr.load("coder").llm.reasoning_effort == "max"


def test_load_agent_with_mcp_merge(tmp_agents_dir, fake_global_config):
    """Agent MCP config deep-merges with global."""
    from ftre.services.agent.profile.manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(
        json.dumps(
            {
                "mcp": {
                    "playwright": {"disabled": True},
                    "extra-server": {"type": "local", "command": ["node", "server.js"]},
                },
            }
        ),
        encoding="utf-8",
    )

    mgr = AgentManager(agents_dir=tmp_agents_dir)
    profile = mgr.load("coder")

    assert "playwright" in profile.mcp_config
    assert profile.mcp_config["playwright"]["disabled"] is True
    assert "extra-server" in profile.mcp_config
    assert profile.mcp_config["extra-server"]["command"] == ["node", "server.js"]


def test_load_agent_with_plugins_merge(tmp_agents_dir, fake_global_config):
    """Agent plugins merge by name with global."""
    from ftre.services.agent.profile.manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(
        json.dumps(
            {
                "plugins": [
                    {"name": "octo_channel", "enabled": False},
                    {"name": "my-plugin", "module": "my_plugin.MyPlugin", "config": {}},
                ],
            }
        ),
        encoding="utf-8",
    )

    mgr = AgentManager(agents_dir=tmp_agents_dir)
    profile = mgr.load("coder")

    plugin_names = [p["name"] for p in profile.plugins_config]
    assert "octo_channel" in plugin_names
    assert "my-plugin" in plugin_names
    octo = next(p for p in profile.plugins_config if p["name"] == "octo_channel")
    assert octo["enabled"] is False


def test_load_agent_with_disabled_skills_override(tmp_agents_dir, fake_global_config):
    """Agent disabled_skills replaces global."""
    from ftre.services.agent.profile.manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(
        json.dumps(
            {
                "disabled_skills": ["playwright-mcp", "brainstorming"],
            }
        ),
        encoding="utf-8",
    )

    mgr = AgentManager(agents_dir=tmp_agents_dir)
    profile = mgr.load("coder")

    assert profile.disabled_skills == ["playwright-mcp", "brainstorming"]
    assert "mcp-guide" not in profile.disabled_skills


def test_load_nonexistent_agent_falls_back_to_default(
    tmp_agents_dir, fake_global_config
):
    """Loading a non-existent agent_id falls back to 'default'."""
    from ftre.services.agent.profile.manager import AgentManager

    mgr = AgentManager(agents_dir=tmp_agents_dir)
    profile = mgr.load("nonexistent")

    assert profile.agent_id == "default"


def test_load_agent_reads_md_files(tmp_agents_dir, fake_global_config):
    """Agent loads SOUL.md, AGENTS.md, USER.md from its directory."""
    from ftre.services.agent.profile.manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text("{}", encoding="utf-8")
    (coder_dir / "SOUL.md").write_text("You are a coding expert.", encoding="utf-8")
    (coder_dir / "AGENTS.md").write_text(
        "# Coding Rules\n\nAlways test.", encoding="utf-8"
    )
    (coder_dir / "USER.md").write_text("Call me boss.", encoding="utf-8")

    mgr = AgentManager(agents_dir=tmp_agents_dir)
    profile = mgr.load("coder")

    assert profile.soul_prompt == "You are a coding expert."
    assert "Coding Rules" in profile.agents_md
    assert profile.user_prompt_md == "Call me boss."


def test_list_agents(tmp_agents_dir, fake_global_config):
    """list_agents returns all agent directories with metadata."""
    from ftre.services.agent.profile.manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(
        json.dumps(
            {
                "llm": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
            }
        ),
        encoding="utf-8",
    )
    (coder_dir / "SOUL.md").write_text("expert", encoding="utf-8")

    mgr = AgentManager(agents_dir=tmp_agents_dir)
    agents = mgr.list_agents()

    assert len(agents) == 2
    ids = [a["id"] for a in agents]
    assert "default" in ids
    assert "coder" in ids

    coder = next(a for a in agents if a["id"] == "coder")
    assert coder["model"] == "claude-sonnet-4-20250514"
    assert coder["provider"] == "anthropic"
    assert coder["reasoning_effort"] == "high"
    assert coder["has_soul"] is True


# ─── Task 6: Integration tests ───────────────────────────────────────


def test_ensure_default_creates_agent_dir(tmp_path, monkeypatch):
    """ensure_default() creates default/ with agent.config.json and md templates."""
    from ftre.services.agent.profile.manager import AgentManager

    agents_dir = tmp_path / "agents"
    global_data = {
        "providers": {
            "openai": {
                "api_key": "sk-test",
                "api_base": "https://api.openai.com/v1",
                "models": [
                    {"id": "gpt-4o", "name": "GPT-4o", "context_window": 128000},
                ],
            },
        },
        "agents": {},
    }
    monkeypatch.setattr(
        "ftre.services.agent.profile.manager.load_config_file", lambda: global_data
    )
    mgr = AgentManager(agents_dir=agents_dir)

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


def test_ensure_default_idempotent(tmp_path, monkeypatch):
    """ensure_default() does not overwrite existing default agent."""
    from ftre.services.agent.profile.manager import AgentManager

    agents_dir = tmp_path / "agents"
    default_dir = agents_dir / "default"
    default_dir.mkdir(parents=True)
    (default_dir / "agent.config.json").write_text(
        json.dumps(
            {"llm": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"}}
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("ftre.services.agent.profile.manager.load_config_file", dict)
    mgr = AgentManager(agents_dir=agents_dir)

    mgr.ensure_default()

    cfg = json.loads((default_dir / "agent.config.json").read_text(encoding="utf-8"))
    assert cfg["llm"]["provider"] == "anthropic"


def test_agent_profile_llm_uses_agent_provider_model(
    tmp_agents_dir, fake_global_config
):
    """Agent with different provider/model gets correct LLMConfig from global providers."""
    from ftre.services.agent.profile.manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(
        json.dumps(
            {
                "llm": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
            }
        ),
        encoding="utf-8",
    )

    mgr = AgentManager(agents_dir=tmp_agents_dir)
    profile = mgr.load("coder")

    assert profile.llm.model == "claude-sonnet-4-20250514"
    assert profile.llm.api_key == "sk-ant-global"
    assert profile.llm.vision is True
    assert profile.llm.context_window == 200000


def test_agent_profile_llm_fallback_on_invalid_provider(
    tmp_agents_dir, fake_global_config
):
    """Agent specifying an invalid provider falls back to global default."""
    from ftre.services.agent.profile.manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(
        json.dumps(
            {
                "llm": {"provider": "nonexistent", "model": "fake-model"},
            }
        ),
        encoding="utf-8",
    )

    mgr = AgentManager(agents_dir=tmp_agents_dir)
    profile = mgr.load("coder")

    assert profile.llm.model == "gpt-4o"
    assert profile.llm.api_key == "sk-global"


def test_default_agent_config_as_global_fallback(tmp_path):
    """Without agents.defaults, default agent's config is the global fallback."""
    from ftre.services.agent.profile.manager import AgentManager

    agents_dir = tmp_path / "agents"
    # default agent with llm config
    default_dir = agents_dir / "default"
    default_dir.mkdir(parents=True)
    (default_dir / "agent.config.json").write_text(
        json.dumps(
            {
                "id": "default",
                "name": "Ftre",
                "llm": {"provider": "openai", "model": "gpt-4o"},
                "workspace": "/global/ws",
            }
        ),
        encoding="utf-8",
    )

    # coder agent with empty config — should inherit from default agent
    coder_dir = agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text("{}", encoding="utf-8")

    mgr = AgentManager(agents_dir=agents_dir)
    profile = mgr.load("coder")

    # coder has no llm → falls back to default agent's llm
    assert profile.llm.model == "gpt-4o"
    assert profile.llm.api_key == "sk-global"
    # coder has no workspace → falls back to default agent's workspace
    assert profile.workspace == "/global/ws"


def test_ensure_default_picks_first_provider(tmp_path, monkeypatch):
    """ensure_default() picks first provider/model when agents.defaults is absent."""
    from ftre.services.agent.profile.manager import AgentManager

    agents_dir = tmp_path / "agents"
    global_data = {
        "providers": {
            "anthropic": {
                "api_key": "sk-ant",
                "api_base": "https://api.anthropic.com",
                "api_protocol": "anthropic",
                "models": [
                    {
                        "id": "claude-sonnet-4",
                        "name": "Claude",
                        "context_window": 200000,
                        "max_output": 16384,
                        "vision": True,
                    },
                ],
            },
        },
        "agents": {},  # no defaults
    }

    monkeypatch.setattr(
        "ftre.services.agent.profile.manager.load_config_file", lambda: global_data
    )
    mgr = AgentManager(agents_dir=agents_dir)
    mgr.ensure_default()

    cfg = json.loads(
        (agents_dir / "default" / "agent.config.json").read_text(encoding="utf-8")
    )
    assert cfg["llm"]["provider"] == "anthropic"
    assert cfg["llm"]["model"] == "claude-sonnet-4"


# ─── Task 7: Create and Delete agents ────────────────────────────────


def test_runtime_factory_passes_reasoning_effort_to_core(
    tmp_agents_dir, fake_global_config, monkeypatch
):
    """Profile Manager 只解析配置，Runtime factory 才负责构造 Core Agent。"""
    from ftre.services.agent.config import AgentConfig
    from ftre.services.agent.profile.manager import AgentManager
    from ftre.services.agent.runtime import factory

    mgr = AgentManager(agents_dir=tmp_agents_dir)
    profile = mgr.load("default")
    created = Mock()
    monkeypatch.setattr(factory, "ReActAgent", created)
    factory.create_core_agent(
        config=AgentConfig(),
        profile_snapshot=profile,
        tool_view=Mock(),
        system_prompt="prompt",
        tracer=Mock(),
        hooks=None,
        hook_context=None,
        state=factory.default_agent_state(),
    )

    assert created.call_args.kwargs["reasoning_effort"] == "high"


def test_create_agent_profile(tmp_path):
    """create_agent_profile creates a new agent directory with config and empty md files."""
    from ftre.services.agent.profile.manager import AgentManager

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    # Create default first
    default_dir = agents_dir / "default"
    default_dir.mkdir()
    (default_dir / "agent.config.json").write_text(
        json.dumps(
            {
                "id": "default",
                "name": "Ftre",
                "llm": {"provider": "openai", "model": "gpt-4o"},
            }
        ),
        encoding="utf-8",
    )

    mgr = AgentManager(agents_dir=agents_dir)

    cfg = mgr.create_agent_profile(
        agent_id="coder",
        name="Coder",
        llm_provider="openai",
        llm_model="gpt-4o",
        workspace="/tmp/code",
    )

    assert cfg["id"] == "coder"
    assert cfg["name"] == "Coder"
    assert cfg["llm"]["provider"] == "openai"
    assert cfg["llm"]["model"] == "gpt-4o"
    assert cfg["workspace"] == "/tmp/code"

    # Files created
    assert (agents_dir / "coder" / "agent.config.json").exists()
    assert (agents_dir / "coder" / "SOUL.md").exists()
    assert (agents_dir / "coder" / "AGENTS.md").exists()
    assert (agents_dir / "coder" / "USER.md").exists()

    # Can load it
    profile = mgr.load("coder")
    assert profile.agent_id == "coder"
    assert profile.name == "Coder"


def test_create_agent_duplicate_raises(tmp_path):
    """Creating an agent that already exists raises ValueError."""
    from ftre.services.agent.profile.manager import AgentManager

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    default_dir = agents_dir / "default"
    default_dir.mkdir()
    (default_dir / "agent.config.json").write_text("{}", encoding="utf-8")

    mgr = AgentManager(agents_dir=agents_dir)
    mgr.create_agent_profile("coder", name="Coder")

    import pytest

    with pytest.raises(ValueError, match="已存在"):
        mgr.create_agent_profile("coder", name="Coder2")


def test_create_agent_invalid_id_raises(tmp_path):
    """Invalid agent_id raises ValueError."""
    from ftre.services.agent.profile.manager import AgentManager

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "default").mkdir()
    (agents_dir / "default" / "agent.config.json").write_text("{}", encoding="utf-8")

    mgr = AgentManager(agents_dir=agents_dir)

    import pytest

    with pytest.raises(ValueError, match="只能包含"):
        mgr.create_agent_profile("bad/id", name="Bad")


def test_delete_agent(tmp_path):
    """delete_agent removes the agent directory."""
    from ftre.services.agent.profile.manager import AgentManager

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    default_dir = agents_dir / "default"
    default_dir.mkdir()
    (default_dir / "agent.config.json").write_text("{}", encoding="utf-8")

    mgr = AgentManager(agents_dir=agents_dir)
    mgr.create_agent_profile("coder", name="Coder")
    assert (agents_dir / "coder").exists()

    mgr.delete_agent("coder")
    assert not (agents_dir / "coder").exists()


def test_delete_default_raises(tmp_path):
    """Deleting default agent raises ValueError."""
    from ftre.services.agent.profile.manager import AgentManager

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    default_dir = agents_dir / "default"
    default_dir.mkdir()
    (default_dir / "agent.config.json").write_text("{}", encoding="utf-8")

    mgr = AgentManager(agents_dir=agents_dir)

    import pytest

    with pytest.raises(ValueError, match="不允许删除 default"):
        mgr.delete_agent("default")


def test_update_agent_name_and_workspace(tmp_path):
    """update_agent supports name and workspace fields."""
    from ftre.services.agent.profile.manager import AgentManager

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    default_dir = agents_dir / "default"
    default_dir.mkdir()
    (default_dir / "agent.config.json").write_text(
        json.dumps(
            {
                "id": "default",
                "name": "Ftre",
            }
        ),
        encoding="utf-8",
    )

    mgr = AgentManager(agents_dir=agents_dir)
    cfg = mgr.update_agent("default", {"name": "NewName", "workspace": "/new/ws"})

    assert cfg["name"] == "NewName"
    assert cfg["workspace"] == "/new/ws"
