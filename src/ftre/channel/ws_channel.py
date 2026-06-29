"""
WebSocket Channel

启动时创建 FastAPI + WebSocket 端点。
多个客户端连接共享同一个全局 AgentLoop。

连接 / session 模型：
- 一个客户端 = 一条物理 WebSocket。
- 一条 WebSocket 可以 attach 到多个 session（前端同时关注多个会话）。
- session_id → set[WebSocket]：同一个 session 也允许被多个客户端 attach（多端同步）。
- 客户端必须显式发送 attach 帧（或在 user_message/cancel 时隐式 attach 当前 session），
  后端才会把这条 ws 加入 session 的推送目标。
"""
import base64
import binascii
import json
import logging
import asyncio
from collections import deque
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from ftre.bus import BusMessage, EventBus, GLOBAL_SESSION
from .base import Channel

logger = logging.getLogger(__name__)


# ============================================================
# 附件校验（user_message.data.attachments）
# ============================================================

# 允许的图片 MIME
ALLOWED_IMAGE_MIME = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})

# 单张附件 base64 解码后的字节数上限
MAX_ATTACHMENT_BYTES = 3 * 1024 * 1024  # 3 MB

# 单条消息允许的最大附件数
MAX_ATTACHMENTS_PER_MESSAGE = 8

VOLATILE_BUFFER_MAX_EVENTS = 1000
# 这些事件只代表“正在流式输出的增量”，不会直接落到 session DB。
# 如果客户端在它们产生时还没 attach，刷新后只读 DB 会看不到这些片段，
# 所以在 WS channel 内短暂缓存，等客户端 attach 后补发。
VOLATILE_EVENT_TYPES = frozenset({
    "assistant_message",
    "reasoning",
    "tool_call_streaming",
})
# 对应的稳定事件到达后，说明这类流式增量已经被最终事件覆盖/持久化，
# 可以从 volatile buffer 删除，避免客户端 attach 后看到旧草稿。
VOLATILE_CLEAR_BY_TYPE = {
    "assistant_message_complete": {"assistant_message"},
    "reasoning_complete": {"reasoning"},
    "tool_call": {"tool_call_streaming"},
    "tool_result": {"tool_call_streaming"},
}
# 一轮执行结束、失败或进入重试后，旧的临时流式片段都不应该再 replay。
VOLATILE_CLEAR_ALL_TYPES = frozenset({"done", "error", "retry"})


