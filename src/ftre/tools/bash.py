"""
bash 工具 - 执行 shell 命令（cwd 来自 sessions 表的 workspace 字段）
"""
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from ftre_agent_core.tool import Tool, ToolParameter, Injected

from ._truncate import truncate_output
from ._workspace import WorkspaceAccessor


# ============== 用户级 PATH 补全 ==============

# 后台进程的 PATH 可能缺少用户级目录（~/.local/bin 等），
# 导致 shutil.which() 找不到 semble 等用户安装的工具。
# 这里列出常见的用户级 bin 目录，作为 fallback。

def _user_bin_dirs() -> list[str]:
    """返回常见的用户级可执行文件目录（仅返回实际存在的）"""
    home = Path.home()
    if sys.platform == "win32":
        candidates = [
            home / ".local" / "bin",
            home / ".mavis" / "bin",
            home / "AppData" / "Local" / "Programs" / "uv",
            home / "AppData" / "Roaming" / "uv",
            home / "AppData" / "Roaming" / "uv" / "tools",
        ]
    else:
        candidates = [
            home / ".local" / "bin",
            home / ".cargo" / "bin",
            home / "go" / "bin",
        ]
    return [str(p) for p in candidates if p.is_dir()]


def _which_with_user_paths(name: str) -> str | None:
    """shutil.which() + 用户级 PATH fallback"""
    # 先试 shutil.which（走当前进程 PATH）
    result = shutil.which(name)
    if result:
        return result
    # fallback：遍历用户级 bin 目录
    for d in _user_bin_dirs():
        if sys.platform == "win32":
            candidate = os.path.join(d, f"{name}.exe")
        else:
            candidate = os.path.join(d, name)
        if os.path.isfile(candidate):
            return candidate
    return None


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


def _shell_executable() -> str | None:
    """返回执行命令时显式指定的 shell 可执行文件，None 表示用平台默认。

    Windows: shell=True 走 cmd /c，无需显式 executable。
    POSIX: 优先 /bin/bash（比 /bin/sh 功能多），不存在则回退默认 sh。

    统一用 shell=True：避免 list2cmdline 把数组拼回命令行时，把命令里的
    双引号转义成 shell 不认识的形式，导致 `git commit -m "msg"` 被拆词。
    """
    if sys.platform == "win32":
        return None
    bash = "/bin/bash"
    return bash if Path(bash).exists() else None


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


def _platform_hints() -> str:
    """根据当前进程所在平台拼装一段提示，让 LLM 用对该平台的命令/路径/编码。"""
    if sys.platform == "win32":
        return (
            "运行环境：Windows（cmd /c 执行）。\n"
            "  - 路径分隔符 `\\`，盘符形式 `D:\\proj`；shell 通配符行为与 POSIX 不同；\n"
            "  - 命令差异：列目录 `dir`（不是 `ls`），删文件 `del`/`rmdir /s /q`，复制 `copy`/`xcopy`,\n"
            "    查看文件 `type`，环境变量 `set NAME=value`，PATH 用 `;` 分隔，行尾 CRLF；\n"
            "  - 多命令串联用 `&&` 或 `&`（不是 `;`，cmd 里 `;` 是普通字符）；\n"
            "  - 编码：cmd 默认 GBK/CP936，输出会按 GBK→UTF-8 顺序解码；\n"
            "    给文件写中文请显式 `chcp 65001` 或直接用 PowerShell；\n"
            "  - 想用类 Unix 命令时改走 `pwsh -c '<cmd>'` 或 `bash -c '<cmd>'`（如装了 Git Bash/WSL）。"
        )
    if sys.platform == "darwin":
        return (
            "运行环境：macOS（/bin/bash -c 执行）。\n"
            "  - 路径分隔符 `/`；BSD 用户态工具（`sed`/`grep`/`awk`）选项与 GNU 版略有差异，\n"
            "    例如 `sed -i ''` 需要空字符串占位（GNU 直接 `sed -i`）；\n"
            "  - 编码默认 UTF-8；多命令串联 `&&`/`||`/`;`/`|` 行为标准。"
        )
    return (
        "运行环境：Linux/POSIX（/bin/bash -c 执行；不存在时回退默认 sh）。\n"
        "  - 路径分隔符 `/`，编码默认 UTF-8；GNU 用户态工具语义；\n"
        "  - 多命令串联 `&&`/`||`/`;`/`|` 行为标准。"
    )


# 缓存 semble 检测结果


@lru_cache(maxsize=1)
def _find_semble() -> str | None:
    """查找 semble 可执行文件路径，结果会被缓存。

    优先在 PATH 中查找（含用户级目录 fallback）；找不到再看 uvx 是否可用，
    尝试通过 `uvx --from "semble[mcp]" semble` 调用作为降级方案。
    """
    # 主路径：shutil.which + 用户级 PATH fallback
    path = _which_with_user_paths("semble")
    if path:
        return path

    # 降级方案：尝试通过 uvx 调用
    uvx_path = _which_with_user_paths("uvx")
    if not uvx_path:
        return None
    try:
        # 用 --help 快速检测 uvx 能否拉起 semble（不真正执行索引）
        result = subprocess.run(
            [uvx_path, "--from", "semble[mcp]", "semble", "--help"],
            capture_output=True,
            timeout=15,
        )
    except Exception:
        return None
    if result.returncode == 0:
        return f"{uvx_path} --from \"semble[mcp]\" semble"
    return None


