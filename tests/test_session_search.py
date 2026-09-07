"""E1 会话搜索——纯函数 search_sessions 的功能与性能基准测试。

检索面是会话元信息：title + last_user_text（PRD-F43）。每会话至多 1 条
last_user_text 命中；正文级检索需要派生全量事件日志，属后续索引阶段。
"""
from __future__ import annotations

import time

import pytest

from ftre.services.session.entity.state import SessionMetaFile, SessionState
from ftre.services.session.search import search_sessions


def _state(
    sid: str,
    title: str = "",
    workspace: str = "",
    updated: str = "2026-08-17T00:00:00+08:00",
    last_user_text: str = "",
) -> SessionMetaFile:
    return SessionMetaFile(
        session=SessionState(
            id=sid,
            channel_id="ws",
            title=title,
            workspace=workspace,
            created_at=updated,
            updated_at=updated,
            last_user_text=last_user_text,
        ),
    )


def test_empty_query_returns_empty():
    out = search_sessions(
        [("s1", _state("s1", last_user_text="任意"))], "  "
    )
    assert out["total"] == 0
    assert out["results"] == []


def test_chinese_substring_any_length():
    """AC1：1 字 / 2 字 / ≥3 字中文子串均可命中。"""
    states = [
        ("s1", _state("s1", last_user_text="这是一个会话搜索性能测试消息")),
    ]
    assert search_sessions(states, "会")["total"] == 1
    assert search_sessions(states, "会话")["total"] == 1
    assert search_sessions(states, "会话搜索")["total"] == 1
    assert search_sessions(states, "搜索性能测试消息")["total"] == 1
    assert search_sessions(states, "不存在的词")["total"] == 0


def test_title_match_ranks_first():
    """标题命中排前，last_user_text 命中的老会话排后。"""
    states = [
        (
            "s_old_body",
            _state(
                "s_old_body",
                last_user_text="包含部署关键字的正文",
                updated="2026-08-15T00:00:00+08:00",
            ),
        ),
        (
            "s_new_title",
            _state("s_new_title", title="部署手册", updated="2026-08-16T00:00:00+08:00"),
        ),
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


def test_last_user_text_hit_yields_single_user_snippet():
    """last_user_text 命中产出恰好一条 role=user 摘要（桌面端 hits 形状）。"""
    states = [("s1", _state("s1", last_user_text="第 9 条包含锚点的消息"))]
    out = search_sessions(states, "锚点")
    hits = out["results"][0]["hits"]
    assert len(hits) == 1
    assert hits[0]["mid"] == ""
    assert hits[0]["role"] == "user"
    assert "锚点" in hits[0]["snippet"]


def test_snippet_contains_hit_and_is_bounded():
    """AC3：摘要包含命中位置，长度受限。"""
    long_text = "前缀填充" * 100 + "这里是要找的关键字" + "后缀填充" * 100
    states = [("s1", _state("s1", last_user_text=long_text))]
    out = search_sessions(states, "关键字")
    snippet = out["results"][0]["hits"][0]["snippet"]
    assert "关键字" in snippet
    assert len(snippet) < 300  # 2×80 半径 + 省略号，远小于原文


def test_offset_pagination_does_not_silently_drop_older_matches():
    states = [
        (
            f"s{i}",
            _state(
                f"s{i}",
                last_user_text="共同关键字",
                # 各不相同且 s0 最新：倒序稳定为 s0..s4
                updated=f"2026-08-1{4 - i}T00:00:00+08:00",
            ),
        )
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
    """检索面只有 title + last_user_text（真实用户输入预览）。

    无用户消息（last_user_text 为空）且标题不匹配的会话不返回——
    system / compact 等内容不进入预览，天然不参与检索。
    """
    states = [("s1", _state("s1"))]  # 空 title、空 last_user_text
    assert search_sessions(states, "关键字")["total"] == 0


def test_case_insensitive_ascii():
    assert search_sessions(
        [("s1", _state("s1", last_user_text="Deploy the Gateway"))], "deploy"
    )["total"] == 1


@pytest.mark.asyncio
async def test_manager_search_via_thread():
    """SessionService 门面可用（线程池执行路径）。"""
    from ftre.services.session.service import SessionService as SessionManager

    mgr = SessionManager(sessions_dir="unused-dir-for-test")
    # 直接注入内存态，绕过磁盘加载
    mgr._repo._states["s1"] = _state("s1", title="检索目标", last_user_text="正文命中检索")
    out = await mgr.search_sessions("检索")
    assert out["total"] == 1
    assert out["results"][0]["title_matched"] is True


def test_performance_benchmark():
    """AC4：性能基准（元信息量级）。

    检索面收缩为每会话两段短文本（title + last_user_text ~200 字符）后，
    真实个人上限量级（~2 万会话 ≈ 数 MB 元信息）单次检索 < 50ms；
    目标放在最后一会话，保证最坏情况全量扫描。
    检索在 to_thread 工作线程执行，不阻塞事件循环/UI。
    """
    unit = "这是一段用于填充搜索基准测试的普通中文文本，补充内容让预览长度接近真实用户输入。"
    filler = unit * 12  # ~480 字符
    assert len(filler) > 400

    def build(n_sessions: int):
        states = []
        for i in range(n_sessions):
            preview = filler
            if i == n_sessions - 1:
                preview = filler + "唯一的独特锚点词组xyz"
            states.append(
                (f"s{i}", _state(f"s{i}", title=f"会话 {i}", last_user_text=preview))
            )
        return states

    real = build(20_000)
    t0 = time.monotonic()
    out = search_sessions(real, "独特锚点词组xyz")
    real_elapsed = time.monotonic() - t0
    assert out["total"] == 1
    assert out["results"][0]["session_id"] == f"s{20_000 - 1}"
    assert real_elapsed < 0.05, f"meta-scale search took {real_elapsed:.3f}s (~20k sessions)"
