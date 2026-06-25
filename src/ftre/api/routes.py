"""
API 路由
"""
import asyncio
import json
import logging
import os
import tempfile
import time
import uuid

from fastapi import APIRouter, HTTPException, Request

from ftre.agent.loop import AgentLoop
from ftre.session import SessionManager
from ftre.config import CONFIG_PATH
from ftre.tools.cron import (
    load_all_jobs,
    save_job,
    delete_job,
    _job_path,
)
from ftre.api import skill as skill_store
from ftre.trace_store import TRACE_DB_PATH, get_trace, get_trace_run, list_trace_summaries
from croniter import croniter

logger = logging.getLogger(__name__)

router = APIRouter()

# SessionManager 实例由外部注入（启动时设置）
_session_manager: SessionManager | None = None
# AgentLoop 实例由外部注入（启动时设置），用于查询 session 是否在跑
_agent_loop: AgentLoop | None = None
_command_manager = None


def set_session_manager(manager: SessionManager) -> None:
    """注入 SessionManager 实例（启动时调用）"""
    global _session_manager
    _session_manager = manager


def set_agent_loop(loop: AgentLoop) -> None:
    """注入 AgentLoop 实例（启动时调用）"""
    global _agent_loop
    _agent_loop = loop


def set_command_manager(cmd) -> None:
    """注入 CommandManager 实例（启动时调用）"""
    global _command_manager
    _command_manager = cmd


@router.get("/traces")
async def list_traces(limit: int = 100, offset: int = 0):
    """List recent Agent traces without returning full prompt/tool payloads."""
    page = await asyncio.to_thread(list_trace_summaries, limit=limit, offset=offset)
    return {**page, "path": str(TRACE_DB_PATH)}


@router.get("/traces/{trace_id}")
async def read_trace(trace_id: str):
    """Return a lightweight Run tree; large payloads are loaded separately."""
    trace = await asyncio.to_thread(get_trace, trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"Trace 不存在: {trace_id}")
    return trace


@router.get("/traces/{trace_id}/runs/{run_id}")
async def read_trace_run(trace_id: str, run_id: str):
    """Return full inputs, outputs, metadata and events for one Run."""
    run = await asyncio.to_thread(get_trace_run, trace_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run 不存在: {run_id}")
    return {"run": run}


@router.post("/sessions")
async def create_session(channel_id: str, title: str = "", workspace: str = ""):
    """创建新 session，返回带 channel 前缀的 session_id（如 'ws::sess_xxx'）。

    workspace 可选；不传时由 agent_loop 在首次执行时回退到 config.workspace。
    """
    session_id = await _session_manager.create_session(
        channel_id=channel_id, title=title, workspace=workspace
    )
    return {"session_id": session_id}


@router.get("/workspaces")
async def list_workspaces(channel_id: str | None = "ws"):
    """
    枚举所有工作区（按各自最新活跃时间倒序）。

    默认只统计 ws channel 下的工作区（前端侧边栏按工作区分组用）。
    传 channel_id="" 或省略过滤可统计全部。
    返回 { workspaces: [{ workspace, session_count, latest_at }] }
    """
    # channel_id 传空串时视为不过滤
    ch = channel_id or None
    workspaces = await _session_manager.list_workspaces(channel_id=ch)
    return {"workspaces": workspaces}


@router.get("/sessions")
async def list_sessions(
    limit: int = 50,
    offset: int = 0,
    channel_id: str | None = None,
    workspace: str | None = None,
):
    """
    获取会话列表（按最近活跃排序）。

    分页参数：limit（默认 50，最大 500）/ offset（默认 0）。
    过滤参数：
    - channel_id：仅返回该 channel
    - workspace：仅返回该 workspace（传空串 "" 表示"未设置工作区"的会话；
                 不传该参数则不按 workspace 过滤）
    返回 { sessions, total, limit, offset }，前端按 (offset + sessions.length) < total 决定是否还有下一页。
    """
    if limit <= 0:
        limit = 50
    if limit > 500:
        limit = 500
    if offset < 0:
        offset = 0
    sessions = await _session_manager.list_sessions(
        limit=limit, offset=offset, channel_id=channel_id, workspace=workspace
    )
    total = await _session_manager.count_sessions(
        channel_id=channel_id, workspace=workspace
    )
    # 标注每个 session 是否有正在执行的 ReActAgent（O(1) dict 查询）
    if _agent_loop is not None:
        for s in sessions:
            s["running"] = _agent_loop.is_session_running(s["id"])
    return {
        "sessions": sessions,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.put("/sessions/{session_id}")
async def update_session(session_id: str, request: Request):
    """更新 session 字段（title / workspace；任传一项即可）"""
    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"非法 JSON: {e}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")

    title = payload.get("title")
    if title is not None and not isinstance(title, str):
        raise HTTPException(status_code=400, detail="title 必须是字符串")

    workspace = payload.get("workspace")
    if workspace is not None and not isinstance(workspace, str):
        raise HTTPException(status_code=400, detail="workspace 必须是字符串")

    if title is None and workspace is None:
        raise HTTPException(status_code=400, detail="至少传入 title / workspace 之一")

    session = await _session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")

    await _session_manager.update_session(
        session_id, title=title, workspace=workspace
    )
    return {"status": "updated", "session_id": session_id}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """删除指定 session 及其所有消息"""
    session = await _session_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
    await _session_manager.delete_session(session_id)
    return {"status": "deleted", "session_id": session_id}


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str):
    """获取指定 session 的全部消息（按时间正序）"""
    messages = await _session_manager.get_messages_by_session(session_id)
    return {"messages": messages}


