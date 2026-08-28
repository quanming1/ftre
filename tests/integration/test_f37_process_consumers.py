from __future__ import annotations

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
