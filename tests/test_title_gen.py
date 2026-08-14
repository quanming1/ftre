from types import SimpleNamespace

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


def test_title_generation_passes_reasoning_effort_to_handler(monkeypatch):
    """Title generation forwards the selected LLM configuration's effort."""
    from ftre.plugin.builtin import title_gen

    captured = {}

    class FakeHandler:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def stream(self, *args, **kwargs):
            if False:
                yield None

    monkeypatch.setattr("ftre_agent_core.llm.LLMHandler", FakeHandler)

    class FakeFuture:
        def result(self, timeout):
            return "Title"

    def schedule(coroutine, loop):
        coroutine.close()
        return FakeFuture()

    monkeypatch.setattr(title_gen.asyncio, "run_coroutine_threadsafe", schedule)
    plugin = TitleGenPlugin()
    plugin._system_prompt = "title"
    plugin._max_chars = 40
    plugin._ctx = SimpleNamespace(event_loop=object())
    config = SimpleNamespace(
        llm=SimpleNamespace(
            model="main",
            api_key="key",
            api_base="",
            api_type="completions",
            reasoning_effort="high",
        ),
        title_llm=None,
    )

    assert plugin._generate_title("test", config) == "Title"
    assert captured["reasoning_effort"] == "high"
