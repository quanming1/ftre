from __future__ import annotations

import asyncio
import inspect
import sys

import pytest
from ftre_process import ProcessService
from mcp.client.stdio import create_windows_process

from ftre.plugins.builtin.core_tools.bash import create_bash_tool
from ftre.services.workspace.accessor import WorkspaceAccessor


class _Workspace(WorkspaceAccessor):
    def __init__(self, path: str) -> None:
        self.path = path

    def get(self) -> str:
        return self.path

    def set(self, new_path: str) -> str:
        previous = self.path
        self.path = new_path
        return previous

    async def aget(self) -> str:
        return self.path

    async def aset(self, new_path: str) -> str:
        previous = self.path
        self.path = new_path
        return previous


class _SessionService:
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace

    async def get_session(self, _session_id: str):
        return {"workspace": self.workspace}

    async def update_session(self, _session_id: str, *, workspace: str):
        self.workspace = workspace


@pytest.mark.asyncio
async def test_bash_executes_through_injected_process_service(tmp_path) -> None:
    service = ProcessService()
    try:
        result = await create_bash_tool().execute(
            {
                "command": "echo process-service-ok",
                "timeout": 5,
                "ws": _Workspace(str(tmp_path)),
                "process": service,
            }
        )
    finally:
        await service.close()

    assert "process-service-ok" in result
    assert f"[cwd] {tmp_path}" in result


@pytest.mark.asyncio
async def test_async_bash_reads_workspace_without_blocking_event_loop(tmp_path) -> None:
    service = ProcessService()
    session_service = _SessionService(str(tmp_path))
    workspace = WorkspaceAccessor(
        session_id="session-1",
        session_manager=session_service,
        event_loop=asyncio.get_running_loop(),
        fallback_cwd=str(tmp_path),
    )
    try:
        result = await asyncio.wait_for(
            create_bash_tool().execute(
                {
                    "command": "echo async-workspace-ok",
                    "ws": workspace,
                    "process": service,
                }
            ),
            timeout=5,
        )
    finally:
        await service.close()

    assert "async-workspace-ok" in result
    assert f"[cwd] {tmp_path}" in result


def test_mcp_stdio_transport_retains_upstream_hidden_windows_policy() -> None:
    source = inspect.getsource(create_windows_process)

    assert "CREATE_NO_WINDOW" in source
    assert "_create_windows_fallback_process" in source


@pytest.mark.asyncio
async def test_bash_preserves_nonzero_exit_code(tmp_path) -> None:
    service = ProcessService()
    command = "exit /b 7" if sys.platform == "win32" else "exit 7"
    try:
        result = await create_bash_tool().execute(
            {"command": command, "ws": _Workspace(str(tmp_path)), "process": service}
        )
    finally:
        await service.close()

    assert "[exit_code] 7" in result


@pytest.mark.asyncio
async def test_bash_reports_timeout_and_terminates_process(tmp_path) -> None:
    service = ProcessService()
    command = "ping 127.0.0.1 -n 4 >nul" if sys.platform == "win32" else "sleep 2"
    try:
        result = await create_bash_tool(default_timeout=1).execute(
            {"command": command, "ws": _Workspace(str(tmp_path)), "process": service}
        )
    finally:
        await service.close()

    assert "命令超时" in result
