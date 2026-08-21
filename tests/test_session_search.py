"""E1 会话内容搜索——纯函数 search_sessions 的功能与性能基准测试。"""
from __future__ import annotations

import time

import pytest
from ftre_agent_core.message import Msg

from ftre.services.session.entity.state import AgentStateFile, SessionState
from ftre.services.session.search import MAX_HITS_PER_SESSION, search_sessions


def _msg(mid: str, role: str, content, created: str = "2026-08-17T00:00:00+08:00") -> Msg:
    """content 接受 str（自动包成 text part）或已构造好的 part list。"""
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    return Msg(id=mid, role=role, name="default", content=content, created_at=created)


def _state(
    sid: str,
    title: str = "",
    workspace: str = "",
    updated: str = "2026-08-17T00:00:00+08:00",
    messages=(),
) -> AgentStateFile:
    return AgentStateFile(
        session=SessionState(
            id=sid,
            channel_id="ws",
            title=title,
            workspace=workspace,
            created_at=updated,
            updated_at=updated,
        ),
        messages=list(messages),
    )


def test_empty_query_returns_empty():
    out = search_sessions([("s1", _state("s1", messages=[_msg("m1", "user", "任意")]))], "  ")
    assert out["total"] == 0
    assert out["results"] == []


def test_chinese_substring_any_length():
    """AC1：1 字 / 2 字 / ≥3 字中文子串均可命中正文。"""
    states = [
        ("s1", _state("s1", messages=[_msg("m1", "user", "这是一个会话搜索性能测试消息")])),
    ]
    assert search_sessions(states, "会")["total"] == 1
    assert search_sessions(states, "会话")["total"] == 1
    assert search_sessions(states, "会话搜索")["total"] == 1
    assert search_sessions(states, "搜索性能测试消息")["total"] == 1
    assert search_sessions(states, "不存在的词")["total"] == 0


def test_title_match_ranks_first():
    """标题命中排前，正文命中的老会话排后。"""
    states = [
        ("s_old_body", _state("s_old_body", messages=[_msg("m1", "user", "包含部署关键字的正文")], updated="2026-08-15T00:00:00+08:00")),
        ("s_new_title", _state("s_new_title", title="部署手册", updated="2026-08-16T00:00:00+08:00")),
    ]
    out = search_sessions(states, "部署")
    assert out["total"] == 2
    assert out["results"][0]["session_id"] == "s_new_title"
    assert out["results"][0]["title_matched"] is True
    assert out["results"][1]["title_matched"] is False


def test_workspace_filter_and_limit():
    """AC2：workspace 精确过滤 + limit 截断且 total 为过滤后总数。"""
    states = [
        ("s1", _state("s1", workspace="E:/a", title="目标")),
        ("s2", _state("s2", workspace="E:/b", title="目标")),
        ("s3", _state("s3", workspace="E:/a", title="目标")),
    ]
    out = search_sessions(states, "目标", workspace="E:/a")
    assert {r["session_id"] for r in out["results"]} == {"s1", "s3"}
    assert out["total"] == 2

    limited = search_sessions(states, "目标", limit=1)
    assert len(limited["results"]) == 1
    assert limited["total"] == 3


def test_hits_recent_first_and_capped():
    """命中摘要按消息序倒序（最近优先），每会话封顶 MAX_HITS_PER_SESSION 条。"""
    msgs = [_msg(f"m{i}", "user", f"第{i}条包含锚点的消息") for i in range(10)]
    states = [("s1", _state("s1", messages=msgs))]
    out = search_sessions(states, "锚点")
    hits = out["results"][0]["hits"]
    assert len(hits) == MAX_HITS_PER_SESSION
    # 最近的 m9 排第一
    assert hits[0]["mid"] == "m9"


def test_snippet_contains_hit_and_is_bounded():
    """AC3：摘要包含命中位置，长度受限。"""
    long_text = "前缀填充" * 100 + "这里是要找的关键字" + "后缀填充" * 100
    states = [("s1", _state("s1", messages=[_msg("m1", "user", long_text)]))]
    out = search_sessions(states, "关键字")
    snippet = out["results"][0]["hits"][0]["snippet"]
    assert "关键字" in snippet
    assert len(snippet) < 300  # 2×80 半径 + 省略号，远小于原文


def test_multimodal_content_searchable():
    """多 text part 拼接后可检索（get_text_content 语义）。"""
    content = [
        {"type": "text", "text": "图片里出现了"},
        {"type": "text", "text": "目标物体"},
    ]
    states = [("s1", _state("s1", messages=[_msg("m1", "user", content)]))]
    assert search_sessions(states, "目标物体")["total"] == 1


