"""
plan 工具 — 创建和管理结构化执行计划。

工作流：
- 模型主动调用 plan(action="create") 创建计划，写入 session.metadata.plan
- 每完成一个步骤后调用 plan(action="update") 更新步骤状态
- 全部步骤完成后调用 plan(action="complete") 标记计划完成

ON_STOP hook 会检查 session.metadata.plan：
  - 无 plan → 放行
  - plan 中存在未完成步骤 → 阻止停止，插入 continuation prompt
  - 全部完成 → 删除 metadata.plan，放行

数据结构（存入 session.metadata.plan）：
{
  "goal": "创建一个博客",
  "raw": "模型传入的原始计划文本",
  "steps": [
    {"id": "1", "content": "设计博客结构\\n完成条件：页面结构图已确定，包含首页/文章页/归档页", "status": "completed"},
    {"id": "2", "content": "实现页面和路由\\n完成条件：所有页面可访问，路由跳转正常", "status": "in_progress"},
    {"id": "3", "content": "运行测试\\n完成条件：全部测试通过，无报错", "status": "pending"}
  ]
}
"""
import asyncio

from ftre_agent_core.tool import Tool, ToolParameter, Injected


def _run_async(coro, event_loop, timeout: float = 10.0):
    """跨线程执行 coroutine 并等结果"""
    return asyncio.run_coroutine_threadsafe(coro, event_loop).result(timeout=timeout)


