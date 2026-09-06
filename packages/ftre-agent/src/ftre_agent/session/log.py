"""SessionLog——会话事件日志的内存提交点（PRD-F43 FR1/FR2）。

append 是同步纯内存操作：JSON 纯净单遍校验 + 深拷贝隔离 + seq=log.length 严格
连续 + 重入禁止 + fire-and-forget 通知（观察者异常被隔离）。热路径零 I/O；
持久化（write-behind）与帧转发由订阅方各自承担（DSH 模式）。

崩溃恢复走 load()：seq 连续性校验 + 未知事件 ignorable 策略 + 幂等索引重建。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from .events import ALL_EVENT_TYPES, IGNORABLE_EVENT_TYPES

logger = logging.getLogger(__name__)

Subscriber = Callable[[dict[str, Any]], None]


def _snapshot_json(value: Any, path: str = "data") -> Any:
    """单遍完成「校验 + 深拷贝」：只放行 JSON 纯净值，拒绝可变/危险值。

    与 DSH snapshotJsonValue 同职责：防止 getter 双读作弊、非序列化对象在
    append 后才暴露问题。
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise TypeError(f"{path}: 非有限 float 不是 JSON 纯净值")
        return value
    if isinstance(value, list):
        return [_snapshot_json(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path}: dict key 必须是 str，得到 {type(key).__name__}")
            out[key] = _snapshot_json(item, f"{path}.{key}")
        return out
    raise TypeError(f"{path}: {type(value).__name__} 不是 JSON 纯净值")


def _request_fingerprint(content: Any) -> str:
    """对用户内容计算稳定指纹（剔除 block id 等随机标识）。

    TextBlock 等内容块的 id 是生成时随机的；重放等价内容（如 steering
    重建）会携带新 id。指纹只对语义字段（text/type/media 等）计算，
    保证"同 request_id + 等价内容"可幂等重放，不同内容仍被拒绝。
    """

    def _strip_ids(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: _strip_ids(item)
                for key, item in value.items()
                if key != "id"
            }
        if isinstance(value, list):
            return [_strip_ids(item) for item in value]
        return value

    encoded = json.dumps(
        _strip_ids(content), ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


class SessionLog:
    """一个会话的 append-only 事件日志（内存权威态）。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._events: list[dict[str, Any]] = []
        self._subscribers: list[Subscriber] = []
        self._appending = False
        # request_id → message_id（user/message 幂等索引）
        self._user_requests: dict[str, str] = {}
        self._fingerprints: dict[str, str] = {}
        # request_id → outcome（turn/end 索引，供 request_state 查询）
        self._turn_outcomes: dict[str, str] = {}

    # ── 查询 ────────────────────────────────────────────────

    @property
    def last_seq(self) -> int:
        """最后一条事件的 seq；空日志为 -1。"""
        return len(self._events) - 1

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        """只读视图。事件为 append 时深拷贝的冻结事实，约定不修改。"""
        return tuple(self._events)

    def tail(self, after_seq: int, limit: int) -> tuple[list[dict[str, Any]], bool]:
        """返回 seq > after_seq 的至多 limit 条事件与 has_more。"""
        start = max(0, after_seq + 1)
        page = self._events[start:start + max(1, limit)]
        has_more = start + len(page) < len(self._events)
        return list(page), has_more

    def has_user_request(self, request_id: str) -> bool:
        return bool(request_id) and request_id in self._user_requests

    def request_state(self, request_id: str) -> str | None:
        """request 的执行状态：completed / failed（turn 已终态）；None=未执行。"""
        if not request_id:
            return None
        outcome = self._turn_outcomes.get(request_id)
        if outcome == "completed":
            return "completed"
        if outcome in ("error", "cancelled"):
            return "failed"
        return None

    # ── 订阅 ────────────────────────────────────────────────

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        """注册同步订阅者；返回撤销函数。append 后按序调用，异常被隔离。"""
        self._subscribers.append(fn)

        def dispose() -> None:
            try:
                self._subscribers.remove(fn)
            except ValueError:
                pass

        return dispose

    # ── 提交 ────────────────────────────────────────────────

    def append(
        self,
        type_: str,
        data: BaseModel | dict[str, Any],
        *,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """提交一个事件。返回冻结的事件 dict（含 seq/time）。

        Raises:
            TypeError: data 含非 JSON 纯净值。
            ValueError: user/message request_id 冲突 / 未知事件类型 / 重入。
        """
        if self._appending:
            raise ValueError("session append cannot reenter while publishing")
        if type_ not in ALL_EVENT_TYPES:
            raise ValueError(f"未知事件类型: {type_!r}（新事件须先登记 ftre_agent.session.events）")
        payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else data
        payload = _snapshot_json(payload)
        message_id = message_id or None

        if type_ == "user/message":
            request_id = str(payload.get("request_id") or "")
            if request_id:
                self._check_user_request(request_id, payload)

        event: dict[str, Any] = {
            "type": type_,
            "seq": len(self._events),
            "time": int(time.time() * 1000),
            "message_id": message_id,
            "data": payload,
        }
        self._appending = True
        try:
            self._events.append(event)
            if event["type"] == "user/message":
                request_id = str(payload.get("request_id") or "")
                if request_id:
                    self._user_requests[request_id] = message_id or ""
                    self._fingerprints[request_id] = _request_fingerprint(payload.get("content"))
            if event["type"] == "turn/end":
                request_id = str(payload.get("request_id") or "")
                if request_id:
                    self._turn_outcomes[request_id] = str(payload.get("outcome") or "")
            self._notify(event)
        finally:
            self._appending = False
        return event

    def append_user_message(
        self,
        *,
        request_id: str,
        content: list[Any],
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any] | None:
        """幂等提交用户消息：request_id 已存在时跳过（返回 None）。

        message_id 缺省时由 request_id 派生稳定值（崩溃重放不产生重复气泡）。
        """
        if request_id and request_id in self._user_requests:
            existing_fp = self._fingerprints.get(request_id)
            if existing_fp is not None and existing_fp != _request_fingerprint(content):
                raise ValueError(f"request_id 已绑定不同内容: {request_id}")
            return None
        if message_id is None:
            if request_id:
                digest = hashlib.sha256(
                    f"{self.session_id}\0{request_id}".encode()
                ).hexdigest()[:24]
                message_id = f"user_{digest}"
            else:
                from ftre_agent.message._msg import _gen_id

                message_id = _gen_id()
        data = {
            "content": content,
            "metadata": dict(metadata or {}),
            "request_id": request_id or "",
        }
        return self.append("user/message", data, message_id=message_id)

    def _check_user_request(self, request_id: str, payload: dict[str, Any]) -> None:
        existing_fp = self._fingerprints.get(request_id)
        if existing_fp is not None and existing_fp != _request_fingerprint(payload.get("content")):
            raise ValueError(f"request_id 已绑定不同内容: {request_id}")

    def _notify(self, event: dict[str, Any]) -> None:
        for fn in list(self._subscribers):
            try:
                fn(event)
            except Exception:
                logger.exception(
                    "[session-log] subscriber failed session=%s seq=%s",
                    self.session_id, event.get("seq"),
                )

    # ── 恢复 ────────────────────────────────────────────────

    def load(self, events: list[dict[str, Any]]) -> None:
        """从持久化序列重建内存日志（重启恢复入口）。

        校验 seq 连续（index i 必须 seq=i）；未知事件类型按 ignorable 策略
        决定跳过或拒绝；重建幂等索引。repair（合成关闭事件）在调用方完成后
        再 load 或随后 append（seq 自动接续）。
        """
        self._events.clear()
        self._user_requests.clear()
        self._fingerprints.clear()
        self._turn_outcomes.clear()
        for index, event in enumerate(events):
            type_ = str(event.get("type") or "")
            if type_ not in ALL_EVENT_TYPES:
                if type_ in IGNORABLE_EVENT_TYPES:
                    continue
                raise ValueError(f"日志含未知事件类型 {type_!r}（seq={index}）且未标 ignorable")
            if event.get("seq") != index:
                raise ValueError(
                    f"seed 事件 seq 不连续: index={index} seq={event.get('seq')!r}"
                )
            snapshot = _snapshot_json(event.get("data"))
            restored: dict[str, Any] = {
                "type": type_,
                "seq": index,
                "time": int(event.get("time") or 0),
                "message_id": event.get("message_id"),
                "data": snapshot,
            }
            self._events.append(restored)
            self._reindex(restored)

    def _reindex(self, event: dict[str, Any]) -> None:
        if event["type"] == "user/message":
            request_id = str(event["data"].get("request_id") or "")
            if request_id:
                self._user_requests[request_id] = event.get("message_id") or ""
                self._fingerprints[request_id] = _request_fingerprint(event["data"].get("content"))
        elif event["type"] == "turn/end":
            request_id = str(event["data"].get("request_id") or "")
            if request_id:
                self._turn_outcomes[request_id] = str(event["data"].get("outcome") or "")


__all__ = ["SessionLog", "_snapshot_json"]
