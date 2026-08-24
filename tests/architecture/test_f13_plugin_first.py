"""F13 Plugin-first architecture gates.

These checks deliberately inspect ownership and dependency direction rather than
implementation details.  They prevent a future feature from moving business
rules back into the lightweight runtime or the Gateway bootstrap.
"""

from pathlib import Path

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "ftre"


def _source(relative: str) -> str:
    return (SRC / relative).read_text(encoding="utf-8")


def test_lightweight_kernel_has_no_product_imports() -> None:
    kernel_files = [
        *sorted((SRC / "kernel" / "plugins").glob("*.py")),
        *sorted((SRC / "kernel" / "hooks").glob("*.py")),
    ]
    forbidden = (
        "ftre.services",
        "ftre.plugins.builtin",
        "ftre_inbox",
        "QueueItem",
        "Compaction",
        "CommandService",
    )
    for path in kernel_files:
        source = path.read_text(encoding="utf-8")
        assert not any(item in source for item in forbidden), path


def test_gateway_bootstrap_does_not_construct_business_services() -> None:
    source = _source("app/gateway/bootstrap.py")
    forbidden_constructors = (
        "SessionService(",
        "EventBus(",
        "ChannelManager(",
        "ToolService(",
        "AgentService(",
        "CommandService(",
        "AgentManager(",
        "AgentRuntimeServices(",
    )
    assert not any(item in source for item in forbidden_constructors)
    plugin = (SRC / "services" / "agent" / "plugin.py").read_text(encoding="utf-8")
    assert "build_runtime(" in plugin


def test_agent_loop_history_handoff_precedes_turn_execution() -> None:
    source = _source("services/agent/runtime/engine.py")
    handoff = source.index("_persist_inbound_user_message(")
    execution = source.index("self._executor.execute(")
    assert handoff < execution


def test_turn_executor_is_not_user_message_owner() -> None:
    source = _source("services/agent/runtime/turn_executor.py")
    forbidden = (
        "_persist_user_message",
        "persist_input",
        "UserMessageEvent",
        "AgentControlPort",
        "MessageJournalPort",
        "CompactionPort",
    )
    assert not any(item in source for item in forbidden)
    assert "user_message_id" in source


def test_session_plugin_owns_the_session_event_service() -> None:
    source = _source("services/session/plugin.py")
    assert 'provide = ("sessions", "session_events")' in source
    assert "SessionEventService(service, ctx.message_bus.bus)" in source


def test_builtin_tools_use_public_channel_names_not_provider_modules() -> None:
    tools_root = SRC / "services" / "tools" / "builtin"
    for path in tools_root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "channel.providers.subagent" not in source, path


def test_agent_runtime_uses_trace_service_owner() -> None:
    engine = _source("services/agent/runtime/engine.py")
    provider = _source("services/agent/runtime/provider.py")
    trace_service = _source("plugins/builtin/trace/service.py")
    assert "SQLiteTraceExporter" not in engine
    assert "TRACE_DB_PATH" not in engine
    assert '"traces"' in provider
    assert "def build_tracer" in trace_service


def test_plugin_first_prd_is_the_current_f13_contract() -> None:
    prd = ROOT / "docs" / "prd" / "PRD-F13-plugin-first-kernel.md"
    text = prd.read_text(encoding="utf-8")
    assert "内核只提供机制" in text
    assert "Plugin-first" in text
    assert "不新增通用" in text
    assert "Queue → History" in text
