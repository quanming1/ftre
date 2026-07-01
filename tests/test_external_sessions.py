import pytest

from ftre.session.manager import SessionManager


@pytest.mark.asyncio
async def test_get_or_create_external_session_reuses_mapping(tmp_path):
    manager = SessionManager(str(tmp_path / "sessions.db"))
    await manager.init()
    try:
        first = await manager.get_or_create_external_session(
            channel_id="octo",
            external_key="octo:2:ch_group_1",
            title="Octo ch_group_1",
            external_data={
                "channel_type": 2,
                "channel_id": "ch_group_1",
                "from_uid": "uid_alice",
            },
        )
        second = await manager.get_or_create_external_session(
            channel_id="octo",
            external_key="octo:2:ch_group_1",
            title="Ignored title",
            external_data={
                "channel_type": 2,
                "channel_id": "ch_group_1",
                "from_uid": "uid_bob",
            },
        )

        assert first == second
        assert first.startswith("octo::sess_")

        session = await manager.get_session(first)
        assert session is not None
        assert session["channel_id"] == "octo"

        external = await manager.get_external_session(first)
        assert external is not None
        assert external["external_key"] == "octo:2:ch_group_1"
        assert external["external_data"]["channel_type"] == 2
        assert external["external_data"]["channel_id"] == "ch_group_1"
        assert external["external_data"]["from_uid"] == "uid_bob"
    finally:
        await manager.close()
