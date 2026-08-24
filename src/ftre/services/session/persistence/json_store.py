"""Agent State JSON 文件存储（设计文档 §5 / §9 / §10）。

磁盘结构：

    ~/.ftre/sessions/
    └── <session_id>/              # 目录名即 session_id（如 ws_sess_ed930104a1d2）
        └── state.json

session_id 规范：只允许 [A-Za-z0-9_-]，由 manager 生成时保证。
本模块在路径解析时再次校验，拒绝含 /、\\、:、.. 等危险字符的 ID。

职责：
- 安全路径解析（session_id 直接作目录名，校验后无需编码）；
- 启动扫描 + Pydantic 加载（损坏文件隔离为 .corrupt-<ts>，不静默当空 Session）；
- 临时文件 + fsync + os.replace 原子写（Windows 重试）；
- per-session asyncio.Lock 与全局锁；
- 内存状态索引（states / corrupt）。

调用方写盘失败时不得提前提交内存缓存：应先构造不可变副本，
write() 成功后再由调用方替换 states 中的引用。

它是 Repository 下方的文件系统适配器，不拥有 Session 业务语义；损坏文件会被
隔离并报告，不能静默当成空 Session，否则会把用户历史误判为已删除。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from ..entity.state import AgentStateFile, parse_agent_state_json

logger = logging.getLogger(__name__)

STATE_FILE_NAME = "state.json"

# session_id 允许的字符集：字母、数字、下划线、连字符
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_session_id(session_id: str) -> None:
    """校验 session_id 可安全用作目录名，否则抛 ValueError。"""
    if not session_id:
        raise ValueError("session_id 不能为空")
    if not _SAFE_SESSION_ID_RE.match(session_id):
        raise ValueError(
            f"session_id 含非法字符（只允许 [A-Za-z0-9_-]）: {session_id!r}"
        )


class CorruptStateError(Exception):
    """单个 state.json 损坏 / 校验失败。"""

    def __init__(self, path: Path, session_hint: str, reason: str):
        self.path = path
        self.session_hint = session_hint
        self.reason = reason
        super().__init__(
            f"state.json 损坏 path={path} session={session_hint!r}: {reason}"
        )


class JsonStateStore:
    """~/.ftre/sessions/ 目录的扫描、原子读写与内存索引。"""

    def __init__(self, root: str | Path):
        self._root = Path(root)
        self.states: dict[str, AgentStateFile] = {}
        # session_hint → 错误信息；访问这些 Session 时应明确报错
        self.corrupt: dict[str, CorruptStateError] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.global_lock = asyncio.Lock()

    @property
    def root(self) -> Path:
        return self._root

    # ─── 路径 ────────────────────────────────────────────────

    def session_dir(self, session_id: str) -> Path:
        """Session 目录（session_id 直接作目录名），校验后解析结果必须仍位于 root 内。"""
        validate_session_id(session_id)
        path = (self._root / session_id).resolve()
        root = self._root.resolve()
        if path.parent != root:
            raise ValueError(f"session 路径越界: {session_id!r}")
        return path

    def state_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / STATE_FILE_NAME

    def lock_for(self, session_id: str) -> asyncio.Lock:
        """一个 Session 一把 asyncio.Lock。"""
        lock = self.locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[session_id] = lock
        return lock

    # ─── 扫描加载 ────────────────────────────────────────────

    async def load_all(self) -> None:
        """扫描 root 下所有 state.json 并加载到内存。

        - 残留 .tmp 不覆盖正式文件；
        - 损坏文件隔离为 .corrupt-<timestamp> 并记入 corrupt；
        - 不因一份文件损坏而中断其他 Session 加载。
        """
        self.states.clear()
        self.corrupt.clear()
        self._root.mkdir(parents=True, exist_ok=True)

        for child in sorted(self._root.iterdir()):
            if not child.is_dir():
                continue
            state_file = child / STATE_FILE_NAME
            if not state_file.exists():
                # 残留 .tmp 等：不处理、不覆盖
                if list(child.glob("*.tmp")):
                    logger.info(
                        "[session-store] 发现残留 .tmp，忽略 dir=%s", child
                    )
                continue
            try:
                payload = await asyncio.to_thread(
                    state_file.read_text, encoding="utf-8"
                )
                state = parse_agent_state_json(payload)
            except Exception as exc:  # noqa: BLE001 legacy compatibility boundary reviewed in F1
                await self._quarantine(state_file, child, exc)
                continue
            self.states[state.session.id] = state

        logger.info(
            "[session-store] backend=json directory=%s loaded sessions=%d messages=%d corrupt=%d",
            self._root,
            len(self.states),
            sum(len(s.messages) for s in self.states.values()),
            len(self.corrupt),
        )

    async def _quarantine(
        self, state_file: Path, session_dir: Path, exc: Exception
    ) -> None:
        """损坏文件改名隔离，记录错误；绝不自动覆盖为新文件。"""
        session_hint = self._session_hint(state_file, session_dir)
        error = CorruptStateError(state_file, session_hint, str(exc))
        self.corrupt[session_hint] = error
        logger.error("[session-store] invalid state file %s", error)
        try:
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")  # noqa: DTZ005 legacy compatibility boundary reviewed in F1
            await asyncio.to_thread(
                os.replace, state_file, state_file.with_name(
                    f"{STATE_FILE_NAME}.corrupt-{stamp}"
                )
            )
        except OSError:
            logger.exception(
                "[session-store] 隔离损坏文件失败 path=%s", state_file
            )

    @staticmethod
    def _session_hint(state_file: Path, session_dir: Path) -> str:
        """尽力从损坏 JSON / 目录名恢复 session_id，用于错误报告。"""
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            session_id = data.get("session", {}).get("id")
            if isinstance(session_id, str) and session_id:
                return session_id
        except Exception:  # noqa: BLE001, S110 legacy compatibility boundary reviewed in F1
            pass
        return session_dir.name

    # ─── 原子读写 ────────────────────────────────────────────

    async def write(self, state: AgentStateFile) -> None:
        """原子写入单个 Session 的 state.json。

        payload 已通过 Pydantic 校验；临时文件与目标同目录；
        写盘异常向上抛，由调用方保证内存缓存不提前提交。
        """
        path = self.state_path(state.session.id)
        payload = state.model_dump_json(indent=2)
        await asyncio.to_thread(self._atomic_replace, path, payload)

    # Windows 的杀毒、索引器或编辑器可能短暂持有目标文件。总等待约 5 秒。
    _REPLACE_RETRIES = 7
    _REPLACE_DELAY = 0.1

    @classmethod
    def _atomic_replace(cls, path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 固定的 state.json.tmp 会让两个 Gateway 进程互相覆盖/移动临时文件。
        # 每次写入拥有独立临时文件，失败时也不会污染其他写入者。
        tmp = path.with_name(
            f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        )
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Windows 上目标文件可能被杀毒/索引器/编辑器短暂锁定，退避重试。
        for attempt in range(cls._REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except OSError as exc:
                # WinError 5=Access denied；32=sharing violation。其他 I/O 错误
                # （例如磁盘满）不可通过重试恢复，直接向调用方报告。
                if not cls._is_transient_replace_error(exc):
                    raise
                if attempt == cls._REPLACE_RETRIES - 1:
                    raise
                delay = min(cls._REPLACE_DELAY * (2**attempt), 2.0)
                logger.warning(
                    "state.json 被占用，%.1fs 后重试 replace: target=%s attempt=%s/%s",
                    delay,
                    path,
                    attempt + 1,
                    cls._REPLACE_RETRIES,
                )
                time.sleep(delay)

    @staticmethod
    def _is_transient_replace_error(exc: OSError) -> bool:
        """仅将 Windows 的拒绝访问/共享冲突视为可重试错误。"""
        return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32}

    async def delete(self, session_id: str) -> bool:
        """删除精确目标 state.json 及其目录；目标不存在返回 False。"""
        path = self.state_path(session_id)  # 越界时抛 ValueError

        def _remove() -> bool:
            if not path.exists():
                return False
            path.unlink()
            try:
                path.parent.rmdir()
            except OSError:
                # 目录内还有其他文件（如 .corrupt 隔离件），保留目录
                pass
            return True

        return await asyncio.to_thread(_remove)
