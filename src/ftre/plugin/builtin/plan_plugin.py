"""
plan_plugin — Plan 模式内置插件

职责：
1. 注册 plan 工具（创建/更新/完成结构化执行计划）
2. 注册 ON_STOP core hook（阻止 Agent 在计划未完成时停止）
3. 注入 system prompt（Plan 使用指引）

Plan 数据存入 session.metadata.plan，前端通过 messages 接口的 metadata 字段读取渲染。
"""
import logging

from ftre.plugin import BEFORE_AGENT_RUN, Plugin, append_to_first_system
from ftre_agent_core.hooks import ON_STOP, HookOutput, StopInput
from ftre.tools.plan import create_plan_tool

logger = logging.getLogger(__name__)

PLAN_SYSTEM_PROMPT = """<plan_guidance>
Plan 工具用于创建和管理结构化执行计划，确保长任务的完整生命周期被跟踪和执行。

调用时机：任何阶段都可以调用 plan 工具——不限于任务开始前。执行了一段时间后发现任务比预期复杂、内容量大、步骤繁多时立即 create。用户需求模糊时也应 create，往往越模糊的需求事情越多。

计划激活期间不能随意结束对话（ON_STOP hook 会阻止）。所有步骤 completed 后调 complete 才能正常结束。
</plan_guidance>"""


class PlanPlugin(Plugin):
    """Plan 模式：结构化执行计划 + 防偷懒 ON_STOP hook。"""

    name = "plan"
    version = "1.0.0"

    def setup(self) -> None:
        session_manager = self.api.session_manager

        # 注册 plan 工具
        self.api.tool_registry.register(create_plan_tool())

        # 注册 ON_STOP core hook — 阻止 Agent 在计划未完成时停止
        self.api.register_core_hook(ON_STOP, self._create_stop_hook(session_manager))

        # 注入 system prompt
        self.api.register_hook(BEFORE_AGENT_RUN, self._inject_prompt)

        logger.info("[plan-plugin] 已注册 plan tool + ON_STOP hook")

    def _create_stop_hook(self, session_manager):
        """创建 plan ON_STOP hook。

        逻辑：
        - 无 plan → 放行（return None）
        - plan 中存在未完成步骤 → block，插入 continuation prompt
        - 全部完成 → 删除 metadata.plan，放行
        """

        async def plan_stop_hook(input: StopInput) -> HookOutput | None:
            session_id = input.session_id or input.runtime_context.get("session_id", "")
            if not session_id:
                return None

            metadata = await session_manager.get_session_metadata(session_id)
            plan = metadata.get("plan")
            if not plan:
                return None

            steps = plan.get("steps", [])
            incomplete = [s for s in steps if s.get("status") != "completed"]

            if not incomplete:
                # 全部完成，删除 plan
                await session_manager.update_session_metadata(session_id, "plan", None)
                logger.info("[plan-hook] 计划已全部完成，已清除 plan: session=%s", session_id)
                return None

            # 未完成，阻止停止
            lines = [
                f"计划尚未完成，还有 {len(incomplete)} 个步骤未完成：",
            ]
            for s in incomplete:
                mark = {"in_progress": "[~]", "pending": "[ ]"}.get(
                    s.get("status", "pending"), "[ ]"
                )
                content = s.get("content", s.get("id", "?"))
                lines.append(f"  {mark} {content}")
            lines.append("")
            lines.append("请继续执行计划中的未完成步骤。每完成一个步骤后，使用 plan 工具 action=update 更新步骤状态为 completed。全部完成后调用 action=complete。")

            logger.info(
                "[plan-hook] 阻止停止: session=%s 未完成=%d",
                session_id,
                len(incomplete),
            )
            return HookOutput(decision="block", reason="\n".join(lines))

        return plan_stop_hook

    async def _inject_prompt(self, ctx):
        """将 Plan 使用指引注入 system prompt。"""
        messages = ctx.messages
        append_to_first_system(messages, PLAN_SYSTEM_PROMPT)
        return ctx
