"""指令系统类型定义。

CommandResult 联合类型让 handler 有明确的返回契约，
_step_command 根据 match-case 分发，不再靠 ctx.meta 约定传结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Union


# ─── 返回值类型 ──────────────────────────────────────────────


@dataclass
class RewritePrompt:
    """重写发给 LLM 的 prompt，原始用户输入保留入库，继续 pipeline。"""
    content: str | list[dict]
    model_override: str | None = None


@dataclass
class SendMessage:
    """给用户显示一条消息，短路 pipeline。"""
    content: str
    level: str = "info"  # info / warning / error


@dataclass
class Handled:
    """已处理完毕，短路 pipeline。"""
    pass


@dataclass
class Passthrough:
    """不是指令，交给 LLM。"""
    pass


CommandResult = Union[RewritePrompt, SendMessage, Handled, Passthrough]
"""handler 返回值联合类型。None 视为 Handled（兼容旧 handler）。"""


# ─── 指令定义 ────────────────────────────────────────────────


@dataclass
class CommandDef:
    """指令定义：注册、匹配、命令面板渲染用元信息。

    handler 可选：内置指令通过 register() 传 handler；
    文件指令 / Skill 指令直接挂在 CommandDef 上。
    """
    command: str                        # "/model"
    description: str = ""               # "切换模型预设"
    args_hint: str = ""                 # "[preset]"；空串 = 无参数
    system: bool = False                # 系统级指令：锁外执行，可立即响应
    persist_input: bool = True          # 是否持久化用户输入（/cancel=False 不入库不回显）
    sub_commands: list["CommandDef"] = field(default_factory=list)
    source: str = "builtin"             # builtin / file / skill
    handler: "Handler | None" = None    # 文件/Skill 指令直接挂载


@dataclass
class CommandContext:
    """dispatch 匹配到指令后传给 handler 的上下文。"""
    raw: str                            # 原始输入 "/model gpt-5"
    command: str                        # 命中的指令 "/model"
    args: str | None                    # "gpt-5"；无则为 None
    meta: Any = None                    # pipeline data（PipelineData 或 dict），handler 可访问 .inbound 等


# ─── Handler 签名 ────────────────────────────────────────────

Handler = Callable[[CommandContext], CommandResult | Awaitable[CommandResult] | None]
"""指令处理函数。

返回 CommandResult 决定 pipeline 走向：
- RewritePrompt → 重写发给 LLM 的 prompt，原始输入保留入库，继续 → LLM
- SendMessage  → 推消息给前端，短路
- Handled      → 短路
- Passthrough  → 继续 → LLM
- None         → 视为 Handled（兼容旧 handler）

同步或 async def 均可，dispatch 会统一 await。
"""
