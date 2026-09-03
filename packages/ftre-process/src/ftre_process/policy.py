"""Platform process policy used by :class:`ProcessService`."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from contextlib import suppress


def windows_creation_flags(
    *, detached: bool, existing: int = 0, platform: str | None = None
) -> int:
    """Return the Windows flags required for a hidden process tree."""

    if (platform or sys.platform) != "win32":
        return 0
    flags = existing
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if detached:
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    return flags


def signal_process_group(pid: int, sig: signal.Signals) -> None:
    """Signal a POSIX process group, falling back to the single process."""

    try:
        os.killpg(pid, sig)
    except (AttributeError, ProcessLookupError, PermissionError):
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, sig)


def taskkill_sync(pid: int, *, force: bool) -> None:
    """Terminate a Windows process tree without creating a console window."""

    args = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        args.append("/F")
    subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=windows_creation_flags(detached=False),
        check=False,
    )


__all__ = ["signal_process_group", "taskkill_sync", "windows_creation_flags"]
