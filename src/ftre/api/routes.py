"""
API 路由
"""
from fastapi import APIRouter

from ftre.session import SessionManager

router = APIRouter()

# SessionManager 实例由外部注入（启动时设置）
_session_manager: SessionManager | None = None


def set_session_manager(manager: SessionManager) -> None:
    """注入 SessionManager 实例（启动时调用）"""
    global _session_manager
    _session_manager = manager


@router.get("/sessions")
async def list_sessions(limit: int = 50):
    """获取会话列表（按最近活跃排序）"""
    sessions = await _session_manager.list_sessions(limit=limit)
    return {"sessions": sessions}


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str):
    """获取指定 session 的全部消息（按时间正序）"""
    messages = await _session_manager.get_messages_by_session(session_id)
    return {"messages": messages}


@router.get("/health")
async def health():
    return {"status": "ok"}
