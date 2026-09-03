from ftre_task.plugin import apply, inject
from ftre_task.task import create_task_tool


def test_public_entry_and_factory() -> None:
    assert callable(apply)
    assert inject == ("channels", "tools", "inbox")
    assert create_task_tool.__module__ == "ftre_task.task"


def test_task_rejects_empty_prompt_without_touching_services() -> None:
    tool = create_task_tool(object(), object())
    result = tool.execute_callable(
        "",
        event_loop=object(),
        session_manager=object(),
        agent_service=object(),
    )
    assert result == "[error] prompt 不能为空"
