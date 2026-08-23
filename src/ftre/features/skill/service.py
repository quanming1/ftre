"""Skill catalog combining runtime, workspace, Agent and global sources."""
# SkillService：按优先级合并多个技能来源，返回确定性结果且不修改源文件。
# 来源优先级（数值越小越优先）：工作区(10) > Agent(20) > 全局(30)，
# 另有运行时注册（register()，owner 可自定义）作为最高优先候选。
# 所有读取都是只读的：不缓存内容、不改写 .md 文件。

from __future__ import annotations

from pathlib import Path
from typing import Any

from .types import SkillRecord


class SkillService:
    """Resolve deterministic skill winners without mutating source files."""
    key = "skills"

    def __init__(self, roots: dict[str, Path] | None = None) -> None:
        # roots：可注入的目录映射（测试用）；默认全局技能目录 ~/.ftre/skills
        self.roots = roots or {"global": Path.home() / ".ftre" / "skills"}
        # 运行时注册的技能（Feature/测试动态加入，不落盘）
        self._runtime: list[SkillRecord] = []
        # session 加载记录：哪个来源给哪个 session 提供了哪个技能（用于诊断）
        self._loaded: dict[tuple[str, str], str] = {}

    def register(self, skill: SkillRecord, owner: str, scope: str = "global"):
        """Add a runtime contribution and return its idempotent disposer."""
        # 追加运行时记录并返回幂等 disposer：重复调用 dispose 只生效一次
        record = SkillRecord(skill.name, skill.content, owner, skill.source, skill.priority, scope)
        self._runtime.append(record)
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            try:
                self._runtime.remove(record)
            except ValueError:
                return False
            return True

        return dispose

    def list(self, agent_id: str = "default", workspace: str | None = None) -> list[dict[str, Any]]:
        """Return winning skill metadata for an Agent/workspace scope."""
        # 只返回元数据（不含正文），供列表/诊断 API 使用
        records = self._resolve(agent_id, workspace)
        return [{"name": item.name, "owner": item.owner, "source": item.source, "scope": item.scope, "priority": item.priority} for item in records]

    def get(self, name: str, agent_id: str = "default", workspace: str | None = None) -> SkillRecord | None:
        """Return the highest-priority skill with the requested name."""
        # 从已决出 winner 的列表里按名字查找
        return next((item for item in self._resolve(agent_id, workspace) if item.name == name), None)

    def sources(self, name: str, agent_id: str = "default", workspace: str | None = None) -> dict[str, Any]:
        """Expose all candidates so callers can explain shadowing decisions."""
        # 诊断用：列出同名技能的全部候选、winner 与被遮蔽者，方便排查"为什么加载的是这个版本"
        candidates = [item for item in self._all(agent_id, workspace) if item.name == name]
        ordered = sorted(candidates, key=lambda item: (item.priority, item.owner))
        return {"candidates": [item.__dict__ for item in ordered], "winner": ordered[0].owner if ordered else None, "shadowed": [item.owner for item in ordered[1:]]}

    def mark_loaded(self, session_id: str, name: str, source: str) -> None:
        """Record which source supplied a skill to a session's runtime."""
        self._loaded[(session_id, name)] = source

    def _all(self, agent_id: str, workspace: str | None) -> list[SkillRecord]:
        """Collect runtime records plus every file from each source root."""
        result = list(self._runtime)
        roots = []
        if workspace:
            # 工作区级：<workspace>/.ftre/skills，优先级最高（10）
            roots.append((Path(workspace) / ".ftre" / "skills", 10, f"workspace:{workspace}"))
        # Agent 级：~/.ftre/agents/<id>/skills（20）
        roots.append((self.roots.get("agent", Path.home() / ".ftre" / "agents" / agent_id / "skills"), 20, f"agent:{agent_id}"))
        # 全局级：~/.ftre/skills（30）
        roots.append((self.roots.get("global", Path.home() / ".ftre" / "skills"), 30, "global"))
        for root, priority, scope in roots:
            if not root.is_dir():
                continue
            # 递归扫 .md：SKILL.md 用父目录名，其余用文件名（去 .md）
            for path in sorted(root.rglob("*.md")):
                name = path.stem if path.name.lower() != "skill.md" else path.parent.name
                try:
                    # errors="replace"：坏编码不致命，用替换字符兜底
                    result.append(SkillRecord(name, path.read_text(encoding="utf-8", errors="replace"), "filesystem", str(root), priority, scope))
                except OSError:
                    continue
        return result

    def _resolve(self, agent_id: str, workspace: str | None) -> list[SkillRecord]:
        """Choose the first record by priority while preserving source order for ties."""
        # 按 (priority, owner, name) 排序后 setdefault：先到者胜（同优先级按 owner 字典序），
        # 得到每个技能名的唯一 winner。
        result: dict[str, SkillRecord] = {}
        for item in sorted(self._all(agent_id, workspace), key=lambda value: (value.priority, value.owner, value.name)):
            result.setdefault(item.name, item)
        return list(result.values())
