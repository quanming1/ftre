"""
bash 工具 - 执行 shell 命令（cwd 来自 sessions 表的 workspace 字段）
支持 RTK 自动重写以减少 token 消耗
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ftre_agent_core.tool import Tool, ToolParameter, Injected

from ._workspace import WorkspaceAccessor


# ============== RTK 集成 ==============

# 缓存 rtk 可执行文件路径（None=未检测，False=不可用，str=路径）
_rtk_path_cache: str | bool | None = None


def _find_rtk() -> str | None:
    """查找 rtk 可执行文件路径，结果会被缓存"""
    global _rtk_path_cache
    if _rtk_path_cache is None:
        path = shutil.which("rtk")
        _rtk_path_cache = path if path else False
    return _rtk_path_cache if _rtk_path_cache else None


def _rtk_rewrite(command: str) -> str | None:
    """
    调用 rtk rewrite 判断命令是否需要重写。
    返回重写后的命令，或 None 表示不需要重写。
    """
    rtk_path = _find_rtk()
    if not rtk_path:
        return None

    try:
        result = subprocess.run(
            [rtk_path, "rewrite", command],
            capture_output=True,
            timeout=2,  # rtk rewrite 应该很快
        )
        # exit 0 或 3 表示需要重写，输出是重写后的命令
        if result.returncode in (0, 3) and result.stdout.strip():
            return result.stdout.decode("utf-8", errors="replace").strip()
        return None
    except Exception:
        return None


def _should_skip_rtk(command: str) -> bool:
    """
    判断命令是否应该跳过 RTK 重写。
    某些命令不适合通过 RTK：
    - 已经是 rtk 命令
    - 纯 shell 内置命令（cd, set, export 等）
    - 环境变量设置
    """
    cmd = command.strip()
    cmd_lower = cmd.lower()

    # 已经是 rtk 命令
    if cmd_lower.startswith("rtk ") or cmd_lower == "rtk":
        return True

    # shell 内置命令（不产生大量输出）
    skip_prefixes = (
        "cd ", "set ", "export ", "unset ", "alias ", "source ",
        "echo ", "printf ", "pwd", "exit ", "return ",
        "setx ", "path ", "cls", "clear",
    )
    for prefix in skip_prefixes:
        if cmd_lower.startswith(prefix) or cmd_lower == prefix.strip():
            return True

    return False


# ============== 原有功能 ==============

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


def _has_shell_operator(cmd: str) -> bool:
    """
    检查命令里是否包含引号外的 shell 操作符（&& || ; | > < &）。
    用于判定"这是组合命令、还是纯 cd"——组合命令必须丢回 shell 跑。

    简易状态机：跟踪单/双引号嵌套，操作符只在"两种引号都不在"时才计数。
    不处理转义引号 / 反引号子命令（罕见到不值得为它写完整 shell 解析）。
    """
    in_single = False
    in_double = False
    i = 0
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            two = cmd[i : i + 2]
            if two in ("&&", "||"):
                return True
            if c in ("|", ";", ">", "<", "&"):
                return True
        i += 1
    return False


def _try_handle_cd(command: str, ws: WorkspaceAccessor) -> str | None:
    """
    检测纯 cd 命令并直接更新会话工作区。
    返回 None 表示不是纯 cd（继续走 subprocess）；返回字符串表示已处理。
    """
    cmd = command.strip()
    # 含 shell 操作符的复合命令（如 `cd x && yyy`）一律走 shell，不在这里持久切换 cwd
    if _has_shell_operator(cmd):
        return None
    if sys.platform == "win32":
        m = re.match(r"^cd(?:\s+/d)?\s+(.+)$", cmd, re.IGNORECASE)
    else:
        m = re.match(r"^cd(?:\s+(.+))?$", cmd)
    if not m:
        return None

    target = m.group(1)
    cwd = Path(ws.get())
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

    ws.set(str(new_dir))
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

    cwd 由当前会话的 workspace 字段承载（sessions 表）。纯 cd 命令会持久切换
    DB 中的 workspace；其他命令交给底层 shell 执行（subprocess.cwd 从 DB 取）。

    RTK 集成：
    - 自动检测 rtk 是否安装
    - 对支持的命令自动重写为 rtk 版本（如 git status → rtk git status）
    - 减少命令输出的 token 消耗（60-90%）

    Args:
        default_timeout: LLM 不传 timeout 时的默认值（秒）
        max_timeout: LLM 可指定的上限（防止"无限挂起"）
    """

    def bash(
        command: str,
        timeout: int = 0,
        ws: WorkspaceAccessor = Injected("workspace"),
    ) -> str:
        if not command.strip():
            return "[error] 空命令"
        if not isinstance(ws, WorkspaceAccessor):
            return "[error] runtime_context.workspace 未注入"

        # timeout 处理：0/负数 → 用默认值；超过上限 → 钳位
        if timeout is None or timeout <= 0:
            effective_timeout = default_timeout
        else:
            effective_timeout = min(int(timeout), max_timeout)

        # 1) 纯 cd → 持久切换（写 DB）
        cd_result = _try_handle_cd(command, ws)
        if cd_result is not None:
            return cd_result

        # 2) RTK 重写（如果可用）
        actual_command = command
        if not _should_skip_rtk(command):
            rewritten = _rtk_rewrite(command)
            if rewritten and rewritten != command:
                actual_command = rewritten

        # 3) 执行命令
        args, shell_flag, executable = _build_subprocess_args(actual_command)
        cwd = ws.get()
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
            "- cwd 来自当前会话的 workspace（持久到 DB），跨工具调用持久；\n"
            "- 仅【纯 cd】命令（如 `cd src`、`cd /d D:\\proj`）会持久切换工作区；\n"
            "- 组合命令（如 `cd x && yyy`）由底层 shell 自行处理，不持久切换；\n"
            "- 平台：Windows 走 cmd /s /c，POSIX 走 /bin/bash -c；\n"
            f"- 默认超时 {default_timeout}s，可通过 timeout 参数延长（上限 {max_timeout}s）；\n"
            "- 输出首行 [cwd] 显示当前工作目录，便于排错；\n"
            "- RTK 集成：自动压缩 git/cargo/npm 等命令输出，减少 token 消耗。"
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
