"""Typed process boundary errors."""

from __future__ import annotations


class ProcessError(RuntimeError):
    """Base error for process lifecycle failures."""


class ProcessSpawnError(ProcessError):
    """The operating system refused to create a process."""


class ProcessTimeoutError(ProcessError):
    """A process exceeded its configured timeout."""

    def __init__(self, message: str, *, stdout: str = "", stderr: str = "") -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr

