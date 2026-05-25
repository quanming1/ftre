"""
bash 工具 - 执行 shell 命令（持久 cwd，由 runtime_context 承载）
"""
import os
import re
import subprocess
import sys
from pathlib import Path

from ftre_agent_core.tool import Tool, ToolParameter, Injected


def _decode(b: bytes) -> str:
    """按系统常见编码解码 subprocess 输出"""
    if sys.platform == "win32":
        for enc in ("gbk", "utf-8"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode("utf-8", errors="replace")
    return b.decode("utf-8", errors="replace")


def _try_handle_cd(command: str, ws: dict) -> str | None:
    """
    检测纯 cd 命令并直接更新 ws['cwd']。
    返回 None 表示不是纯 cd（继续走 subprocess）；返回字符串表示已处理。
    """
    cmd = command.strip()
    if sys.platform == "win32":
        m = re.match(r"^cd(?:\s+/d)?\s+(.+)$", cmd, re.IGNORECASE)
    else:
        m = re.match(r"^cd(?:\s+(.+))?$", cmd)
    if not m:
        return None

    target = m.group(1)
    cwd = Path(ws["cwd"])
    if not target:
        new_dir = Path.home()
    else:
        target = target.strip().strip('"').strip("'")
        target = os.path.expandvars(os.path.expanduser(target))
        new_dir = Path(target)
        if not new_dir.is_absolute():
            new_dir = cwd / new_dir

    try:
        new_dir = new_dir.resolve()
    except OSError as e:
        return f"[error] 无法解析路径: {e}"
    if not new_dir.exists():
        return f"[error] 目录不存在: {new_dir}"
    if not new_dir.is_dir():
        return f"[error] 不是目录: {new_dir}"

    ws["cwd"] = str(new_dir)
    return f"已切换到 {new_dir}"


def _build_subprocess_args(
    command: str,
) -> tuple[str | list[str], bool, str | None]:
    """
    根据平台决定如何把 command 交给 shell。

    返回 (args_or_command, shell_flag, executable)。

    Windows 上使用 shell=True 让 subprocess 直接调用 cmd /c <command>，
    避免 list2cmdline 把数组拼回命令行时把命令里的双引号 `\"` 转义成 cmd
    不认识的 `\\\"`，导致 `git commit -m "msg"` 这类命令被拆词。

    POSIX 上同样 shell=True，但显式用 /bin/bash（比 /bin/sh 功能多），
    fallback 到 sh 由 subprocess 自动处理。
    """
    if sys.platform == "win32":
        return (command, True, None)
    bash = "/bin/bash"
    if Path(bash).exists():
        return (command, True, bash)
    return (command, True, None)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """跨平台杀掉进程组（Unix）或进程树（Windows），避免子进程残留"""
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
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
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    _os.killpg(proc.pid, _signal.SIGKILL)
                except ProcessLookupError:
                    pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def create_bash_tool(default_timeout: int = 60, max_timeout: int = 600) -> Tool:
    """创建 bash 工具

    cwd 由 runtime_context['workspace'] 承载（一个 {'cwd': str} dict）。
    纯 cd 命令会持久切换 ws['cwd']；其他命令交给底层 shell 执行。

    Args:
        default_timeout: LLM 不传 timeout 时的默认值（秒）
        max_timeout: LLM 可指定的上限（防止"无限挂起"）
    """

    def bash(
        command: str,
        timeout: int = 0,
        ws: dict = Injected("workspace"),
    ) -> str:
        if not command.strip():
            return "[error] 空命令"
        if not isinstance(ws, dict) or "cwd" not in ws:
            return "[error] runtime_context.workspace 未注入"

        # timeout 处理：0/负数 → 用默认值；超过上限 → 钳位
        if timeout is None or timeout <= 0:
            effective_timeout = default_timeout
        else:
            effective_timeout = min(int(timeout), max_timeout)

        # 1) 纯 cd → 持久切换
        cd_result = _try_handle_cd(command, ws)
        if cd_result is not None:
            return cd_result

        # 2) 其他命令走 subprocess
        args, shell_flag, executable = _build_subprocess_args(command)
        cwd = ws["cwd"]
        popen_kwargs: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": cwd,
            "shell": shell_flag,
        }
        if executable is not None:
            popen_kwargs["executable"] = executable
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(args, **popen_kwargs)
            try:
                stdout_b, stderr_b = proc.communicate(timeout=effective_timeout)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                try:
                    stdout_b, stderr_b = proc.communicate(timeout=2)
                except Exception:
                    stdout_b, stderr_b = b"", b""
                stdout = _decode(stdout_b)
                stderr = _decode(stderr_b)
                msg = (
                    f"[error] 命令超时（{effective_timeout}s），进程已被强制结束。"
                    f"如需运行长任务请拆分命令或调大 timeout 参数（最大 {max_timeout}s）。"
                )
                tail = []
                if stdout.strip():
                    tail.append(stdout.rstrip()[-2000:])
                if stderr.strip():
                    tail.append(f"[stderr]\n{stderr.rstrip()[-2000:]}")
                return msg + ("\n" + "\n".join(tail) if tail else "")

            stdout = _decode(stdout_b)
            stderr = _decode(stderr_b)
            output_lines = [f"[cwd] {cwd}"]
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
            "- cwd 由 runtime_context.workspace 承载，跨工具调用持久；\n"
            "- 仅【纯 cd】命令（如 `cd src`、`cd /d D:\\proj`）会持久切换 cwd；\n"
            "- 组合命令（如 `cd x && yyy`）由底层 shell 自行处理，不持久切换；\n"
            "- 平台：Windows 走 cmd /s /c，POSIX 走 /bin/bash -c；\n"
            f"- 默认超时 {default_timeout}s，可通过 timeout 参数延长（上限 {max_timeout}s）；\n"
            "- 输出首行 [cwd] 显示当前工作目录，便于排错。"
        ),
        parameters=[
            ToolParameter(
                name="command",
                type="string",
                description="要执行的 shell 命令",
                required=True,
            ),
            ToolParameter(
                name="timeout",
                type="number",
                description=(
                    f"超时秒数。0 或不传 → 默认 {default_timeout}s；"
                    f"上限 {max_timeout}s（超过会被钳位）。"
                    "长任务（npm install / 大型构建 / 网络抓取等）传更大值"
                ),
                required=False,
            ),
        ],
        func=bash,
    )
