from ftre.plugin.builtin.title_gen import TitleGenPlugin


def test_first_turn_is_not_rejected_after_current_user_msg_is_persisted():
    messages = [{"role": "user", "metadata": {}}]
    assert TitleGenPlugin._has_prior_user_message(messages) is False


def test_later_turn_has_a_prior_user_msg():
    messages = [
        {"role": "user", "metadata": {}},
        {"role": "user", "metadata": {}},
    ]
    assert TitleGenPlugin._has_prior_user_message(messages) is True


def test_hidden_user_msg_does_not_count_as_prior_turn():
    messages = [
        {"role": "user", "metadata": {"hide": True}},
        {"role": "user", "metadata": {}},
    ]
    assert TitleGenPlugin._has_prior_user_message(messages) is False
