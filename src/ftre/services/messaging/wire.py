"""下行 Wire 帧契约——F41/F44 唯一实现。

帧信封：{v, session_id, type, payload}；无帧级 seq（session 帧内 seq 为权威，
queue/projection 各自携带 revision/seq）。rpc 帧不经 bus 广播，由 WS Channel
对发起连接直回。

payload 子模型同时是 TS wire 类型的生成源（``scripts/gen_wire_types.py``）：
字段形状在此声明一次，desktop 的 ``types/wire.gen.ts`` 由脚本导出，禁止手写。
``SessionEventFramePayload.event`` 运行时保持 dict 透传（未知事件由 F41 FR6
兜底，不在帧层二次校验），TS 契约侧声明为 SessionEvent 联合。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FrameBase(BaseModel):
    """所有下行帧的公共信封。"""
    model_config = ConfigDict(extra="forbid")
    v: Literal[1] = 1
    session_id: str


# ── 帧载荷契约（PRD-F41 §4.4；TS 生成源）─────────────────────────

class SessionEventFramePayload(BaseModel):
    """直播透传：event 是完整事件信封（含 seq），零变换。"""
    model_config = ConfigDict(extra="allow")
    event: dict[str, Any]


class SessionSubscribedPayload(BaseModel):
    """attach 响应：返回基线 seq 之后尚未折叠为 Msg 的 Event。"""
    model_config = ConfigDict(extra="allow")
    seq: int = -1
    events: list[dict[str, Any]] = Field(default_factory=list)
    status: str = "idle"
    has_more: bool = False
    resync_required: bool = False


class SessionProjectionPayload(BaseModel):
    """派生状态快照（last-wins）：todo/plan/title/token 等。"""
    model_config = ConfigDict(extra="allow")
    key: str
    value: Any = None
    seq: int = 0


class SessionMaintenancePayload(BaseModel):
    """非日志文本反馈：command_message、compaction start/failed 等瞬态。"""
    model_config = ConfigDict(extra="allow")
    name: str
    value: dict[str, Any] = Field(default_factory=dict)


class RpcError(BaseModel):
    """rpc 帧错误载荷（统一 error envelope 字段）。"""
    model_config = ConfigDict(extra="allow")
    code: str = ""
    message: str = ""
    session_id: str = ""
    retryable: bool | None = None


class RpcPayload(BaseModel):
    """上行操作结算（prompt/updateQueue/resume → queue 快照或 error；cancel → accepted）。"""
    model_config = ConfigDict(extra="allow")
    request_id: str
    ok: bool
    value: Any = None
    error: RpcError | None = None


# ── 帧模型（6 种）─────────────────────────────────────────────────

class SessionEventFrame(FrameBase):
    """直播透传：payload.event 是完整事件信封（含 seq），零变换。"""
    type: Literal["session/event"] = "session/event"
    payload: SessionEventFramePayload


class SessionSubscribedFrame(FrameBase):
    """attach 响应：事件数组和当前 Session seq 以同一帧返回。"""
    type: Literal["session/subscribed"] = "session/subscribed"
    payload: SessionSubscribedPayload


class SessionQueueFrame(FrameBase):
    """Inbox 队列权威快照（last-wins；payload 形状 F24 冻结，含 revision）。"""
    type: Literal["session/queue"] = "session/queue"
    payload: dict[str, Any]


class SessionProjectionFrame(FrameBase):
    """派生状态快照（last-wins）：todo/plan/title/token 等。"""
    type: Literal["session/projection"] = "session/projection"
    payload: SessionProjectionPayload


class SessionMaintenanceFrame(FrameBase):
    """非日志文本反馈：command_message、compaction start/failed 等瞬态。"""
    type: Literal["session/maintenance"] = "session/maintenance"
    payload: SessionMaintenancePayload


class RpcFrame(FrameBase):
    """上行操作结算（prompt/updateQueue/resume → queue 快照或 error；cancel → accepted）。

    payload 运行时为 dict 直通（value/error 缺省时不上 wire，保持既有字节
    形状）；``RpcPayload``/``RpcError`` 是 TS 生成用的契约模型。
    """
    type: Literal["rpc"] = "rpc"
    payload: dict[str, Any]


DownstreamFrame = (
    SessionEventFrame
    | SessionSubscribedFrame
    | SessionQueueFrame
    | SessionProjectionFrame
    | SessionMaintenanceFrame
    | RpcFrame
)

__all__ = [
    "DownstreamFrame",
    "FrameBase",
    "RpcError",
    "RpcFrame",
    "RpcPayload",
    "SessionEventFrame",
    "SessionEventFramePayload",
    "SessionMaintenanceFrame",
    "SessionMaintenancePayload",
    "SessionProjectionFrame",
    "SessionProjectionPayload",
    "SessionQueueFrame",
    "SessionSubscribedFrame",
    "SessionSubscribedPayload",
]