def _semble_hints() -> str:
    """检测到 semble 已安装时，生成一段精简的代码检索使用建议；否则返回空串。

    强调 semble 是 CLI 命令而非工具，给少量精准示例 + 何时用/何时不用，
    完整子命令与参数引导 LLM 自行用 `--help` 查看。
    """
    semble_path = _find_semble()
    if not semble_path:
        return ""

    # 判断是直接安装还是 uvx 降级调用
    is_uvx = "uvx" in semble_path and "--from" in semble_path
    cmd_prefix = semble_path if is_uvx else "semble"

    install_hint = (
        f"（通过 uvx 调用：{cmd_prefix}）"
        if is_uvx
        else "（已在本机 PATH 中可直接调用）"
    )

    return (
        "\n\n"
        f"【代码检索：本机已安装 semble，按行为/功能找代码时优先用它】\n"
        "semble 不是工具（tool），是命令行程序（CLI），必须通过本 bash 工具作为 shell 命令执行。\n"
        f"它是为 AI 设计的语义代码检索 CLI{install_hint}：用自然语言描述意图即可，"
        "无需先猜函数/类名，只返回最相关的代码片段（带 file_path 和行号），token 远少于 grep+read。\n"
        "精准示例（路径省略默认当前目录）：\n"
        f"  {cmd_prefix} search \"how authentication is handled\" .   # 按行为找代码，英文描述更准\n"
        f"  {cmd_prefix} find-related src/auth.py 42 .              # 找与某段代码相似的实现\n"
        "何时用：想理解某功能在哪实现、找同类/重复代码、不确定命名时。\n"
        "何时回退 grep/findstr/rg：要穷尽某字面串的全部出现位置、重命名 API 列全部 caller、"
        "或仅验证某固定字符串是否存在。\n"
        f"更多子命令和参数（如 --top-k / --content docs|config|all / 搜远程仓库）直接运行 "
        f"`{cmd_prefix} --help` 查看，按需使用。"
    )


def create_bash_tool(default_timeout: int = 60, max_timeout: int = 3600) -> Tool:
    """创建 bash 工具

    cwd 由当前会话的 workspace 字段承载（sessions 表）。纯 cd 命令会持久切换
    DB 中的 workspace；其他命令交给底层 shell 执行（subprocess.cwd 从 DB 取）。

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

        # 2) 执行命令
        cwd = ws.get()
        popen_kwargs: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": cwd,
            "shell": True,
        }
        executable = _shell_executable()
        if executable is not None:
            popen_kwargs["executable"] = executable
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(command, **popen_kwargs)
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
            output_lines = ["<FTRE_SYSTEM_FACT>", f"[cwd] {cwd}", "</FTRE_SYSTEM_FACT>"]
            if stdout.strip():
                output_lines.append(stdout.rstrip())
            if stderr.strip():
                output_lines.append(f"[stderr]\n{stderr.rstrip()}")
            if proc.returncode != 0:
                output_lines.append(f"[exit_code] {proc.returncode}")
            return truncate_output("\n".join(output_lines))
        except FileNotFoundError as e:
            return f"[error] 未找到 shell: {e}"
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}"

    return Tool(
        name="bash",
        description=(
            "执行 shell 命令并返回输出。\n"
            f"{_platform_hints()}\n"
            "通用规则：\n"
            "- cwd 来自当前会话的 workspace（持久到 DB），跨工具调用持久；\n"
            "- 仅【纯 cd】命令（如 `cd src`、`cd /d D:\\proj`）会持久切换工作区；\n"
            "- 组合命令（如 `cd x && yyy`）由底层 shell 自行处理，不持久切换；\n"
            f"- 默认超时 {default_timeout}s，可通过 timeout 参数延长（上限 {max_timeout}s）；\n"
            "- 输出开头的 [cwd] 显示当前工作目录（包在 <FTRE_SYSTEM_FACT> 中，是系统事实），便于排错；\n"
            "- 字节输出按平台默认编码解码（Windows 优先 GBK 再退 UTF-8；其他平台 UTF-8），\n"
            "  解码失败的字节会被替换字符占位，必要时让程序直接输出 UTF-8；\n"
            "- 【重要】命令执行结束后，其进程/线程会被立即回收，不会在后台存活；\n"
            "  因此严禁用本工具直接启动需要长期驻留的进程（如开发服务器、watch、"
            "数据库、`npm run dev`/`start.bat` 等）——它们会随命令返回被一并杀掉，"
            "或在超时后被强制终止；\n"
            "  若需启动这类常驻服务，请新开一个独立窗口运行（如 Windows `start cmd /k \"...\"`、"
            "`start powershell ...`，其他平台用 `nohup ... &`、`setsid`、`tmux`/`screen` 等），"
            "让进程脱离本工具的生命周期独立存活；\n"
            f"{_semble_hints()}"
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
