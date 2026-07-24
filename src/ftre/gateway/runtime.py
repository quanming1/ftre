"""
ftre gateway 后台进程管理。

本模块的核心思路：
  ┌──────────────────────────────────────────────────────────────────┐
  │  ftre gateway --background                                       │
  │     │                                                            │
  │     ▼  GatewayRuntime.start()                                    │
  │  subprocess.Popen([                                              │
  │     "python", "-m", "ftre", "gateway",                           │
  │     "--foreground",          ← 子进程跑前台模式                   │
  │     "--port", "8000",                                            │
  │     "--host", "127.0.0.1"                                        │
  │  ], stdout=日志文件, stderr=STDOUT)                              │
  │     │                                                            │
  │     ├── 子进程在后台跑（脱离当前终端，关终端不死）                  │
  │     │                                                            │
  │     ▼  把 PID 写入 ~/.ftre/run/gateway.json                      │
  │  当前命令立即返回，打印 PID 和日志路径                            │
  │                                                                  │
  │  之后用 ftre gateway status / stop / logs 管理这个后台进程       │
  └──────────────────────────────────────────────────────────────────┘

文件布局：
  ~/.ftre/
    run/gateway.json   ← 状态文件（PID、端口、启动时间、命令行）
    logs/gateway.log   ← 后台进程的 stdout+stderr 合并日志
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _ftre_home() -> Path:
    """获取 ftre 数据根目录。

    Windows: %USERPROFILE%\\.ftre
    Linux:   ~/.ftre
    """
    if sys.platform == "win32":
        return Path(os.environ.get("USERPROFILE", Path.home())) / ".ftre"
    return Path.home() / ".ftre"


def _platform_name() -> str:
    """返回当前操作系统名称，用于选择平台特定的进程管理策略。"""
    if sys.platform.startswith("win"):
        return "Windows"
    if sys.platform == "darwin":
        return "Darwin"
    return "Linux"


def _utc_now() -> str:
    """返回 ISO 8601 格式的当前 UTC 时间（如 2025-07-24T12:34:56Z）。"""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class GatewayStatus:
    """后台 gateway 进程的快照状态。

    这是一个不可变的数据容器，由 GatewayRuntime.status() 返回。
    CLI 命令（ftre gateway status）读取这个对象并格式化打印给用户。

    Attributes:
        running:    后台进程是否仍在运行
        pid:        后台进程的 PID（None = 没有启动过）
        started_at: 启动时间（ISO 8601 UTC 字符串）
        port:       监听端口
        host:       绑定地址
        state_path: 状态文件路径（~/.ftre/run/gateway.json）
        log_path:   日志文件路径（~/.ftre/logs/gateway.log）
        reason:     状态原因标签（not_started / running / process_dead / stopped 等）
    """

    running: bool
    pid: int | None = None
    started_at: str | None = None
    port: int | None = None
    host: str | None = None
    state_path: Path = None  # type: ignore[assignment]
    log_path: Path = None  # type: ignore[assignment]
    reason: str = "not_started"


class GatewayRuntime:
    """管理 ftre gateway 后台进程的生命周期。

    核心设计：后台启动 = Popen fork 一个前台子进程 + 把 PID 写到状态文件。
    不依赖 nohup / tmux / screen 等外部工具，纯 Python 标准库实现。

    状态文件 (~/.ftre/run/gateway.json) 的内容示例：
        {
          "pid": 12345,
          "started_at": "2025-07-24T12:00:00Z",
          "port": 8000,
          "host": "127.0.0.1",
          "command": ["python", "-m", "ftre", "gateway", "--foreground", ...],
          "log_path": "~/.ftre/logs/gateway.log"
        }

    用法：
        runtime = GatewayRuntime()
        ok, msg, status = runtime.start(port=8000)   # 后台启动
        status = runtime.status()                     # 查状态
        ok, msg, status = runtime.stop()              # 停止
        lines = runtime.read_log_tail(tail=200)       # 读日志
    """

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        platform_name: str | None = None,
        python_executable: str | None = None,
    ) -> None:
        """初始化运行时，确定文件路径和平台参数。

        Args:
            data_dir:          ftre 数据根目录，默认 ~/.ftre（测试时可传临时目录）
            platform_name:     平台名，默认自动检测（测试时可 mock）
            python_executable: Python 解释器路径，默认 sys.executable（子进程用它启动）
        """
        home = data_dir or _ftre_home()
        # 状态文件和日志文件的路径
        self.run_dir = home / "run"       # 状态文件目录
        self.logs_dir = home / "logs"    # 日志文件目录
        self.state_path = self.run_dir / "gateway.json"
        self.log_path = self.logs_dir / "gateway.log"
        # 平台信息（影响 Popen 参数和进程终止方式）
        self.platform_name = platform_name or _platform_name()
        # 子进程用哪个 Python 解释器启动
        self.python_executable = python_executable or sys.executable

    # ── 启动 ──────────────────────────────────────────────────

    def start(self, *, port: int, host: str = "127.0.0.1") -> tuple[bool, str, GatewayStatus]:
        """后台启动 gateway 子进程。

        流程：
        1. 先检查是否已有进程在跑（防重复启动）
        2. 构建 "python -m ftre gateway --foreground --port N --host H" 命令
        3. 创建日志文件目录
        4. Popen 启动子进程，stdout/stderr 重定向到日志文件，脱离当前终端
        5. 等 0.3 秒确认子进程没崩溃（端口冲突、import 错误等会导致秒退）
        6. 把 PID + 端口 + 启动时间 + 命令行写入状态 JSON

        Args:
            port: 监听端口
            host: 绑定地址

        Returns:
            (ok, message, status)
            ok=True 表示成功启动，status 包含 PID 等信息
            ok=False 表示启动失败，message 说明原因
        """
        # 1. 防重复启动
        current = self.status()
        if current.running:
            return False, "gateway_already_running", current

        # 2. 构建子进程命令行
        command = self._build_child_command(port=port, host=host)

        # 3. 确保目录存在
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        # 写 UTF-8 BOM（仅新文件/空文件），让 Windows 记事本能正确识别编码
        if not self.log_path.exists() or self.log_path.stat().st_size == 0:
            self.log_path.write_bytes(b"\xef\xbb\xbf")

        # 4. Popen 启动子进程
        #    - stdin=DEVNULL：子进程不读 stdin，避免阻塞
        #    - stdout=log_handle：子进程的 stdout 写入日志文件
        #    - stderr=STDOUT：stderr 也合并到日志文件
        #    - env: 强制 UTF-8 编码，避免 Windows GBK 导致中文乱码
        #    - _popen_kwargs()：平台特定参数，让子进程脱离当前终端
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        with self.log_path.open("a", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=child_env,
                **self._popen_kwargs(),
            )

        # 5. 等 0.3 秒确认子进程没崩溃
        #    常见崩溃原因：端口被占用、import 失败、配置文件错误
        pid = process.pid
        time.sleep(0.3)
        if not self._is_pid_running(pid):
            return False, "gateway_exited_during_startup", self.status()

        # 6. 写状态文件（PID + 端口 + 启动时间 + 命令行 + 日志路径）
        self._write_state({
            "pid": pid,
            "started_at": _utc_now(),
            "port": port,
            "host": host,
            "command": command,
            "log_path": str(self.log_path),
        })
        return True, "gateway_started", self.status()

    # ── 停止 ──────────────────────────────────────────────────

    def stop(self, *, timeout_s: int = 20) -> tuple[bool, str, GatewayStatus]:
        """停止后台 gateway 进程。

        流程：
        1. 读状态文件拿 PID，如果没有说明没启动过
        2. 调用 _terminate() 终止进程（先 SIGTERM 优雅停，超时再 SIGKILL 强杀）
        3. 清除状态文件

        Args:
            timeout_s: 优雅停止的等待秒数，超时后强制 kill

        Returns:
            (ok, message, status)
        """
        status = self.status()
        if not status.pid:
            return False, "gateway_not_running", status

        pid = status.pid
        # 尝试终止进程，先优雅后强制
        if not self._terminate(pid, timeout_s=timeout_s):
            return False, "gateway_stop_timeout", self.status()

        # 进程已终止，清除状态文件
        self._clear_state()
        return True, "gateway_stopped", GatewayStatus(
            running=False,
            state_path=self.state_path,
            log_path=self.log_path,
            reason="stopped",
        )

    # ── 重启 ──────────────────────────────────────────────────

    def restart(
        self,
        *,
        port: int | None = None,
        host: str | None = None,
        timeout_s: int = 20,
    ) -> tuple[bool, str, GatewayStatus]:
        """重启后台 gateway 进程。

        流程：读状态文件拿旧 PID + port/host → 杀旧进程 → 启动新进程。

        port/host 不传时从状态文件读上次的值，传了就用新值覆盖。
        如果没有状态文件（从没后台启动过），报错。
        用于改完代码后手动重启加载新代码。

        Args:
            port:      新端口，None 时沿用上次启动的端口
            host:      新地址，None 时沿用上次启动的地址
            timeout_s: 停旧进程的等待秒数

        Returns:
            (ok, message, status)
        """
        # 1. 读旧状态，拿旧 PID 和旧 port/host
        state = self._read_state()
        if not state:
            return False, "gateway_not_running", self.status()

        # 确定新进程的参数：优先用传入的，否则从状态文件读
        new_port = port if port is not None else state.get("port")
        new_host = host if host is not None else state.get("host")
        if not isinstance(new_port, int):
            return False, "no_port_in_state", self.status()

        # 2. 停止旧进程
        old_pid = state.get("pid")
        if isinstance(old_pid, int) and self._is_pid_running(old_pid):
            if not self._terminate(old_pid, timeout_s=timeout_s):
                return False, "gateway_stop_timeout", self.status()

        # 3. 清除旧状态文件
        self._clear_state()

        # 4. 启动新进程
        return self.start(port=new_port, host=new_host or "127.0.0.1")

    # ── 状态 ──────────────────────────────────────────────────

    def status(self) -> GatewayStatus:
        """查询后台 gateway 进程的当前状态。

        流程：
        1. 读状态文件，文件不存在 → not_started（从没启动过）
        2. 从状态文件取 PID
        3. 检查 PID 是否仍在运行（调用 _is_pid_running）
        4. 如果 PID 已死 → 清除状态文件，返回 process_dead
        5. PID 存活 → 返回 running + PID + 端口等信息

        这个方法有"自动清理"行为：如果发现状态文件存在但进程已死，
        会自动删除状态文件，避免下次 start() 误判为"已在运行"。
        """
        # 1. 读状态文件
        state = self._read_state()
        if not state:
            # 状态文件不存在 → 从没启动过后台进程
            return GatewayStatus(
                running=False,
                state_path=self.state_path,
                log_path=self.log_path,
                reason="not_started",
            )

        # 2. 取 PID
        pid = state.get("pid")
        if not isinstance(pid, int):
            # 状态文件损坏（PID 字段不是整数）
            return GatewayStatus(
                running=False,
                state_path=self.state_path,
                log_path=self.log_path,
                reason="invalid_state",
            )

        # 3. 检查 PID 是否存活
        if not self._is_pid_running(pid):
            # 进程已死 → 清除过期状态文件
            self._clear_state()
            return GatewayStatus(
                running=False,
                state_path=self.state_path,
                log_path=self.log_path,
                reason="process_dead",
            )

        # 4. 进程存活 → 返回完整状态
        return GatewayStatus(
            running=True,
            pid=pid,
            started_at=state.get("started_at"),
            port=state.get("port"),
            host=state.get("host"),
            state_path=self.state_path,
            log_path=self.log_path,
            reason="running",
        )

    # ── 日志 ──────────────────────────────────────────────────

    def read_log_tail(self, *, tail: int = 200) -> list[str]:
        """读取日志文件的最后 N 行。

        后台进程的 stdout/stderr 都写入 ~/.ftre/logs/gateway.log，
        这个方法读取该文件的尾部。

        Args:
            tail: 读取的行数（从尾部倒数）

        Returns:
            日志行列表，每行一个字符串。文件不存在时返回空列表。
        """
        if tail <= 0 or not self.log_path.exists():
            return []
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-tail:]

    def follow_logs(self, *, tail: int = 200) -> int:
        """打印已有日志尾部并持续跟随新日志，类似 `tail -f`。

        流程：
        1. 先打印已有的最后 tail 行（让用户看到上下文）
        2. seek 到文件末尾
        3. 循环 readline()，有新行就打印，没有就 sleep 0.5s
        4. 用户 Ctrl+C 退出，返回 130（Unix 惯例：信号中断的退出码）

        Args:
            tail: 先打印已有日志的行数

        Returns:
            退出码（130 = 被 Ctrl+C 中断）
        """
        # 1. 先打印已有日志的尾部
        for line in self.read_log_tail(tail=tail):
            print(line)

        # 2. 确保日志文件存在
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.touch(exist_ok=True)

        # 3. 持续跟随
        try:
            with self.log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(0, os.SEEK_END)  # 跳到文件末尾
                while True:
                    line = handle.readline()
                    if line:
                        # 有新内容，打印（去掉末尾换行，print 会再加）
                        print(line.rstrip("\n"))
                    else:
                        # 没有新内容，等 0.5 秒再读
                        time.sleep(0.5)
        except KeyboardInterrupt:
            return 130  # Ctrl+C 的退出码

    # ── 内部方法 ──────────────────────────────────────────────

    def _build_child_command(self, *, port: int, host: str) -> list[str]:
        """构建子进程的命令行。

        本质就是再调一次 ftre 的前台模式：
            python -m ftre gateway --foreground --port 8000 --host 127.0.0.1

        --foreground 标记让子进程知道自己是被后台启动的，走前台逻辑。
        """
        command = [
            self.python_executable,
            "-m",
            "ftre",
            "gateway",
            "--foreground",
            "--port",
            str(port),
            "--host",
            host,
        ]
        return command

    def _popen_kwargs(self) -> dict[str, Any]:
        """返回平台特定的 Popen 参数，让子进程脱离当前终端。

        Windows:
            CREATE_NEW_PROCESS_GROUP — 子进程创建独立进程组，
                Ctrl+C 不会传递到子进程（避免前台 Ctrl+C 杀死后台进程）
            CREATE_NO_WINDOW — 不弹出新的控制台窗口

        Linux/macOS:
            start_new_session=True — 调用 setsid()，子进程创建新会话，
                脱离当前控制终端。关 SSH/关终端时子进程不会收到 SIGHUP。
        """
        if self.platform_name == "Windows":
            flags = 0
            flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
            return {"creationflags": flags}
        return {"start_new_session": True}

    def _terminate(self, pid: int, *, timeout_s: int) -> bool:
        """终止进程，分平台调用不同实现。

        策略：先发优雅终止信号（SIGTERM / CTRL_BREAK），
        等待 timeout_s 秒，如果还没退出就强制 kill（SIGKILL / taskkill /F）。
        """
        if self.platform_name == "Windows":
            return self._terminate_windows(pid, timeout_s=timeout_s)
        return self._terminate_posix(pid, timeout_s=timeout_s)

    def _terminate_posix(self, pid: int, *, timeout_s: int) -> bool:
        """Linux/macOS 下的进程终止。

        1. 尝试获取进程组 ID（pgid），如果后台子进程是用 start_new_session 启动的，
           它有独立的进程组，用 killpg 可以杀掉它和它的所有子进程
        2. 发 SIGTERM（优雅终止，进程可以清理后退出）
        3. 等 timeout_s 秒
        4. 如果还没退出，发 SIGKILL（不可忽略，内核直接回收）
        5. 再等 2 秒确认
        """
        # 获取进程组 ID
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = None

        # 第一轮：SIGTERM 优雅终止
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGTERM)  # 杀整个进程组
            else:
                os.kill(pid, signal.SIGTERM)      # 只杀单个进程
        except ProcessLookupError:
            # 进程已经不存在了
            return True

        # 等待优雅退出
        if self._wait_for_exit(pid, timeout_s):
            return True

        # 第二轮：SIGKILL 强制终止
        with suppress(ProcessLookupError):
            if pgid is not None:
                os.killpg(pgid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        return self._wait_for_exit(pid, 2)

    def _terminate_windows(self, pid: int, *, timeout_s: int) -> bool:
        """Windows 下的进程终止（三段式）。

        Windows 没有 SIGTERM/SIGKILL，用以下方式：
        1. CTRL_BREAK_EVENT — 相当于 Ctrl+Break，子进程可以捕获并优雅退出
           （前提是子进程在 CREATE_NEW_PROCESS_GROUP 里，否则会报错）
        2. taskkill /PID N /T — 终止进程及其所有子进程（/T = tree）
        3. taskkill /PID N /T /F — 强制终止（/F = force，相当于 SIGKILL）
        """
        # 第一轮：CTRL_BREAK（优雅）
        # Windows 上 os.kill(pid, CTRL_BREAK_EVENT) 可能报 SystemError/OSError，
        # 失败了就跳过，直接用 taskkill
        ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
        if ctrl_break is not None:
            with suppress(ProcessLookupError, OSError, SystemError):
                os.kill(pid, ctrl_break)
            if self._wait_for_exit(pid, timeout_s):
                return True

        # 第二轮：taskkill /T（终止进程树）
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], check=False)
        if self._wait_for_exit(pid, 2):
            return True

        # 第三轮：taskkill /T /F（强制终止进程树）
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
        return self._wait_for_exit(pid, 2)

    def _wait_for_exit(self, pid: int, timeout_s: int | float) -> bool:
        """轮询等待进程退出，每 0.1 秒检查一次。

        Args:
            timeout_s: 最大等待秒数
        Returns:
            True = 进程已退出，False = 超时仍未退出
        """
        deadline = time.monotonic() + max(float(timeout_s), 0.0)
        while time.monotonic() < deadline:
            if not self._is_pid_running(pid):
                return True
            time.sleep(0.1)
        return not self._is_pid_running(pid)

    def _is_pid_running(self, pid: int) -> bool:
        """检查 PID 对应的进程是否仍在运行。

        Linux/macOS:
            os.kill(pid, 0) — 发信号 0（空操作），不实际杀进程，
            只检查进程是否存在。ProcessLookupError = 进程不存在。

        Windows:
            调用 _windows_pid_running()，用 Win32 API OpenProcess +
            GetExitCodeProcess 检查。退出码 259 (STILL_ACTIVE) = 还在跑。
        """
        if pid <= 0:
            return False
        if self.platform_name == "Windows":
            return _windows_pid_running(pid)
        try:
            os.kill(pid, 0)  # 信号 0 = 只检查不实际发信号
        except ProcessLookupError:
            return False   # 进程不存在
        except PermissionError:
            return True     # 进程存在但没权限发信号
        except OSError:
            return False    # 其他错误，保守返回 False
        return True

    def _read_state(self) -> dict[str, Any] | None:
        """读取状态文件 ~/.ftre/run/gateway.json。

        文件不存在或解析失败时返回 None。
        """
        try:
            with self.state_path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_state(self, payload: dict[str, Any]) -> None:
        """写入状态文件，使用原子写（先写临时文件再 rename）。

        原子写的目的是防止读到写了一半的 JSON：
        1. mkstemp 创建临时文件
        2. 写入 JSON + flush + fsync（确保数据落盘）
        3. rename 覆盖目标文件（原子操作，不会出现中间状态）

        这样即使写到一半断电，状态文件要么是旧的完整内容，
        要么是新的完整内容，不会是半个 JSON。
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # 1. 创建临时文件（和目标文件同目录，确保同文件系统，rename 才是原子的）
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{self.state_path.name}.",
            suffix=".tmp",
            dir=self.run_dir,
        )
        tmp_path = Path(tmp_name)
        try:
            # 2. 写入 JSON
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()            # 刷 Python 缓冲区
                os.fsync(handle.fileno())  # 刷 OS 缓冲区，确保数据落盘
            # 3. 原子替换
            tmp_path.replace(self.state_path)
        finally:
            # 清理可能残留的临时文件（如果 replace 之前出了异常）
            tmp_path.unlink(missing_ok=True)

    def _clear_state(self) -> None:
        """删除状态文件（进程已停止或已死时调用）。"""
        self.state_path.unlink(missing_ok=True)


def _windows_pid_running(pid: int) -> bool:
    """Windows 下检查 PID 是否仍在运行。

    用 Win32 API 实现：
    1. OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, pid) 获取进程句柄
    2. GetExitCodeProcess(handle) 获取退出码
    3. 退出码 == 259 (STILL_ACTIVE) → 进程还在跑

    如果 OpenProcess 返回 0（句柄为空），说明进程不存在或没权限。

    参数说明：
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            最低权限的进程查询权限，只需要知道进程是否存在
        STILL_ACTIVE = 259
            Windows 的特殊退出码，表示进程仍在运行
    """
    process_query_limited_information = 0x1000
    kernel32 = ctypes.windll.kernel32

    # 1. 打开进程，获取句柄
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # 句柄为空 → 进程不存在或没权限
        return False

    try:
        # 2. 查询退出码
        exit_code = ctypes.c_uint32()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            # API 调用失败
            return False
        # 3. 259 = STILL_ACTIVE
        return exit_code.value == 259
    finally:
        # 无论结果如何都要关闭句柄，避免句柄泄漏
        kernel32.CloseHandle(handle)
