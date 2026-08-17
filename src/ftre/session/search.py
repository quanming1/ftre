"""会话内容搜索——内存态纯函数检索。

前提：网关启动时 ``JsonStateStore.load_all()`` 已把全部 state.json 加载进内存，
运行期读写均在内存。因此搜索直接遍历内存快照（``list[(sid, AgentStateFile)]``），
绝不读盘 / 解析 JSON——这就是性能保证的全部来源，无需任何旁路索引。

调用方（SessionManager.search_sessions）负责用 ``asyncio.to_thread`` 把本模块
的同步函数移出事件循环。
"""
from __future__ import annotations

from typing import Any

from ftre.session.entity.state import AgentStateFile

# 每会话返回的命中摘要条数（最近的优先）
MAX_HITS_PER_SESSION = 3
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


def _single_text(msg) -> str | None:
    """单 text block 的快路径（最常见形态），避免 list+join 分配。"""
    blocks = msg.content
    if len(blocks) == 1:
        b = blocks[0]
        if b.type == "text":
            return b.text
    return None


def search_sessions(
    states: list[tuple[str, AgentStateFile]],
    q: str,
    limit: int = 30,
    workspace: str | None = None,
) -> dict[str, Any]:
    """在内存快照上按子串检索会话（标题 + user/assistant 正文）。

    - 大小写不敏感（ASCII lower；中文不受影响）；
    - 标题命中排前，组内按 session.updated_at 倒序（两次稳定排序）；
    - 每会话最多 MAX_HITS_PER_SESSION 条摘要（消息序倒序取最近）；
    - workspace 传值时精确过滤（空串匹配"未设置工作区"）。
    """
    q = q.strip()
    if not q:
        return {"query": q, "total": 0, "results": []}
    q_lower = q.lower()
    # q 含 ASCII 字母时才需要大小写折叠（文本侧可能有大小写差异）；
    # 纯中文/数字/标点查询跳过每条消息的 lower() 分配（200MB 基准下省 ~40% 耗时）
    fold_case = any(c.isascii() and c.isalpha() for c in q)

    results: list[dict[str, Any]] = []
    for sid, state in states:
        ws = state.session.workspace or ""
        if workspace is not None and ws != workspace:
            continue
        title = state.session.title or ""
        title_matched = q_lower in (title.lower() if fold_case else title)

        hits: list[dict[str, Any]] = []
        # 倒序扫，先命中最近的；凑满即可提前结束该会话的正文扫描
        for msg in reversed(state.messages):
            if len(hits) >= MAX_HITS_PER_SESSION:
                break
            if msg.role != "user" and msg.role != "assistant":
                continue
            text = _single_text(msg)
            if text is None:
                text = msg.get_text_content() or ""
                if not text:
                    continue
            if fold_case:
                if q_lower not in text.lower():
                    continue
            elif q not in text:
                continue
            hits.append({"mid": msg.id, "role": msg.role, "snippet": _snippet(text, q_lower)})

        if not title_matched and not hits:
            continue
        results.append(
            {
                "session_id": sid,
                "title": title,
                "workspace": ws,
                "channel": state.session.channel_id,
                "updated_at": state.session.updated_at,
                "title_matched": title_matched,
                "hits": hits,
            }
        )

    results.sort(key=lambda r: r["updated_at"], reverse=True)
    results.sort(key=lambda r: r["title_matched"], reverse=True)
    total = len(results)
    return {"query": q, "total": total, "results": results[: max(1, min(limit, 100))]}
