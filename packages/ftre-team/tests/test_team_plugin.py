from ftre_team.plugin import apply, inject
from ftre_team.team import create_team_tools


def test_public_entry_and_factory() -> None:
    assert callable(apply)
    assert inject == ("sessions", "agents", "channels", "tools", "inbox", "agent_profiles")
    tools = create_team_tools(object(), object(), object())
    assert create_team_tools.__module__ == "ftre_team.team"
    assert {tool.name for tool in tools} == {
        "team_create", "team_add_agent", "team_say", "team_agent_status",
        "team_delete", "wait_agent",
    }


def test_team_create_rejects_empty_name_before_session_access() -> None:
    tool = create_team_tools(object(), object(), object())[0]
    result = tool.func(
        "",
        session_id="leader",
        event_loop=object(),
        session_manager=object(),
        agent_service=object(),
        caller_channel="ws",
    )
    assert result == "[error] team_name 不能为空"
