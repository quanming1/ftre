"""JsonStateStore 文件安全测试（设计文档 §18.2）。

验收标准：
- 并发保存两条消息不丢失（配合 per-session lock，见 manager 测试）；
- 模拟写入失败后旧 state.json 保持完整；
- 进程重启后状态可恢复；
- 损坏文件明确报错并隔离，不影响其他 Session。
"""
import json
import os

import pytest
import pytest_asyncio
from ftre_agent_core.message import UserMsg

from ftre.session.storage.json_store import JsonStateStore, validate_session_id
from ftre.session.entity.state import AgentStateFile


def _state(session_id: str, *texts: str) -> AgentStateFile:
    return AgentStateFile(
        session={
            "id": session_id,
            "agent_id": "default",
            "channel_id": session_id.split("_sess_")[0],
            "title": "",
            "workspace": "",
            "created_at": "2026-07-27T18:00:00+08:00",
            "updated_at": "2026-07-27T18:00:00+08:00",
        },
        messages=[
            UserMsg(name="default", content=text, id=f"msg_{i}")
            for i, text in enumerate(texts)
        ],
    )


@pytest_asyncio.fixture
async def store(tmp_path):
    s = JsonStateStore(tmp_path / "sessions")
    await s.load_all()
    return s


# ─── 路径校验 ──────────────────────────────────────────────────


def test_valid_session_id_used_directly_as_dir_name(store):
    path = store.session_dir("ws_sess_abc123")
    assert path.name == "ws_sess_abc123"
    assert path.parent == store.root.resolve()


def test_session_id_with_dangerous_chars_rejected(store):
    for bad_id in ["../evil", "a/b", "a\\b", "a:b", "..", "", "a b", "a.b/c"]:
        with pytest.raises(ValueError):
            store.session_dir(bad_id)


def test_validate_session_id_function():
    validate_session_id("ws_sess_ed930104a1d2")  # 合法
    validate_session_id("octo_sess_abc-123_XYZ")  # 合法
    with pytest.raises(ValueError):
        validate_session_id("ws::sess_x")
    with pytest.raises(ValueError):
        validate_session_id("")


# ─── 写入 / 恢复 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_and_reload_recovers_state(store):
    state = _state("ws_sess_1", "第一条", "第二条")
    await store.write(state)

    # 文件可读（人类可读 JSON），目录名即 session_id
    raw = json.loads(store.state_path("ws_sess_1").read_text(encoding="utf-8"))
    assert set(raw) == {"schema_version", "session", "messages", "metadata"}
    assert raw["messages"][0]["content"][0]["text"] == "第一条"
    assert (store.root / "ws_sess_1" / "state.json").exists()

    # 模拟进程重启
    store2 = JsonStateStore(store.root)
    await store2.load_all()
    assert "ws_sess_1" in store2.states
    assert [m.id for m in store2.states["ws_sess_1"].messages] == ["msg_0", "msg_1"]


@pytest.mark.asyncio
async def test_failed_write_keeps_old_file(store, monkeypatch):
    await store.write(_state("ws_sess_1", "old"))

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        await store.write(_state("ws_sess_1", "new"))

    monkeypatch.undo()
    # 正式文件仍是旧内容（.tmp 残留不影响）
    store2 = JsonStateStore(store.root)
    await store2.load_all()
    assert store2.states["ws_sess_1"].messages[0].get_text_content() == "old"


@pytest.mark.asyncio
async def test_replace_uses_unique_tmp_and_retries_sharing_violation(store, monkeypatch):
    """短暂文件锁应重试；每次写入的临时文件不能是共享的固定路径。"""
    original_replace = os.replace
    calls = 0

    def flaky_replace(source, target):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError(5, "Access is denied", str(target))
        return original_replace(source, target)

    monkeypatch.setattr(os, "replace", flaky_replace)
    monkeypatch.setattr("ftre.session.storage.json_store.time.sleep", lambda _: None)
    await store.write(_state("ws_sess_1", "saved"))

    assert calls == 3
    assert store.state_path("ws_sess_1").exists()
    assert not list(store.state_path("ws_sess_1").parent.glob("state.json.tmp-*"))


@pytest.mark.asyncio
async def test_leftover_tmp_does_not_override(store):
    await store.write(_state("ws_sess_1", "official"))
    tmp = store.state_path("ws_sess_1").with_suffix(".json.tmp")
    tmp.write_text('{"schema_version": 1, "session": {"id": "fake"}}', encoding="utf-8")

    store2 = JsonStateStore(store.root)
    await store2.load_all()
    assert store2.states["ws_sess_1"].messages[0].get_text_content() == "official"


# ─── 损坏处理 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_corrupt_file_quarantined_and_reported(store):
    await store.write(_state("ws_good", "ok"))
    bad_dir = store.session_dir("ws_bad")
    bad_dir.mkdir(parents=True)
    bad_file = bad_dir / "state.json"
    bad_file.write_text("{not valid json", encoding="utf-8")

    store2 = JsonStateStore(store.root)
    await store2.load_all()

    # 好 Session 正常加载；坏 Session 明确记录，不静默当空 Session
    assert "ws_good" in store2.states
    assert "ws_bad" not in store2.states
    assert "ws_bad" in store2.corrupt
    # 原文件被隔离，不存在新覆盖
    assert not bad_file.exists()
    assert list(bad_dir.glob("state.json.corrupt-*"))


@pytest.mark.asyncio
async def test_unsupported_schema_version_quarantined(store):
    bad_dir = store.session_dir("ws_future")
    bad_dir.mkdir(parents=True)
    (bad_dir / "state.json").write_text(
        json.dumps({"schema_version": 99, "session": {"id": "ws_future"}}),
        encoding="utf-8",
    )
    store2 = JsonStateStore(store.root)
    await store2.load_all()
    assert "ws_future" in store2.corrupt


# ─── 删除 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_only_removes_exact_target(store):
    await store.write(_state("ws_a", "1"))
    await store.write(_state("ws_a2", "2"))

    assert await store.delete("ws_a") is True
    assert not store.state_path("ws_a").exists()
    assert store.state_path("ws_a2").exists()
    # 重复删除返回 False，不报错
    assert await store.delete("ws_a") is False
    assert await store.delete("ws_missing") is False


# ─── 锁 ────────────────────────────────────────────────────────


def test_lock_for_returns_stable_per_session_lock(store):
    assert store.lock_for("ws_a") is store.lock_for("ws_a")
    assert store.lock_for("ws_a") is not store.lock_for("ws_b")
