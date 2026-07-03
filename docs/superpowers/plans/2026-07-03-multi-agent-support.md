# Multi-Agent Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable per-message agent routing — each `user_message` can specify `metadata.agent_id`, and the backend loads the corresponding agent's LLM config, tool whitelist/blacklist, workspace, MCP config, plugins, disabled_skills, and prompt files (SOUL.md / AGENTS.md / USER.md) from `~/.ftre/agents/<agent_id>/`.

**Architecture:** A new `AgentManager` module loads and caches per-agent configurations from `~/.ftre/agents/<name>/agent.config.json`, merging them with the global `config.json`. `AgentLoop._dispatch` reads `metadata.agent_id` (defaulting to `"default"`) and passes the resolved `AgentProfile` through the pipeline. Tool filtering, prompt composition, and context_govern's AGENTS.md injection all become agent-aware. A `GET /api/agents` endpoint exposes the agent list to the frontend.

**Tech Stack:** Python 3.12, asyncio, FastAPI, dataclasses, SQLite (aiosqlite)

## Global Constraints

- Python 3.12 + TypeScript
- No new third-party dependencies
- Backend path: `E:\ftre\src\ftre\`
- Config dir: `~/.ftre/` (Windows: `C:\Users\<user>\.ftre\`)
- Logging via `logging` module
- No private git commit / push unless explicitly told
- All agent files live under `~/.ftre/agents/<agent_id>/`
- `default` agent always exists; `metadata.agent_id` missing → route to `default`
- `agent.config.json` `llm` field only carries `provider` + `model`; api_key / base_url / vision always inherited from global config.json

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `src/ftre/agent/agent_manager.py` | `AgentProfile` dataclass + `AgentManager` class: load/merge/cache per-agent configs, ensure default exists, list agents for API |
| `tests/test_agent_manager.py` | Unit tests for AgentManager: config merging, tool filtering, prompt loading, default fallback, mtime cache |

### Modified Files

| File | Changes |
|------|---------|
| `src/ftre/config.py` | Add `AGENTS_DIR` constant |
| `src/ftre/tools/__init__.py` | Add `filter_tools()` function; `build_default_tools()` gains optional `tools_config` param |
| `src/ftre/agent/loop.py` | `_dispatch` reads `metadata.agent_id`; `_run_async` / `_create_agent` / `_compose_system_prompt` use `AgentProfile`; pass agent_dir to context_govern hook |
| `src/ftre/plugin/builtin/context_govern.py` | `_inject_agents_md` reads AGENTS.md from agent_dir first, falls back to workspace AGENTS.md |
| `src/ftre/plugin/hook_manager.py` | `MessagesBuildContext` gains `agent_dir: str` field |
| `src/ftre/api/routes.py` | Add `GET /api/agents` endpoint; add `set_agent_manager()` injection |
| `src/ftre/main.py` | Instantiate `AgentManager`, call `ensure_default()`, inject into routes and AgentLoop |

---

## Task 1: AgentProfile dataclass and config merge logic

**Files:**
- Create: `src/ftre/agent/agent_manager.py`
- Test: `tests/test_agent_manager.py`

**Interfaces:**
- Consumes: `ftre.config.AgentConfig`, `ftre.config.LLMConfig`, `ftre.config.load_config()`, `ftre.config.load_config_file()`, `ftre.config._build_llm_config()`
- Produces: `AgentProfile` dataclass with fields: `agent_id`, `llm` (LLMConfig), `workspace` (str), `tools_config` (dict|None), `mcp_config` (dict), `plugins_config` (list), `disabled_skills` (list), `soul_prompt` (str), `user_prompt_md` (str), `agents_md` (str), `agent_dir` (str)

- [ ] **Step 1: Write the failing test for AgentProfile and basic merge**

```python
# tests/test_agent_manager.py
"""Tests for AgentManager: config merging, tool filtering, prompt loading."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_agents_dir(tmp_path):
    """Create a temporary ~/.ftre/agents/ directory with a default agent."""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    # Create default agent
    default_dir = agents_dir / "default"
    default_dir.mkdir()
    (default_dir / "agent.config.json").write_text("{}", encoding="utf-8")
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
        "agents": {
            "defaults": {
                "provider": "openai",
                "model": "gpt-4o",
                "workspace": "/global/workspace",
            },
        },
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
    assert profile.workspace == "/global/workspace"
    assert profile.tools_config is None  # no tools key → all available
    assert "playwright" in profile.mcp_config
    assert len(profile.plugins_config) == 1
    assert profile.plugins_config[0]["name"] == "octo_channel"
    assert "mcp-guide" in profile.disabled_skills
    assert profile.soul_prompt == ""  # no SOUL.md
    assert profile.user_prompt_md == ""
    assert profile.agents_md == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_manager.py::test_load_default_agent_uses_global_config -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ftre.agent.agent_manager'`

- [ ] **Step 3: Write minimal AgentProfile + AgentManager implementation**

```python
# src/ftre/agent/agent_manager.py
"""
AgentManager — 加载和管理 ~/.ftre/agents/ 下的 per-agent 配置。

每个 agent 目录结构：
  ~/.ftre/agents/<agent_id>/
    ├── agent.config.json    # LLM、tools、workspace、mcp、plugins、disabled_skills
    ├── SOUL.md              # 人设（追加到全局 system_prompt 之后）
    ├── AGENTS.md            # 项目约定（context_govern 注入）
    └── USER.md              # 用户偏好（追加到 SOUL.md 之后）

合并规则：
  - llm: 仅 provider + model 可覆盖，api_key/base_url/vision 始终用全局
  - tools: 整体替换（写了就用 agent 的，不写则全部可用）
  - workspace: 标量覆盖
  - mcp: 深度合并（按 server name 为 key）
  - plugins: 按 name 合并（同名 agent 覆盖全局，全局有但 agent 没提的保留）
  - disabled_skills: 整体替换
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ftre.config import AgentConfig, LLMConfig, load_config_file, _build_llm_config

