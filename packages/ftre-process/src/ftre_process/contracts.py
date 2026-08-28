"""Public contracts for process execution."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

ProcessMode = Literal["capture", "stream", "detached"]


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    """Describe one external process without exposing platform flags."""

    argv: Sequence[str]
    cwd: Path | str | None = None
    env: Mapping[str, str] | None = None
    timeout: float | None = None
    mode: ProcessMode = "capture"
    encoding: str | Sequence[str] = "utf-8"
    creationflags: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.argv, (str, bytes)):
            raise TypeError("ProcessSpec.argv 必须是参数序列，不能是单个字符串")
        argv = tuple(str(part) for part in self.argv)
        if not argv:
            raise ValueError("ProcessSpec.argv 不能为空")
        if any(not part for part in argv):
            raise ValueError("ProcessSpec.argv 不能包含空参数")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError("ProcessSpec.timeout 必须大于 0")
        if self.creationflags < 0:
            raise ValueError("ProcessSpec.creationflags 不能为负数")
        if self.mode not in ("capture", "stream", "detached"):
            raise ValueError(f"未知进程模式: {self.mode!r}")
        object.__setattr__(self, "argv", argv)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Completed process output and exit metadata."""

    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: int


class ProcessHandle(Protocol):
    """Long-lived process operations exposed to consumers."""

    @property
    def pid(self) -> int: ...

    async def wait(self, timeout: float | None = None) -> ProcessResult: ...

    async def communicate(self, timeout: float | None = None) -> ProcessResult: ...

    async def terminate(self, grace_period: float = 1.5) -> None: ...

    async def kill(self) -> None: ...

    def stdout(self) -> AsyncIterator[str]: ...

    def stderr(self) -> AsyncIterator[str]: ...


class SyncProcessHandle(Protocol):
    """Synchronous handle used by CLI/background process boundaries."""

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def communicate(self, timeout: float | None = None) -> ProcessResult: ...

    def terminate(self, grace_period: float = 1.5) -> None: ...

    def kill(self) -> None: ...
