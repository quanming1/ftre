"""gen_wire_types.py —— Pydantic wire 契约 → desktop TypeScript 类型（PRD-F41 FR9/AC7）。

唯一事实源（Python 侧）：
  - ``packages/ftre-agent/src/ftre_agent/session/events.py``   事件表（13 种 + 信封）
  - ``packages/ftre-agent/src/ftre_agent/message/{_msg,_block}.py``  Msg / ContentBlock
  - ``src/ftre/services/messaging/wire.py``                     帧表（6 种 + payload 契约）

产物：
  - ``E:\\binn\\ftre-desktop\\packages\\renderer\\src\\types\\wire.gen.ts``
  - 同目录 ``wire.golden.json``（服务端 golden fixture 的同步拷贝，供跨语言
    对拍测试消费；源文件在 ftre 仓 ``packages/ftre-agent/tests/fixtures/``）

幂等性：脚本无时间戳、无随机排序——重复运行产物必须 byte-identical
（``py scripts/gen_wire_types.py`` 双跑 diff 为空即 AC7 门禁）。

TS 侧命名映射（message 域加 Wire 前缀，事件/帧域保持原名）与字段可选性
规则见下方 ``TS_NAME_MAP`` / ``REQUIRED_FIELDS`` / ``FIELD_TYPE_OVERRIDES``
三张声明表——形状与类型来自 Pydantic；仅"构造侧可选性"（客户端字面量
构造是否可省略某字段）与个别开放字段（如 Msg.content 兼容 user parts）
无法从运行时模型推导，以声明表固化。
"""
from __future__ import annotations

import json
import shutil
import sys
import types as pytypes
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "ftre-agent" / "src"))

DESKTOP_TYPES_DIR = Path(r"E:\binn\ftre-desktop\packages\renderer\src\types")
GOLDEN_FIXTURE_SRC = REPO_ROOT / "packages" / "ftre-agent" / "tests" / "fixtures" / "session_events_golden.json"

from ftre_agent.message import (
    DataBlock,
    HintBlock,
    Msg,
    MsgToken,
    TextBlock,
    ThinkingBlock,
    TokenUsage,
    ToolCallBlock,
    ToolResultBlock,
)
from ftre_agent.message._block import Base64Source, URLSource
from ftre_agent.session import events as session_events

from ftre.services.messaging import wire

HEADER = """\
/**
 * GENERATED FILE —— 禁止手写（PRD-F41 FR9/AC7）。
 * 由 ftre 仓 `scripts/gen_wire_types.py` 从 Pydantic 契约生成：
 *   - packages/ftre-agent/src/ftre_agent/session/events.py   事件表（13 种）
 *   - packages/ftre-agent/src/ftre_agent/message/{_msg,_block}.py  Msg/Block
 *   - src/ftre/services/messaging/wire.py                    帧表（6 种）
 * 重新生成：`py scripts/gen_wire_types.py`；产物 diff 必须为空。
 *
 * 帧信封：{v: 1, session_id, type, payload}，共 6 种帧（F41 §4.4）。
 * 事件信封：{type, seq, time, message_id?, data}，共 13 种事件（F41 §4.2）。
 */

"""

# ── 命名映射：message 域类型在 TS 侧加 Wire 前缀；事件/帧域保持原名 ──
TS_NAME_MAP: dict[type, str] = {
    TokenUsage: "WireTokenUsage",
    MsgToken: "WireMsgToken",
    TextBlock: "WireTextBlock",
    ThinkingBlock: "WireThinkingBlock",
    DataBlock: "WireDataBlock",
    HintBlock: "WireHintBlock",
    ToolCallBlock: "WireToolCallBlock",
    ToolResultBlock: "WireToolResultBlock",
    Base64Source: "WireBase64Source",
    URLSource: "WireURLSource",
    Msg: "WireMsg",
    session_events.UserPart: "WireUserPart",
    wire.RpcError: "WireRpcError",
}

