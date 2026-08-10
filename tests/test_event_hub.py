"""AgentEventHub 单元测试：一次性等待 / 重复拒绝 / unregister / emit 唤醒 / 取消兜底。"""
from concurrent.futures import Future

from ftre.agent.event_hub import AgentEventHub


def test_wait_returns_future_and_emit_wakes_it():
    hub = AgentEventHub()
    fut = hub.wait("sid-1", AgentEventHub.AGENT_FINISHED)
    assert isinstance(fut, Future)

    payload = {"session_id": "sid-1", "status": "completed", "final_content": "done"}
    hub.emit("sid-1", AgentEventHub.AGENT_FINISHED, payload)

    assert fut.done()
    assert fut.result() == payload


def test_wait_rejects_existing_waiter_until_finished():
    hub = AgentEventHub()
    first = hub.wait("sid-1", AgentEventHub.AGENT_FINISHED)
    assert first is not None

    # 已有未完成等待者 → 拒绝重复
    assert hub.wait("sid-1", AgentEventHub.AGENT_FINISHED) is None

    # 完成后再 wait 允许（多轮 team_say / task 语义）
    hub.emit("sid-1", AgentEventHub.AGENT_FINISHED, {"status": "completed"})
    assert hub.wait("sid-1", AgentEventHub.AGENT_FINISHED) is not None


def test_unregister_removes_waiter_before_emit():
    hub = AgentEventHub()
    fut = hub.wait("sid-1", AgentEventHub.AGENT_FINISHED)
    assert fut is not None

    hub.unregister("sid-1", AgentEventHub.AGENT_FINISHED, fut)
    hub.emit("sid-1", AgentEventHub.AGENT_FINISHED, {"status": "completed"})

    assert not fut.done()


def test_unregister_only_removes_matching_future():
    hub = AgentEventHub()
    first = hub.wait("sid-1", AgentEventHub.AGENT_FINISHED)
    assert first is not None
    second = Future()

    hub.unregister("sid-1", AgentEventHub.AGENT_FINISHED, second)  # 不匹配，应无效果
    hub.emit("sid-1", AgentEventHub.AGENT_FINISHED, {"status": "completed"})
    assert first.done()


def test_emit_is_one_shot_and_scoped_by_session():
    hub = AgentEventHub()
    fut_a = hub.wait("sess-a", AgentEventHub.AGENT_FINISHED)
    fut_b = hub.wait("sess-b", AgentEventHub.AGENT_FINISHED)

    hub.emit("sess-a", AgentEventHub.AGENT_FINISHED, {"session_id": "sess-a"})

    assert fut_a.done()
    assert not fut_b.done()  # 事件按 (session_id, event) 精确匹配

    hub.emit("sess-b", AgentEventHub.AGENT_FINISHED, {"session_id": "sess-b"})
    assert fut_b.done()


def test_emit_without_waiters_is_noop():
    hub = AgentEventHub()
    hub.emit("nobody", AgentEventHub.AGENT_FINISHED, {"status": "completed"})  # 不应抛


def test_cancel_all_wakes_all_pending_waiters():
    hub = AgentEventHub()
    fut_a = hub.wait("s1", AgentEventHub.AGENT_FINISHED)
    fut_b = hub.wait("s2", AgentEventHub.AGENT_FINISHED)
    assert fut_a is not None and fut_b is not None

    hub.cancel_all({"status": "cancelled", "final_content": ""})

    assert fut_a.done() and fut_a.result()["status"] == "cancelled"
    assert fut_b.done() and fut_b.result()["status"] == "cancelled"
    # 兜底后注册表清空，新 wait 不受影响
    assert hub.wait("s1", AgentEventHub.AGENT_FINISHED) is not None


def test_subscribe_callback_invoked_on_emit():
    hub = AgentEventHub()
    calls = []
    hub.subscribe(AgentEventHub.AGENT_FINISHED, lambda sid, payload: calls.append((sid, payload)))

    hub.emit("s1", AgentEventHub.AGENT_FINISHED, {"status": "completed"})

    assert calls == [("s1", {"status": "completed"})]


def test_subscriber_exception_does_not_break_emit():
    hub = AgentEventHub()
    fut = hub.wait("s1", AgentEventHub.AGENT_FINISHED)

    def bad_cb(sid, payload):
        raise RuntimeError("boom")

    hub.subscribe(AgentEventHub.AGENT_FINISHED, bad_cb)
    hub.emit("s1", AgentEventHub.AGENT_FINISHED, {"status": "completed"})

    assert fut.done()  # 等待者仍被唤醒，订阅者异常被隔离
