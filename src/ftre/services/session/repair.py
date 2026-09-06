"""崩溃恢复策略——torn-tail 截断后的合成关闭事件（PRD-F43 FR7）。

load 时对末尾不完整 turn 的处理：
- 为未闭合的 tool_call 合成 tool/result(state=interrupted)；
- 为开着的 turn 合成 turn/end(outcome=cancelled, reason=crashed)。

合成事件属于会话事实：调用方必须在 ``SessionLog.load`` 前把返回值写回
session.jsonl，避免每次重启重复 repair。
"""
from __future__ import annotations

import time
from typing import Any


def repair_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """返回 events + 合成关闭事件（原列表不被修改）。"""
    result = list(events)
    open_tool_calls: dict[str, tuple[str, str]] = {}  # id → (name, message_id)
    open_turn: dict[str, Any] | None = None
    open_assistant_message_id: str | None = None

    for event in events:
        type_ = event.get("type")
        data = event.get("data") or {}
        if type_ == "turn/start":
            open_turn = dict(data)
            open_assistant_message_id = None
            open_tool_calls = {}
        elif type_ == "turn/end":
            open_turn = None
            open_assistant_message_id = None
            open_tool_calls = {}
        elif type_ == "tool/call-start":
            if open_turn is None:
                continue
            if event.get("message_id"):
                open_assistant_message_id = str(event["message_id"])
            tool_call_id = str(data.get("tool_call_id") or "")
            if tool_call_id:
                open_tool_calls[tool_call_id] = (
                    str(data.get("name") or ""),
                    str(event.get("message_id") or ""),
                )
        elif type_ in {"assistant/chunk", "assistant/message", "hint/message"}:
            if open_turn is not None and event.get("message_id"):
                open_assistant_message_id = str(event["message_id"])
        elif type_ == "tool/result":
            tool_call_id = str(data.get("tool_call_id") or "")
            if tool_call_id:
                open_tool_calls.pop(tool_call_id, None)

    pending_calls = [
        (tool_call_id, name, message_id)
        for tool_call_id, (name, message_id) in open_tool_calls.items()
    ]
    if open_turn is None and not pending_calls:
        return result

    now = int(time.time() * 1000)
    next_seq = len(events)

    for tool_call_id, name, call_message_id in pending_calls:
        result.append({
            "type": "tool/result",
            "seq": next_seq,
            "time": now,
            "message_id": call_message_id or None,
            "data": {
                "tool_call_id": tool_call_id,
                "name": name,
                "output": [{"type": "text", "text": "[CRASHED] 执行中断，结果未知"}],
                "state": "interrupted",
                "metadata": {"synthetic": True},
            },
        })
        next_seq += 1

    if open_turn is not None:
        turn_id = str(open_turn.get("turn_id") or "")
        request_id = str(open_turn.get("request_id") or "")
        result.append({
            "type": "turn/end",
            "seq": next_seq,
            "time": now,
            "message_id": open_assistant_message_id,
            "data": {
                "turn_id": turn_id,
                "request_id": request_id,
                "outcome": "cancelled",
                "reason": "crashed",
                "error": {"code": "crashed", "message": "Gateway 异常退出，turn 由 repair 收尾"},
                "usage": None,
                "iterations": 0,
                "metadata": {"synthetic": True},
            },
        })
    return result


__all__ = ["repair_events"]