# ── 构造侧可选性声明：这些字段"总是被序列化"，TS 侧保持必填 ──
REQUIRED_FIELDS: set[tuple[str, str]] = {
    # Msg 身份字段（wire dump 恒有）
    ("Msg", "name"), ("Msg", "content"), ("Msg", "id"),
    ("Msg", "metadata"), ("Msg", "created_at"),
    # 工具块核心字段
    ("ToolCallBlock", "arguments"),
    # 事件 data：运行时由模型构造，字段恒在
    ("UserMessageData", "content"), ("UserMessageData", "metadata"),
    ("UserMessageData", "request_id"),
    ("ToolResultData", "output"), ("ToolResultData", "metadata"),
    ("CompactData", "mode"), ("CompactData", "summary_text"),
    ("CompactData", "through_message_id"), ("CompactData", "trigger"),
    ("CompactData", "tokens_before"), ("CompactData", "tokens_after"),
    ("CompactData", "tool_results"),
    ("AssistantChunkData", "delta"),
    ("ToolCallStartData", "arguments"),
    ("ApprovalAskedData", "arguments"), ("ApprovalAskedData", "reason"),
    ("TurnStartData", "request_id"), ("TurnStartData", "trigger"),
    ("TurnStartData", "command_name"), ("TurnStartData", "agent_id"),
    ("TurnStartData", "model"),
    ("TurnEndData", "request_id"), ("TurnEndData", "reason"),
    ("TurnEndData", "iterations"),
    ("SessionStatusData", "status"), ("SessionStatusData", "reason"),
    # 帧载荷
    ("SessionSubscribedPayload", "last_seq"),
    ("SessionSubscribedPayload", "status"),
    ("SessionProjectionPayload", "seq"),
    ("SessionMaintenancePayload", "value"),
    ("WireRpcError", "code"), ("WireRpcError", "message"),
}

# ── 开放字段类型声明（运行时宽松 / TS 契约侧收紧或放宽）──
FIELD_TYPE_OVERRIDES: dict[tuple[str, str], str] = {
    # 事件信封透传（运行时 dict 直通，未知事件由 F41 FR6 兜底）
    ("SessionEventFramePayload", "event"): "SessionEvent",
    # whole-value Msg 载荷
    ("AssistantMessageData", "message"): "WireMsg",
    # 客户端 user 消息 content 携带原始 parts（服务端 dump 为 Block）
    ("Msg", "content"): "Array<WireBlock | WireUserPart>",
}

# 开放形状（extra=allow）的模型 → TS 侧加索引签名
OPEN_MODELS: set[str] = {"WireUserPart"}

# ── 发射顺序（确定性输出）──────────────────────────────────────
MESSAGE_MODELS: list[tuple[type, str]] = [
    (TokenUsage, "单次或累计的 token 用量。"),
    (MsgToken, "assistant Reply 的 token 用量快照。"),
    (Base64Source, "base64 数据源。"),
    (URLSource, "URL 数据源。"),
    (TextBlock, "纯文本内容块。"),
    (ThinkingBlock, "模型推理过程（思维链）内容块。"),
    (DataBlock, "二进制数据块（图片等），source 为 base64 或 URL。"),
    (HintBlock, "提示块（默认隐藏渲染，注入上下文）。"),
    (ToolCallBlock, "工具调用块；arguments 为 whole-value。"),
    (ToolResultBlock, "工具执行结果块。"),
    (Msg, "assistant/message whole-value 载荷；HTTP /messages 记录共用形状。"),
    (session_events.UserPart, "user/message content 的原始 part（type 判别，其余字段开放）。"),
]

EVENT_DATA_MODELS: list[type] = [
    session_events.UserMessageData,
    session_events.AssistantMessageData,
    session_events.ToolResultData,
    session_events.HintData,
    session_events.CompactData,
    session_events.AssistantChunkData,
    session_events.ToolCallStartData,
    session_events.ToolResultStartData,
    session_events.ApprovalAskedData,
    session_events.TurnStartData,
    session_events.TurnRetryData,
    session_events.TurnEndData,
    session_events.SessionStatusData,
]

FRAME_PAYLOAD_MODELS: list[type] = [
    wire.SessionEventFramePayload,
    wire.SessionSubscribedPayload,
    wire.SessionProjectionPayload,
    wire.SessionMaintenancePayload,
    wire.RpcPayload,
    wire.RpcError,
]

EVENT_ALIAS_MAP: dict[type, str] = {
    session_events.UserMessageData: "UserMessageEvent",
    session_events.AssistantMessageData: "AssistantMessageEvent",
    session_events.ToolResultData: "ToolResultEvent",
    session_events.HintData: "HintMessageEvent",
    session_events.CompactData: "CompactMessageEvent",
    session_events.AssistantChunkData: "AssistantChunkEvent",
    session_events.ToolCallStartData: "ToolCallStartEvent",
    session_events.ToolResultStartData: "ToolResultStartEvent",
    session_events.ApprovalAskedData: "ApprovalAskedEvent",
    session_events.TurnStartData: "TurnStartEvent",
    session_events.TurnRetryData: "TurnRetryEvent",
    session_events.TurnEndData: "TurnEndEvent",
    session_events.SessionStatusData: "SessionStatusEvent",
}

