from concurrent.futures import Future

from ftre.agent.loop import AgentLoop


def _agent_loop_with_subagent_futures() -> AgentLoop:
    loop = object.__new__(AgentLoop)
    loop._subagent_done_futures = {}
    return loop


def test_subagent_done_future_rejects_existing_waiter():
    loop = _agent_loop_with_subagent_futures()
    first = Future()
    second = Future()

    assert loop.register_subagent_done_future("sid-1", first) is True
    assert loop.register_subagent_done_future("sid-1", second) is False

    first.set_result({"status": "completed"})

    assert loop.register_subagent_done_future("sid-1", second) is True


def test_unregister_subagent_done_future_only_removes_matching_future():
    loop = _agent_loop_with_subagent_futures()
    first = Future()
    second = Future()

    assert loop.register_subagent_done_future("sid-1", first) is True

    loop.unregister_subagent_done_future("sid-1", second)
    assert loop._subagent_done_futures["sid-1"] is first

    loop.unregister_subagent_done_future("sid-1", first)
    assert "sid-1" not in loop._subagent_done_futures
