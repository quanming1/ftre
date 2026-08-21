"""
Bus wire 协议契约（唯一事实源，Pydantic 定死）

所有进出 Bus 的消息结构都在此定义，生产方/消费方一律引用本模块，
不允许再出现裸字符串键的 metadata 约定：

Inbound（外部 → Bus → Agent）：
    data     : InboundData      user_message 载荷
    metadata : InboundMetadata  请求/执行标记（request_id、agent_id、agent_ref）

Outbound（Agent → Bus → 外部）：
    data     : dict             事件 dump（core Event 形状，由 ftre-agent-core 定义）
    metadata : OutboundMetadata 序列化到 ws 帧时由 InboundMetadata 透传字段
                                + channel_id/session_id 组成

安全边界：
    客户端帧只能构造 agent_id（from_client 白名单）。WS frame_id 在边界处
    转换为 request_id，
    agent_ref 是 team 机制服务端专属标记——外部构造可用来加载他人 session 的
    成员 profile（目录穿越读取），因此在协议层直接封死。
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# BusMessage.type 全集。新增消息类型必须先改这里。
MessageType = Literal[
    "user_message",
    "agent_event",
    "global_event",
    "session_event",
    "agent_event:stream",
    "agent_event:complete",
    "session_event:command_message",
    "session_event:mailbox_snapshot",
    "turn_cancel",
]


class AgentRef(BaseModel):
    """团队成员 profile 定位标记（服务端 team 工具投递时携带）。"""

    model_config = ConfigDict(frozen=True)

    leader_session: str
    sub_agent: str


class InboundMetadata(BaseModel):
    """Inbound 消息 metadata 契约。

    request_id: 请求唯一标识；WS 会把客户端 frame_id 转换为该字段
    agent_id  : 客户端选择的全局 agent（多 agent 切换）
    agent_ref : 团队成员定位（仅服务端 team 机制可构造）
    """

    model_config = ConfigDict(frozen=True)

    request_id: str = ""
    agent_id: str = ""
    agent_ref: AgentRef | None = None

    @classmethod
    def from_client(cls, raw: Any) -> "InboundMetadata":
        """客户端帧 metadata → InboundMetadata（白名单：只收 agent_id）。

        request_id / agent_ref 等一律由服务端构造，客户端传了也丢弃。
        """
        if not isinstance(raw, dict):
            return cls()
        agent_id = raw.get("agent_id")
        return cls(agent_id=agent_id if isinstance(agent_id, str) else "")


class InboundData(BaseModel):
    """user_message 载荷契约（inbound data 字段）。

    content 是纯字符串：Inbound（wire）只承载客户端发送的纯文本。
    结构化 part（[{type,text}] 数组）是 Msg 存储层/前端渲染的形态，
    不在 wire 协议内——skill part 构造点已随 SkillChip 删除，
    协议不为死代码买单。

    RewritePrompt 的临时改写内容属于 Turn 内存状态，不进入 Bus data。
    """

    content: str = ""
    session_id: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list)

    @classmethod
    def coerce(cls, raw: Any) -> "InboundData":
        """dict → InboundData（未知键丢弃）。非 dict 视为空载荷。"""
        if isinstance(raw, InboundData):
            return raw
        if not isinstance(raw, dict):
            return cls()
        return cls.model_validate(raw)


class OutboundMetadata(BaseModel):
    """Outbound 帧 metadata 契约（ws payload 的 metadata 字段）。

    前端消费：metadata.frame_id（当前兼容 echo）、metadata.session_id（路由）。
    """

    model_config = ConfigDict(frozen=True)

    # 消息目标通道 id（= BusMessage.to_channel）。前端可据此区分消息来自
    # 哪个渠道（ws / octo / subagent 等）。
    channel_id: str = ""
    # 消息目标 session id（= BusMessage.to_session）。前端多会话并存时
    # 用它把事件路由到正确的对话视图。
    session_id: str = ""
    # 下行兼容字段：由 request_id 投影为客户端的原始 frame_id。
    # AgentLoop/SessionLane 不保存也不读取它。
    frame_id: str = ""
    request_id: str = ""
    # 本轮使用的全局 agent id（inbound 透传，客户端多 agent 切换时携带）。
    # 未指定时为空串。
    agent_id: str = ""
    # 团队成员定位标记（inbound 透传，仅 team 工具投递的消息携带）。
    # 普通消息为 None，序列化时 exclude_none 不出现在 wire 上。
    agent_ref: AgentRef | None = None

    @classmethod
    def from_inbound(
        cls,
        meta: InboundMetadata | None,
        *,
        channel_id: str,
        session_id: str,
    ) -> "OutboundMetadata":
        """inbound metadata 透传字段 + outbound 路由字段。"""
        meta = meta or InboundMetadata()
        return cls(
            channel_id=channel_id,
            session_id=session_id,
            frame_id=meta.request_id,
            # 旧桌面端仍以这个字段合并乐观消息；它只是 frame_id 的输出别名。
            request_id=meta.request_id,
            agent_id=meta.agent_id,
            agent_ref=meta.agent_ref,
        )


def coerce_inbound_metadata(raw: Any) -> InboundMetadata:
    """受信任来源（服务端内部、octo/task/team 等）的 metadata 归一。

    与 InboundMetadata.from_client 的区别：不做白名单过滤——调用方
    是服务端代码，允许携带 request_id/agent_ref。非法形状回退为空实例。
    """
    if isinstance(raw, InboundMetadata):
        return raw
    if not isinstance(raw, dict):
        return InboundMetadata()
    try:
        return InboundMetadata.model_validate(raw)
    except ValueError:
        return InboundMetadata()
