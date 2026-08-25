"""把纯 Recovery 策略接入 ftre HookRuntime 的唯一装配入口。

数据流如下：

``Core attempt 失败 → llm/error → on_error → decide → Core Retry Loop``

本文件只做装配和生命周期绑定。配置清洗在 ``config.py``，错误决策在 ``policy.py``；
这样卸载 Plugin 时只需移除一个 Hook Receipt，不存在后台任务或额外 Service 要清理。
"""

from __future__ import annotations

from cordis import Context

from ftre.services.llm.hooks import LLM_ERROR_SPEC

from .config import parse_config
from .policy import decide

# Cordis 在调用 apply 前保证 hook_runtime 已经可用；缺失时 Fiber 会保持 pending，
# 不会启动一个只有一半能力的 Plugin。
#
# 这是无状态行为 Plugin，因此不 provide 公共 Service。配置来自当前 Manifest 的 config，
# 不是全局 ConfigService：同一个 Package 的不同加载实例可以拥有不同策略快照。
inject = ("hook_runtime",)
provide = ()


def apply(ctx: Context, config=None):
    """解析一次配置，并把 ``llm/error`` listener 绑定到当前 Plugin Fiber。"""

    # snapshot 被闭包只读持有；运行中修改原始 config 不会造成半轮请求使用新旧两套规则。
    snapshot = parse_config(config)

    async def on_error(payload, next_):
        """贡献恢复建议；未命中时继续 Waterfall Hook 链。"""

        result = decide(payload, snapshot)
        if result is not None:
            return result
        # 未匹配时继续 Hook 链，最终回到 Core 默认策略。
        return await next_()

    # context=ctx 让 HookRuntime 把 Receipt 绑定到 Cordis Fiber：Plugin unload/restart 时
    # listener 会自动撤销，避免重复注册和已经卸载的策略继续影响新请求。
    ctx.hook_runtime.register(
        LLM_ERROR_SPEC,
        on_error,
        owner="ftre-llm-recovery",
        context=ctx,
        all_agent_scopes=True,
    )


__all__ = ["apply", "inject", "provide"]