def test_reasoning_and_tool_content_searchable():
    """聊天界面可见的推理、工具参数和工具输出都参与检索。"""
    message = _msg("m1", "assistant", [
        {"type": "thinking", "thinking": "推理专有锚点"},
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "bash",
            "arguments": {"command": "rg 工具参数锚点"},
        },
        {
            "type": "tool_result",
            "id": "call-1",
            "name": "bash",
            "output": "工具输出专有锚点",
        },
    ])
    states = [("s1", _state("s1", messages=[message]))]

    assert search_sessions(states, "推理专有锚点")["total"] == 1
    assert search_sessions(states, "工具参数锚点")["total"] == 1
    assert search_sessions(states, "工具输出专有锚点")["total"] == 1


def test_offset_pagination_does_not_silently_drop_older_matches():
    states = [
        (f"s{i}", _state(f"s{i}", messages=[_msg(f"m{i}", "user", "共同关键字")]))
        for i in range(5)
    ]

    first = search_sessions(states, "共同关键字", limit=2)
    second = search_sessions(states, "共同关键字", limit=2, offset=2)
    third = search_sessions(states, "共同关键字", limit=2, offset=4)

    assert first["total"] == 5
    assert first["has_more"] is True
    assert [result["session_id"] for result in first["results"]] == ["s0", "s1"]
    assert [result["session_id"] for result in second["results"]] == ["s2", "s3"]
    assert second["has_more"] is True
    assert [result["session_id"] for result in third["results"]] == ["s4"]
    assert third["has_more"] is False


def test_non_indexable_roles_skipped():
    """system 角色不参与正文检索。"""
    states = [("s1", _state("s1", messages=[_msg("m1", "system", "系统提示里的关键字")]))]
    assert search_sessions(states, "关键字")["total"] == 0


def test_case_insensitive_ascii():
    assert search_sessions(
        [("s1", _state("s1", messages=[_msg("m1", "user", "Deploy the Gateway")]))], "deploy"
    )["total"] == 1


@pytest.mark.asyncio
async def test_manager_search_via_thread():
    """SessionManager 门面可用（线程池执行路径）。"""
    from ftre.services.session.service import SessionService as SessionManager

    mgr = SessionManager(sessions_dir="unused-dir-for-test")
    # 直接注入内存态，绕过磁盘加载
    mgr._repo._states["s1"] = _state("s1", title="检索目标", messages=[_msg("m1", "user", "正文命中检索")])
    out = await mgr.search_sessions("检索")
    assert out["total"] == 1
    assert out["results"][0]["title_matched"] is True


def test_performance_benchmark():
    """AC4：性能基准。

    - 真实个人数据上限量级（~50MB）：单次检索 < 600ms；
    - 极端压力量级（~200MB，个人用户多年也难达到）：< 2.5s 兜底（防退化为
      Python 逐字符循环；此时为 C 层子串扫描吞吐上限 ~140MB/s）。
    检索在 to_thread 工作线程执行，任何量级都不阻塞事件循环/UI。
    """
    filler = "这是一段用于填充搜索基准测试的普通中文文本，长度大约一千个字符左右。" * 32  # ~1.1KB
    assert len(filler) > 1000

    def build(n_sessions: int):
        states = []
        for i in range(n_sessions):
            msgs = []
            for j in range(400):
                text = filler
                if i == n_sessions - 1 and j == 399:
                    text = filler + "唯一的独特锚点词组xyz"
                msgs.append(_msg(f"m{i}_{j}", "user", text))
            states.append((f"s{i}", _state(f"s{i}", messages=msgs)))
        return states

    # 真实规模：125 会话 × 400 条 × 1.1KB ≈ 55MB
    real = build(125)
    t0 = time.monotonic()
    out = search_sessions(real, "独特锚点词组xyz")
    real_elapsed = time.monotonic() - t0
    assert out["total"] == 1
    assert real_elapsed < 0.6, f"real-scale search took {real_elapsed:.2f}s (~55MB)"

    # 极端规模：500 会话 ≈ 220MB（最坏情况：目标在最后一会话，全量扫描）
    stress = build(500)
    t0 = time.monotonic()
    out = search_sessions(stress, "独特锚点词组xyz")
    stress_elapsed = time.monotonic() - t0
    assert out["total"] == 1
    assert out["results"][0]["session_id"] == "s499"
    assert stress_elapsed < 2.5, f"stress search took {stress_elapsed:.2f}s (~220MB)"
