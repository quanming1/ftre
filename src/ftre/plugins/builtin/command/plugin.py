"""Command Service 的 Provider Plugin（命令接入层）。

这个文件是命令系统的装配入口，不是具体命令的实现文件。它主要负责：

1. 从 Cordis Context 取得公开 Service；
2. 创建并发布 ``CommandService``；
3. 注册内置命令和其它 Package 贡献的命令；
4. 在普通消息进入 Inbox 之前识别 slash command。

命令和普通聊天消息是两条不同的路径：

``/compact`` → CommandService → 后台命令 Task

普通文本 → Inbox Package → AgentService → Agent Turn

命令不能被转成 Agent 消息，也不能在 MessageBus 的 inbound 消费协程中等待慢操作。
因此识别出命令后，本文件只负责“接纳并立即 ACK”，真正的 handler 由
``CommandService.submit_inbound`` 在后台执行，结果再通过 outbound Bus 推给客户端。
"""

from __future__ import annotations

from cordis import Context

from ftre.services.messaging.bus import (
    MESSAGING_ROUTE_SPEC,
    BusMessage,
    CommandMessagePayload,
    IngressResult,
    SessionCommandMessage,
)

from .builtin import register_builtin_commands
from .service import CommandService
from .types import CommandResult

# ``provide`` 是本 Plugin 对外发布的 Service key；消费者应 inject("commands")，
# 不要直接 import 本目录里的 CommandRuntime。
provide = ("commands",)

# apply() 可以读取的公开依赖：Session 记录命令生命周期，HTTP 提供命令列表，
# HookRuntime 注册接入 Hook，MessageBus 发布命令结果，AgentService 支持取消/确认。
inject = ("sessions", "http", "hook_runtime", "message_bus", "agents")


