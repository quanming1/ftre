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
  - workspace: Agent 的"家目录"（存放 SOUL/AGENTS/USER.md 的路径，不是对话 cwd）
  - mcp: 深度合并（按 server name 为 key）
  - plugins: 按 name 合并（同名 agent 覆盖全局，全局有但 agent 没提的保留）
  - disabled_skills: 整体替换
"""
from __future__ import annotations

import json
import logging
import os
import copy
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ftre.config import AgentConfig, LLMConfig, _build_llm_config, load_config_file

logger = logging.getLogger(__name__)


@dataclass
class AgentProfile:
    """一个 agent 的完整运行时配置（已与全局合并）。"""
    agent_id: str = "default"
    name: str = ""
    llm: LLMConfig = field(default_factory=LLMConfig)
    workspace: str = ""                    # Agent 的"家目录"（存放 prompt 文件的路径，不是对话 cwd）
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

    def __init__(self, agents_dir: Path):
        self._agents_dir = Path(agents_dir)

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

        # 每次加载 Agent 都读取最新的供应商模型配置，确保 vision 等模型属性不使用启动时快照。
        global_data = load_config_file()
        profile = self._load_and_merge(agent_id, agent_dir, global_data)
        return profile

    def _load_and_merge(
        self, agent_id: str, agent_dir: Path, global_data: dict
    ) -> AgentProfile:
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
        # 全局兜底：从 default agent 的 agent.config.json 读取
        global_provider, global_model, global_workspace = self._read_default_agent_llm()

        agent_llm = agent_cfg.get("llm", {})
        if not isinstance(agent_llm, dict):
            agent_llm = {}

        provider = agent_llm.get("provider", "") or global_provider
        model = agent_llm.get("model", "") or global_model

        llm = _build_llm_config(global_data, provider, model)
        if not llm.model:
            # agent 指定的 provider/model 在全局找不到 → 回退全局默认
            logger.warning(
                f"[agent-manager] agent '{agent_id}' 的 provider={provider} model={model} "
                f"在全局配置中找不到，回退到全局默认"
            )
            llm = _build_llm_config(global_data, global_provider, global_model)

        # ─── 合并 workspace ─────────────────────────────────
        workspace = agent_cfg.get("workspace", "") or global_workspace or ""
        if not isinstance(workspace, str):
            workspace = ""

        # ─── 合并 tools ─────────────────────────────────────
        tools_config = agent_cfg.get("tools")
        if tools_config is not None and not isinstance(tools_config, dict):
            tools_config = None

        # ─── 合并 MCP（深度合并） ───────────────────────────
        global_mcp = global_data.get("mcp", {})
        agent_mcp = agent_cfg.get("mcp", {})
        if not isinstance(agent_mcp, dict):
            agent_mcp = {}
        if not isinstance(global_mcp, dict):
            global_mcp = {}
        merged_mcp = {**global_mcp, **agent_mcp}

        # ─── 合并 plugins（按 name 合并） ───────────────────
        global_plugins = global_data.get("plugins", [])
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
            disabled_skills = global_data.get("disabled_skills", [])
            if not isinstance(disabled_skills, list):
                disabled_skills = []

        # ─── 读取 name ─────────────────────────────────────
        name = agent_cfg.get("name", "") or agent_id
        if not isinstance(name, str):
            name = agent_id

        # ─── 读取提示词文件 ─────────────────────────────────
        soul_prompt = self._read_md(agent_dir / "SOUL.md")
        user_prompt_md = self._read_md(agent_dir / "USER.md")
        agents_md = self._read_md(agent_dir / "AGENTS.md")

        return AgentProfile(
            agent_id=agent_id,
            name=name,
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
                result.append(agent_by_name[name])
                seen_names.add(name)
            else:
                result.append(p)
                seen_names.add(name)

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

            # 用 load() 获取合并后的完整配置
            profile = self.load(agent_id)

            model = profile.llm.model if profile.llm else ""
            provider = ""
            # 从 agent.config.json 原始数据拿 provider
            config_path = entry / "agent.config.json"
            if config_path.is_file():
                try:
                    cfg = json.loads(config_path.read_text(encoding="utf-8"))
                    llm_cfg = cfg.get("llm", {})
                    if isinstance(llm_cfg, dict):
                        provider = llm_cfg.get("provider", "")
                except (json.JSONDecodeError, OSError):
                    pass

            # 工具权限
            tools_allow: list | None = None
            tools_deny: list | None = None
            if profile.tools_config:
                allow = profile.tools_config.get("allow", [])
                deny = profile.tools_config.get("deny", [])
                if allow:
                    tools_allow = allow
                if deny:
                    tools_deny = deny

            # MCP 连接
            mcp_servers = list(profile.mcp_config.keys()) if profile.mcp_config else []

            result.append({
                "id": agent_id,
                "name": profile.name or agent_id,
                "model": model,
                "provider": provider,
                "workspace": profile.workspace,
                "tools_allow": tools_allow,
                "tools_deny": tools_deny,
                "mcp_servers": mcp_servers,
                "has_soul": (entry / "SOUL.md").is_file(),
                "has_agents_md": (entry / "AGENTS.md").is_file(),
                "has_user_md": (entry / "USER.md").is_file(),
            })

        return result

    def update_agent(self, agent_id: str, patch: dict) -> dict:
        """更新 agent.config.json 的字段（支持 llm、name、workspace）。

        Args:
            agent_id: agent ID
            patch: 可包含以下字段:
                - {"llm": {"provider": "...", "model": "..."}}
                - {"name": "..."}
                - {"workspace": "..."}

        Returns:
            更新后的 agent.config.json 内容
        """
        agent_dir = self._agents_dir / agent_id
        if not agent_dir.is_dir():
            raise FileNotFoundError(f"agent '{agent_id}' 不存在")

        config_path = agent_dir / "agent.config.json"

        # 读取现有配置
        cfg: dict = {}
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"[agent-manager] 读取 {config_path} 失败: {e}")

        # 合并 patch
        if "llm" in patch and isinstance(patch["llm"], dict):
            existing_llm = cfg.get("llm", {})
            if not isinstance(existing_llm, dict):
                existing_llm = {}
            existing_llm.update(patch["llm"])
            cfg["llm"] = existing_llm

        if "name" in patch and isinstance(patch["name"], str):
            cfg["name"] = patch["name"]
        if "workspace" in patch and isinstance(patch["workspace"], str):
            cfg["workspace"] = patch["workspace"]

        # 写回
        config_path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 清除缓存
        logger.info(f"[agent-manager] 已更新 agent '{agent_id}' 的配置: {patch}")
        return cfg

    def create_agent_profile(
        self,
        agent_id: str,
        name: str = "",
        llm_provider: str = "",
        llm_model: str = "",
        workspace: str = "",
    ) -> dict:
        """创建一个新 agent。

        Args:
            agent_id: agent ID（用作目录名，必须合法）
            name: 显示名称
            llm_provider: LLM provider 名称
            llm_model: LLM model ID
            workspace: 工作区路径

        Returns:
            创建后的 agent.config.json 内容

        Raises:
            ValueError: agent_id 非法或已存在
        """
        # 验证 agent_id
        if not agent_id or not isinstance(agent_id, str):
            raise ValueError("agent_id 不能为空")
        if not all(c.isalnum() or c in "-_" for c in agent_id):
            raise ValueError("agent_id 只能包含字母、数字、连字符和下划线")

        agent_dir = self._agents_dir / agent_id
        if agent_dir.exists():
            raise ValueError(f"agent '{agent_id}' 已存在")

        agent_dir.mkdir(parents=True)

        cfg: dict = {"id": agent_id}
        if name:
            cfg["name"] = name
        if llm_provider and llm_model:
            cfg["llm"] = {"provider": llm_provider, "model": llm_model}
        if workspace:
            cfg["workspace"] = workspace

        (agent_dir / "agent.config.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 创建空的 prompt 文件
        (agent_dir / "SOUL.md").write_text("", encoding="utf-8")
        (agent_dir / "AGENTS.md").write_text("", encoding="utf-8")
        (agent_dir / "USER.md").write_text("", encoding="utf-8")

        logger.info(f"[agent-manager] 已创建 agent: {agent_dir}")
        return cfg

    def delete_agent(self, agent_id: str) -> None:
        """删除一个 agent。不允许删除 default。

        Raises:
            ValueError: 尝试删除 default agent
            FileNotFoundError: agent 不存在
        """
        if agent_id == "default":
            raise ValueError("不允许删除 default agent")

        agent_dir = self._agents_dir / agent_id
        if not agent_dir.is_dir():
            raise FileNotFoundError(f"agent '{agent_id}' 不存在")

        import shutil
        shutil.rmtree(agent_dir)

        # 清除缓存
        logger.info(f"[agent-manager] 已删除 agent: {agent_id}")

    def _read_default_agent_llm(self) -> tuple[str, str, str]:
        """读取 default agent 的 llm provider/model 和 workspace。

        全局兜底配置的单一事实源——其他 agent 未指定 llm 时回退到 default agent。
        """
        cfg_path = self._agents_dir / "default" / "agent.config.json"
        if not cfg_path.exists():
            return "", "", ""
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            llm = cfg.get("llm", {})
            if not isinstance(llm, dict):
                llm = {}
            workspace = cfg.get("workspace", "")
            if not isinstance(workspace, str):
                workspace = ""
            return llm.get("provider", ""), llm.get("model", ""), workspace
        except (json.JSONDecodeError, OSError):
            return "", "", ""

    # ─── 默认 agent 内置提示词 ─────────────────────────────

    _DEFAULT_SOUL = "你是 ftre，一个 AI 编程助手。"

    _DEFAULT_AGENTS = """\