# 事件分组的发射顺序（表面 5 → 流式 4 → 生命周期 4，与 F41 §4.2 一致）
EVENT_TYPE_ORDER: list[str] = [
    "user/message", "assistant/message", "tool/result", "hint/message", "compact/message",
    "assistant/chunk", "tool/call-start", "tool/result-start", "approval/asked",
    "turn/start", "turn/retry", "turn/end", "session/status",
]

FRAME_CLASSES: list[type] = [
    wire.SessionEventFrame, wire.SessionSubscribedFrame, wire.SessionQueueFrame,
    wire.SessionProjectionFrame, wire.SessionMaintenanceFrame, wire.RpcFrame,
]


# ── 类型解析 ────────────────────────────────────────────────────

def ts_name(cls: type) -> str:
    return TS_NAME_MAP.get(cls, cls.__name__)


def unwrap_annotated(annotation: Any) -> Any:
    if hasattr(annotation, "__metadata__"):
        return get_args(annotation)[0]
    return annotation


def resolve_type(annotation: Any) -> str:
    """Python 注解 → TS 类型字符串（不含 optional/null 标记）。"""
    annotation = unwrap_annotated(annotation)

    if annotation is Any:
        return "any"
    if isinstance(annotation, type) and annotation in TS_NAME_MAP:
        return TS_NAME_MAP[annotation]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return ts_name(annotation)

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is Literal:
        return " | ".join(json.dumps(arg) for arg in args)
    if origin in (Union, pytypes.UnionType):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return resolve_type(non_none[0])
        return " | ".join(resolve_type(a) for a in non_none)
    if origin is list:
        if not args or args[0] is Any:
            return "any[]"
        return f"{resolve_type(args[0])}[]"
    if origin is dict:
        return "Record<string, any>"
    if annotation is list:
        return "any[]"
    if annotation is dict:
        return "Record<string, any>"
    if annotation is str:
        return "string"
    if annotation in (int, float):
        return "number"
    if annotation is bool:
        return "boolean"
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        # 枚举字段在客户端按开放字符串消费（服务端已校验）
        return "string"
    return "any"


def model_fields_sorted(cls: type) -> list[tuple[str, str, bool, bool]]:
    """(字段名, TS 类型, 可选, 可空) 按模型声明顺序返回。"""
    out: list[tuple[str, str, bool, bool]] = []
    for name, info in cls.model_fields.items():
        annotation = unwrap_annotated(info.annotation)
        override = FIELD_TYPE_OVERRIDES.get((cls.__name__, name))
        ts_type = override if override is not None else resolve_type(annotation)

        nullable = False
        if override is not None:
            nullable = "null" in override
        else:
            origin = get_origin(annotation)
            if origin in (Union, pytypes.UnionType) and type(None) in get_args(annotation):
                nullable = True
            if ts_type == "any":
                # Any 吸收 null，避免 `any | null` 噪声
                nullable = False

        has_default = not info.is_required()
        required_override = (cls.__name__, name) in REQUIRED_FIELDS
        # 判别字段（type: Literal[...]）恒被序列化，保持必填
        is_discriminator = name == "type" and ts_type.startswith('"')
        optional = has_default and not required_override and not is_discriminator

        out.append((name, ts_type, optional, nullable))
    return out


# ── 片段发射 ────────────────────────────────────────────────────

def emit_interface(cls: type, doc: str | None = None) -> list[str]:
    lines: list[str] = []
    header_doc = doc or (cls.__doc__.strip().splitlines()[0] if cls.__doc__ else "")
    if header_doc:
        lines.append(f"/** {header_doc} */")
    lines.append(f"export interface {ts_name(cls)} {{")
    for name, ts_type, optional, nullable in model_fields_sorted(cls):
        opt = "?" if optional else ""
        null_suffix = " | null" if nullable else ""
        lines.append(f"  {name}{opt}: {ts_type}{null_suffix};")
    if ts_name(cls) in OPEN_MODELS:
        lines.append("  [key: string]: unknown;")
    lines.append("}")
    return lines


def join_literals(items: list[str], indent: str = "  | ") -> str:
    return ("\n" + indent).join(json.dumps(item) for item in items)