class _VolatileReplayBuffer:
    """缓存未入库的流式事件，供客户端 attach 时补发。

    这个类只处理 WS 层的临时恢复，不替代数据库历史：
    - DB 负责稳定消息（*_complete、tool_call、tool_result 等）。
    - 这里负责还没稳定落库的流式片段（assistant_message、reasoning 等）。
    - attach 时 replay 当前 session 的临时片段，随后客户端继续接收 live 流。
    """

    def __init__(self) -> None:
        # session_id -> 最近的 volatile 下行帧。deque 自带 maxlen，防止无限增长。
        self._buffers: dict[str, deque[dict[str, Any]]] = {}
        # 每个 session 独立递增 seq，客户端用 session_id + seq 去重 replay/live 重叠帧。
        self._next_seq: dict[str, int] = {}
        # send() 和 attach replay 都在事件循环里运行；加锁保证 buffer/seq 快照一致。
        self._lock = asyncio.Lock()

    async def track(self, msg: BusMessage, metadata: dict[str, Any]) -> dict[str, Any]:
        """检查一条 agent_event 是否需要缓存，并返回下发时要携带的 metadata。"""
        ev_type = msg.data.get("type") if isinstance(msg.data, dict) else None
        if not isinstance(ev_type, str):
            return metadata

        session_id = msg.to_session
        # done/error/retry 表示这一轮临时流结束，整个 session 的 volatile 草稿都清掉。
        if ev_type in VOLATILE_CLEAR_ALL_TYPES:
            await self._clear(session_id)
            return metadata

        # 稳定事件到达后，只清理它所覆盖的那一类流式草稿。
        # 例如 assistant_message_complete 只清 assistant_message，不影响 tool_call_streaming。
        clear_types = VOLATILE_CLEAR_BY_TYPE.get(ev_type)
        if clear_types:
            await self._clear(session_id, event_types=clear_types)
            return metadata

        # 其他事件要么已经会入库，要么不是流式片段，不进入 volatile buffer。
        if ev_type not in VOLATILE_EVENT_TYPES:
            return metadata

        async with self._lock:
            seq = self._next_seq.get(session_id, 1)
            self._next_seq[session_id] = seq + 1
            # buffer 里存的是最终要发给客户端的帧形态。
            # replay 时直接原样发送，不需要重新理解 BusMessage。
            event_metadata = {
                **metadata,
                "channel_id": msg.to_channel,
                "session_id": session_id,
                "volatile_seq": seq,
            }
            self._buffers.setdefault(
                session_id,
                deque(maxlen=VOLATILE_BUFFER_MAX_EVENTS),
            ).append({
                "id": msg.id,
                "type": msg.type,
                "data": msg.data,
                "metadata": event_metadata,
            })

        # live 下发的同一帧也带 volatile_seq。
        # 如果客户端刚 attach 时 replay 和 live 发生重叠，可以靠 seq 去重。
        return {
            **metadata,
            "volatile_seq": seq,
        }

    async def replay(self, session_id: str, ws: WebSocket) -> None:
        """把某个 session 当前还没稳定落库的流式片段补发给刚 attach 的 ws。"""
        for item in await self._snapshot(session_id):
            payload = {
                **item,
                "metadata": {
                    **(item.get("metadata") or {}),
                },
            }
            try:
                await ws.send_text(json.dumps(payload, ensure_ascii=False, default=str))
            except Exception as e:
                logger.debug(f"[ws-channel] volatile replay failed: {e}")
                break

    async def _clear(
        self,
        session_id: str,
        *,
        event_types: set[str] | frozenset[str] | None = None,
    ) -> None:
        async with self._lock:
            if event_types is None:
                # 不传 event_types 表示整轮结束，直接清空该 session 的全部 volatile 缓存。
                self._buffers.pop(session_id, None)
                return

            buf = self._buffers.get(session_id)
            if not buf:
                return

            # 只删除被稳定事件覆盖的类型，保留其他仍在流式中的类型。
            kept = deque(
                (
                    item for item in buf
                    if (item.get("data") or {}).get("type") not in event_types
                ),
                maxlen=VOLATILE_BUFFER_MAX_EVENTS,
            )
            if kept:
                self._buffers[session_id] = kept
            else:
                self._buffers.pop(session_id, None)

    async def _snapshot(self, session_id: str) -> list[dict[str, Any]]:
        # replay 期间不持锁发送网络数据，先复制快照，避免阻塞 send() 写入。
        async with self._lock:
            return [dict(item) for item in self._buffers.get(session_id, ())]


