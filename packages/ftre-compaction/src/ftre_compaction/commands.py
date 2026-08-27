"""压缩包的 Command Plane 适配器。

命令解析和生命周期由 ftre 核心 CommandService 拥有；本模块只贡献两个
命令处理器。处理器直接调用公开的 CompactionService，不会把 /compact
重新塞回 Agent Inbox，也不会创建一次普通 Agent Turn。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ftre_agent import AgentConfig

from ftre.plugins.builtin.command import CommandContext, CommandResult, CommandService
from ftre.services.agent.config import load_config

logger = logging.getLogger(__name__)


def register_commands(
    commands: CommandService,
    compaction,
    *,
    load_runtime_config: Callable[[], AgentConfig] = load_config,
) -> list[Callable[[], bool]]:
    """注册命令并返回可逆 disposer。

    load_runtime_config 只负责提供本次命令所需的主 Agent LLM 配置；
    阈值、摘要模型等压缩专属配置仍由 Service 从注入的 ConfigService 读取。
    这种分工使命令可以复用当前 Agent，同时不把压缩字段重新塞回核心配置。
    """

    async def on_compact(ctx: CommandContext) -> CommandResult:
        # /compact 是维护命令：它直接触发 Service，不进入 Inbox 的
        # pending，也不经过 TurnExecutor。
        try:
            await compaction.compact_now(
                ctx.session_id,
                ctx.channel_id,
                config=load_runtime_config(),
                focus_hint=(ctx.args or "").strip(),
            )
        except Exception as exc:
            logger.exception("[ftre-compaction] /compact failed session=%s", ctx.session_id)
            return CommandResult.error(f"压缩失败：{exc}")
        return CommandResult.success()

    async def on_compress_fast(ctx: CommandContext) -> CommandResult:
        # 快速模式只裁剪旧工具结果，不调用摘要 LLM；参数表示要保护的最近轮数。
        arg = (ctx.args or "").strip()
        keep_turns = int(arg) if arg.isdigit() else 0
        try:
            await compaction.compress_fast(
                ctx.session_id,
                ctx.channel_id,
                config=load_runtime_config(),
                keep_turns=keep_turns,
            )
        except Exception as exc:
            logger.exception(
                "[ftre-compaction] /compress-fast failed session=%s", ctx.session_id
            )
            return CommandResult.error(f"快速压缩失败：{exc}")
        return CommandResult.success()

    return [
        commands.register(
            "/compact",
            on_compact,
            description="压缩当前会话上下文（可附提示词强调优先保留的内容）",
            args_hint="[强调保留的内容]",
            persist_input=False,
            source="ftre-compaction",
        ),
        commands.register(
            "/compress-fast",
            on_compress_fast,
            description="快速压缩：裁剪旧工具输出，不调 LLM（可附轮数保护最近 N 轮）",
            args_hint="[保护最近轮数]",
            persist_input=False,
            source="ftre-compaction",
        ),
    ]


__all__ = ["register_commands"]
