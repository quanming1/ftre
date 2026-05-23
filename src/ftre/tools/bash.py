"""
bash 工具 - 执行 shell 命令（持久 cwd）
"""
import os
import re
import subprocess
import sys
from pathlib import Path

from ftre_agent_core.tool import Tool, ToolParameter


class _BashState:
    """会话级 bash 状态：维护当前工作目录"""

    def __init__(self, initial_cwd: str | None = None):
        self.cwd = Path(initial_cwd or os.getcwd()).resolve()


def _decode(b: bytes) -> str:
    """按系统常见编码解码 subprocess 输出"""
    if sys.platform == "win32":
        # Windows 中文环境优先 GBK，回退 UTF-8
        for enc in ("gbk", "utf-8"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode("utf-8", errors="replace")
    return b.decode("utf-8", errors="replace")


def _try_handle_cd(command: str, state: _BashState) -> str | None:
    """
    检测纯 cd 命令并直接更新 state.cwd。
    返回 None 表示不是纯 cd（继续走 subprocess）；返回字符串表示已处理。

    只处理"纯 cd <dir>"（如 `cd src`、`cd "D:\\proj"`、Windows 的 `cd /d X`），
    不处理 `cd && yyy`，也不处理无参 `cd`（Windows 下 `cd` 是显示当前目录）。
    """
    cmd = command.strip()
    if sys.platform == "win32":
        # 必须有参数：`cd /d X` 或 `cd X`
        m = re.match(r"^cd(?:\s+/d)?\s+(.+)$", cmd, re.IGNORECASE)
    else:
        # POSIX：`cd` (回 home) 或 `cd X`
        m = re.match(r"^cd(?:\s+(.+))?$", cmd)
    if not m:
        return None

    target = m.group(1)
    if not target:
        # 仅 POSIX 走到这里：cd → home
        new_dir = Path.home()
    else:
        target = target.strip().strip('"').strip("'")
        target = os.path.expandvars(os.path.expanduser(target))
        new_dir = Path(target)
        if not new_dir.is_absolute():
            new_dir = state.cwd / new_dir

    try:
        new_dir = new_dir.resolve()
    except OSError as e:
        return f"[error] 无法解析路径: {e}"
    if not new_dir.exists():
        return f"[error] 目录不存在: {new_dir}"
    if not new_dir.is_dir():
        return f"[error] 不是目录: {new_dir}"

    state.cwd = new_dir
    return f"已切换到 {new_dir}"


def _build_subprocess_args(command: str) -> tuple[list[str], bool]:
    """
    根据平台显式选择 shell。
    返回 (args_list_or_str, shell_flag)。

    Windows: cmd /c "command"
    Unix:    /bin/bash -c "command"  （不用 -lc，避免触发 login profile 拖慢）
    """
    if sys.platform == "win32":
        # 用 list 形式 + shell=False，避免外层 PowerShell 等环境变量误干扰；
        # /s /c 让 cmd 不丢首尾引号、按字面执行
        return (["cmd.exe", "/s", "/c", command], False)
    # POSIX：找一个可用的 bash
    bash = "/bin/bash"
    if not Path(bash).exists():
        bash = "/usr/bin/env"
        return ([bash, "bash", "-c", command], False)
    return ([bash, "-c", command], False)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """跨平台杀掉进程组（Unix）或进程树（Windows），避免子进程残留"""
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            # taskkill /T 终止整棵进程树
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
            )
        else:
            import os as _os
            import signal as _signal
            try:
                _os.killpg(proc.pid, _signal.SIGTERM)
            except ProcessLookupError:
                pass
            # 给个机会 graceful 退出
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    _os.killpg(proc.pid, _signal.SIGKILL)
                except ProcessLookupError:
                    pass
    except Exception:
        # 兜底：直接 kill
        try:
            proc.kill()
        except Exception:
            pass


def create_bash_tool(
    timeout: int = 60,
    initial_cwd: str | None = None,
    state: "_BashState | None" = None,
) -> Tool:
    """创建 bash 工具

    工具维护一个会话级 cwd。**只有"纯 cd"命令**会持久切换 cwd；
    其他命令一律原样交给底层 shell 执行。

    Args:
        timeout: 单次命令超时秒数（默认 60）
        state: 共享 cwd 状态（与 read/write/edit 共用）。不传则新建。
    """
    if state is None:
        state = _BashState(initial_cwd)

    def bash(command: str) -> str:
        if not command.strip():
            return "[error] 空命令"

        # 1) 纯 cd → 持久切换 cwd，不执行 subprocess
        cd_result = _try_handle_cd(command, state)
        if cd_result is not None:
            return cd_result

        # 2) 其他命令一律走 subprocess（含 `cd && xxx` 这种组合，由 shell 自己处理）
        args, shell_flag = _build_subprocess_args(command)

        # Windows 用 CREATE_NEW_PROCESS_GROUP；POSIX 用 setsid，方便超时时整组 kill
        popen_kwargs: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": str(state.cwd),
            "shell": shell_flag,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(args, **popen_kwargs)
            try:
                stdout_b, stderr_b = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # 整组 kill，避免子进程残留
                _kill_process_tree(proc)
                try:
                    stdout_b, stderr_b = proc.communicate(timeout=2)
                except Exception:
                    stdout_b, stderr_b = b"", b""
                stdout = _decode(stdout_b)
                stderr = _decode(stderr_b)
                msg = (
                    f"[error] 命令超时（{timeout}s），进程已被强制结束。"
                    f"如需运行长任务请拆分命令或调高 timeout。"
                )
                tail = []
                if stdout.strip():
                    tail.append(stdout.rstrip()[-2000:])
                if stderr.strip():
                    tail.append(f"[stderr]\n{stderr.rstrip()[-2000:]}")
                return msg + ("\n" + "\n".join(tail) if tail else "")

            stdout = _decode(stdout_b)
            stderr = _decode(stderr_b)

            output_lines = [f"[cwd] {state.cwd}"]
            if stdout.strip():
                output_lines.append(stdout.rstrip())
            if stderr.strip():
                output_lines.append(f"[stderr]\n{stderr.rstrip()}")
            if proc.returncode != 0:
                output_lines.append(f"[exit_code] {proc.returncode}")
            return "\n".join(output_lines)
        except FileNotFoundError as e:
            return f"[error] 未找到 shell: {e}"
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}"

    return Tool(
        name="bash",
        description=(
            "执行 shell 命令并返回输出。\n"
            "- 持久 cwd：仅【纯 cd】命令（如 `cd src`、`cd /d D:\\proj`）会跨调用保留；\n"
            "- 组合命令（如 `cd x && yyy`）由底层 shell 自行处理，不持久切换 cwd；\n"
            "- 平台：Windows 走 cmd /s /c，POSIX 走 /bin/bash -c；\n"
            "- 单条命令默认超时 60 秒，挂死会被强制终止。\n"
            "- 输出首行 [cwd] 显示当前工作目录，便于排错。"
        ),
        parameters=[
            ToolParameter(
                name="command",
                type="string",
                description="要执行的 shell 命令",
                required=True,
            ),
        ],
        func=bash,
    )
