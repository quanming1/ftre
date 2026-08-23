"""cron Agent Tool contributed by the Schedule Feature."""
# Cron Agent Tool：把模型参数转换为 ScheduleService 调用，工具层不接触 Store 文件格式。
# 关键约束：cron/subagent 内部触发的会话禁止使用本工具（防循环创建/修改任务）。

from __future__ import annotations

import time
import uuid
from typing import Any

from croniter import croniter
from ftre_agent_core.tool import Injected, Tool, ToolParameter

from .service import ScheduleService

# 工具说明（注入到模型上下文）：说明四个 action 的用法、cron 表达式语法和 prompt 语义
CRON_TOOL_DESCRIPTION = """\
管理定时任务。通过 action 参数分发不同操作：
- action=\"create\"  创建任务，必填: cron, title, prompt；可选: disabled
- action=\"list\"    列出所有任务
- action=\"delete\"  删除任务，必填: job_id
- action=\"update\"  更新任务字段，必填: job_id；可选: cron, title, prompt, disabled（任填一项）
cron 表达式（5 段：分 时 日 月 周）
  例：'*/5 * * * *' 每5分钟；'0 9 * * *' 每天9点；'0 */1 * * *' 每小时整点
任务到期会触发 agent 在独立 cron session 中执行 prompt。
disabled=true 时调度器会跳过该任务（保留任务定义和历史，可随时启用）。
⚠️ 关于 prompt 字段（重要！避免误解）：
- prompt 是**每次到期单独触发**时发给 agent 的指令，描述“这一次要做的事”
- 调度频率已由 cron 表达式表达，prompt 中不要再写“每隔X分钟/每天/定时”等频率词
- 写法应像一次性命令，例如：“写一首诗，要求选一个国家作为灵感，注明国家名”
"""


def build_cron_tool(schedule: ScheduleService) -> Tool:
    """Build a Tool bound to one ScheduleService instance."""
    # caller_channel 由 AgentLoop 注入：用于识别触发来源，阻止内部会话滥用 cron
    def cron(
        action: str,
        cron: str = "",
        title: str = "",
        prompt: str = "",
        job_id: str = "",
        disabled: bool | None = None,
        caller_channel: str = Injected("channel_id"),
    ) -> str:
        # Internal scheduled/subagent runs cannot create persistent side effects.
        # cron 触发的会话：禁止再用 cron 工具（否则每个任务可以无限派生出新任务）
        if caller_channel == "cron":
            return "[error] cron 触发的会话中禁止使用 cron 工具（避免循环创建/修改任务）"
        # subagent：只允许完成当前任务，不允许注册定时任务
        if caller_channel == "subagent":
            return "[error] subagent 内不允许调用 cron 工具，请把任务做完即可，不要注册定时任务"
        # ── create：创建任务 ──
        if action == "create":
            if not cron or not title or not prompt:
                return "[error] create 需要 cron, title, prompt 三个参数"
            if not croniter.is_valid(cron):
                return f"[error] 无效的 cron 表达式: {cron}"
            try:
                job = schedule.create(
                    {
                        "id": f"job_{uuid.uuid4().hex[:10]}",
                        "cron": cron,
                        "title": title,
                        "prompt": prompt,
                        "disabled": bool(disabled),
                    }
                )
            except (ValueError, RuntimeError) as exc:
                return f"[error] 创建失败: {exc}"
            # 计算下一次运行时间展示给模型
            next_run = croniter(job["cron"], job["created_at"]).get_next(ret_type=float)
            status = "已禁用，将不会触发" if job["disabled"] else (
                f"下次运行: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(next_run))}"
            )
            return f"已创建定时任务 {job['id']}: {job['title']}\n{status}"
        # ── list：列出任务 ──
        if action == "list":
            jobs = schedule.list()
            if not jobs:
                return "<FTRE_SYSTEM_FACT>当前没有定时任务</FTRE_SYSTEM_FACT>"
            lines = ["<FTRE_SYSTEM_FACT>"]
            for job in jobs:
                history = job.get("run_history") or []
                last = (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(history[-1]))
                    if history else "未运行"
                )
                status = "[已禁用]" if job.get("disabled") else "[启用]"
                lines.append(
                    f"- {job['id']} | {status} | {job.get('cron', '')} | {job.get('title', '')}"
                )
                lines.append(f"  prompt: {job.get('prompt', '')[:80]}")
                lines.append(f"  上次运行: {last} | 累计运行: {len(history)} 次")
            lines.append("</FTRE_SYSTEM_FACT>")
            return "\n".join(lines)
        # ── delete：删除任务 ──
        if action == "delete":
            if not job_id:
                return "[error] delete 需要 job_id"
            try:
                deleted = schedule.delete(job_id)
            except (ValueError, RuntimeError) as exc:
                return f"[error] 删除失败: {exc}"
            return f"已删除定时任务 {job_id}" if deleted else f"[error] 任务不存在: {job_id}"
        # ── update：更新任务字段（增量 patch）──
        if action == "update":
            if not job_id:
                return "[error] update 需要 job_id"
            patch: dict[str, Any] = {}
            if cron:
                if not croniter.is_valid(cron):
                    return f"[error] 无效的 cron 表达式: {cron}"
                patch["cron"] = cron
            if title:
                patch["title"] = title
            if prompt:
                patch["prompt"] = prompt
            if disabled is not None:
                patch["disabled"] = bool(disabled)
            if not patch:
                return "[error] update 至少需要 cron, title, prompt, disabled 中的一项"
            try:
                schedule.update(job_id, patch)
            except KeyError:
                return f"[error] 任务不存在: {job_id}"
            except (ValueError, RuntimeError) as exc:
                return f"[error] 更新失败: {exc}"
            return f"已更新 {job_id}"
        return f"[error] 未知 action: {action}（支持 create/list/delete/update）"

    return Tool(
        name="cron",
        description=CRON_TOOL_DESCRIPTION,
        parameters=[
            ToolParameter(
                name="action",
                type="string",
                description="操作：create/list/delete/update",
                required=True,
                enum=["create", "list", "delete", "update"],
            ),
            ToolParameter(name="cron", type="string", description="cron 表达式（create/update 用）", required=False),
            ToolParameter(name="title", type="string", description="任务标题（create/update 用）", required=False),
            ToolParameter(name="prompt", type="string", description="到期触发的一次性提示词", required=False),
            ToolParameter(name="job_id", type="string", description="任务 ID（delete/update 用）", required=False),
            ToolParameter(name="disabled", type="boolean", description="是否禁用任务（create/update 用）", required=False),
        ],
        func=cron,
    )
