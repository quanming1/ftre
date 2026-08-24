"""HTTP routes owned by the Session Service.

Handlers capture the Session and Agent Service instances supplied by
Composition.  No module-level setter or aggregate API router is involved.
"""
# 中文说明：Session HTTP 路由：通过 SessionService/AgentService 查询和修改会话，不直接触碰 state.json。

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request


def build_router(sessions, agents, inbox) -> APIRouter:
    """Build the session HTTP surface from public Service handles."""
    router = APIRouter()

    @router.post("/sessions")
    async def create_session(channel_id: str, title: str = "", workspace: str = ""):
        session_id = await sessions.create_session(channel_id, title, workspace)
        return {"session_id": session_id}

    @router.get("/workspaces")
    async def list_workspaces(channel_id: str | None = "ws"):
        return {"workspaces": await sessions.list_workspaces(channel_id=channel_id or None)}

    @router.get("/sessions")
    async def list_sessions(
        limit: int = 50,
        offset: int = 0,
        channel_id: str | None = None,
        workspace: str | None = None,
    ):
        limit = min(max(limit, 1), 500)
        offset = max(offset, 0)
        items = await sessions.list_sessions(limit, offset, channel_id, workspace)
        total = await sessions.count_sessions(channel_id=channel_id, workspace=workspace)
        agent_service = agents
        for item in items:
            item["running"] = agent_service.is_session_busy(item["id"])
            item["activity"] = agent_service.get_session_status(item["id"])
        return {"sessions": items, "total": total, "limit": limit, "offset": offset}

    @router.get("/sessions/search")
    async def search_sessions(
        q: str = "",
        limit: int = 30,
        workspace: str | None = None,
        offset: int = 0,
    ):
        return await sessions.search_sessions(q, limit, workspace or None, offset)

    @router.put("/sessions/{session_id}")
    async def update_session(session_id: str, request: Request):
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"非法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")
        title = payload.get("title")
        workspace = payload.get("workspace")
        if title is not None and not isinstance(title, str):
            raise HTTPException(status_code=400, detail="title 必须是字符串")
        if workspace is not None and not isinstance(workspace, str):
            raise HTTPException(status_code=400, detail="workspace 必须是字符串")
        if title is None and workspace is None:
            raise HTTPException(status_code=400, detail="至少传入 title / workspace 之一")
        if await sessions.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
        await sessions.update_session(session_id, title=title, workspace=workspace)
        return {"status": "updated", "session_id": session_id}

    @router.delete("/sessions/{session_id}")
    async def delete_session(session_id: str):
        if await sessions.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
        await agents.delete_session(session_id)
        return {"status": "deleted", "session_id": session_id}

    @router.post("/sessions/{session_id}/fork")
    async def fork_session(session_id: str):
        if await sessions.get_session(session_id) is None:
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
        result = await sessions.fork_session(session_id)
        return {"fork_session_id": result.fork_session_id, "title": result.title, "workspace": result.workspace}

    @router.get("/sessions/{session_id}/messages")
    async def get_messages(
        session_id: str,
        limit_turns: int | None = None,
        before_ts: float | None = None,
    ):
        agent_service = agents
        inbox_service = inbox
        status = agent_service.get_session_status(session_id)
        queue = await inbox_service.wire_snapshot(session_id) if inbox_service is not None else None
        session = await sessions.get_session(session_id)
        metadata = session["metadata"] if session else {}
        if limit_turns is not None and limit_turns > 0:
            messages, has_more = await sessions.get_recent_messages_by_turns(session_id, limit_turns, before_ts=before_ts)
            return {"messages": messages, "has_more": has_more, "status": status, "queue": queue, "metadata": metadata}
        return {"messages": await sessions.get_messages_by_session(session_id), "status": status, "queue": queue, "metadata": metadata}

    @router.get("/sessions/{session_id}/state")
    async def get_session_state(
        session_id: str,
        offset: int | None = None,
        limit: int = 50,
        max_string_chars: int = 20_000,
    ):
        page = await sessions.get_state_page(session_id, offset=offset, limit=limit, max_string_chars=max_string_chars)
        if page is None:
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
        return page

    @router.get("/sessions/{session_id}/state/messages/{message_id}")
    async def get_session_state_message(session_id: str, message_id: str):
        message = await sessions.get_state_message(session_id, message_id)
        if message is None:
            raise HTTPException(status_code=404, detail=f"消息不存在: {message_id}")
        return message

    @router.get("/sessions/{session_id}/token_usage")
    async def get_token_usage(session_id: str):
        return await sessions.get_token_usage(session_id)

    return router
