from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
from ftre_process import ProcessService, ProcessSpec, ProcessTimeoutError
from ftre_process.policy import windows_creation_flags


def _python(*code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", *code)


@pytest.mark.asyncio
async def test_run_captures_output_and_exit_code() -> None:
    service = ProcessService()
    result = await service.run(ProcessSpec(argv=_python("print('ok')")))

    assert result.returncode == 0
    assert result.stdout.strip() == "ok"
    assert result.stderr == ""
    await service.close()


@pytest.mark.asyncio
async def test_handle_reuses_completed_result() -> None:
    service = ProcessService()
    handle = await service.spawn(ProcessSpec(argv=_python("print('cached')")))

    first = await handle.communicate()
    second = await handle.communicate()

    assert first == second
    assert second.stdout.strip() == "cached"
    await service.close()


@pytest.mark.asyncio
async def test_run_shell_uses_explicit_shell_and_captures_output() -> None:
    service = ProcessService()
    command = "echo shell-ok" if sys.platform == "win32" else "printf shell-ok"
    result = await service.run_shell(command)

    assert result.returncode == 0
    assert result.stdout.strip() == "shell-ok"
    await service.close()


def test_run_sync_captures_output() -> None:
    service = ProcessService()
    result = service.run_sync(ProcessSpec(argv=_python("print('sync-ok')")))

    assert result.returncode == 0
    assert result.stdout.strip() == "sync-ok"


@pytest.mark.asyncio
async def test_wait_is_available_on_process_service() -> None:
    service = ProcessService()
    handle = await service.spawn(ProcessSpec(argv=_python("print('wait-ok')"), mode="stream"))

    result = await service.wait(handle)

    assert result.returncode == 0
    await service.close()


@pytest.mark.asyncio
async def test_cancellation_kills_process_and_releases_handle() -> None:
    service = ProcessService()
    task = asyncio.create_task(service.run(ProcessSpec(argv=_python("import time; time.sleep(10)"))))
    await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not service._handles
    await service.close()


@pytest.mark.asyncio
async def test_timeout_terminates_process() -> None:
    service = ProcessService()
    with pytest.raises(ProcessTimeoutError):
        await service.run(ProcessSpec(argv=_python("import time; time.sleep(10)"), timeout=0.05))

    await asyncio.sleep(0)
    assert not service._handles
    await service.close()


def test_process_spec_rejects_empty_command() -> None:
    with pytest.raises(ValueError):
        ProcessSpec(argv=())
    with pytest.raises(TypeError):
        ProcessSpec(argv="echo")


@pytest.mark.asyncio
async def test_close_is_idempotent_for_long_lived_process() -> None:
    service = ProcessService()
    await service.spawn(ProcessSpec(argv=_python("import time; time.sleep(10)"), mode="stream"))

    await service.close()
    await service.close()
    assert not service._handles


def test_windows_flags_preserve_hidden_and_group_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x00000008, raising=False)

    custom = 0x40000000
    flags = windows_creation_flags(detached=True, existing=custom, platform="win32")

    assert flags & custom
    assert flags & subprocess.CREATE_NO_WINDOW
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert flags & subprocess.DETACHED_PROCESS
    assert windows_creation_flags(detached=True, platform="linux") == 0
