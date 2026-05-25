"""
API 路由
"""
import json
import logging
import os
import tempfile
import time
import uuid

from fastapi import APIRouter, HTTPException, Request

from ftre.session import SessionManager
from ftre.config import CONFIG_PATH
from ftre.tools.cron import (
    load_all_jobs,
    save_job,
    delete_job,
    _job_path,
)
from croniter import croniter

logger = logging.getLogger(__name__)

router = APIRouter()

# SessionManager 实例由外部注入（启动时设置）
_session_manager: SessionManager | None = None


def set_session_manager(manager: SessionManager) -> None:
    """注入 SessionManager 实例（启动时调用）"""
    global _session_manager
    _session_manager = manager


@router.post("/sessions")
async def create_session(channel_id: str, title: str = ""):
    """创建新 session，返回带 channel 前缀的 session_id（如 'ws::sess_xxx'）"""
    session_id = await _session_manager.create_session(channel_id=channel_id, title=title)
    return {"session_id": session_id}


@router.get("/sessions")
async def list_sessions(limit: int = 50, channel_id: str | None = None):
    """
    获取会话列表（按最近活跃排序）。
    可选 channel_id 过滤：仅返回指定 channel 的会话。
    """
    sessions = await _session_manager.list_sessions(
        limit=limit, channel_id=channel_id
    )
    return {"sessions": sessions}


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str):
    """获取指定 session 的全部消息（按时间正序）"""
    messages = await _session_manager.get_messages_by_session(session_id)
    return {"messages": messages}


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


@router.get("/health")
async def health():
    return {"status": "ok"}
