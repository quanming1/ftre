from ftre_messaging.plugin import apply, inject
from ftre_messaging.send_message import create_send_message_tool


def test_public_entry_and_factory() -> None:
    assert callable(apply)
    assert inject == ("channels", "tools", "inbox")
    assert create_send_message_tool.__module__ == "ftre_messaging.send_message"


def test_send_message_rejects_unknown_channel_before_dispatch() -> None:
    class EmptyChannels:
        @staticmethod
        def get(_channel_id):
            return None

    tool = create_send_message_tool(EmptyChannels(), object())
    result = tool.execute_callable(
        "missing",
        "session-1",
        "hello",
        caller_channel="ws",
        caller_session="session-2",
        event_loop=object(),
    )
    assert result == "[error] 频道不存在: missing"
