"""
team_plugin — 团队提示词注入。

两个注入（都在 before_agent_run hook）：
1. <team_usage> 决策块：对所有 session 无条件注入——什么时候该组建团队、
   边界在哪（NEVER 场景）。让每个 agent 拿到任务先决策，而不是无脑组队
   或从不组队。完整操作手册在 agent-team SKILL（loadSkill 加载）。
2. <teams> 概览：仅为创建过团队的 leader session 注入团队概览，
   让 leader 每轮都能看到自己手里有哪些团队、每个团队有哪些成员
   （含成员 session_id 与角色摘要），无需靠记忆或翻工具返回值。

成员的角色定义不再经本插件注入：成员 profile 落盘在
<leader session>/sub_agents/<member>/（AGENTS.md 等），由标准 agent
加载路径（turn_executor 按 agent_ref / team_member 绑定解析 +
context_govern 注入 AGENTS.md）生效。

普通 session（没建过团队）只收到 <team_usage> 决策块，无概览注入。
"""
import logging

from ftre.agent import sub_agent_profile
from ftre.plugin import Plugin, BEFORE_AGENT_RUN, append_to_first_system

logger = logging.getLogger(__name__)

# 何时用 Team / 边界——所有 session 无条件注入（精简决策版）
_TEAM_USAGE_PROMPT = """<team_usage desc="多 agent 团队使用决策：先判断要不要组建团队，再动手。完整操作手册见 agent-team SKILL。">
你有 team 工具可以组建多 agent 团队（team_create / team_add_agent / team_say / wait_agent / team_delete）。组建有成本：token 约单 agent 10-15 倍 + 协调开销，先决策再用。

ALWAYS 用团队：
- 多角度并行审查（代码/PR/方案/迁移结果）——审查天然可并行，角度分离交叉验证
- 并行调查/根因分析（任务卡住、问题定位）——多 agent 独立探索不同假设
- 研究/调研（对比方案、读大量代码、信息超单 context）
- 跨文件梳理（迁移影响面、多格式产出）——按文件/模块切互不重叠

NEVER 用团队：
- NEVER 并行编码实施——多 agent 改同一批文件必冲突。实施串行：一人实施 + 多人并行审查
- NEVER 内聚小任务——一个一次性子任务用 task 同步派发
- NEVER 自己几步能做完的活——别为"显得专业"组队
- NEVER 让校验成员自己改自己审——校验只出报告，你来改或转执行组

决策：能拆成 2-4 个互不依赖、可独立验证的子任务 + 需要独立探索/阅读/下结论 + 高价值 → 用；否则自己直接做。团队 3-5 人，NEVER 超过 5 人。
</team_usage>"""


class TeamPlugin(Plugin):
    name = "team"
    version = "2.1.0"

    def setup(self) -> None:
        self.api.register_hook(BEFORE_AGENT_RUN, self._inject_team_prompt)

    async def _inject_team_prompt(self, ctx):
        """before_agent_run hook：注入 team 使用决策 + 团队概览（leader）。"""
        session_id = getattr(ctx, "session_id", "") or ""
        if not session_id:
            return ctx
        session_manager = self.api.session_manager
        if session_manager is None:
            return ctx

        # 注入 1：team 使用决策（所有 session 无条件）
        append_to_first_system(ctx.messages, _TEAM_USAGE_PROMPT)

        # 注入 2：leader 团队概览（仅建过团队的 session）
        try:
            metadata = await session_manager.get_session_metadata(session_id)
        except Exception:
            return ctx

        # 概览是增强信息：任何脏数据都不应杀死整个 turn
        try:
            overview = _render_teams_overview(
                metadata.get("teams"), session_manager, session_id
            )
        except Exception:
            logger.exception("[team-plugin] 团队概览渲染失败，跳过注入")
            overview = ""
        if overview:
            append_to_first_system(ctx.messages, overview)

        return ctx


def _render_teams_overview(teams, session_manager, leader_session_id: str) -> str:
    """把 metadata['teams'] 渲染成给 leader 看的团队概览提示词。空则返回 ''。

    成员角色摘要从 <leader session>/sub_agents/<member>/AGENTS.md 读取
    （profile 落盘位置），读不到时留空。
    """
    if not isinstance(teams, dict) or not teams:
        return ""

    lines = [
        "<teams desc=\"你（作为 leader）当前已创建的团队及其成员。"
        "用 team_say(team_id, session_id, ...) 给成员派活，"
        "team_agent_status 查看成员最新状态，wait_agent 等成员完成。\">",
    ]
    for tid, team in teams.items():
        if not isinstance(team, dict):
            continue
        name = team.get("name", "")
        members = team.get("members")
        if not isinstance(members, dict):
            members = {}
        lines.append(f'  <team id="{tid}" name="{name}" members="{len(members)}">')
        for msid, member in members.items():
            if not isinstance(member, dict):
                continue
            mname = member.get("name", "")
            role_brief = sub_agent_profile.role_brief(
                session_manager, leader_session_id, msid
            )
            lines.append(
                f'    <member name="{mname}" session_id="{msid}">'
                f"{role_brief}</member>"
            )
        lines.append("  </team>")
    lines.append("</teams>")
    return "\n".join(lines)
