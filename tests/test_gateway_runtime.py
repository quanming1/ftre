"""GatewayRuntime 单元测试 — mock Popen，不启动真进程。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from ftre.gateway.runtime import GatewayRuntime


def _make_runtime(tmp_path: Path) -> GatewayRuntime:
    """创建指向临时目录的 GatewayRuntime。"""
    return GatewayRuntime(
        data_dir=tmp_path,
        python_executable="python",
        platform_name="Linux",  # 避免 Windows ctypes 调用
    )


def test_status_no_state_file(tmp_path: Path):
    """没有状态文件时 status 返回 not_started。"""
    runtime = _make_runtime(tmp_path)
    status = runtime.status()
    assert not status.running
    assert status.reason == "not_started"
    assert status.pid is None


def test_status_with_stale_state(tmp_path: Path):
    """状态文件存在但 PID 已死时 status 返回 process_dead 并清理状态。"""
    runtime = _make_runtime(tmp_path)
    runtime.run_dir.mkdir(parents=True, exist_ok=True)
    runtime._write_state({"pid": 99999, "started_at": "2025-01-01T00:00:00Z", "port": 8000})

    # mock _is_pid_running 返回 False
    with patch.object(runtime, "_is_pid_running", return_value=False):
        status = runtime.status()

    assert not status.running
    assert status.reason == "process_dead"
    assert not runtime.state_path.exists()  # 状态文件被清理


def test_status_running(tmp_path: Path):
    """状态文件存在且 PID 存活时 status 返回 running。"""
    runtime = _make_runtime(tmp_path)
    runtime.run_dir.mkdir(parents=True, exist_ok=True)
    runtime._write_state({
        "pid": 12345,
        "started_at": "2025-01-01T00:00:00Z",
        "port": 8000,
        "host": "127.0.0.1",
    })

    with patch.object(runtime, "_is_pid_running", return_value=True):
        status = runtime.status()

    assert status.running
    assert status.pid == 12345
    assert status.port == 8000
    assert status.host == "127.0.0.1"
    assert status.reason == "running"


def test_start_already_running(tmp_path: Path):
    """已有进程在跑时 start 返回失败。"""
    runtime = _make_runtime(tmp_path)
    runtime.run_dir.mkdir(parents=True, exist_ok=True)
    runtime._write_state({"pid": 12345, "port": 8000})

    with patch.object(runtime, "_is_pid_running", return_value=True):
        ok, msg, status = runtime.start(port=9000)

    assert not ok
    assert msg == "gateway_already_running"
    assert status.running


def test_start_success(tmp_path: Path):
    """成功启动后台进程：mock Popen 返回假 PID，mock _is_pid_running 返回 True。"""
    runtime = _make_runtime(tmp_path)

    fake_process = MagicMock()
    fake_process.pid = 4242

    with (
        patch("ftre.gateway.runtime.subprocess.Popen", return_value=fake_process),
        patch.object(runtime, "_is_pid_running", return_value=True),
    ):
        ok, msg, status = runtime.start(port=8000, host="0.0.0.0")

    assert ok
    assert msg == "gateway_started"
    assert status.running
    assert status.pid == 4242
    assert status.port == 8000
    assert status.host == "0.0.0.0"

    # 验证状态文件写入
    state = json.loads(runtime.state_path.read_text(encoding="utf-8"))
    assert state["pid"] == 4242
    assert state["port"] == 8000
    assert state["host"] == "0.0.0.0"


def test_start_exits_during_startup(tmp_path: Path):
    """子进程启动后立即退出时返回 gateway_exited_during_startup。"""
    runtime = _make_runtime(tmp_path)

    fake_process = MagicMock()
    fake_process.pid = 9999

    with (
        patch("ftre.gateway.runtime.subprocess.Popen", return_value=fake_process),
        patch.object(runtime, "_is_pid_running", return_value=False),
    ):
        ok, msg, status = runtime.start(port=8000)

    assert not ok
    assert msg == "gateway_exited_during_startup"
    assert not status.running


def test_stop_not_running(tmp_path: Path):
    """没有后台进程时 stop 返回 gateway_not_running。"""
    runtime = _make_runtime(tmp_path)
    ok, msg, status = runtime.stop()
    assert not ok
    assert msg == "gateway_not_running"


def test_stop_success(tmp_path: Path):
    """成功停止后台进程。"""
    runtime = _make_runtime(tmp_path)
    runtime.run_dir.mkdir(parents=True, exist_ok=True)
    runtime._write_state({"pid": 12345, "port": 8000})

    with (
        patch.object(runtime, "_is_pid_running", side_effect=[True, False]),
        patch.object(runtime, "_terminate", return_value=True),
    ):
        ok, msg, status = runtime.stop()

    assert ok
    assert msg == "gateway_stopped"
    assert not runtime.state_path.exists()  # 状态文件被清理


def test_read_log_tail(tmp_path: Path):
    """读取日志文件尾部。"""
    runtime = _make_runtime(tmp_path)
    runtime.logs_dir.mkdir(parents=True, exist_ok=True)
    runtime.log_path.write_text("line1\nline2\nline3\n", encoding="utf-8")

    lines = runtime.read_log_tail(tail=2)
    assert lines == ["line2", "line3"]


def test_read_log_tail_no_file(tmp_path: Path):
    """日志文件不存在时返回空列表。"""
    runtime = _make_runtime(tmp_path)
    lines = runtime.read_log_tail(tail=10)
    assert lines == []


def test_restart_no_state(tmp_path: Path):
    """没有状态文件时 restart 返回 gateway_not_running。"""
    runtime = _make_runtime(tmp_path)
    ok, msg, status = runtime.restart()
    assert not ok
    assert msg == "gateway_not_running"


def test_restart_success(tmp_path: Path):
    """成功重启：旧进程被停掉，新进程启动，端口沿用。"""
    runtime = _make_runtime(tmp_path)
    runtime.run_dir.mkdir(parents=True, exist_ok=True)
    runtime._write_state({"pid": 12345, "port": 8000, "host": "127.0.0.1"})

    fake_process = MagicMock()
    fake_process.pid = 9999

    with (
        patch("ftre.gateway.runtime.subprocess.Popen", return_value=fake_process),
        patch.object(runtime, "_is_pid_running", return_value=True),
        patch.object(runtime, "_terminate", return_value=True),
    ):
        ok, msg, status = runtime.restart()

    assert ok
    assert msg == "gateway_started"
    assert status.pid == 9999
    assert status.port == 8000  # 沿用旧端口


def test_restart_with_new_port(tmp_path: Path):
    """restart 时传 --port 覆盖旧端口。"""
    runtime = _make_runtime(tmp_path)
    runtime.run_dir.mkdir(parents=True, exist_ok=True)
    runtime._write_state({"pid": 12345, "port": 8000, "host": "127.0.0.1"})

    fake_process = MagicMock()
    fake_process.pid = 8888

    with (
        patch("ftre.gateway.runtime.subprocess.Popen", return_value=fake_process),
        patch.object(runtime, "_is_pid_running", return_value=True),
        patch.object(runtime, "_terminate", return_value=True),
    ):
        ok, msg, status = runtime.restart(port=9000)

    assert ok
    assert status.port == 9000  # 用新端口


def test_restart_stop_timeout(tmp_path: Path):
    """旧进程停不掉时 restart 返回 gateway_stop_timeout。"""
    runtime = _make_runtime(tmp_path)
    runtime.run_dir.mkdir(parents=True, exist_ok=True)
    runtime._write_state({"pid": 12345, "port": 8000})

    with (
        patch.object(runtime, "_is_pid_running", return_value=True),
        patch.object(runtime, "_terminate", return_value=False),
    ):
        ok, msg, status = runtime.restart()

    assert not ok
    assert msg == "gateway_stop_timeout"
