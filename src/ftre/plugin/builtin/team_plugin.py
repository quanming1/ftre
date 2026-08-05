"""
team_plugin — 团队相关系统提示词注入。

在 before_agent_run hook 里，根据当前 session 的 metadata 注入两类提示词：

1. 成员角色（成员 session）：metadata['team_sys_prompt'] 存了成员的角色定义，
   注入为该成员的 system prompt，并附加约束（成员不得再建子团队）。
2. 团队概览（leader session）：metadata['teams'] 记录了该 session 创建的所有
   团队及成员，注入一份概览，让 leader 每轮都能看到自己手里有哪些团队、
   每个团队有哪些成员（含成员 session_id），无需靠记忆或翻工具返回值。

普通 session（既非成员、也没建过团队）无任何注入。
"""
import logging

from ftre.plugin import Plugin, BEFORE_AGENT_RUN, append_to_first_system

logger = logging.getLogger(__name__)

# 成员硬编码约束：不得再创建/管理子团队，避免无限嵌套
_MEMBER_CONSTRAINT = (
    "你是一个团队成员 agent，专注完成 leader 交给你的任务。"
    "你不能创建或管理子团队（team 工具在成员内被禁用）。"
    "完成任务后在最后一条消息里清晰总结你的产出或结论。"
)


class TeamPlugin(Plugin):
    name = "team"
    version = "1.0.0"

    def setup(self) -> None:
        self.api.register_hook(BEFORE_AGENT_RUN, self._inject_team_prompt)

    async def _inject_team_prompt(self, ctx):
        """before_agent_run hook：注入成员角色 + 团队概览。"""
        session_id = getattr(ctx, "session_id", "") or ""
        if not session_id:
            return ctx
        session_manager = self.api.session_manager
        if session_manager is None:
            return ctx
        try:
            metadata = await session_manager.get_session_metadata(session_id)
        except Exception:
            return ctx

        # 1. 成员角色注入（成员 session）
        sys_prompt = (metadata.get("team_sys_prompt") or "").strip()
        if sys_prompt:
            append_to_first_system(
                ctx.messages,
                "<team_member_role>\n"
                f"{sys_prompt}\n"
                f"\n{_MEMBER_CONSTRAINT}\n"
                "</team_member_role>",
            )

        # 2. 团队概览注入（leader session）
        overview = _render_teams_overview(metadata.get("teams"))
        if overview:
            append_to_first_system(ctx.messages, overview)

        return ctx


def _render_teams_overview(teams) -> str:
    """把 metadata['teams'] 渲染成给 leader 看的团队概览提示词。空则返回 ''。"""
    if not isinstance(teams, dict) or not teams:
        return ""

    lines = [
        "<teams desc=\"你（作为 leader）当前已创建的团队及其成员。"
        "用 team_say(team_id, session_id, ...) 给成员派活，"
        "team_agent_peep 查看成员最新状态，wait_agent 等成员完成。\">",
    ]
    for tid, team in teams.items():
        if not isinstance(team, dict):
            continue
        name = team.get("name", "")
        members = team.get("members") or {}
        lines.append(f'  <team id="{tid}" name="{name}" members="{len(members)}">')
        for msid, member in members.items():
            if not isinstance(member, dict):
                continue
            mname = member.get("name", "")
            role = (member.get("sys_prompt") or "").strip().replace("\n", " ")
            role_brief = role[:60] + ("…" if len(role) > 60 else "")
            lines.append(
                f'    <member name="{mname}" session_id="{msid}">'
                f"{role_brief}</member>"
            )
        lines.append("  </team>")
    lines.append("</teams>")
    return "\n".join(lines)
