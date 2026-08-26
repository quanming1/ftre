"""团队成员 AgentProfile 落盘的唯一 owner。

团队成员的 profile 与全局 agent（~/.ftre/agents/<id>/）完全同构，落盘在
leader session 目录下：

    ~/.ftre/sessions/<leader_session_id>/sub_agents/<member_session_id>/
    ├── AGENTS.md           # 成员角色定义 + 成员约束
    ├── agent.config.json   # 可选覆盖：llm / tools / disabled_skills / mcp
    ├── SOUL.md             # 可选，手工添加
    └── USER.md             # 可选，手工添加

目录布局、读写、加载、删除以及成员绑定（metadata['team_member']）的形状
知识全部收口在本模块；tools/team.py、agent/turn_executor.py、
plugin/builtin/team_plugin.py 都是纯消费方。
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ftre.services.agent.profile.manager import AgentProfile
    from ftre.services.session import SessionService

logger = logging.getLogger(__name__)

SUB_AGENTS_DIRNAME = "sub_agents"
MEMBER_AGENTS_MD = "AGENTS.md"
MEMBER_CONFIG_JSON = "agent.config.json"

# 成员硬编码约束：写入成员 AGENTS.md 尾部，避免无限嵌套
MEMBER_CONSTRAINT = (
    "你是一个团队成员 agent，专注完成 leader 交给你的任务。"
    "你不能创建或管理子团队（team 工具在成员内被禁用）。"
    "完成任务后在最后一条消息里清晰总结你的产出或结论。"
)

# metadata['team_member'] 绑定：成员 session 的结构性身份标记，
# 形状对齐 metadata['external'] 的既有先例。
_BINDING_KEYS = ("leader_session", "team_id", "name", "created_at")


# ── 路径 ──────────────────────────────────────────────────────

def profile_dir_of(
    session_manager: SessionService,
    leader_session_id: str,
    member_session_id: str,
) -> Path:
    """成员 profile 目录（session_manager 内部含 id 校验与越界检查）。"""
    return (
        session_manager.session_dir(leader_session_id)
        / SUB_AGENTS_DIRNAME
        / member_session_id
    )


# ── 校验与写入 ────────────────────────────────────────────────

def validate_profile_overrides(profile: dict) -> dict:
    """纯校验 profile 覆盖字段，返回将写入 agent.config.json 的 cfg。

    非法抛 ValueError；不产生任何磁盘副作用。
    """
    cfg: dict = {}
    llm = {
        key: profile[key].strip()
        for key in ("provider", "model", "reasoning_effort")
        if isinstance(profile.get(key), str) and profile[key].strip()
    }
    if llm:
        cfg["llm"] = llm

    tools = profile.get("tools")
    if tools is not None:
        if not isinstance(tools, dict):
            raise ValueError('profile.tools 必须是 {"allow": [...], "deny": [...]} 对象')
        unknown = set(tools) - {"allow", "deny"}
        if unknown:
            raise ValueError(f"profile.tools 含未知字段: {sorted(unknown)}")
        from ftre.services.tools import coerce_tool_name_list
        for field in ("allow", "deny"):
            if field in tools:
                tools[field] = coerce_tool_name_list(tools[field], field)
        cfg["tools"] = tools

    disabled = profile.get("disabled_skills")
    if disabled is not None:
        if not isinstance(disabled, list) or not all(
            isinstance(x, str) and x.strip() for x in disabled
        ):
            raise ValueError("profile.disabled_skills 必须是非空字符串列表")
        cfg["disabled_skills"] = disabled

    mcp = profile.get("mcp")
    if mcp is not None:
        if not isinstance(mcp, dict):
            raise ValueError("profile.mcp 必须是对象（格式同全局 config.json 的 mcp 段）")
        cfg["mcp"] = mcp
    return cfg


def write_member_profile(
    session_manager: SessionService,
    leader_session_id: str,
    member_session_id: str,
    role: str,
    overrides: dict,
) -> Path:
    """落盘成员 profile：AGENTS.md（role + 成员约束）+ agent.config.json。

    先全量校验后写盘。抛 OSError/ValueError，由调用方回滚。返回 profile 目录。
    """
    cfg = validate_profile_overrides(overrides)
    base_dir = profile_dir_of(session_manager, leader_session_id, member_session_id)
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / MEMBER_AGENTS_MD).write_text(
        f"{role.strip()}\n\n{MEMBER_CONSTRAINT}\n", encoding="utf-8"
    )
    if cfg:
        (base_dir / MEMBER_CONFIG_JSON).write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return base_dir


# ── 加载 ──────────────────────────────────────────────────────

def load_member_profile(
    session_manager: SessionService,
    leader_session_id: str,
    member_session_id: str,
) -> AgentProfile | None:
    """加载成员 AgentProfile，走与全局 agent 相同的 AgentManager 管线。

    成员目录不存在（团队已解散/目录被清理）时返回 None，由调用方退回
    全局 agent 解析——绝不返回空壳 profile 污染 config.llm。
    """
    try:
        base_dir = profile_dir_of(session_manager, leader_session_id, member_session_id)
        if not (base_dir / MEMBER_AGENTS_MD).is_file():
            logger.warning(
                "[sub-agent-profile] 成员 profile 不存在，退回全局 default: "
                "leader=%s member=%s",
                leader_session_id, member_session_id,
            )
            return None
        from ftre.services.agent.profile.manager import AgentManager
        from ftre.services.config.paths import AGENTS_DIR

        return AgentManager(
            base_dir.parent, fallback_agents_dir=AGENTS_DIR
        ).load(member_session_id, strict=True)
    except Exception:
        logger.exception(
            "[sub-agent-profile] 加载成员 profile 失败 leader=%s member=%s",
            leader_session_id, member_session_id,
        )
        return None


# ── 删除 ──────────────────────────────────────────────────────

def delete_member_profile(
    session_manager: SessionService,
    leader_session_id: str,
    member_session_id: str,
) -> bool:
    """删除单个成员的 profile 目录；返回目录原先是否存在。"""
    try:
        base_dir = profile_dir_of(session_manager, leader_session_id, member_session_id)
    except ValueError:
        return False
    if not base_dir.exists():
        return False
    shutil.rmtree(base_dir, ignore_errors=True)
    return True


def delete_all_profiles(session_manager: SessionService, leader_session_id: str) -> bool:
    """删除 leader 的整棵 sub_agents 树；返回目录原先是否存在。"""
    try:
        root = session_manager.session_dir(leader_session_id) / SUB_AGENTS_DIRNAME
    except ValueError:
        return False
    if not root.exists():
        return False
    shutil.rmtree(root, ignore_errors=True)
    return True


# ── 成员绑定（metadata['team_member']）───────────────────────

def build_team_member_binding(leader_session_id: str, team_id: str, name: str) -> dict:
    """构造写入成员 session metadata['team_member'] 的绑定结构。"""
    return {
        "leader_session": leader_session_id,
        "team_id": team_id,
        "name": name,
        "created_at": datetime.now(UTC).isoformat(),
    }


def binding_of(session_metadata: dict) -> dict | None:
    """从 session metadata 提取并校验 team_member 绑定。

    形状不合法（非 dict / leader_session 非空字符串）返回 None。
    """
    binding = session_metadata.get("team_member")
    if not isinstance(binding, dict):
        return None
    leader = binding.get("leader_session")
    if not isinstance(leader, str) or not leader:
        return None
    return binding
