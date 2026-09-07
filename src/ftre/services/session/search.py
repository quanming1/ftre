"""会话元信息搜索——内存态纯函数检索（PRD-F43 检索面收缩）。

消息事实位于 per-session session.json Snapshot；会话列表检索
只扫元信息——title + last_user_text（最后一条真实用户消息的反规范化预览，
由 SessionService 在 user/message 事件后维护）。正文级检索需要派生全量
消息，留待后续阶段（索引层）再引入。

前提：网关启动时 ``JsonStateStore.load_all()`` 已把全部 session.json 加载进
内存，运行期读写均在内存。因此搜索直接遍历内存快照
（``list[(sid, SessionMetaFile)]``），绝不读盘 / 解析 JSON——这就是性能
保证的全部来源，无需任何旁路索引。

调用方（SessionService.search_sessions）负责用 ``asyncio.to_thread`` 把本模块
的同步函数移出事件循环。
"""
from __future__ import annotations

from typing import Any

from ftre.services.session.entity.state import SessionMetaFile

# 摘要半径：命中位置前后各取的字符数
SNIPPET_RADIUS = 80


def _snippet(text: str, q_lower: str) -> str:
    """命中位置前后各 SNIPPET_RADIUS 字符；未定位到则取开头。"""
    idx = text.lower().find(q_lower) if q_lower else -1
    if idx < 0:
        head = text[: SNIPPET_RADIUS * 2]
        return head + ("…" if len(text) > len(head) else "")
    start = max(0, idx - SNIPPET_RADIUS)
    end = min(len(text), idx + len(q_lower) + SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def search_sessions(
    states: list[tuple[str, SessionMetaFile]],
    q: str,
    limit: int = 30,
    workspace: str | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """在内存快照上按子串检索会话（title + last_user_text 元信息）。

    - 大小写不敏感（ASCII lower；中文不受影响）；
    - 标题命中排前，组内按 session.updated_at 倒序（两次稳定排序）；
    - last_user_text 命中产出恰好一条 role=user 摘要（桌面端 hits 形状
      兼容：mid 为空串、role="user"、snippet 定位命中位置）；
    - workspace 传值时精确过滤（空串匹配"未设置工作区"）；
    - limit / offset 分页，避免常用词命中超过首屏上限时静默漏会话。
    """
    q = q.strip()
    if not q:
        return {"query": q, "total": 0, "results": []}
    q_lower = q.lower()
    # q 含 ASCII 字母时才需要大小写折叠（文本侧可能有大小写差异）；
    # 纯中文/数字/标点查询跳过每条预览的 lower() 分配
    fold_case = any(c.isascii() and c.isalpha() for c in q)

    results: list[dict[str, Any]] = []
    for sid, state in states:
        session = state.session
        ws = session.workspace or ""
        if workspace is not None and ws != workspace:
            continue
        title = session.title or ""
        title_matched = q_lower in (title.lower() if fold_case else title)

        # 检索面：last_user_text 反规范化预览（用户输入），最多 1 条命中
        preview = session.last_user_text or ""
        preview_matched = bool(
            preview and q_lower in (preview.lower() if fold_case else preview)
        )

        if not title_matched and not preview_matched:
            continue
        hits = (
            [{"mid": "", "role": "user", "snippet": _snippet(preview, q_lower)}]
            if preview_matched
            else []
        )
        results.append(
            {
                "session_id": sid,
                "title": title,
                "workspace": ws,
                "channel": state.session.channel_id,
                "updated_at": session.updated_at,
                "title_matched": title_matched,
                "hits": hits,
            }
        )

    results.sort(
        key=lambda r: (r["title_matched"], r["updated_at"]),
        reverse=True,
    )
    total = len(results)
    page_limit = max(1, min(limit, 100))
    page_offset = max(0, offset)
    page = results[page_offset: page_offset + page_limit]
    return {
        "query": q,
        "total": total,
        "limit": page_limit,
        "offset": page_offset,
        "has_more": page_offset + len(page) < total,
        "results": page,
    }
