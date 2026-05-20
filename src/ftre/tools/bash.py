"""
bash 工具 - 执行 shell 命令（持久 cwd）
"""
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from ftre_agent_core.tool import Tool, ToolParameter


class _BashState:
    """会话级 bash 状态：维护当前工作目录"""

    def __init__(self, initial_cwd: str | None = None):
        self.cwd = Path(initial_cwd or os.getcwd()).resolve()


def _decode(b: bytes) -> str:
    """按当前系统的常见编码解码"""
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
    检测纯 cd 命令并直接更新 state.cwd
    返回 None 表示不是 cd（继续走 subprocess），返回字符串表示已处理
    """
    cmd = command.strip()
    # 仅处理简单的 `cd <dir>` 或 `cd` (回 home)
    m = re.match(r"^cd(?:\s+(.+))?$", cmd)
    if not m:
        return None

    target = m.group(1)
    if not target:
        new_dir = Path.home()
    else:
        target = target.strip().strip('"').strip("'")
        # 展开 ~ 和环境变量
        target = os.path.expandvars(os.path.expanduser(target))
        new_dir = Path(target)
        if not new_dir.is_absolute():
            new_dir = state.cwd / new_dir

    new_dir = new_dir.resolve()
    if not new_dir.exists():
        return f"[error] 目录不存在: {new_dir}"
    if not new_dir.is_dir():
        return f"[error] 不是目录: {new_dir}"

    state.cwd = new_dir
    return f"已切换到 {new_dir}"


def create_bash_tool(
    timeout: int = 60,
    initial_cwd: str | None = None,
    state: "_BashState | None" = None,
) -> Tool:
    """创建 bash 工具

    工具维护一个会话级 cwd，`cd` 命令会持久生效。

    Args:
        state: 共享 cwd 状态（与 read/write/edit 共用）。如果不传则新建。
    """
    if state is None:
        state = _BashState(initial_cwd)

    def bash(command: str) -> str:
        # 处理 cd
        cd_result = _try_handle_cd(command, state)
        if cd_result is not None:
            return cd_result

        # 检测 `cd X && Y` 类组合：先 cd 再执行余下的命令
        parts = re.match(r"^cd\s+([^&;]+?)\s*(?:&&|;)\s*(.+)$", command.strip())
        sub_cwd = state.cwd
        actual_cmd = command
        if parts:
            target = parts.group(1).strip().strip('"').strip("'")
            target = os.path.expandvars(os.path.expanduser(target))
            new_dir = Path(target)
            if not new_dir.is_absolute():
                new_dir = state.cwd / new_dir
            new_dir = new_dir.resolve()
            if new_dir.exists() and new_dir.is_dir():
                state.cwd = new_dir
                sub_cwd = new_dir
                actual_cmd = parts.group(2).strip()

        try:
            result = subprocess.run(
                actual_cmd,
                shell=True,
                capture_output=True,
                timeout=timeout,
                cwd=str(sub_cwd),
            )
            stdout = _decode(result.stdout)
            stderr = _decode(result.stderr)

            output_lines = [f"[cwd] {sub_cwd}"]
            if stdout.strip():
                output_lines.append(stdout.rstrip())
            if stderr.strip():
                output_lines.append(f"[stderr]\n{stderr.rstrip()}")
            if result.returncode != 0:
                output_lines.append(f"[exit_code] {result.returncode}")
            return "\n".join(output_lines)
        except subprocess.TimeoutExpired:
            return f"[error] 命令超时（{timeout}s）"
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}"

    return Tool(
        name="bash",
        description=(
            "执行 shell 命令并返回输出。会话级维护 cwd —— `cd` 会持久生效，"
            "后续命令在新目录执行。每次输出会带 [cwd] 显示当前目录。"
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