logger = logging.getLogger(__name__)


@dataclass
class AgentProfile:
    """一个 agent 的完整运行时配置（已与全局合并）。"""
    agent_id: str = "default"
    llm: LLMConfig = field(default_factory=LLMConfig)
    workspace: str = ""
    tools_config: dict | None = None         # {"allow": [...], "deny": [...]} 或 None
    mcp_config: dict = field(default_factory=dict)
    plugins_config: list = field(default_factory=list)
    disabled_skills: list = field(default_factory=list)
    soul_prompt: str = ""                     # SOUL.md 内容
    user_prompt_md: str = ""                  # USER.md 内容
    agents_md: str = ""                       # AGENTS.md 内容
    agent_dir: str = ""                       # agent 目录的绝对路径


class AgentManager:
    """加载和管理 ~/.ftre/agents/ 下的 agent 配置。"""

    def __init__(self, agents_dir: Path, global_config_data: dict | None = None):
        self._agents_dir = agents_dir
        self._global_data = global_config_data or {}
        self._cache: dict[str, AgentProfile] = {}
        self._cache_key: dict[str, str] = {}  # agent_id → serialized config hash for mtime check

    def load(self, agent_id: str) -> AgentProfile:
        """加载 agent 配置。agent_id 不存在时回退到 default。"""
        agent_dir = self._agents_dir / agent_id
        if not agent_dir.is_dir():
            logger.warning(f"[agent-manager] agent '{agent_id}' 不存在，回退到 default")
            agent_id = "default"
            agent_dir = self._agents_dir / agent_id
            if not agent_dir.is_dir():
                # default 也不存在——返回空 profile（走全局兜底）
                return AgentProfile(agent_id="default")

        # mtime 缓存检查
        config_path = agent_dir / "agent.config.json"
        try:
            current_sig = str(config_path.stat().st_mtime) if config_path.exists() else "0"
        except OSError:
            current_sig = "0"

        # 也检查 md 文件的 mtime
        for md_name in ("SOUL.md", "AGENTS.md", "USER.md"):
            md_path = agent_dir / md_name
            try:
                current_sig += "|" + str(md_path.stat().st_mtime) if md_path.exists() else "|0"
            except OSError:
                current_sig += "|0"

        if agent_id in self._cache and self._cache_key.get(agent_id) == current_sig:
            return self._cache[agent_id]

        profile = self._load_and_merge(agent_id, agent_dir)
        self._cache[agent_id] = profile
        self._cache_key[agent_id] = current_sig
        return profile

    def _load_and_merge(self, agent_id: str, agent_dir: Path) -> AgentProfile:
        """读取 agent.config.json 并与全局配置合并。"""
        # 读取 agent.config.json
        agent_cfg: dict = {}
        config_path = agent_dir / "agent.config.json"
        if config_path.exists():
            try:
                agent_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"[agent-manager] 读取 {config_path} 失败: {e}")

        # ─── 合并 LLM ──────────────────────────────────────
        global_defaults = self._global_data.get("agents", {}).get("defaults", {})
        global_provider = global_defaults.get("provider", "")
        global_model = global_defaults.get("model", "")

        agent_llm = agent_cfg.get("llm", {})
        if not isinstance(agent_llm, dict):
            agent_llm = {}

        provider = agent_llm.get("provider", "") or global_provider
        model = agent_llm.get("model", "") or global_model

        llm = _build_llm_config(self._global_data, provider, model)
        if not llm.model:
            # agent 指定的 provider/model 在全局找不到 → 回退全局默认
            logger.warning(
                f"[agent-manager] agent '{agent_id}' 的 provider={provider} model={model} "
                f"在全局配置中找不到，回退到全局默认"
            )
            llm = _build_llm_config(self._global_data, global_provider, global_model)

        # ─── 合并 workspace ─────────────────────────────────
        workspace = agent_cfg.get("workspace", "") or global_defaults.get("workspace", "") or ""
        if not isinstance(workspace, str):
            workspace = ""

        # ─── 合并 tools ─────────────────────────────────────
        tools_config = agent_cfg.get("tools")
        if tools_config is not None and not isinstance(tools_config, dict):
            tools_config = None

        # ─── 合并 MCP（深度合并） ───────────────────────────
        global_mcp = self._global_data.get("mcp", {})
        agent_mcp = agent_cfg.get("mcp", {})
        if not isinstance(agent_mcp, dict):
            agent_mcp = {}
        merged_mcp = {**global_mcp, **agent_mcp} if isinstance(global_mcp, dict) else dict(agent_mcp)

        # ─── 合并 plugins（按 name 合并） ───────────────────
        global_plugins = self._global_data.get("plugins", [])
        agent_plugins = agent_cfg.get("plugins", [])
        if not isinstance(global_plugins, list):
            global_plugins = []
        if not isinstance(agent_plugins, list):
            agent_plugins = []
        merged_plugins = self._merge_plugins(global_plugins, agent_plugins)

        # ─── 合并 disabled_skills（整体替换） ───────────────
        if "disabled_skills" in agent_cfg:
            disabled_skills = agent_cfg.get("disabled_skills", [])
            if not isinstance(disabled_skills, list):
                disabled_skills = []
        else:
            disabled_skills = self._global_data.get("disabled_skills", [])
            if not isinstance(disabled_skills, list):
                disabled_skills = []

        # ─── 读取提示词文件 ─────────────────────────────────
        soul_prompt = self._read_md(agent_dir / "SOUL.md")
        user_prompt_md = self._read_md(agent_dir / "USER.md")
        agents_md = self._read_md(agent_dir / "AGENTS.md")

        return AgentProfile(
            agent_id=agent_id,
            llm=llm,
            workspace=workspace,
            tools_config=tools_config,
            mcp_config=merged_mcp,
            plugins_config=merged_plugins,
            disabled_skills=disabled_skills,
            soul_prompt=soul_prompt,
            user_prompt_md=user_prompt_md,
            agents_md=agents_md,
            agent_dir=str(agent_dir.resolve()),
        )

    @staticmethod
    def _merge_plugins(global_list: list, agent_list: list) -> list:
        """按 name 合并 plugins：同名 agent 覆盖全局，全局有但 agent 没提的保留。"""
        agent_by_name = {}
        for p in agent_list:
            if isinstance(p, dict) and p.get("name"):
                agent_by_name[p["name"]] = p

        result = []
        seen_names = set()
        for p in global_list:
            if not isinstance(p, dict):
                continue
            name = p.get("name", "")
            if name in agent_by_name:
                # agent 覆盖全局
                result.append(agent_by_name[name])
                seen_names.add(name)
            else:
                result.append(p)
                seen_names.add(name)

        # agent 独有的插件
        for name, p in agent_by_name.items():
            if name not in seen_names:
                result.append(p)

        return result

    @staticmethod
    def _read_md(path: Path) -> str:
        """读取 Markdown 文件，返回 stripped 内容；不存在则返回空串。"""
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            try:
                return path.read_text(encoding="gbk").strip()
            except (OSError, UnicodeDecodeError):
                return ""

    def list_agents(self) -> list[dict]:
        """返回所有 agent 的摘要信息，供 GET /api/agents 使用。"""
        result = []
        if not self._agents_dir.is_dir():
            return result

        for entry in sorted(self._agents_dir.iterdir()):
            if not entry.is_dir():
                continue
            agent_id = entry.name
            config_path = entry / "agent.config.json"
            has_config = config_path.is_file()

            # 读取 agent.config.json 获取 model 信息
            model = ""
            provider = ""
            if has_config:
                try:
                    cfg = json.loads(config_path.read_text(encoding="utf-8"))
                    llm_cfg = cfg.get("llm", {})
                    if isinstance(llm_cfg, dict):
                        provider = llm_cfg.get("provider", "")
                        model = llm_cfg.get("model", "")
                except (json.JSONDecodeError, OSError):
                    pass

            result.append({
                "id": agent_id,
                "name": agent_id,
                "model": model,
                "provider": provider,
                "has_soul": (entry / "SOUL.md").is_file(),
                "has_agents_md": (entry / "AGENTS.md").is_file(),
                "has_user_md": (entry / "USER.md").is_file(),
            })

        return result

    def ensure_default(self) -> None:
        """首次启动时确保 default agent 存在。从全局 config 生成 agent.config.json。"""
        default_dir = self._agents_dir / "default"
        if default_dir.exists():
            return

        self._agents_dir.mkdir(parents=True, exist_ok=True)
        default_dir.mkdir()

        # 从全局配置提取 provider/model/workspace
        global_defaults = self._global_data.get("agents", {}).get("defaults", {})
        default_cfg = {}
        if global_defaults.get("provider") and global_defaults.get("model"):
            default_cfg["llm"] = {
                "provider": global_defaults["provider"],
                "model": global_defaults["model"],
            }
        if global_defaults.get("workspace"):
            default_cfg["workspace"] = global_defaults["workspace"]

        (default_dir / "agent.config.json").write_text(
            json.dumps(default_cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 创建空模板提示词文件
        (default_dir / "SOUL.md").write_text(
            "# Default Agent\n\n这是默认 Agent。你可以在这里定义它的人设、语气和行为边界。\n",
            encoding="utf-8",
        )
        (default_dir / "AGENTS.md").write_text("", encoding="utf-8")
        (default_dir / "USER.md").write_text("", encoding="utf-8")

        logger.info(f"[agent-manager] 已创建默认 agent: {default_dir}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent_manager.py::test_load_default_agent_uses_global_config -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ftre/agent/agent_manager.py tests/test_agent_manager.py
git commit -m "feat: add AgentManager with config merge logic"
```

---

## Task 2: Tool filtering and per-agent config overrides

**Files:**
- Modify: `src/ftre/tools/__init__.py`
- Test: `tests/test_agent_manager.py`

**Interfaces:**
- Consumes: `AgentProfile.tools_config` from Task 1
- Produces: `filter_tools(all_tools: list[Tool], tools_config: dict | None) -> list[Tool]`

- [ ] **Step 1: Write the failing test for tool filtering and per-agent overrides**

```python
# Append to tests/test_agent_manager.py

def test_tool_filter_allow_deny():
    """filter_tools respects allow and deny lists."""
    from ftre.tools import filter_tools
    from ftre_agent_core.tool import Tool, ToolParameter

    tools = [
        Tool(name="bash", description="", parameters=[], func=lambda: ""),
        Tool(name="read", description="", parameters=[], func=lambda: ""),
        Tool(name="write", description="", parameters=[], func=lambda: ""),
        Tool(name="cron", description="", parameters=[], func=lambda: ""),
        Tool(name="mcp__playwright__browser_navigate", description="", parameters=[], func=lambda: ""),
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

    # Create a 'coder' agent with tool restrictions
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
    assert profile.llm.api_key == "sk-global"  # inherited
    assert profile.workspace == "/custom/workspace"
    assert profile.tools_config == {"allow": ["bash", "read", "write", "edit"], "deny": ["cron", "task", "send_message"]}


def test_load_agent_with_mcp_merge(tmp_agents_dir, fake_global_config):
    """Agent MCP config deep-merges with global."""
    from ftre.agent.agent_manager import AgentManager

    coder_dir = tmp_agents_dir / "coder"
    coder_dir.mkdir()
    (coder_dir / "agent.config.json").write_text(json.dumps({
        "mcp": {
            "playwright": {"disabled": True},  # override global
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
            {"name": "octo_channel", "enabled": False},  # disable global
            {"name": "my-plugin", "module": "my_plugin.MyPlugin", "config": {}},
        ],
    }), encoding="utf-8")

    mgr = AgentManager(agents_dir=tmp_agents_dir, global_config_data=fake_global_config)
    profile = mgr.load("coder")

    plugin_names = [p["name"] for p in profile.plugins_config]
    assert "octo_channel" in plugin_names
    assert "my-plugin" in plugin_names
    # octo_channel should be disabled
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
    # Should NOT contain global's "mcp-guide"
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

    # Create a second agent
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_agent_manager.py -v`
Expected: FAIL — `filter_tools` not found, and test assertions for overrides fail

- [ ] **Step 3: Add `filter_tools` to `src/ftre/tools/__init__.py`**

Add after the `ToolRegistry` class, before `build_default_tools`:

```python
def filter_tools(all_tools: list[Tool], tools_config: dict | None) -> list[Tool]:
    """按 agent 的 tools.allow / tools.deny 过滤工具列表。

    Args:
        all_tools: 内置工具 + 插件工具 + MCP 工具的完整列表
        tools_config: agent.config.json 的 tools 字段，格式为
                      {"allow": [...], "deny": [...]} 或 None

    Returns:
        过滤后的工具列表。tools_config 为 None 时返回原列表。
    """
    if not tools_config:
        return all_tools

    allow = set(tools_config.get("allow", []))
    deny = set(tools_config.get("deny", []))

    result = []
    for tool in all_tools:
        name = getattr(tool, "name", "")
        if name in deny:
            continue
        # allow 为空 = 不做白名单限制，只看 deny
        if allow and name not in allow:
            continue
        result.append(tool)
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_agent_manager.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/ftre/tools/__init__.py tests/test_agent_manager.py
git commit -m "feat: add filter_tools and per-agent config override tests"
```

---

## Task 3: Wire AgentManager into AgentLoop (agent creation delegated to AgentManager)

**Files:**
- Modify: `src/ftre/config.py` — add `AGENTS_DIR` constant
- Modify: `src/ftre/agent/loop.py` — `_run_async` resolves `metadata.agent_id`; `_create_agent` delegates to `agent_manager.create_agent()`
- Modify: `src/ftre/agent/agent_manager.py` — add `create_agent()` + `_compose_system_prompt()` methods (moved from AgentLoop)
- Modify: `src/ftre/plugin/hook_manager.py` — `MessagesBuildContext` gains `agent_dir` field

**Interfaces:**
- Consumes: `AgentManager.load(agent_id) -> AgentProfile` from Task 1, `filter_tools` from Task 2
- Produces: `AgentLoop` accepts `agent_manager` in constructor; `AgentManager.create_agent()` builds and returns a `ReActAgent`

**Design note:** `_create_agent` and `_compose_system_prompt` are moved from `AgentLoop` to `AgentManager`. `AgentLoop._create_agent` becomes a one-line delegation. This keeps `AgentLoop` focused on message dispatch/lifecycle, while `AgentManager` owns all agent construction logic.

- [ ] **Step 1: Add `AGENTS_DIR` constant to config.py**

In `src/ftre/config.py`, after the `CONFIG_PATH` line (line 16), add:

```python
# Agent 目录路径
AGENTS_DIR = CONFIG_PATH.parent / "agents"
```

- [ ] **Step 2: Add `agent_dir` field to `MessagesBuildContext`**

In `src/ftre/plugin/hook_manager.py`, add `agent_dir` to the `MessagesBuildContext` dataclass (after `workspace`):

```python
    agent_dir: str = ""
```

- [ ] **Step 3: Modify `AgentLoop.__init__` to accept `agent_manager`**

In `src/ftre/agent/loop.py`, add `agent_manager` parameter to `__init__`:

```python
    def __init__(
        self,
        bus: EventBus,
        session_manager: SessionManager,
        channel_manager=None,
        config: AgentConfig = None,
        hook_manager=None,
        tool_registry: ToolRegistry | None = None,
        command_manager=None,
        plugin_manager=None,
        agent_manager=None,
    ):
```

Add to the body after `self.plugin_manager = plugin_manager`:

```python
        self.agent_manager = agent_manager
```

- [ ] **Step 4: Modify `_run_async` to resolve agent_id and load AgentProfile**

In `src/ftre/agent/loop.py`, at the top of `_run_async` (after Step 1 入参校验, before Step 2 鉴权), add agent resolution:

```python
        # Step 1.5: 解析 agent_id，加载 per-agent 配置
        agent_id = (inbound.metadata or {}).get("agent_id", "") or "default"
        agent_profile = None
        if self.agent_manager is not None:
            agent_profile = self.agent_manager.load(agent_id)
```

Then modify Step 2.8 (关键路径压缩) to use agent_profile's config:

Replace the `config = self._load_current_config()` line in the compression section with:

```python
        config = self._load_current_config()
        # 如果有 per-agent 配置，覆盖 llm 和 workspace
        if agent_profile is not None:
            config = copy.deepcopy(config)
            config.llm = agent_profile.llm
            if agent_profile.workspace:
                config.workspace = agent_profile.workspace
```

Then modify Step 4 (加载历史消息) — after `workspace = session.get(...)`, use agent_profile workspace:

```python
        workspace = session.get("workspace", "") or os.getcwd()
        if agent_profile and agent_profile.workspace:
            workspace = agent_profile.workspace
```

Pass `agent_dir` to `_build_messages`:

```python
        messages, hook_config = await self._build_messages(
            session_id,
            content,
            attachments,
            config,
            inbound_data=inbound.data,
            channel_id=inbound.from_channel,
            workspace=workspace,
            agent_dir=(agent_profile.agent_dir if agent_profile else ""),
        )
```

- [ ] **Step 5: Modify `_build_messages` to pass `agent_dir` through hook context**

In `_build_messages`, add `agent_dir` parameter and pass to `MessagesBuildContext`:

```python
    async def _build_messages(
        self,
        session_id: str,
        content: str,
        attachments: list[dict],
        config: AgentConfig,
        *,
        inbound_data: dict | None = None,
        channel_id: str = "",
        workspace: str = "",
        agent_dir: str = "",
    ) -> tuple[list[dict], AgentConfig]:
```

In the hook trigger section:

```python
            ctx = MessagesBuildContext(
                session_id=session_id,
                channel_id=channel_id,
                inbound_data=inbound_data or {},
                workspace=workspace,
                agent_dir=agent_dir,
                config=hook_config,
                events=events,
            )
```

- [ ] **Step 6: Add `create_agent` and `_compose_system_prompt` to `AgentManager`**

In `src/ftre/agent/agent_manager.py`, add these imports at the top:

```python
import copy
import os
```

Add these methods to the `AgentManager` class (after `list_agents`):

```python
    def create_agent(
        self,
        profile: AgentProfile | None,
        config: AgentConfig,
        *,
        channel_manager=None,
        tool_registry=None,
        tracer=None,
        channel_id: str | None = None,
        session_id: str | None = None,
    ) -> "ReActAgent":
        """根据 AgentProfile + 全局 config 构建 ReActAgent。

        所有 agent 构建逻辑集中在此：LLM 覆盖 → 工具构建+过滤 → prompt 合成 → ReActAgent 实例化。
        """
        from ftre_agent_core.agent import ReActAgent
        from ftre.tools import build_default_tools, filter_tools

        c = copy.deepcopy(config)

        # 用 profile 的 llm 覆盖
        if profile is not None:
            c.llm = profile.llm

        # 构建 + 过滤工具
        tools = build_default_tools(
            channel_manager=channel_manager,
            tool_registry=tool_registry,
            llm_config=c.llm,
        )
        if profile is not None and profile.tools_config:
            tools = filter_tools(tools, profile.tools_config)

        # 合成 system prompt
        system_prompt = self._compose_system_prompt(
            c, profile, channel_id=channel_id, session_id=session_id
        )

        return ReActAgent(
            model=c.llm.model,
            api_key=c.llm.api_key,
            api_base=c.llm.api_base,
            api_type=c.llm.api_type,
            system_prompt=system_prompt,
            tools=tools,
            max_iterations=c.max_iterations,
            max_tokens=c.llm.max_output,
            tracer=tracer,
        )

    @staticmethod
    def _compose_system_prompt(
        config: AgentConfig,
        profile: AgentProfile | None,
        *,
        channel_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """合成最终 system prompt：全局 prompt + SOUL.md + USER.md + <env> 环境块。

        从 AgentLoop._compose_system_prompt 迁移而来，增加 per-agent 提示词注入。
        """
        c = config
        system_prompt = c.system_prompt

        # 追加 per-agent 提示词
        if profile is not None:
            if profile.soul_prompt:
                system_prompt = system_prompt + "\n\n" + profile.soul_prompt
            if profile.user_prompt_md:
                system_prompt = system_prompt + "\n\n" + profile.user_prompt_md

        env_lines = [
            "<FTRE_SYSTEM_FACT>",
            "<env>",
            f"channel_id={channel_id or ''}",
            f"session_id={session_id or ''}",
            f"os={os.name}",
        ]
        if os.name == "nt":
            env_lines.append(
                "当前是 Windows 系统。书写路径时优先使用正斜杠 /（如 "
                "C:/Users/name/AppData/Roaming/npm/x.cmd），Windows 下的命令与 "
                "Node/npm 系工具都能正确识别正斜杠，可彻底避免反斜杠转义问题。"
                "如果必须用反斜杠，在 JSON/字符串里务必写成双反斜杠 \\\\；切勿漏写，"
                "尤其当反斜杠后面跟 n、t、r 等字母时（如 \\npm、\\temp），单反斜杠会被"
                "当成换行/制表符等转义字符，导致路径被截断、命令执行失败。"
            )
        else:
            env_lines.append(
                "当前是类 Unix 系统（Linux/macOS）。路径使用正斜杠 /，区分大小写；"
                "优先使用绝对路径或 ~ 展开，避免依赖当前工作目录。"
            )
        if getattr(c.llm, "vision", False):
            env_lines.append(
                "vision=true：你当前使用的模型具备识图（视觉理解）能力，可以直接"
                "看懂图片、截图、浏览器画面和 UI 视觉状态，不要因为自己是文本模型"
                "而拒绝看图。需要理解视觉内容时，使用 read 工具读取图片文件。"
                "read 支持图片的绝对路径、相对当前工作区路径，以及 HTTP(S) 图片 URL；"
                "读取图片后可用于辅助修改 UI、判断浏览器操控结果、检查视觉回归和还原设计细节。"
            )
        env_lines.append("</env>")
        env_lines.append("</FTRE_SYSTEM_FACT>")

        return system_prompt + "\n\n" + "\n".join(env_lines)
```

- [ ] **Step 7: Replace `AgentLoop._create_agent` with delegation**

In `src/ftre/agent/loop.py`, replace the entire `_create_agent` method with:

```python
    def _create_agent(
        self,
        config: AgentConfig,
        *,
        channel_id: str | None = None,
        session_id: str | None = None,
        agent_profile=None,
    ) -> ReActAgent:
        """创建 ReActAgent 实例 — 委托给 AgentManager。"""
        if self.agent_manager is not None:
            return self.agent_manager.create_agent(
                profile=agent_profile,
                config=config,
                channel_manager=self.channel_manager,
                tool_registry=self.tool_registry,
                tracer=self.tracer,
                channel_id=channel_id,
                session_id=session_id,
            )
        # agent_manager 不存在时走旧逻辑（向后兼容）
        c = copy.deepcopy(config)
        tools = build_default_tools(
            channel_manager=self.channel_manager,
            tool_registry=self.tool_registry,
            llm_config=c.llm,
        )
        system_prompt = self._compose_system_prompt(
            c, channel_id=channel_id, session_id=session_id
        )
        return ReActAgent(
            model=c.llm.model,
            api_key=c.llm.api_key,
            api_base=c.llm.api_base,
            api_type=c.llm.api_type,
            system_prompt=system_prompt,
            tools=tools,
            max_iterations=c.max_iterations,
            max_tokens=c.llm.max_output,
            tracer=self.tracer,
        )
```

- [ ] **Step 8: Keep `_compose_system_prompt` on AgentLoop for backward compat**

The existing `AgentLoop._compose_system_prompt` stays as-is (used by the fallback path when `agent_manager is None`). No changes needed to it.

- [ ] **Step 9: Update the call site in `_run_async`**

In `_run_async`, update the `_create_agent` call (Step 5):

```python
        agent = self._create_agent(
            hook_config,
            channel_id=inbound.from_channel,
            session_id=session_id,
            agent_profile=agent_profile,
        )
```

- [ ] **Step 10: Verify compilation**

Run: `python -m py_compile src/ftre/agent/loop.py && python -m py_compile src/ftre/agent/agent_manager.py && python -m py_compile src/ftre/plugin/hook_manager.py && python -m py_compile src/ftre/config.py`
Expected: no output (success)

- [ ] **Step 11: Run existing tests to verify no regression**

Run: `python -m pytest tests/test_compact_algo.py tests/test_agent_manager.py -v`
Expected: all PASS

- [ ] **Step 12: Commit**

```bash
git add src/ftre/config.py src/ftre/agent/loop.py src/ftre/agent/agent_manager.py src/ftre/plugin/hook_manager.py
git commit -m "feat: delegate agent creation to AgentManager, per-message agent routing"
```

---

## Task 4: context_govern AGENTS.md per-agent injection

**Files:**
- Modify: `src/ftre/plugin/builtin/context_govern.py`

**Interfaces:**
- Consumes: `MessagesBuildContext.agent_dir` from Task 3
- Produces: `_inject_agents_md` reads AGENTS.md from agent_dir first, falls back to workspace

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_agent_manager.py

def test_context_govern_injects_agent_dir_agents_md(tmp_path):
    """context_govern reads AGENTS.md from agent_dir, not just workspace."""
    from ftre.plugin.builtin.context_govern import ContextGovernPlugin
    from ftre.plugin.hook_manager import MessagesBuildContext
    from ftre.config import AgentConfig

    # Create agent_dir with AGENTS.md
    agent_dir = tmp_path / "agents" / "coder"
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENTS.md").write_text("# Agent Rules\n\nUse Python 3.12.", encoding="utf-8")

    # Create workspace with a different AGENTS.md
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
    # Workspace AGENTS.md should NOT be injected when agent_dir has one
    assert "Workspace Rules" not in ctx.config.system_prompt


def test_context_govern_falls_back_to_workspace_agents_md(tmp_path):
    """When agent_dir has no AGENTS.md, fall back to workspace."""
    from ftre.plugin.builtin.context_govern import ContextGovernPlugin
    from ftre.plugin.hook_manager import MessagesBuildContext
    from ftre.config import AgentConfig

    agent_dir = tmp_path / "agents" / "coder"
    agent_dir.mkdir(parents=True)
    # No AGENTS.md in agent_dir

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_manager.py::test_context_govern_injects_agent_dir_agents_md tests/test_agent_manager.py::test_context_govern_falls_back_to_workspace_agents_md -v`
Expected: FAIL — `_inject_agents_md` doesn't check `agent_dir`

- [ ] **Step 3: Modify `_inject_agents_md` in `context_govern.py`**

Replace the existing `_inject_agents_md` method with:

```python
    def _inject_agents_md(self, ctx) -> None:
        """读取 AGENTS.md 并注入到 config.system_prompt。

        优先级：agent_dir/AGENTS.md > workspace/AGENTS.md。
        """
        import os

        # 优先从 agent_dir 读取
        agent_dir = (getattr(ctx, "agent_dir", "") or "").strip()
        agents_path = ""

        if agent_dir and os.path.isdir(agent_dir):
            candidate = os.path.join(agent_dir, "AGENTS.md")
            if os.path.isfile(candidate):
                agents_path = candidate

        # agent_dir 没有 → 回退 workspace
        if not agents_path:
            ws = (ctx.workspace or "").strip()
            if ws and os.path.isdir(ws):
                candidate = os.path.join(ws, "AGENTS.md")
                if os.path.isfile(candidate):
                    agents_path = candidate

        if not agents_path:
            return

        try:
            content = open(agents_path, encoding="utf-8").read().strip()
        except OSError:
            logger.warning(f"[context_govern] 无法读取 {agents_path}")
            return

        if not content:
            return

        current = (ctx.config.system_prompt or "").strip()
        ctx.config.system_prompt = (
            f"""{current}

<AGENTS_RULE desc="以下是用户在工作区自定义的规则与指令，你必须严格遵守" path="{agents_path}">
{content}
</AGENTS_RULE>"""
        )
        logger.info(f"[context_govern] 已注入 {agents_path} ({len(content)} chars)")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_agent_manager.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/ftre/plugin/builtin/context_govern.py tests/test_agent_manager.py
git commit -m "feat: context_govern reads AGENTS.md from agent_dir first"
```

---

## Task 5: GET /api/agents endpoint

**Files:**
- Modify: `src/ftre/api/routes.py`
- Modify: `src/ftre/main.py`

**Interfaces:**
- Consumes: `AgentManager.list_agents()` from Task 1
- Produces: `GET /api/agents` → `{"agents": [{id, name, model, provider, has_soul, has_agents_md, has_user_md}]}`

- [ ] **Step 1: Add `set_agent_manager` and the route to `routes.py`**

In `src/ftre/api/routes.py`, after the `set_command_manager` function (around line 54), add:

```python
_agent_manager = None


def set_agent_manager(mgr) -> None:
    """注入 AgentManager 实例（启动时调用）"""
    global _agent_manager
    _agent_manager = mgr
```

At the end of the file (after the `/images/{filename}` route), add:

```python
@router.get("/agents")
async def list_agents():
    """返回所有已注册的 agent 列表。"""
    if _agent_manager is None:
        return {"agents": []}
    return {"agents": _agent_manager.list_agents()}
```

- [ ] **Step 2: Wire AgentManager into `main.py`**

In `src/ftre/main.py`, after `config_data = load_config_file()` (line 145), add:

```python
    # Agent 管理器 — 加载 ~/.ftre/agents/ 下的 per-agent 配置
    from ftre.config import AGENTS_DIR
    from ftre.agent.agent_manager import AgentManager
    agent_manager = AgentManager(agents_dir=AGENTS_DIR, global_config_data=config_data)
    agent_manager.ensure_default()
```

After `set_command_manager(cmd)` (line 142), add:

```python
    from ftre.api.routes import set_agent_manager
    set_agent_manager(agent_manager)
```

In the `AgentLoop` constructor call (around line 161), add `agent_manager=agent_manager`:

```python
    agent_loop = AgentLoop(
        bus=bus,
        session_manager=session_manager,
        channel_manager=mgr,
        hook_manager=hook_manager,
        tool_registry=tool_registry,
        command_manager=cmd,
        plugin_manager=plugin_manager,
        agent_manager=agent_manager,
    )
```

- [ ] **Step 3: Verify compilation**

Run: `python -m py_compile src/ftre/api/routes.py && python -m py_compile src/ftre/main.py`
Expected: no output (success)

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/test_agent_manager.py tests/test_compact_algo.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/ftre/api/routes.py src/ftre/main.py
git commit -m "feat: add GET /api/agents endpoint and wire AgentManager into main"
```

---

## Task 6: Integration test and full pipeline verification

**Files:**
- Test: `tests/test_agent_manager.py`

- [ ] **Step 1: Write integration test for full dispatch flow**

```python
# Append to tests/test_agent_manager.py

def test_ensure_default_creates_agent_dir(tmp_path):
    """ensure_default() creates default/ with agent.config.json and md templates."""
    from ftre.agent.agent_manager import AgentManager

    agents_dir = tmp_path / "agents"
    # Don't create it yet
    mgr = AgentManager(agents_dir=agents_dir, global_config_data={
        "agents": {"defaults": {"provider": "openai", "model": "gpt-4o", "workspace": "/tmp"}},
    })

    mgr.ensure_default()

    default_dir = agents_dir / "default"
    assert default_dir.is_dir()
    assert (default_dir / "agent.config.json").is_file()
    assert (default_dir / "SOUL.md").is_file()
    assert (default_dir / "AGENTS.md").is_file()
    assert (default_dir / "USER.md").is_file()

    import json
    cfg = json.loads((default_dir / "agent.config.json").read_text(encoding="utf-8"))
    assert cfg["llm"]["provider"] == "openai"
    assert cfg["llm"]["model"] == "gpt-4o"
    assert cfg["workspace"] == "/tmp"


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
        "agents": {"defaults": {"provider": "openai", "model": "gpt-4o"}},
    })

    mgr.ensure_default()  # should NOT overwrite

    cfg = json.loads((default_dir / "agent.config.json").read_text(encoding="utf-8"))
    assert cfg["llm"]["provider"] == "anthropic"  # preserved


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
    assert profile.llm.api_key == "sk-ant-global"  # from global anthropic provider
    assert profile.llm.vision is True  # from global model entry
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

    # Should fall back to global default (openai/gpt-4o)
    assert profile.llm.model == "gpt-4o"
    assert profile.llm.api_key == "sk-global"
```

- [ ] **Step 2: Run all tests**

Run: `python -m pytest tests/test_agent_manager.py -v`
Expected: all PASS

- [ ] **Step 3: Run full test suite for regression check**

Run: `python -m pytest tests/ -v --timeout=30`
Expected: no new failures beyond pre-existing ones

- [ ] **Step 4: Commit**

```bash
git add tests/test_agent_manager.py
git commit -m "test: add integration tests for AgentManager lifecycle"
```

---

## Task 7 (Frontend): Wire AgentSelector to real GET /api/agents

**Repo:** `E:\binn\ftre-desktop\`
**Files:**
- Modify: `packages/renderer/src/services/api.ts` — replace mock `fetchChatAgents` with real `GET /api/agents`
- Modify: `packages/renderer/src/features/chat/AgentSelector.tsx` — adapt to new API response shape
- Modify: `packages/renderer/src/stores/chat.ts` — default `agentId` from `"code_agent"` to `"default"`

**Current state:** `AgentSelector.tsx` already exists and renders a dropdown. `fetchChatAgents()` is a hardcoded mock returning one `code_agent`. `chat.ts` already has `agentId` state + `setAgentId` + sends `agent_id` in WS `metadata`. The only missing piece is connecting to the real backend API.

**Interfaces:**
- Consumes: `GET /api/agents` from Task 5 → `{"agents": [{id, name, model, provider, has_soul, has_agents_md, has_user_md}]}`
- Produces: `AgentSelector` populated with real agent list; `sendMessage` already passes `agent_id` in WS metadata (no change needed)

- [ ] **Step 1: Replace `fetchChatAgents` and `ChatAgent` interface in `api.ts`**

In `packages/renderer/src/services/api.ts`, replace lines 841-861 (the `ChatAgent` interface + `fetchChatAgents` mock) with:

```typescript
export interface ChatAgent {
  id: string;
  name: string;
  model?: string;
  provider?: string;
  has_soul?: boolean;
  has_agents_md?: boolean;
  has_user_md?: boolean;
  is_builtin?: boolean;
  tools?: string[];
}

export async function fetchChatAgents(
  _workspace?: string | null,
): Promise<ChatAgent[]> {
  try {
    const res = await fetch(`${API_BASE}/api/agents`);
    if (!res.ok) return [{ id: "default", name: "Default", is_builtin: true }];
    const data = await res.json();
    const agents: ChatAgent[] = (data.agents || []).map((a: any) => ({
      id: a.id,
      name: a.name || a.id,
      model: a.model,
      provider: a.provider,
      has_soul: a.has_soul,
      has_agents_md: a.has_agents_md,
      has_user_md: a.has_user_md,
      is_builtin: a.id === "default",
    }));
    return agents.length > 0 ? agents : [{ id: "default", name: "Default", is_builtin: true }];
  } catch {
    return [{ id: "default", name: "Default", is_builtin: true }];
  }
}
```

- [ ] **Step 2: Update `AgentSelector.tsx` to remove workspace dependency and adapt filtering**

In `packages/renderer/src/features/chat/AgentSelector.tsx`:

1. Remove the `useWorkspace` import and `workspace` state (line 14, 22) — agent list is now global, not per-workspace.

2. Change the fetch effect (line 38-42) to not depend on workspace:

```tsx
  // 每次展开时重新请求 agent 列表
  useEffect(() => {
    if (open) {
      fetchChatAgents().then(setAgents);
    }
  }, [open]);
```

3. Remove the workspace reset effect (lines 33-35):

```tsx
  // workspace 变化时重置选中 — 删除这整个 useEffect
```

4. Change the agent filtering (lines 58-61) — remove the `send_email` filter, show all non-builtin agents:

```tsx
  const builtinAgents = agents.filter((a) => a.is_builtin);
  const customAgents = agents.filter((a) => !a.is_builtin);
```

- [ ] **Step 3: Change default `agentId` in `chat.ts`**

In `packages/renderer/src/stores/chat.ts`, line 1030:

```typescript
  agentId: "default",
```

Also update test fixtures that reference `"code_agent"`:
- `packages/renderer/src/stores/chat.test.ts` line 33: `agentId: "default"`
- `packages/renderer/src/stores/session.test.ts` line 64: `agentId: "default"`
- `packages/renderer/src/stores/session.test.ts` line 144: `"default"` (or the appropriate test agent)
- `packages/renderer/src/test/performance-verification.test.ts` line 42: `agentId: "default"`

- [ ] **Step 4: Verify TypeScript compiles**

Run: `cd packages/renderer && npx tsc --noEmit 2>&1 | grep -i "AgentSelector\|api.ts\|chat.ts" || echo "No errors in target files"`
Expected: no errors in the modified files (pre-existing errors in other files are OK)

- [ ] **Step 5: Commit**

```bash
cd E:\binn\ftre-desktop
git add packages/renderer/src/services/api.ts packages/renderer/src/features/chat/AgentSelector.tsx packages/renderer/src/stores/chat.ts packages/renderer/src/stores/chat.test.ts packages/renderer/src/stores/session.test.ts packages/renderer/src/test/performance-verification.test.ts
git commit -m "feat: wire AgentSelector to real GET /api/agents endpoint"
```

---

## Self-Review

### 1. Spec Coverage

| Spec Requirement | Task |
|-----------------|------|
| `~/.ftre/agents/<name>/agent.config.json` with llm, tools, workspace, mcp, plugins, disabled_skills | Task 1 |
| SOUL.md / AGENTS.md / USER.md prompt files | Task 1 (load) + Task 3 (inject) + Task 4 (context_govern) |
| Config inheritance: llm field-level, mcp deep-merge, plugins by-name, disabled_skills replace | Task 1 + Task 2 tests |
| `metadata.agent_id` per-message routing, default fallback | Task 3 |
| `tools.allow`/`deny` filters built-in + MCP + plugin tools | Task 2 |
| `GET /api/agents` endpoint | Task 5 |
| `ensure_default()` on startup | Task 1 (method) + Task 5 (wired into main.py) |
| agent.config.json llm only has provider+model, rest from global | Task 1 + Task 6 tests |
| Frontend: `fetchChatAgents` calls real `GET /api/agents` | Task 7 |
| Frontend: `AgentSelector` populated from backend agent list | Task 7 |
| Frontend: `sendMessage` passes `metadata.agent_id` (already exists) | Already done in `chat.ts:1096` |

### 2. Placeholder Scan

No placeholders found — all code blocks contain complete implementations.

### 3. Type Consistency

- `AgentProfile` fields: `agent_id`, `llm`, `workspace`, `tools_config`, `mcp_config`, `plugins_config`, `disabled_skills`, `soul_prompt`, `user_prompt_md`, `agents_md`, `agent_dir` — consistent across all tasks
- `filter_tools(all_tools, tools_config)` — used in Task 3 via `from ftre.tools import filter_tools`
- `MessagesBuildContext.agent_dir` — added in Task 2, consumed in Task 4
- `AgentManager.__init__(agents_dir, global_config_data)` — used consistently in Task 5 and tests

### 4. Architecture Notes

**What this plan does NOT change (by design):**

- MCP connections remain global — `McpManager` connects to all servers at startup. The `mcp_config` on `AgentProfile` is available for future per-agent MCP filtering, but the current plan does not implement per-agent MCP connection management (that would require significant McpManager refactoring). Tools from MCP servers are filtered via `tools.allow`/`deny` at the `filter_tools` level.
- Plugin loading remains global — `PluginManager.load_all()` runs once at startup. The `plugins_config` on `AgentProfile` is available for future per-agent plugin enable/disable, but the current plan does not implement per-agent plugin loading.
- `disabled_skills` on `AgentProfile` is available but the `SkillPlugin` currently reads `disabled_skills` from `config.json` at setup time. Making it per-agent would require the skill plugin to become agent-aware. This is left for a future iteration.
- `context` (compaction config) remains global — not part of `agent.config.json`.
- `title_llm` / `compact_llm` remain global — not per-agent.

**Per-agent MCP config in `agent.config.json`:** The `mcp` field is merged and stored on `AgentProfile.mcp_config`, but it is NOT yet consumed by `McpManager`. This means the `mcp` field in `agent.config.json` currently serves as documentation/metadata only. A future task can wire `AgentProfile.mcp_config` into `McpManager` for per-agent MCP connections. The tool-level filtering via `tools.allow`/`deny` is the active mechanism for controlling which MCP tools an agent can use.