def apply(ctx: Context, config=None):
    """装配 CommandService、命令定义、HTTP 路由和消息入口 Hook。

    所有注册动作都绑定到当前 Plugin Fiber 的 Effect；unload/restart 时，命令、
    路由、监听器和后台 Task 会一起撤销，不会残留到下一次 Composition。
    """
    owns_service = False

    async def persist_command_event(event_type, payload):
        """把 command/run、command/done 写入 Session metadata，不写聊天历史。"""
        session_id = payload.get("session_id") or ""
        if not session_id:
            return
        await ctx.sessions.append_command_event(
            session_id,
            {"type": event_type, **payload},
        )

    service = ctx.get("commands", strict=False)
    if service is None:
        # 正常 Gateway 启动时这里创建唯一的 CommandService，并发布为 Context
        # 的 ``commands`` key。嵌入式宿主/测试也可以预先 provide 自己的实例；
        # 这种情况下本 Plugin 只贡献命令和路由，不接管外部实例的关闭。
        service = CommandService(lifecycle=persist_command_event)
        ctx.provide("commands", service)
        owns_service = True

    if owns_service:
        # Command handler 可能调用 LLM/磁盘等慢资源；关闭时先取消它们，避免
        # Plugin 卸载后任务仍引用已经销毁的 Compaction/Session Service。
        ctx.effect(lambda: service.close, label="command:background-tasks")

    # 内置命令只贡献 Handler；注册表、命令匹配和 request_id 去重仍由
    # CommandRuntime/CommandService 持有。
    disposers = register_builtin_commands(
        service.runtime,
        agents=ctx.agents,
        sessions=ctx.sessions,
    )
    for index, disposer in enumerate(disposers):
        # disposer 是幂等的。Fiber 卸载时调用它，保证本 Plugin 注册的命令
        # 不会继续被后续 inbound 消息匹配到。
        ctx.effect(lambda disposer=disposer: disposer, label=f"command:builtin:{index}")

    from .router import build_router

    # HTTP 路由只提供命令列表/诊断，不负责执行 WebSocket 命令；命令执行仍统一
    # 经过下面的 messaging/route Hook，避免出现第二个命令入口。
    route_disposer = ctx.http.register_router(build_router(service), owner="commands")
    ctx.effect(lambda: route_disposer, label="http:commands")

    async def on_inbound(message: BusMessage, next_):
        """在进入 Inbox 前裁决命令；普通输入继续交给后续 Listener。

        ``MESSAGING_ROUTE_SPEC`` 是 WATERFALL Hook。调用 ``next_()`` 表示“我不
        处理这条消息，请下一个 Listener 继续”，下一个 Listener 通常就是 Inbox
        Package。返回 ``IngressResult`` 则表示当前消息已被命令层接管，后续不会
        再进入 Inbox。

        识别到命令后调用的是 ``submit_inbound``，不是 ``await dispatch_inbound``。
        前者只创建后台 Task 并立即返回，保证一个慢的 ``/compact`` 不会阻塞
        MessageBus 处理其它用户消息。
        """
        if message.type == "turn_cancel":
            # cancel 控制帧不是 slash command，沿原有控制路径传播，避免把控制
            # 协议误解析成普通用户命令。
            return await next_()
        definition = service.parse({"inbound": message})
        if definition is None:
            if not service.is_command_input({"inbound": message}):
                # 非 slash 的普通文本由 Inbox 做 durable admission，之后才由
                # AgentService 执行 Turn；Command Plugin 不拥有这条消息。
                return await next_()
            # 以“/”开头但没有注册定义的输入，明确返回错误，不能静默落入
            # Agent 上下文，否则客户端会误以为命令已经执行。
            result = CommandResult.error("命令不可用或未启用")
        elif getattr(definition, "system", False):
            # system command（例如 /cancel）也走后台提交，但告诉 Runtime 使用
            # system 注册表。ACK 表示“命令已接纳”，不是“handler 已执行完毕”。
            accepted = service.submit_inbound(
                message,
                definition=definition,
                system=True,
                on_result=lambda result: _publish_result(ctx, message, result),
            )
            return _accepted(message) if accepted else await next_()
        else:
            # 普通业务命令（例如 /compact、/compress-fast、/fork）同样只在这里
            # 被识别和接纳；具体 Handler 由注册它的 Plugin/Package 实现。
            accepted = service.submit_inbound(
                message,
                definition=definition,
                on_result=lambda result: _publish_result(ctx, message, result),
            )
            return _accepted(message) if accepted else await next_()

        # 未知 slash command 是同步的快速拒绝：不进入 Inbox，也不创建后台 Task。
        # 已识别命令不会走到这里，而是在上面的 submit_inbound 分支立即返回 ACK。
        await _publish_result(ctx, message, result)
        return _accepted(message)

    # 把“命令优先于 Inbox”注册为可撤销 Hook。all_agent_scopes=True 表示这是
    # Gateway 全局接入规则，不依赖某一个 Agent 的 isolate Context。
    receipt = ctx.hook_runtime.register(
        MESSAGING_ROUTE_SPEC,
        on_inbound,
        owner="commands",
        context=ctx,
        all_agent_scopes=True,
    )
    # HookRuntime 已绑定当前 Plugin Fiber；不为同一 receipt 增加第二个 Effect。
    del receipt


def _accepted(message: BusMessage) -> IngressResult:
    """构造统一接纳 ACK，但不创建 Agent Turn。

    对已识别命令，ACK 表示“CommandService 已接管并安排执行”；对未知 slash
    command，ACK 表示“错误结果已经发布”。两者都不会进入 Inbox。
    """
    return IngressResult(
        accepted=True,
        session_id=str(message.data.get("session_id") or message.from_session),
        request_id=str(message.metadata.request_id or message.id),
        created=True,
    )


async def _publish_result(ctx: Context, inbound: BusMessage, result) -> None:
    """通过统一的 ``session/command`` outbound 帧发送命令结果。

    命令结果不是 Agent assistant 消息，不写入 LLM 上下文；它使用结构化 Bus
    Envelope，让 WebSocket/其它 Channel 按自己的协议展示成功或错误文本。空文本
    结果不发送气泡，但 command/run、command/done 仍会写入 Session metadata 供诊断。
    """
    if result is None or not getattr(result, "text", ""):
        return
    level = "error" if getattr(result, "kind", "success") == "error" else "info"
    await ctx.message_bus.bus.publish_outbound(
        SessionCommandMessage(
            from_channel=inbound.from_channel,
            to_channel=inbound.from_channel,
            from_session=inbound.from_session,
            to_session=inbound.from_session,
            data=CommandMessagePayload(content=result.text, level=level),
            metadata=inbound.metadata,
        )
    )
