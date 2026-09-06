"""Session JSONL 的 assistant/chunk 无损物理压缩。

事件协议仍然是 ``assistant/chunk``；这里的 row 只存在于磁盘编码层，读取时
必须还原为原始事件，不能被 SessionLog 或下行 wire 看见。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

_KIND_TO_ROW = {
    "text": "text-chunks",
    "thinking": "thinking-chunks",
    "tool_result_text": "tool-result-chunks",
}
_ROW_TYPES = frozenset(_KIND_TO_ROW.values())
_MIN_RUN = 3
_CHUNK_DATA_KEYS = frozenset({"kind", "delta", "block_id", "tool_call_id"})
_EVENT_KEYS = frozenset({"type", "seq", "time", "message_id", "data"})


def _is_packable(event: dict[str, Any]) -> bool:
    if set(event) != _EVENT_KEYS or event.get("type") != "assistant/chunk":
        return False
    if not isinstance(event.get("seq"), int) or event["seq"] < 0:
        return False
    if not isinstance(event.get("time"), int):
        return False
    data = event.get("data")
    if not isinstance(data, dict) or set(data) != _CHUNK_DATA_KEYS:
        return False
    return (
        data.get("kind") in _KIND_TO_ROW
        and isinstance(data.get("delta"), str)
        and (data.get("block_id") is None or isinstance(data.get("block_id"), str))
        and (data.get("tool_call_id") is None or isinstance(data.get("tool_call_id"), str))
    )


def _continues(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    if not _is_packable(previous) or not _is_packable(current):
        return False
    if current["seq"] != previous["seq"] + 1:
        return False
    if current["data"]["kind"] != previous["data"]["kind"]:
        return False
    if current.get("message_id") != previous.get("message_id"):
        return False
    return all(
        current["data"].get(key) == previous["data"].get(key)
        for key in ("block_id", "tool_call_id")
    )


def _build_row(run: Sequence[dict[str, Any]]) -> dict[str, Any]:
    first = run[0]
    kind = first["data"]["kind"]
    return {
        "type": _KIND_TO_ROW[kind],
        "seq0": first["seq"],
        "time0": first["time"],
        "message_id": first.get("message_id"),
        "data": {
            "kind": kind,
            "block_id": first["data"].get("block_id"),
            "tool_call_id": first["data"].get("tool_call_id"),
            "dt": [
                event["time"] - run[index]["time"]
                for index, event in enumerate(run[1:])
            ],
            "deltas": [event["data"]["delta"] for event in run],
        },
    }


def pack_chunk_runs(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """将连续的同类 chunk 编码为 storage row，其他事件原样保留。"""
    output: list[dict[str, Any]] = []
    run: list[dict[str, Any]] = []

    def flush() -> None:
        if len(run) >= _MIN_RUN:
            output.append(_build_row(run))
        else:
            output.extend(run)
        run.clear()

    for event in events:
        if not _is_packable(event):
            flush()
            output.append(event)
            continue
        if run and not _continues(run[-1], event):
            flush()
        run.append(event)
    flush()
    return output


def is_chunk_row(record: dict[str, Any]) -> bool:
    """判断记录是否是本模块产生的 storage row。"""
    return record.get("type") in _ROW_TYPES


def decode_storage_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    """将一个普通事件或 packed row 还原为原始事件列表。"""
    if not is_chunk_row(record):
        return [record]
    required = {"type", "seq0", "time0", "message_id", "data"}
    if set(record) != required:
        raise ValueError("损坏的 chunk storage row：字段不完整")
    if not isinstance(record["seq0"], int) or not isinstance(record["time0"], int):
        raise TypeError("损坏的 chunk storage row：seq0/time0 非整数")
    data = record["data"]
    if not isinstance(data, dict) or set(data) != {"kind", "block_id", "tool_call_id", "dt", "deltas"}:
        raise ValueError("损坏的 chunk storage row：data 字段不完整")
    kind = data["kind"]
    if _KIND_TO_ROW.get(kind) != record["type"]:
        raise ValueError("损坏的 chunk storage row：kind/type 不匹配")
    deltas = data["deltas"]
    gaps = data["dt"]
    if (
        not isinstance(deltas, list)
        or len(deltas) < _MIN_RUN
        or not all(isinstance(item, str) for item in deltas)
        or not isinstance(gaps, list)
        or len(gaps) != len(deltas) - 1
        or not all(isinstance(item, int) for item in gaps)
    ):
        raise ValueError("损坏的 chunk storage row：delta/dt 长度或类型错误")
    if not all(value is None or isinstance(value, str) for value in (data["block_id"], data["tool_call_id"])):
        raise ValueError("损坏的 chunk storage row：block_id/tool_call_id 类型错误")

    events: list[dict[str, Any]] = []
    time_value = record["time0"]
    for index, delta in enumerate(deltas):
        if index:
            time_value += gaps[index - 1]
        events.append({
            "type": "assistant/chunk",
            "seq": record["seq0"] + index,
            "time": time_value,
            "message_id": record["message_id"],
            "data": {
                "kind": kind,
                "delta": delta,
                "block_id": data["block_id"],
                "tool_call_id": data["tool_call_id"],
            },
        })
    return events


def decode_storage_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量解码 storage rows，保持原事件顺序。"""
    output: list[dict[str, Any]] = []
    for record in records:
        output.extend(decode_storage_record(record))
    return output


__all__ = [
    "decode_storage_record",
    "decode_storage_records",
    "is_chunk_row",
    "pack_chunk_runs",
]