def _validate_attachments(attachments) -> tuple[bool, str]:
    """
    校验 user_message.data.attachments。
    返回 (ok, error_message)。无附件视为合法。
    """
    if attachments is None:
        return True, ""
    if not isinstance(attachments, list):
        return False, "attachments 必须是数组"
    if len(attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
        return False, f"附件数量超过上限 {MAX_ATTACHMENTS_PER_MESSAGE}"

    for i, att in enumerate(attachments):
        if not isinstance(att, dict):
            return False, f"attachments[{i}] 必须是对象"

        att_type = att.get("type")
        if att_type != "image":
            return False, f"attachments[{i}].type 仅支持 'image'，收到 {att_type!r}"

        mime = att.get("mime_type", "")
        if mime not in ALLOWED_IMAGE_MIME:
            return False, f"attachments[{i}].mime_type 不支持: {mime!r}"

        b64 = att.get("data")
        if not isinstance(b64, str) or not b64:
            return False, f"attachments[{i}].data 缺失或非字符串"

        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return False, f"attachments[{i}].data 不是合法 base64"

        if len(raw) > MAX_ATTACHMENT_BYTES:
            limit_mb = MAX_ATTACHMENT_BYTES / 1024 / 1024
            actual_mb = len(raw) / 1024 / 1024
            return False, (
                f"attachments[{i}] 大小 {actual_mb:.2f}MB 超过上限 {limit_mb:.0f}MB"
            )

    return True, ""


def _persist_attachments(attachments: list | None) -> None:
    """将 attachments 中的 base64 data 落盘，替换为 path。

    在 _validate_attachments 校验通过后调用。原地修改 attachments 列表。
    """
    if not attachments:
        return

    from ftre.utils.image_store import save_image

    for att in attachments:
        if not isinstance(att, dict):
            continue
        if att.get("type") != "image":
            continue

        b64 = att.get("data", "")
        mime = att.get("mime_type", "image/png")
        name = att.get("name", "")

        try:
            raw = base64.b64decode(b64)
        except Exception:
            logger.warning(f"[ws-channel] 附件落盘失败，跳过: {name}")
            continue

        path = save_image(raw, mime, original_name=name)
        del att["data"]
        att["path"] = path


class WebSocketChannel(Channel):

    def __init__(self, bus: EventBus, host: str = "0.0.0.0", port: int = 48650, plugin_manager=None):
        super().__init__(channel_id="ws", name="WebSocket Channel", bus=bus)
        self.host = host
        self.port = port
        self.app = FastAPI(title="ftre-gateway")
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        # session_id → 关注该 session 的 ws 连接集合
        self._connections: dict[str, set[WebSocket]] = {}
        # 反向索引：ws → 它 attach 过的所有 session_id（断开时清理用）
        self._ws_sessions: dict[WebSocket, set[str]] = {}
        self._volatile_replay = _VolatileReplayBuffer()
        self._server = None
        self._server_task: asyncio.Task | None = None

        # 注册路由
        self.app.websocket("/")(self._ws_endpoint)

        # 挂载 HTTP API 路由
        from ftre.api.routes import router as api_router
        self.app.include_router(api_router, prefix="/api")

        # 挂载插件注册的路由
        if plugin_manager:
            for router in plugin_manager.routers:
                self.app.include_router(router, prefix="/api")

    async def start(self) -> None:
        """启动 WebSocket 服务"""
        import uvicorn
        config = uvicorn.Config(
            self.app, host=self.host, port=self.port,
            log_level="warning", log_config=None,
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve())
        logger.info(f"[ws-channel] listening on ws://{self.host}:{self.port}/")

    async def stop(self) -> None:
        """停止服务"""
        if self._server:
            self._server.should_exit = True
        if self._server_task:
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
        logger.info("[ws-channel] stopped")

    async def send(self, msg: BusMessage) -> None:
        """Bus outbound → 推送给 ws 连接。

        - 普通消息：按 to_session 推给所有 attach 该 session 的 ws。
        - 全局广播（to_session == GLOBAL_SESSION）：扇出给所有活跃 ws，
          无视 attach 关系（用于 session 状态等全局控制信号）。
        """
        metadata = dict(msg.metadata or {})
        if msg.type == "agent_event" and msg.to_session != GLOBAL_SESSION:
            # 先 track 再找订阅者：即使当前没有 ws attach，也要缓存未入库的流式片段，
            # 这样客户端稍后 attach 时还能 replay 补齐。
            metadata = await self._volatile_replay.track(msg, metadata)

        if msg.to_session == GLOBAL_SESSION:
            targets = list(self._ws_sessions.keys())
        else:
            conns = self._connections.get(msg.to_session)
            if not conns:
                return
            targets = list(conns)

        if not targets:
            return

        payload = {
            "id": msg.id,
            "type": msg.type,
            "data": msg.data,
            "metadata": {
                **metadata,
                "channel_id": msg.to_channel,
                "session_id": msg.to_session,
            },
        }
        text = json.dumps(payload, ensure_ascii=False, default=str)

        # 拷贝一份再迭代，避免发送过程中其它路径改动 set
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(text)
            except Exception as e:
                logger.debug(f"[ws-channel] send 失败，准备关闭: {e}")
                dead.append(ws)

        # 主动关闭坏连接，receive 循环 finally 会兜底清理索引
        for ws in dead:
            try:
                await ws.close()
            except Exception:
                pass

    # ============================================================
    # WebSocket 端点
    # ============================================================

    async def _ws_endpoint(self, ws: WebSocket) -> None:
        """WebSocket 连接入口"""
        await ws.accept()
        self._ws_sessions[ws] = set()

        logger.info("[ws-channel] connection established")

        try:
            while True:
                raw = await ws.receive_text()
                await self._on_message(raw, ws)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning(f"[ws-channel] connection error: {e}")
        finally:
            attached = self._ws_sessions.get(ws, set())
            logger.info(f"[ws-channel] connection closed (sessions={list(attached)})")
            self._detach_all(ws)

    # ============================================================
    # 连接登记
    # ============================================================

    def _attach(self, session_id: str, ws: WebSocket) -> None:
        if not session_id:
            return
        self._connections.setdefault(session_id, set()).add(ws)
        self._ws_sessions.setdefault(ws, set()).add(session_id)

    def _detach(self, session_id: str, ws: WebSocket) -> None:
        conns = self._connections.get(session_id)
        if conns:
            conns.discard(ws)
            if not conns:
                self._connections.pop(session_id, None)
        sids = self._ws_sessions.get(ws)
        if sids:
            sids.discard(session_id)

    def _detach_all(self, ws: WebSocket) -> None:
        for sid in list(self._ws_sessions.get(ws, ())):
            self._detach(sid, ws)
        self._ws_sessions.pop(ws, None)

    # ============================================================
    # 上行帧处理
    # ============================================================

    async def _on_message(self, raw: str, ws: WebSocket) -> None:
        """
        收到客户端消息 → 投递到 Bus

        上行帧格式: {id, type, data: {...}, metadata?}

        type:
        - attach     声明这条 ws 关心的 session（data.session_id）
        - detach     取消关心（data.session_id）
        - user_message 用户消息（隐式 attach data.session_id）
        - cancel       取消生成（转为 /cancel user_message，隐式 attach data.session_id）
        """
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(frame, dict):
            return

        frame_type = frame.get("type", "")
        data = frame.get("data") or {}
        if not isinstance(data, dict):
            return
        session_id = data.get("session_id", "")

        if frame_type == "attach":
            self._attach(session_id, ws)
            # 客户端流程是先 HTTP 读 DB 历史，再 WS attach。
            # DB 里没有流式增量，所以 attach 后立即补发 volatile buffer。
            await self._volatile_replay.replay(session_id, ws)
            return

        if frame_type == "detach":
            self._detach(session_id, ws)
            return

        # ─── cancel 帧：转为 /cancel 的 user_message ───
        # 取消操作统一走系统级 /cancel 指令，不再有 type="cancel" 的 BusMessage
        if frame_type == "cancel":
            if not session_id:
                logger.warning("[ws-channel] cancel 缺少 session_id，忽略")
                return
            self._attach(session_id, ws)
            metadata = frame.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            frame_id = frame.get("id") or ""
            if frame_id:
                metadata = {**metadata, "frame_id": frame_id}
            await self.receive(
                session_id,
                data={"content": "/cancel", "session_id": session_id},
                metadata=metadata,
                kind="user_message",
            )
            return

        if frame_type != "user_message":
            logger.debug(f"[ws-channel] unknown frame type: {frame_type}")
            return

        if not session_id:
            logger.warning(f"[ws-channel] {frame_type} 缺少 session_id，忽略")
            return

        # user_message 附件校验：违规直接拒绝，不进 Bus
        ok, err = _validate_attachments(data.get("attachments"))
        if not ok:
            logger.warning(f"[ws-channel] user_message 附件非法: {err}")
            await self._reject(ws, frame.get("id", ""), session_id, err)
            return

        # 附件落盘：base64 → temp 文件路径，事件链路不再携带 base64
        _persist_attachments(data.get("attachments"))

        # user_message 隐式 attach（保持向后兼容）
        self._attach(session_id, ws)

        metadata = frame.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        # 把客户端协议帧 id 装进 metadata.frame_id，AgentLoop echo 时
        # 回填给前端，前端用它去重本地乐观占位。
        frame_id = frame.get("id") or ""
        if frame_id:
            metadata = {**metadata, "frame_id": frame_id}

        await self.receive(session_id, data, metadata, kind="user_message")

    async def _reject(self, ws: WebSocket, frame_id: str, session_id: str, reason: str) -> None:
        """向客户端回写一帧拒绝消息（不入 Bus）"""
        payload = {
            "id": frame_id or "",
            "type": "error",
            "data": {
                "code": "invalid_input",
                "message": reason,
                "session_id": session_id,
            },
            "metadata": {
                "channel_id": self.channel_id,
                "session_id": session_id,
            },
        }
        try:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"[ws-channel] reject 回写失败: {e}")
