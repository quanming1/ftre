from types import SimpleNamespace

from ftre.plugins.builtin.session_title.generator import TitleGenPlugin


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
    from ftre.plugins.builtin.session_title import generator as title_gen

    captured = {}

    class FakePrepared:
        def __init__(self, config):
            self.config = config

        async def stream(self, request):
            captured["reasoning_effort"] = request.config.reasoning_effort
            yield SimpleNamespace(type="text-delta", text="Title")

    class FakeLlm:
        async def prepare_call(self, config, **_kwargs):
            captured["api_type"] = config.api_type
            captured["reasoning_effort"] = config.reasoning_effort
            return FakePrepared(config)

        async def stream(self, request, **_kwargs):
            captured["api_type"] = request.config.api_type
            captured["reasoning_effort"] = request.config.reasoning_effort
            yield SimpleNamespace(type="text-delta", text="Title")

    def schedule(coroutine, loop):
        class Future:
            def result(self, timeout):
                return title_gen.asyncio.run(coroutine)

        return Future()

    monkeypatch.setattr(title_gen.asyncio, "run_coroutine_threadsafe", schedule)
    plugin = TitleGenPlugin(llm=FakeLlm())
    plugin._system_prompt = "title"
    plugin._max_chars = 40
    plugin._event_loop = object()
    config = SimpleNamespace(
        llm=SimpleNamespace(
            provider="title-provider",
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


def test_title_generation_close_prevents_new_workers():
    plugin = TitleGenPlugin()
    plugin.close()
    plugin._spawn_title_generation("session-1", "hello", SimpleNamespace(), object())
    assert plugin._threads == set()
