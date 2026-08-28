"""唯一的跨平台外部进程 Service。"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import IO

from .contracts import ProcessHandle, ProcessResult, ProcessSpec, SyncProcessHandle
from .errors import ProcessSpawnError, ProcessTimeoutError
from .policy import signal_process_group, taskkill_sync, windows_creation_flags


class _AsyncProcessHandle:
    def __init__(self, service: ProcessService, process: asyncio.subprocess.Process, spec: ProcessSpec) -> None:
        self._service = service
        self._process = process
        self._spec = spec
        self._started = time.monotonic()
        self._communicated = False
        self._result: ProcessResult | None = None

    @property
    def pid(self) -> int:
        if self._process.pid is None:
            raise RuntimeError("进程尚未分配 PID")
        return self._process.pid

    async def wait(self, timeout: float | None = None) -> ProcessResult:
        if self._spec.mode == "capture":
            return await self.communicate(timeout=timeout)
        started = self._started
        try:
            if timeout is None:
                await self._process.wait()
            else:
                await asyncio.wait_for(self._process.wait(), timeout)
        except asyncio.CancelledError:
            await self.kill()
            raise
        except TimeoutError as exc:
            await self.kill()
            raise ProcessTimeoutError(f"进程等待超时（pid={self.pid}）") from exc
        self._result = ProcessResult(self._process.returncode or 0, "", "", _elapsed_ms(started))
        self._service._handles.discard(self)
        return self._result

    async def communicate(self, timeout: float | None = None) -> ProcessResult:
        if self._result is not None:
            return self._result
        if self._communicated:
            return self._result or ProcessResult(self._process.returncode or 0, "", "", _elapsed_ms(self._started))
        self._communicated = True
        try:
            if timeout is None:
                stdout, stderr = await self._process.communicate()
            else:
                stdout, stderr = await asyncio.wait_for(self._process.communicate(), timeout)
        except asyncio.CancelledError:
            await self.kill()
            raise
        except TimeoutError as exc:
            await self.kill()
            raise ProcessTimeoutError(
                f"进程执行超时（pid={self.pid}）",
                stdout=_decode(stdout if "stdout" in locals() else b"", self._spec.encoding),
                stderr=_decode(stderr if "stderr" in locals() else b"", self._spec.encoding),
            ) from exc
        self._result = ProcessResult(
            self._process.returncode or 0,
            _decode(stdout, self._spec.encoding),
            _decode(stderr, self._spec.encoding),
            _elapsed_ms(self._started),
        )
        self._service._handles.discard(self)
        return self._result

    async def terminate(self, grace_period: float = 1.5) -> None:
        if self._process.returncode is not None:
            return
        await self._service.terminate(self, grace_period=grace_period)

    async def kill(self) -> None:
        if self._process.returncode is not None:
            return
        await self._service.kill(self)

    def stdout(self) -> AsyncIterator[str]:
        return _read_lines(self._process.stdout, self._spec.encoding)

    def stderr(self) -> AsyncIterator[str]:
        return _read_lines(self._process.stderr, self._spec.encoding)


class _SyncProcessHandle:
    def __init__(self, service: ProcessService, process: subprocess.Popen[bytes], spec: ProcessSpec) -> None:
        self._service = service
        self._process = process
        self._spec = spec
        self._result: ProcessResult | None = None

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self._process.wait(timeout=timeout)

    def communicate(self, timeout: float | None = None) -> ProcessResult:
        started = time.monotonic()
        if self._result is not None:
            return self._result
        try:
            stdout, stderr = self._process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self.kill()
            raise ProcessTimeoutError(
                f"进程执行超时（pid={self.pid}）",
                stdout=_decode(exc.stdout, self._spec.encoding),
                stderr=_decode(exc.stderr, self._spec.encoding),
            ) from exc
        finally:
            self._service._handles.discard(self)
        self._result = ProcessResult(
            self._process.returncode or 0,
            _decode(stdout, self._spec.encoding),
            _decode(stderr, self._spec.encoding),
            _elapsed_ms(started),
        )
        return self._result

    def terminate(self, grace_period: float = 1.5) -> None:
        self._service.terminate_sync(self, grace_period=grace_period)

    def kill(self) -> None:
        self._service.kill_sync(self)


class ProcessService:
    """创建、观察和回收外部进程；不承载命令业务语义。"""

    key = "process"

    def __init__(self) -> None:
        self._handles: set[_AsyncProcessHandle | _SyncProcessHandle] = set()
        self._closed = False

    async def run(self, spec: ProcessSpec) -> ProcessResult:
        handle = await self.spawn(spec)
        try:
            return await handle.communicate(timeout=spec.timeout)
        except asyncio.CancelledError:
            await handle.kill()
            raise
        finally:
            self._handles.discard(handle)

    def run_sync(self, spec: ProcessSpec) -> ProcessResult:
        handle = self.spawn_sync(spec)
        return handle.communicate(timeout=spec.timeout)

    async def wait(self, handle: ProcessHandle, timeout: float | None = None) -> ProcessResult:
        """Wait for an async process through the service boundary."""

        return await handle.wait(timeout=timeout)

    def wait_sync(self, handle: SyncProcessHandle, timeout: float | None = None) -> int:
        """Wait for a sync process through the service boundary."""

        return handle.wait(timeout=timeout)

    async def run_shell(
        self,
        command: str,
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        encoding: str | Sequence[str] = ("utf-8", "gbk"),
    ) -> ProcessResult:
        if not command.strip():
            raise ValueError("shell command 不能为空")
        if sys.platform == "win32":
            shell = os.environ.get("COMSPEC") or "cmd.exe"
            argv = (shell, "/d", "/s", "/c", command)
        else:
            shell = "/bin/bash" if Path("/bin/bash").exists() else "/bin/sh"
            argv = (shell, "-lc", command)
        return await self.run(ProcessSpec(argv=argv, cwd=cwd, env=env, timeout=timeout, encoding=encoding))

    async def spawn(self, spec: ProcessSpec) -> _AsyncProcessHandle:
        if self._closed:
            raise RuntimeError("ProcessService 已关闭")
        kwargs = self._async_options(spec)
        try:
            process = await asyncio.create_subprocess_exec(*spec.argv, **kwargs)
        except (OSError, ValueError) as exc:
            raise ProcessSpawnError(f"无法启动进程: {spec.argv[0]}: {exc}") from exc
        handle = _AsyncProcessHandle(self, process, spec)
        self._handles.add(handle)
        return handle

    def spawn_sync(self, spec: ProcessSpec, *, stdout: IO[bytes] | int | None = None, stderr: IO[bytes] | int | None = None, stdin: IO[bytes] | int | None = None) -> SyncProcessHandle:
        if self._closed:
            raise RuntimeError("ProcessService 已关闭")
        kwargs = self._sync_options(spec)
        if stdout is not None:
            kwargs["stdout"] = stdout
        if stderr is not None:
            kwargs["stderr"] = stderr
        if stdin is not None:
            kwargs["stdin"] = stdin
        try:
            process = subprocess.Popen(list(spec.argv), **kwargs)
        except (OSError, ValueError) as exc:
            raise ProcessSpawnError(f"无法启动进程: {spec.argv[0]}: {exc}") from exc
        handle = _SyncProcessHandle(self, process, spec)
        self._handles.add(handle)
        return handle

    async def terminate(self, handle: _AsyncProcessHandle, *, grace_period: float = 1.5) -> None:
        process = handle._process
        if process.returncode is not None:
            self._handles.discard(handle)
            return
        if sys.platform == "win32":
            await self._taskkill(process.pid, force=False)
        else:
            signal_process_group(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_period)
        except TimeoutError:
            await self.kill(handle)
        finally:
            self._handles.discard(handle)

    async def kill(self, handle: _AsyncProcessHandle) -> None:
        process = handle._process
        if process.returncode is not None:
            self._handles.discard(handle)
            return
        if sys.platform == "win32":
            await self._taskkill(process.pid, force=True)
        else:
            signal_process_group(process.pid, signal.SIGKILL)
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(process.wait(), timeout=3)
        self._handles.discard(handle)

    def terminate_sync(self, handle: _SyncProcessHandle, *, grace_period: float = 1.5) -> None:
        process = handle._process
        if process.poll() is not None:
            self._handles.discard(handle)
            return
        if sys.platform == "win32":
            taskkill_sync(process.pid, force=False)
        else:
            signal_process_group(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=grace_period)
        except subprocess.TimeoutExpired:
            self.kill_sync(handle)
        finally:
            self._handles.discard(handle)

    def kill_sync(self, handle: _SyncProcessHandle) -> None:
        process = handle._process
        if process.poll() is not None:
            self._handles.discard(handle)
            return
        if sys.platform == "win32":
            taskkill_sync(process.pid, force=True)
        else:
            signal_process_group(process.pid, signal.SIGKILL)
        with suppress(Exception):
            process.wait(timeout=3)
        self._handles.discard(handle)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        handles = tuple(self._handles)
        await asyncio.gather(
            *(handle.kill() for handle in handles if isinstance(handle, _AsyncProcessHandle)),
            return_exceptions=True,
        )
        for handle in handles:
            if isinstance(handle, _SyncProcessHandle):
                self.kill_sync(handle)
        self._handles.clear()

    @staticmethod
    def _async_options(spec: ProcessSpec) -> dict:
        options: dict = {
            "cwd": str(spec.cwd) if spec.cwd is not None else None,
            "env": _merge_env(spec.env),
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.DEVNULL if spec.mode == "detached" else asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.DEVNULL if spec.mode == "detached" else asyncio.subprocess.PIPE,
        }
        if sys.platform == "win32":
            options["creationflags"] = windows_creation_flags(
                detached=spec.mode == "detached", existing=spec.creationflags
            )
        else:
            options["start_new_session"] = True
        return options

    @staticmethod
    def _sync_options(spec: ProcessSpec) -> dict:
        options: dict = {
            "cwd": str(spec.cwd) if spec.cwd is not None else None,
            "env": _merge_env(spec.env),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL if spec.mode == "detached" else subprocess.PIPE,
            "stderr": subprocess.DEVNULL if spec.mode == "detached" else subprocess.PIPE,
        }
        if sys.platform == "win32":
            options["creationflags"] = windows_creation_flags(
                detached=spec.mode == "detached", existing=spec.creationflags
            )
        else:
            options["start_new_session"] = True
        return options

    async def _taskkill(self, pid: int, *, force: bool) -> None:
        args = ("taskkill", "/PID", str(pid), "/T") + (("/F",) if force else ())
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=windows_creation_flags(detached=False),
        )
        await process.wait()


def _merge_env(overrides: Mapping[str, str] | None) -> dict[str, str]:
    env = os.environ.copy()
    if overrides:
        env.update({str(key): str(value) for key, value in overrides.items()})
    return env


def _decode(value: bytes | str | None, encodings: str | Sequence[str]) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    candidates = (encodings,) if isinstance(encodings, str) else tuple(encodings)
    for encoding in candidates:
        try:
            return value.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return value.decode("utf-8", errors="replace")


async def _read_lines(stream: asyncio.StreamReader | None, encodings: str | Sequence[str]) -> AsyncIterator[str]:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            break
        yield _decode(line, encodings).rstrip("\r\n")


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