def emit_union(name: str, comment_lines: list[str], items: list[str]) -> list[str]:
    lines = [f"export type {name} ="]
    lines.extend(comment_lines)
    lines.append("  | " + join_literals(items).lstrip(" "))
    lines.append(";")
    return lines


def main() -> int:
    out: list[str] = [HEADER]

    # ── 帧表 ──
    out.append("// ─── 帧表（6 种）──────────────────────────────────────────────")
    out.append("")
    frame_literals = [cls.model_fields["type"].default for cls in FRAME_CLASSES]
    out.extend(emit_union("DownstreamFrameType", [], frame_literals))
    out.append("")
    out.append("/** 下行帧公共信封；无帧级 seq（事件帧内 seq 为权威）。 */")
    out.append("export interface WireFrame<TPayload = unknown> {")
    out.append("  v: 1;")
    out.append("  session_id: string;")
    out.append("  type: DownstreamFrameType;")
    out.append("  payload?: TPayload;")
    out.append("}")
    out.append("")
    out.append("// ─── 帧载荷 ─────────────────────────────────────────────────────")
    out.append("")
    for payload_cls in FRAME_PAYLOAD_MODELS:
        out.extend(emit_interface(payload_cls))
        out.append("")

    # ── 事件信封 ──
    out.append("// ─── 事件信封（13 种）─────────────────────────────────────────")
    out.append("")
    assert set(EVENT_TYPE_ORDER) == set(session_events.ALL_EVENT_TYPES), "事件表与生成器分组不一致"
    surface = [t for t in EVENT_TYPE_ORDER if t in session_events.SURFACE_EVENT_TYPES]
    rest = [t for t in EVENT_TYPE_ORDER if t not in session_events.SURFACE_EVENT_TYPES]
    out.extend(emit_union(
        "SessionEventType",
        ["  // 事件全集：表面 5 + 流式 4 + 生命周期 4（分组见 F41 §4.2）"],
        surface + rest,
    ))
    out.append("")
    out.append("export interface SessionEvent<TData = any> {")
    out.append("  type: SessionEventType | (string & {});")
    out.append("  /** 会话内从 0 严格连续的事件序号。 */")
    out.append("  seq: number;")
    out.append("  /** epoch 毫秒。 */")
    out.append("  time: number;")
    out.append("  /** 仅表面事件与 chunk 事件携带（chunk 归属目标消息）。 */")
    out.append("  message_id?: string | null;")
    out.append("  data: TData;")
    out.append("}")
    out.append("")

    # ── 事件 data 载荷 ──
    out.append("// ─── 事件 data 载荷 ────────────────────────────────────────────")
    out.append("")
    for data_cls in EVENT_DATA_MODELS:
        out.extend(emit_interface(data_cls))
        out.append("")

    # ── Msg 镜像 ──
    out.append("// ─── Msg dump（assistant/message 载荷 / HTTP messages 共用形状）──")
    out.append("")
    for cls, doc in MESSAGE_MODELS:
        out.extend(emit_interface(cls, doc))
        out.append("")
    out.append("export type WireBlock =")
    out.append("  | WireTextBlock")
    out.append("  | WireThinkingBlock")
    out.append("  | WireDataBlock")
    out.append("  | WireHintBlock")
    out.append("  | WireToolCallBlock")
    out.append("  | WireToolResultBlock;")
    out.append("")

    # ── 事件判别联合别名 ──
    out.append("// ─── 事件判别联合（fold 引擎 switch 使用）─────────────────────")
    out.append("")
    for data_cls in EVENT_DATA_MODELS:
        out.append(f"export type {EVENT_ALIAS_MAP[data_cls]} = SessionEvent<{data_cls.__name__}>;")
    out.append("")

    content = "\n".join(out).rstrip() + "\n"

    # ── 写出（幂等：固定 LF、无时间戳）──
    target = DESKTOP_TYPES_DIR / "wire.gen.ts"
    target.write_text(content, encoding="utf-8", newline="\n")
    print(f"[gen-wire] wrote {target} ({len(content.splitlines())} lines)")

    # ── golden fixture 同步拷贝（跨语言对拍共享；F42 AC1 / F43 AC5）──
    if GOLDEN_FIXTURE_SRC.exists():
        fixture_target = DESKTOP_TYPES_DIR / "wire.golden.json"
        shutil.copyfile(GOLDEN_FIXTURE_SRC, fixture_target)
        print(f"[gen-wire] synced fixture -> {fixture_target}")
    else:
        print(f"[gen-wire] WARN: golden fixture 不存在，跳过拷贝: {GOLDEN_FIXTURE_SRC}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
