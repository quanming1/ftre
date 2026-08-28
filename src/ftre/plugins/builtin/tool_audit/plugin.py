"""tool/after 审计日志 Plugin：每次工具调用输出一行结构化日志。

这是 tool/after Hook 的最小真实消费者：只观察、不改写结果（透传 next_()）。
与 Runtime Tracer 的 TOOL span 定位不同——Tracer 是可 purge 的诊断库，
这里是 append-only 的运维日志（logger="ftre.tool_audit"），不新建存储表。
guard 门禁（tool/before + ToolDeny）不属于本 Plugin，另立后续阶段。
"""

from __future__ import annotations

import logging

from cordis import Context

from ftre.services.tools.hooks import TOOL_AFTER_SPEC, ToolAfterPayload

logger = logging.getLogger("ftre.tool_audit")

inject = ("hook_runtime",)
provide = ()


def apply(ctx: Context, config=None):
    """注册可逆的 tool/after 监听，行为随 Plugin 卸载完整消失。"""

    async def on_tool_after(payload: ToolAfterPayload, next_):
        # waterfall：先让下游（Runtime 默认行为及其他监听者）完成，再记录最终结果，
        # 保证日志反映的是真正返回给 Runtime 的展示/状态快照。
        result = await next_()
        logger.info(
            "tool_call: session_id=%s agent_id=%s turn_id=%s call_id=%s "
            "name=%s status=%s error=%s",
            payload.call.session_id,
            payload.call.agent_id,
            payload.call.turn_id,
            payload.call.call_id,
            payload.call.name,
            getattr(result, "status", None),
            getattr(result, "error", None),
        )
        return result

    receipt = ctx.hook_runtime.register(
        TOOL_AFTER_SPEC,
        on_tool_after,
        owner="tool-audit",
        context=ctx,
        all_agent_scopes=True,
    )
    # HookRuntime 的注册已绑定当前 Plugin Fiber，receipt 无需再挂第二个 Effect。
    del receipt