@router.get("/sessions/{session_id}/token_usage")
async def get_token_usage(session_id: str):
    """
    获取该 session 的 token 用量。

    返回字段：
    - anchor: 最近一次 LLM 实算的 usage（含 timestamp 和 source），无则 null
    - pending_estimated: 锚点之后会进下次 prompt 但尚未实算的事件的字符级粗估
    - total: anchor.total_tokens + pending_estimated（无锚点时即全量估算）
    """
    return await _session_manager.get_token_usage(session_id)


# ─────────────────────────────────────────────────────────────
# 应用配置（~/.ftre/config.json）
# ─────────────────────────────────────────────────────────────

@router.get("/config")
async def get_config():
    """读取 ~/.ftre/config.json 全文。文件不存在时返回空对象。"""
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"[config] 读取失败: {e}")
        raise HTTPException(status_code=500, detail=f"读取配置失败: {e}")


@router.put("/config")
async def put_config(request: Request):
    """覆盖写 ~/.ftre/config.json。原子写入（tmp + rename）。

    body 必须是 JSON 对象。
    """
    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"非法 JSON: {e}")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="config 必须是 JSON 对象")

    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 同目录写临时文件再 rename，保证写入原子性，避免半截内容
        fd, tmp_path = tempfile.mkstemp(
            prefix=".config.", suffix=".tmp", dir=str(CONFIG_PATH.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, CONFIG_PATH)
        except Exception:
            # 写失败时清理临时文件
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as e:
        logger.error(f"[config] 写入失败: {e}")
        raise HTTPException(status_code=500, detail=f"写入配置失败: {e}")

    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────
# Cron 任务（~/.ftre/cron/<job_id>.json）
# ─────────────────────────────────────────────────────────────
#
# 与 ftre.tools.cron.create_cron_tool 共用底层文件 IO，HTTP 接口在前端
# 提供 CRUD UI 用，行为语义与 cron 工具的 create/list/update/delete 对齐。

# 允许前端 PATCH 修改的字段（白名单）
_CRON_PATCH_FIELDS = {"cron", "title", "prompt", "disabled"}


def _validate_cron_payload(
    payload: dict, *, require_all: bool
) -> tuple[dict, str | None]:
    """
    校验创建/更新时的 payload。
    require_all=True 用于创建（cron/title/prompt 都必填，disabled 可选）；
    require_all=False 用于 PATCH（任填一项即可，但出现的字段必须合法）。
    返回 (cleaned_dict, error_message)；error_message 为 None 表示校验通过。
    """
    cleaned: dict = {}

    cron_expr = payload.get("cron")
    if cron_expr is not None:
        if not isinstance(cron_expr, str) or not cron_expr.strip():
            return {}, "cron 不能为空"
        if not croniter.is_valid(cron_expr.strip()):
            return {}, f"无效的 cron 表达式: {cron_expr}"
        cleaned["cron"] = cron_expr.strip()
    elif require_all:
        return {}, "缺少字段: cron"

    title = payload.get("title")
    if title is not None:
        if not isinstance(title, str) or not title.strip():
            return {}, "title 不能为空"
        cleaned["title"] = title.strip()
    elif require_all:
        return {}, "缺少字段: title"

    prompt = payload.get("prompt")
    if prompt is not None:
        if not isinstance(prompt, str) or not prompt.strip():
            return {}, "prompt 不能为空"
        cleaned["prompt"] = prompt.strip()
    elif require_all:
        return {}, "缺少字段: prompt"

    if "disabled" in payload:
        v = payload["disabled"]
        if not isinstance(v, bool):
            return {}, "disabled 必须是布尔值"
        cleaned["disabled"] = v

    if not require_all and not cleaned:
        return {}, "至少需要更新 cron / title / prompt / disabled 中的一项"

    return cleaned, None


@router.get("/cron")
async def list_cron_jobs():
    """列出所有 cron 任务（按 created_at 倒序，新建的在前）"""
    jobs = load_all_jobs()
    jobs.sort(key=lambda j: j.get("created_at", 0.0), reverse=True)
    return {"jobs": jobs}


@router.get("/cron/{job_id}")
async def get_cron_job(job_id: str):
    """获取单个 cron 任务"""
    p = _job_path(job_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {e}")


@router.post("/cron", status_code=201)
async def create_cron_job(request: Request):
    """创建 cron 任务

    body: {"cron": "...", "title": "...", "prompt": "..."}
    """
    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"非法 JSON: {e}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")

    cleaned, err = _validate_cron_payload(payload, require_all=True)
    if err:
        raise HTTPException(status_code=400, detail=err)

    job = {
        "id": f"job_{uuid.uuid4().hex[:10]}",
        "cron": cleaned["cron"],
        "title": cleaned["title"],
        "prompt": cleaned["prompt"],
        "disabled": bool(cleaned.get("disabled", False)),
        "created_at": time.time(),
        "run_history": [],
    }
    try:
        save_job(job)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {e}")
    return job


@router.patch("/cron/{job_id}")
async def update_cron_job(job_id: str, request: Request):
    """局部更新 cron 任务（仅 cron / title / prompt）

    出于安全考虑，created_at / run_history 等内部字段不允许通过 API 修改。
    """
    p = _job_path(job_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")

    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"非法 JSON: {e}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")

    # 拒绝试图改内部字段
    illegal = set(payload.keys()) - _CRON_PATCH_FIELDS
    if illegal:
        raise HTTPException(
            status_code=400, detail=f"不允许修改字段: {sorted(illegal)}"
        )

    cleaned, err = _validate_cron_payload(payload, require_all=False)
    if err:
        raise HTTPException(status_code=400, detail=err)

    try:
        job = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {e}")

    job.update(cleaned)
    try:
        save_job(job)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {e}")
    return job


@router.delete("/cron/{job_id}", status_code=204)
async def remove_cron_job(job_id: str):
    """删除 cron 任务"""
    if not delete_job(job_id):
        raise HTTPException(status_code=404, detail=f"任务不存在: {job_id}")
    return None


# ─────────────────────────────────────────────────────────────
# Skill（~/.ftre/skills/<name>.md 或 <name>/SKILL.md）
# ─────────────────────────────────────────────────────────────
#
# 与 ~/.ftre/plugins/skill_plugin.py 的加载约定一致：Skill 是可复用的本地能力
# 说明，插件会把它们的描述注入 system_prompt，并提供 loadSkill 工具按需读取。
# 这里为前端提供管理 UI 所需的 CRUD 接口（底层 IO 见 ftre.skill）。


@router.get("/skills")
async def list_skills():
    """列出所有 Skill 元信息（不含正文），按名称排序。

    返回 { skills: [{ name, description, kind, updated_at }] }
    """
    return {"skills": skill_store.list_skills()}


@router.get("/skills/{name}")
async def get_skill(name: str):
    """读取单个 Skill 的完整信息（含正文 content）。"""
    if not skill_store.is_valid_name(name):
        raise HTTPException(status_code=400, detail=f"非法的 Skill 名称: {name}")
    try:
        skill = skill_store.read_skill(name)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {e}")
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill 不存在: {name}")
    return skill


@router.post("/skills", status_code=201)
async def create_skill(request: Request):
    """创建 Skill。

    body: {"name": "...", "content": "...", "kind": "dir" | "file"}
    - content 可选；缺省时用模板（含 frontmatter）预填
    - kind 可选，默认 "dir"（写入 <name>/SKILL.md）
    """
    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"非法 JSON: {e}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")

    name = payload.get("name")
    if not isinstance(name, str) or not skill_store.is_valid_name(name):
        raise HTTPException(status_code=400, detail=f"非法的 Skill 名称: {name!r}")
    name = name.strip()

    kind = payload.get("kind", "dir")
    if kind not in ("dir", "file"):
        raise HTTPException(status_code=400, detail="kind 仅支持 'dir' / 'file'")

    content = payload.get("content")
    if content is not None and not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content 必须是字符串")
    if not content:
        description = payload.get("description")
        if not isinstance(description, str):
            description = ""
        content = skill_store.SKILL_TEMPLATE.format(
            name=name, description=description.strip()
        )

    try:
        skill = skill_store.create_skill(name, content, kind=kind)
    except FileExistsError:
        raise HTTPException(status_code=409, detail=f"Skill 已存在: {name}")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"创建失败: {e}")
    return skill


@router.put("/skills/{name}")
async def update_skill(name: str, request: Request):
    """覆盖写 Skill 正文。

    body: {"content": "..."}
    """
    if not skill_store.is_valid_name(name):
        raise HTTPException(status_code=400, detail=f"非法的 Skill 名称: {name}")

    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"非法 JSON: {e}")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")

    content = payload.get("content")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content 必须是字符串")

    try:
        skill = skill_store.update_skill(name, content)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"保存失败: {e}")
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill 不存在: {name}")
    return skill


@router.delete("/skills/{name}", status_code=204)
async def remove_skill(name: str):
    """删除 Skill（目录形态会连同 references/scripts 一并删除）。"""
    if not skill_store.is_valid_name(name):
        raise HTTPException(status_code=400, detail=f"非法的 Skill 名称: {name}")
    try:
        ok = skill_store.delete_skill(name)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {e}")
    if not ok:
        raise HTTPException(status_code=404, detail=f"Skill 不存在: {name}")
    return None


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/commands")
async def list_commands():
    """返回已注册的斜杠指令列表，供前端命令面板渲染。"""
    if _command_manager is None:
        return {"commands": []}
    return {"commands": _command_manager.list_commands()}
