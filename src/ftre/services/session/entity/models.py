"""Session 对外数据模型（TypedDict 投影）。

这些是 state.json 存储结构面向调用方的只读投影形状：
- SessionModel: 会话元信息（时间戳转为 epoch）
- MessageModel: 持久化 Msg 快照
- ExternalSessionModel: 外部平台会话绑定信息
- StatePageModel: state.json 分页只读视图

纯数据结构定义，不含行为。
"""
from __future__ import annotations

from typing import Any, TypedDict


class SessionModel(TypedDict):
    """会话元信息"""
    id: str              # 会话唯一标识（格式: '<channel_id>_sess_<hex12>'）
    channel_id: str      # 来源 channel（如 'ws' / 'cron' / 'cli'）
    title: str           # 对话标题
    workspace: str       # 当前工作区绝对路径（cwd 来源；为空表示未设置）
    metadata: dict       # 会话级元数据（JSON 解析后的 dict，如 plan 等）
    created_at: float    # 创建时间戳
    updated_at: float    # 最后活跃时间戳
    last_user_text: str  # 最后一条真实用户消息的文本摘要（跳过 compact 摘要；可能为空串）


class MessageModel(TypedDict):
    """持久化的 Msg 快照。"""
    id: str              # Msg.id
    session_id: str      # 所属会话 ID
    name: str
    role: str
    content: list[dict[str, Any]]
    metadata: dict[str, Any]
    created_at: str
    token: dict[str, Any] | None
    finished_at: str | None
    finished_reason: str | None
    structured_output: dict[str, Any] | None
    error: dict[str, Any] | None
    timestamp: float     # 排序/分页游标（由 created_at 派生）


class ExternalSessionModel(TypedDict):
    channel_id: str
    external_key: str
    session_id: str
    external_data: dict[str, Any]
    created_at: float
    updated_at: float


class StatePageModel(TypedDict):
    """state.json 的分页只读视图。messages 保持原始 Msg 结构。"""

    schema_version: int
    file_path: str
    session: dict[str, Any]
    messages: list[dict[str, Any]]
    metadata: dict[str, Any]
    truncated_message_ids: list[str]
    stats: dict[str, int | str | None]
    page: dict[str, int | bool]
