"""
Cron 工具 - 让 Agent 创建/管理定时任务

每个 job 是 ~/.ftre/cron/<job_id>.json：
{
    "id": "job_xxx",
    "cron": "*/5 * * * *",
    "title": "每5分钟提醒",
    "prompt": "提醒我喝水",
    "disabled": false,
    "created_at": 1700000000.0,
    "run_history": [1700000000.0, 1700000300.0, ...]
}

调度由 AgentLoop._cron_loop 协程负责：每分钟扫描目录，
对到期任务生成 user_input 投递到 Bus（在独立 cron session 中执行）。
"""
import json
import logging
import os
import time
import uuid
from pathlib import Path

from croniter import croniter
from ftre_agent_core.tool import Tool, ToolParameter, Injected
from ftre.channel import Channel

logger = logging.getLogger(__name__)

CRON_DIR = Path(os.environ.get("USERPROFILE", Path.home())) / ".ftre" / "cron"


def _ensure_dir() -> None:
    CRON_DIR.mkdir(parents=True, exist_ok=True)


def _job_path(job_id: str) -> Path:
    return CRON_DIR / f"{job_id}.json"


def load_all_jobs() -> list[dict]:
    """读取所有 cron 任务"""
    _ensure_dir()
    jobs = []
    for f in CRON_DIR.glob("*.json"):
        try:
            jobs.append(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[cron] 读取失败 {f.name}: {e}")
    return jobs


def save_job(job: dict) -> None:
    _ensure_dir()
    _job_path(job["id"]).write_text(
        json.dumps(job, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def delete_job(job_id: str) -> bool:
    p = _job_path(job_id)
    if p.exists():
        p.unlink()
        return True
    return False


def append_run(job_id: str, ts: float | None = None) -> None:
    """追加一次运行时间记录到 run_history"""
    p = _job_path(job_id)
    if not p.exists():
        return
    try:
        job = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    job.setdefault("run_history", []).append(ts if ts is not None else time.time())
    save_job(job)


def last_run(job: dict) -> float:
    """取 job 最近一次运行时间，没有则返回 created_at"""
    history = job.get("run_history") or []
    if history:
        return history[-1]
    return job.get("created_at", 0.0)


# ============================================================
# Cron Channel - 静默通道，不向任何外部投递（cron session 仅通过 send_message 推到其他 channel）
# ============================================================

class CronChannel(Channel):
    def __init__(self, bus):
        super().__init__(channel_id="cron", name="Cron Channel", bus=bus)

    async def send(self, msg) -> None:
        """cron channel 是静默的，outbound 不推送任何地方"""
        return


# ============================================================
# Scheduler
# ============================================================

class CronScheduler:
    """
    Cron 调度器：周期扫描 ~/.ftre/cron/，对到期任务投递 user_input 到 Bus。

    每个任务在独立 cron session 中执行，结果不污染原始会话。
    Agent 收到 prompt 后可调用 send_message 向其他 session 推送结果。
    """

    def __init__(self, bus, session_manager, channel_manager=None, default_channel: str = "cron", scan_interval: int = 30):
        self.bus = bus
        self.session_manager = session_manager
        self.default_channel = default_channel
        self.scan_interval = scan_interval
        self._task: 'asyncio.Task | None' = None
        # 注册静默 cron channel 让 outbound 分发不报 unknown channel
        if channel_manager is not None:
            channel_manager.register(CronChannel(bus))

    def start(self) -> None:
        import asyncio
        self._task = asyncio.create_task(self._loop())
        logger.warning(f"[cron] 调度器已启动 (扫描间隔 {self.scan_interval}s)")

    async def stop(self) -> None:
        import asyncio
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        import asyncio
        try:
            while True:
                try:
                    await self._tick()
                except Exception as e:
                    logger.error(f"[cron] tick 出错: {e}")
                await asyncio.sleep(self.scan_interval)
        except asyncio.CancelledError:
            pass

    async def _tick(self) -> None:
        """扫描所有 job，触发到期的"""
        from ftre.bus import BusMessage

        now = time.time()
        for job in load_all_jobs():
            if job.get("disabled"):
                continue

            cron_expr = job.get("cron", "")
            if not cron_expr or not croniter.is_valid(cron_expr):
                continue

            base = last_run(job)
            try:
                next_ts = croniter(cron_expr, base).get_next(ret_type=float)
            except Exception as e:
                logger.warning(f"[cron] {job['id']} 解析失败: {e}")
                continue

            if next_ts > now:
                continue  # 还没到点

            # 触发：先记录时间（避免下一轮重复触发），再投递消息
            append_run(job["id"], now)

            cron_session_id = await self.session_manager.create_session(
                channel_id=self.default_channel,
                title=f"[cron] {job.get('title', job['id'])}",
            )

            msg = BusMessage(
                type="user_input",
                from_channel=self.default_channel,
                to_channel=self.default_channel,
                from_session=cron_session_id,
                to_session=cron_session_id,
                data={"content": job.get("prompt", ""), "session_id": cron_session_id},
            )
            await self.bus.publish_inbound(msg)
            logger.warning(f"[cron] 触发 {job['id']} → session={cron_session_id}")


# ============================================================
# Tool: 单一 cron 工具，用 action 分发
# ============================================================

CRON_TOOL_DESCRIPTION = """\
管理定时任务。通过 action 参数分发不同操作：

- action="create"  创建任务，必填: cron, title, prompt；可选: disabled
- action="list"    列出所有任务
- action="delete"  删除任务，必填: job_id
- action="update"  更新任务字段，必填: job_id；可选: cron, title, prompt, disabled（任填一项）

cron 表达式（5 段：分 时 日 月 周）
  例：'*/5 * * * *' 每5分钟；'0 9 * * *' 每天9点；'0 */1 * * *' 每小时整点

任务到期会触发 agent 在独立 cron session 中执行 prompt。
disabled=true 时调度器会跳过该任务（保留任务定义和历史，可随时启用）。

⚠️ 关于 prompt 字段（重要！避免误解）：
- prompt 是**每次到期单独触发**时发给 agent 的指令，描述"这一次要做的事"
- 调度频率已由 cron 表达式表达，**prompt 中绝不要再写"每隔X分钟/每天/定时"等时间频率词**
- 不要写"持续生成/不停地..."等暗示循环的措辞
- 写法应像一次性命令，例如：
    ✅ 好："写一首诗，要求选一个国家作为灵感，注明国家名"
    ❌ 差："每隔1分钟写一首诗，每次换一个国家"  ← 把调度信息混进了任务内容
- 如果有"不要重复"等跨次约束，应明确说"参考最近的历史/上次"，不要用"每次/连续"
"""


def _cron(
    action: str,
    cron: str = "",
    title: str = "",
    prompt: str = "",
    job_id: str = "",
    disabled: bool | None = None,
    caller_channel: str = Injected("channel_id"),
) -> str:
    # 在 cron 触发的 session 中禁止再调用 cron 工具，避免无限套娃
    if caller_channel == "cron":
        return "[error] cron 触发的会话中禁止使用 cron 工具（避免循环创建/修改任务）"
    # subagent 不允许注册定时任务（避免子任务遗留副作用）
    if caller_channel == "subagent":
        return "[error] subagent 内不允许调用 cron 工具，请把任务做完即可，不要注册定时任务"

    if action == "create":
        if not cron or not title or not prompt:
            return "[error] create 需要 cron, title, prompt 三个参数"
        if not croniter.is_valid(cron):
            return f"[error] 无效的 cron 表达式: {cron}"

        new_id = f"job_{uuid.uuid4().hex[:10]}"
        now = time.time()
        save_job({
            "id": new_id,
            "cron": cron,
            "title": title,
            "prompt": prompt,
            "disabled": bool(disabled),
            "created_at": now,
            "run_history": [],
        })
        next_run = croniter(cron, now).get_next(ret_type=float)
        status = "已禁用，将不会触发" if disabled else (
            f"下次运行: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_run))}"
        )
        return f"已创建定时任务 {new_id}: {title}\n{status}"

    if action == "list":
        jobs = load_all_jobs()
        if not jobs:
            return "当前没有定时任务"
        lines = []
        for j in jobs:
            history = j.get("run_history") or []
            last_str = (
                time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(history[-1]))
                if history else "未运行"
            )
            status = "[已禁用]" if j.get("disabled") else "[启用]"
            lines.append(f"- {j['id']} | {status} | {j['cron']} | {j.get('title', '')}")
            lines.append(f"  prompt: {j.get('prompt', '')[:80]}")
            lines.append(f"  上次运行: {last_str} | 累计运行: {len(history)} 次")
        return "\n".join(lines)

    if action == "delete":
        if not job_id:
            return "[error] delete 需要 job_id"
        if delete_job(job_id):
            return f"已删除定时任务 {job_id}"
        return f"[error] 任务不存在: {job_id}"

    if action == "update":
        if not job_id:
            return "[error] update 需要 job_id"
        p = _job_path(job_id)
        if not p.exists():
            return f"[error] 任务不存在: {job_id}"
        job = json.loads(p.read_text(encoding="utf-8"))
        if cron:
            if not croniter.is_valid(cron):
                return f"[error] 无效的 cron 表达式: {cron}"
            job["cron"] = cron
        if title:
            job["title"] = title
        if prompt:
            job["prompt"] = prompt
        if disabled is not None:
            job["disabled"] = bool(disabled)
        save_job(job)
        return f"已更新 {job_id}"

    return f"[error] 未知 action: {action}（支持 create/list/delete/update）"


def create_cron_tool() -> Tool:
    return Tool(
        name="cron",
        description=CRON_TOOL_DESCRIPTION,
        parameters=[
            ToolParameter(name="action", type="string", description="操作：create/list/delete/update", required=True, enum=["create", "list", "delete", "update"]),
            ToolParameter(name="cron", type="string", description="cron 表达式（create/update 用）", required=False),
            ToolParameter(name="title", type="string", description="任务标题（create/update 用）", required=False),
            ToolParameter(name="prompt", type="string", description="到期触发的提示词（create/update 用）。这是每次单独触发时发给 agent 的一次性指令，不要包含频率/周期词（'每隔X分钟'、'每天'、'连续'等），频率由 cron 表达式表达", required=False),
            ToolParameter(name="job_id", type="string", description="任务 ID（delete/update 用）", required=False),
            ToolParameter(name="disabled", type="boolean", description="是否禁用任务（create/update 用）。true 时调度器跳过该任务，但任务定义和历史保留", required=False),
        ],
        func=_cron,
    )
