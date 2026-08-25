"""SessionRepository 批量 Msg 更新的原子性与写放大回归测试。"""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from ftre_agent_core.message import AssistantMsg, UserMsg

from ftre.services.session.persistence.repository import SessionRepository


@pytest_asyncio.fixture
async def repository(tmp_path):
    repo = SessionRepository(sessions_dir=str(tmp_path / "sessions"))
    await repo.init()
    return repo


@pytest.mark.asyncio
async def test_update_messages_commits_once_and_preserves_order(repository, monkeypatch):
    session_id = await repository.create_session("ws")
    first = UserMsg(name="default", content="first")
    second = AssistantMsg(name="default", content="second")
    third = AssistantMsg(name="default", content="third")
    for message in (first, second, third):
        await repository.save_message(session_id, message)

    first.content[0].text = "first-updated"
    third.content[0].text = "third-updated"
    commit_spy = AsyncMock(wraps=repository.commit)
    monkeypatch.setattr(repository, "commit", commit_spy)

    # 故意以逆序提交，磁盘 transcript 的原顺序仍必须保持不变。
    await repository.update_messages([third, first])

    commit_spy.assert_awaited_once()
    messages = await repository.get_messages_by_session(session_id)
    assert [message["id"] for message in messages] == [first.id, second.id, third.id]
    assert [message["content"][0]["text"] for message in messages] == [
        "first-updated", "second", "third-updated",
    ]


@pytest.mark.asyncio
async def test_update_messages_rejects_cross_session_without_partial_update(
    repository, monkeypatch,
):
    first_session = await repository.create_session("ws")
    second_session = await repository.create_session("ws")
    first = UserMsg(name="default", content="first")
    second = UserMsg(name="default", content="second")
    await repository.save_message(first_session, first)
    await repository.save_message(second_session, second)

    first.content[0].text = "first-updated"
    second.content[0].text = "second-updated"
    commit_spy = AsyncMock(wraps=repository.commit)
    monkeypatch.setattr(repository, "commit", commit_spy)

    with pytest.raises(ValueError, match="跨 session"):
        await repository.update_messages([first, second])

    commit_spy.assert_not_awaited()
    assert (await repository.get_messages_by_session(first_session))[0]["content"][0]["text"] == "first"
    assert (await repository.get_messages_by_session(second_session))[0]["content"][0]["text"] == "second"


@pytest.mark.asyncio
async def test_update_messages_rejects_unknown_id_without_partial_update(
    repository, monkeypatch,
):
    session_id = await repository.create_session("ws")
    saved = UserMsg(name="default", content="saved")
    await repository.save_message(session_id, saved)
    saved.content[0].text = "saved-updated"
    unknown = AssistantMsg(name="default", content="unknown")
    commit_spy = AsyncMock(wraps=repository.commit)
    monkeypatch.setattr(repository, "commit", commit_spy)

    with pytest.raises(ValueError, match="message 不存在"):
        await repository.update_messages([saved, unknown])

    commit_spy.assert_not_awaited()
    messages = await repository.get_messages_by_session(session_id)
    assert messages[0]["content"][0]["text"] == "saved"