def create_plan_tool() -> Tool:
    """创建 plan 工具"""

    def plan(
        action: str,
        goal: str = "",
        raw: str = "",
        steps: list = None,
        step_id: str = "",
        status: str = "",
        updates: list = None,
        session_id: str = Injected("session_id"),
        event_loop=Injected("event_loop"),
        session_manager=Injected("session_manager"),
    ) -> str:
        if not session_id or event_loop is None or session_manager is None:
            return "[error] runtime context 未注入完整"

        action = (action or "").strip()

        # ── create ──────────────────────────────────────────
        if action == "create":
            if not goal.strip():
                return "[error] action=create 时 goal 不能为空"
            if not steps:
                return "[error] action=create 时 steps 不能为空"

            normalized = _normalize_steps(steps)
            plan_data = {
                "goal": goal.strip(),
                "raw": raw.strip(),
                "steps": normalized,
            }
            _run_async(
                session_manager.update_session_metadata(
                    session_id, "plan", plan_data
                ),
                event_loop,
            )
            return _format_plan_response("已创建计划", plan_data)

        # ── update ──────────────────────────────────────────
        if action == "update":
            metadata = _run_async(
                session_manager.get_session_metadata(session_id),
                event_loop,
            )
            plan_data = metadata.get("plan")
            if not plan_data:
                return "[error] 当前 session 没有 plan，请先使用 action=create 创建"

            # 构建更新列表：updates 数组优先，否则用单个 step_id + status
            if updates:
                update_list = updates
            elif step_id.strip() and status.strip():
                update_list = [{"id": step_id.strip(), "status": status.strip()}]
            else:
                return (
                    "[error] action=update 需要 updates 数组或 step_id + status。\n"
                    "单个更新：step_id + status\n"
                    "批量更新：updates=[{\"id\": \"2\", \"status\": \"completed\"}, {\"id\": \"3\", \"status\": \"in_progress\"}]"
                )

            # 校验并执行更新
            valid_statuses = ("pending", "in_progress", "completed")
            updated_ids: list[str] = []
            not_found_ids: list[str] = []

            for u in update_list:
                if not isinstance(u, dict):
                    continue
                uid = str(u.get("id", "")).strip()
                ustatus = str(u.get("status", "")).strip()
                if not uid or not ustatus:
                    continue
                if ustatus not in valid_statuses:
                    return f"[error] status 必须是 {'/'.join(valid_statuses)} 之一，收到: {ustatus}"

                found = False
                for s in plan_data.get("steps", []):
                    if s.get("id") == uid:
                        s["status"] = ustatus
                        found = True
                        updated_ids.append(uid)
                        break
                if not found:
                    not_found_ids.append(uid)

            if not updated_ids:
                return (
                    f"[error] 未找到任何匹配的步骤。"
                    f"现有步骤: {[s.get('id') for s in plan_data.get('steps', [])]}"
                )

            _run_async(
                session_manager.update_session_metadata(
                    session_id, "plan", plan_data
                ),
                event_loop,
            )
            suffix = ""
            if not_found_ids:
                suffix = f"\n⚠ 未找到的步骤 id: {not_found_ids}"
            return _format_plan_response(f"已更新步骤 {updated_ids}", plan_data) + suffix

        # ── complete ────────────────────────────────────────
        if action == "complete":
            metadata = _run_async(
                session_manager.get_session_metadata(session_id),
                event_loop,
            )
            plan_data = metadata.get("plan")
            if not plan_data:
                return "[error] 当前 session 没有 plan"

            incomplete = [
                s
                for s in plan_data.get("steps", [])
                if s.get("status") != "completed"
            ]
            if incomplete:
                names = "、".join(
                    s.get("content", s.get("id", "?"))[:20] for s in incomplete
                )
                return (
                    f"[error] 计划尚未全部完成，还有 {len(incomplete)} 个步骤未完成：{names}。"
                    f"请先用 action=update 更新这些步骤状态为 completed，再调用 complete。"
                )

            _run_async(
                session_manager.update_session_metadata(
                    session_id, "plan", None
                ),
                event_loop,
            )
            return (
                "计划已全部完成，已清除 plan 数据。\n"
                "你现在可以正常结束当前对话了。"
            )

        # ── 未知 action ─────────────────────────────────────
        return f"[error] 未知 action: {action}，支持: create / update / complete"

    return Tool(
        name="plan",
        description=(
            "创建和管理结构化执行计划，确保长任务的完整生命周期被跟踪和执行。\n"
            "\n"
            "三个 action：\n"
            "1. create — 创建计划。传入 goal、steps（含 id/content/status）、raw（可选原文）\n"
            "2. update — 更新步骤状态。传入 step_id 和 status（pending/in_progress/completed）\n"
            "3. complete — 标记完成。所有步骤必须已是 completed，否则拒绝\n"
            "\n"
            "调用时机：任何阶段都可以调用。不限于任务开始前——执行了一段时间后发现任务比预期复杂、内容量大、步骤繁多时立即 create。用户需求模糊时也应 create，往往越模糊的需求事情越多。\n"
            "\n"
            "创建计划前必须用第一性原理分析用户真实需求：\n"
            "- 显性需求：用户直接说了什么\n"
            "- 隐性需求：用户没说但实际需要的（如 404 页面、错误处理、响应式布局——用户可能不知道自己需要这些，但没有它们产品就不完整）\n"
            "- 未知需求：用户自己都不知道但用了之后会发现重要的（如搜索性能、空结果处理）\n"
            "- 把这些需求整合到计划的 steps 中，而不是只做用户字面说的那些\n"
            "\n"
            "计划必须覆盖完整生命周期，NEVER 只规划核心实现：\n"
            "- 构思 → 设计 → 实现 → 验证 → 测试 → 清理\n"
            "- 验证步骤：构建通过、类型检查、核心流程跑通\n"
            "- 测试步骤：跑测试套件、边界情况、手动验证\n"
            "- 清理步骤：删临时文件、删调试代码、检查无残留\n"
            "\n"
            "规划原则：\n"
            "- 先建立全局理解，再决定局部改动。不要找到第一个相关文件就直接跳到计划\n"
            "- 贴合现有代码库，适配已有模式，不要另起一套并行模式\n"
            "- 引用应复用的函数和工具，标注文件路径\n"
            "- 计划中应包含验证方式，描述如何端到端测试\n"
            "\n"
            "防偷懒规则：\n"
            "- NEVER 从 pending 直接跳到 completed。必须 in_progress → 执行 → 验证 → completed\n"
            "- NEVER 一口气把多个步骤标 completed。每步独立执行、独立验证\n"
            "- NEVER 用空函数、TODO、placeholder 假装完成\n"
            "- NEVER 重复性任务做几个就声称全部完成。逐个执行，验证全部\n"
            "- NEVER 跳过失败的步骤。修复后重新验证\n"
            "- NEVER 跳过验证、测试、清理步骤直接 complete。这些不是可选项\n"
            "\n"
            "约束：\n"
            "- 计划激活期间不能随意结束对话（ON_STOP hook 会阻止）\n"
            "- 所有步骤 completed 后调 complete 才能正常结束\n"
            "- status 只能是 pending / in_progress / completed\n"
            "- 每个 step 的 content 中必须包含完成条件（可执行的验证 + 预期结果）"
        ),
        parameters=[
            ToolParameter(
                name="action",
                type="string",
                description="操作类型：create（创建计划）/ update（更新步骤状态）/ complete（标记计划完成）",
                required=True,
                enum=["create", "update", "complete"],
            ),
            ToolParameter(
                name="goal",
                type="string",
                description="计划目标（action=create 时必填）",
                required=False,
            ),
            ToolParameter(
                name="raw",
                type="string",
                description="计划的原始文本（action=create 时可选，用于保留原文）",
                required=False,
            ),
            ToolParameter(
                name="steps",
                type="array",
                description=(
                    "计划步骤列表（action=create 时必填）。每项含：\n"
                    "- id(string): 步骤唯一标识\n"
                    "- content(string): 步骤内容描述，必须包含完成条件（自然语言描述什么情况下算完成）\n"
                    "- status(string: pending/in_progress/completed)"
                ),
                required=False,
            ),
            ToolParameter(
                name="step_id",
                type="string",
                description="要更新的步骤 id（action=update 单个更新时使用，与 updates 二选一）",
                required=False,
            ),
            ToolParameter(
                name="status",
                type="string",
                description="步骤新状态（action=update 单个更新时使用）：pending / in_progress / completed",
                required=False,
                enum=["pending", "in_progress", "completed"],
            ),
            ToolParameter(
                name="updates",
                type="array",
                description=(
                    "批量更新步骤状态（action=update 时使用，与 step_id + status 二选一）。"
                    "每项含：id(string) + status(string: pending/in_progress/completed)。\n"
                    '示例：[{"id": "2", "status": "completed"}, {"id": "3", "status": "in_progress"}]'
                ),
                required=False,
            ),
        ],
        func=plan,
    )


def _normalize_steps(steps: list) -> list[dict]:
    """规范化步骤列表，确保每个步骤有 id/content/status。"""
    result = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id", i + 1))
        content = str(s.get("content", f"步骤 {sid}"))
        status = s.get("status", "pending")
        if status not in ("pending", "in_progress", "completed"):
            status = "pending"
        result.append({"id": sid, "content": content, "status": status})
    return result


def _format_plan_response(prefix: str, plan_data: dict) -> str:
    """格式化返回给模型的计划摘要。"""
    lines = [prefix]
    lines.append(f"目标：{plan_data.get('goal', '')}")
    lines.append("步骤：")
    for s in plan_data.get("steps", []):
        mark = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}.get(
            s.get("status", "pending"), "[ ]"
        )
        lines.append(f"  {mark} {s.get('id', '?')}. {s.get('content', '')}")
    return "\n".join(lines)
