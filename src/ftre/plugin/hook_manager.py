"""
Hook 系统 — 让插件挂到 ftre 内部生命周期的关键点上，运行自己的逻辑。

执行模型：filter chain
- 一个 hook point 上可注册多个 hook，按注册顺序依次执行
- 每个 hook 接收 ctx，返回（可能被修改的）ctx；不允许拦截/中止主流程
- hook 内部抛异常 → 捕获 + log，当作该 hook 未注册，用原 ctx 继续后续 hook
  （插件出错不应拖垮主流程）
- hook 误返回 None → 当作未改写，沿用当前 ctx 继续

当前挂点：
- "before_messages_build": events 加载完毕后、to_openai_messages 之前触发。
  插件可改写 events（裁剪/注入）、config（model/system_prompt）。
- "before_agent_run": Agent 创建后、agent.run(messages) 之前触发。
  插件可操作 OpenAI 格式的 messages 列表——插入 system 消息（系统身份）或
  user 消息（对话上下文），实现 OpenClaw 的 prependContext/prependSystemContext 双轨注入。
"""
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from ftre.config import AgentConfig

logger = logging.getLogger(__name__)


# hook point 字符串常量
BEFORE_MESSAGES_BUILD = "before_messages_build"
BEFORE_AGENT_RUN = "before_agent_run"


def append_to_first_system(messages: list[dict], text: str) -> None:
    """将 text 追加到 messages 中第一条 system 消息的 content 末尾。

    如果没有 system 消息，则在列表开头插入一条新的。
    多个插件调用此函数时，内容会依次拼接到同一条 system 消息中，
    避免产生多条 system 消息。
    """
    text = (text or "").strip()
    if not text:
        return
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            current = (msg.get("content") or "").rstrip()
            msg["content"] = f"{current}\n\n{text}" if current else text
            return
    messages.insert(0, {"role": "system", "content": text})


@dataclass
class MessagesBuildContext:
    """
    "before_messages_build" hook 的上下文。

    只读字段（hook 改了也不会被采纳）：
    - session_id / channel_id：当前会话标识
    - inbound_data：本次 user_message 的完整 payload
    - workspace：当前会话工作区的绝对路径
    - event_loop：主 asyncio 事件循环引用（plugin 用于 run_coroutine_threadsafe）

    可改字段（hook 修改后会被采纳）：
    - config：AgentConfig 的深拷贝，改 llm / system_prompt / max_iterations 等
    - events：从 DB 加载的事件流（list[dict]），hook 可裁剪/注入/重排
    """
    # 只读
    session_id: str
    channel_id: str
    inbound_data: dict
    workspace: str
    event_loop: Any = None

    # 可改
    config: "AgentConfig" = None
    events: list = field(default_factory=list)


@dataclass
class AgentRunContext:
    """
    "before_agent_run" hook 的上下文。

    触发时机：Agent 已创建、messages 已转 OpenAI 格式、agent.run() 调用前。

    插件可以自由操作 messages 列表——插入任意 role 的消息（system/user/assistant），
    实现类似 OpenClaw 的 prependContext / prependSystemContext 双轨注入效果：

    - 对话上下文（群信息、成员列表、聊天历史）→ ctx.messages.insert(0, {"role": "user", "content": "..."})
    - 系统身份（persona 提示、工具可用性提示）  → ctx.messages.insert(0, {"role": "system", "content": "..."})
    """

    # readonly
    session_id: str
    channel_id: str

    # mutable — OpenAI 格式的消息列表，每项 {"role": str, "content": str|list}
    messages: list[dict]
    config: "AgentConfig"


# hook 函数签名：接收 ctx，返回（可能被改写的）ctx
HookFunc = Callable[[Any], Any]


class HookManager:
    """注册 + 调度 hook 的中心。"""

    def __init__(self) -> None:
        self._hooks: dict[str, list[HookFunc]] = {}

    def register(self, point: str, fn: HookFunc) -> None:
        """在指定挂点注册一个 hook（按注册顺序执行）。"""
        if not callable(fn):
            raise TypeError(f"hook 必须可调用，收到 {type(fn).__name__}")
        self._hooks.setdefault(point, []).append(fn)
        logger.info(f"[hook] 注册: point={point} fn={getattr(fn, '__qualname__', fn)}")

    def has_hooks(self, point: str) -> bool:
        """该挂点是否有已注册的 hook。"""
        return bool(self._hooks.get(point))

    def trigger_sync(self, point: str, ctx: Any) -> Any:
        """
        同步触发一条 hook 链。

        返回最终 ctx（可能被各 hook 改写过）。
        - hook 抛异常被捕获、记录后跳过（用当前 ctx 继续）
        - hook 误返回 None 视为未改写，沿用当前 ctx
        """
        hooks = self._hooks.get(point)
        if not hooks:
            return ctx

        current = ctx
        for fn in hooks:
            try:
                result = fn(current)
            except Exception:
                logger.exception(
                    f"[hook] 执行异常，已跳过: point={point} "
                    f"fn={getattr(fn, '__qualname__', fn)}"
                )
                continue
            if result is not None:
                current = result
        return current
