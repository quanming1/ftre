from types import SimpleNamespace

from ftre.plugin.builtin.title_gen import TitleGenPlugin


def test_first_turn_is_not_rejected_after_current_user_message_is_persisted():
    current_reply = "turn-current"
    events = [
        SimpleNamespace(type="user_message", reply_id=current_reply, data={"metadata": {}}),
    ]

    assert TitleGenPlugin._has_prior_user_message(events, current_reply) is False


def test_later_turn_has_a_prior_user_message():
    events = [
        SimpleNamespace(type="user_message", reply_id="turn-old", data={"metadata": {}}),
        SimpleNamespace(type="user_message", reply_id="turn-current", data={"metadata": {}}),
    ]

    assert TitleGenPlugin._has_prior_user_message(events, "turn-current") is True