## 沟通

- 输出文本用于和用户交流；所有非工具调用的输出都会展示给用户。
- 只使用工具完成任务，不要用 Bash 或代码注释等工具作为会话中和用户沟通的方式。
- 除非用户明确要求，否则不要使用 emoji。

## 工作方式

- 只在用户要求做事时才动手；用户问"怎么做"先回答，别直接改代码。
- 把请求做完整，包括必要的后续动作，但不做用户没要求的额外动作；改完直接停，不要附带解释或总结。
- 动手前先读周围上下文（尤其 imports），摸清该文件和项目的约定、风格、已用的库与模式，然后照着做。
- 不假设某个库可用——用之前先确认项目已依赖它（看相邻文件、package.json、cargo.toml 等）。
- 不引入或记录 secrets、keys，更不提交进仓库。
- 同一问题反复改不好就停下，回到最初假设、复现路径和失败证据重新判断，换方向，别钻牛角尖。
- 收尾前通读改过的文件，确认连贯、无语法错误和残留调试代码。

## 临时文件与工作区

- 尊重工作区文件：工作区中的文件是用户的核心资产，改动前先读取确认内容，不要盲目覆盖或删除已有文件。
- 不污染工作区：调试脚本、测试输出、临时图片等与当前任务无关的文件一律不要写入工作区目录。
- 按需创建文件：在工作区中新建文件前，确认它是用户明确要求或项目结构隐含必需的（如项目约定的标准目录、配置文件模板等）；不要创建"看起来有用"但用户未提及的示例、说明、辅助脚本等文件。
- 临时文件统一放临时目录：交换文件、中间产物、截图等所有临时文件存放到系统临时目录——Windows 用 `%TEMP%`，Linux/macOS 用 `/tmp`（或 `$TMPDIR`）；写代码时用各语言的临时目录 API（如 Python `tempfile`）获取，不要硬编码路径。
- 用完即清：任务结束前删掉自己创建的临时文件，不要遗留。
"""

    def ensure_default(self) -> None:
        """首次启动时确保 default agent 存在。"""
        default_dir = self._agents_dir / "default"

        if default_dir.exists():
            return

        self._agents_dir.mkdir(parents=True, exist_ok=True)
        default_dir.mkdir()

        # agent.config.json — 从 providers 中选第一个可用 provider/model
        default_cfg: dict = {
            "id": "default",
            "name": "Ftre",
        }

        providers = load_config_file().get("providers", {})
        if isinstance(providers, dict) and providers:
            first_name = next(iter(providers))
            first_provider = providers[first_name]
            if isinstance(first_provider, dict):
                models = first_provider.get("models", [])
                if isinstance(models, list) and models:
                    first_model = models[0]
                    if isinstance(first_model, dict) and first_model.get("id"):
                        default_cfg["llm"] = {
                            "provider": first_name,
                            "model": first_model["id"],
                        }

        (default_dir / "agent.config.json").write_text(
            json.dumps(default_cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (default_dir / "SOUL.md").write_text(self._DEFAULT_SOUL, encoding="utf-8")
        (default_dir / "AGENTS.md").write_text(self._DEFAULT_AGENTS, encoding="utf-8")
        (default_dir / "USER.md").write_text("", encoding="utf-8")

        logger.info(f"[agent-manager] 已创建默认 agent: {default_dir}")

    # ─── Prompt 文件读写 ───────────────────────────────────────

    _PROMPT_FILES = ("SOUL.md", "AGENTS.md", "USER.md")

    def read_prompts(self, agent_id: str) -> dict[str, str]:
        """读取 agent 的三个 prompt 文件内容。agent_id 不存在时回退到 default。"""
        agent_dir = self._agents_dir / agent_id
        if not agent_dir.is_dir():
            agent_dir = self._agents_dir / "default"
        if not agent_dir.is_dir():
            return {f: "" for f in self._PROMPT_FILES}

        result = {}
        for fname in self._PROMPT_FILES:
            result[fname] = self._read_md(agent_dir / fname)
        return result

    def write_prompt(self, agent_id: str, filename: str, content: str) -> None:
        """写入 agent 的指定 prompt 文件。agent_id 不存在时回退到 default。"""
        if filename not in self._PROMPT_FILES:
            raise ValueError(f"不支持的 prompt 文件: {filename}，仅支持 {self._PROMPT_FILES}")

        agent_dir = self._agents_dir / agent_id
        if not agent_dir.is_dir():
            agent_dir = self._agents_dir / "default"
        if not agent_dir.is_dir():
            raise FileNotFoundError(f"agent '{agent_id}' 不存在且 default 也不存在")

        filepath = agent_dir / filename
        logger.info(f"[agent-manager] 已写入 {filepath} ({len(content)} chars)")

    # ─── Agent 构建（委托自 AgentLoop） ────────────────────

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
        hook_manager=None,
    ):
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
        registry = build_default_tools(
            channel_manager=channel_manager,
            tool_registry=tool_registry,
            llm_config=c.llm,
        )
        if profile is not None and profile.tools_config:
            filter_tools(registry, profile.tools_config)

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
            tool_registry=registry,
            max_iterations=c.max_iterations,
            max_tokens=c.llm.max_output,
            tracer=tracer,
            hook_manager=hook_manager,
        )
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
                soul_path = f"{profile.agent_dir}/SOUL.md" if profile.agent_dir else ""
                system_prompt = (
                    system_prompt + "\n\n"
                    f'<SOUL desc="智能体人设：角色定义、语气、行为边界" path="{soul_path}">\n'
                    f"{profile.soul_prompt}\n"
                    f"</SOUL>"
                )
            if profile.user_prompt_md:
                user_path = f"{profile.agent_dir}/USER.md" if profile.agent_dir else ""
                system_prompt = (
                    system_prompt + "\n\n"
                    f'<USER_PROFILE desc="用户偏好与个人要求" path="{user_path}">\n'
                    f"{profile.user_prompt_md}\n"
                    f"</USER_PROFILE>"
                )

        env_lines = [
            "<FTRE_SYSTEM_FACT>",
            "<env>",
            f"channel_id={channel_id or ''}",
            f"session_id={session_id or ''}",
            f"os={os.name}",
            f"date={date.today().isoformat()}",
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
