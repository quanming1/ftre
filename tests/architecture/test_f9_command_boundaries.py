"""F6.9 command ingress and retired-filter architecture gates."""

from pathlib import Path

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "ftre"


def _text(relative: str) -> str:
    return (SRC / relative).read_text(encoding="utf-8")


def test_turn_executor_has_no_command_matching_or_legacy_filter_path() -> None:
    source = _text("services/agent/runtime/turn_executor.py")
    forbidden = (
        "ftre.plugins.builtin.command",
        "command_manager",
        "execute_command",
        "TurnStatus.COMMAND",
        "command_name",
        "_command(",
        "match_any(",
        "try_dispatch(",
        "BEFORE_AGENT_RUN",
        "BEFORE_MESSAGES_BUILD",
        "MessagesBuildContext",
        "AgentRunContext",
        "_run_legacy_waterfall",
    )
    assert not any(item in source for item in forbidden)


def test_message_bus_dispatches_commands_before_inbox_admission() -> None:
    agent_source = _text("services/agent/runtime/engine.py")
    bus_source = _text("services/messaging/bus/ingress.py")
    command_source = _text("plugins/builtin/command/plugin.py")
    assert "_parse_ingress_command" not in agent_source
    assert "_inbound_handler" not in agent_source
    assert '"messaging/inbound"' in bus_source
    assert "MESSAGING_INBOUND_SPEC" in command_source


def test_command_layer_does_not_import_private_agent_runtime() -> None:
    command_root = SRC / "services" / "command"
    forbidden = ("ftre.services.agent_loop", "TurnExecutor", "CompactManager")
    for path in command_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert not any(item in source for item in forbidden), path


def test_builtin_commands_use_public_service_owners() -> None:
    source = _text("plugins/builtin/command/builtin.py")
    forbidden = ("AgentLoop", "loop.compaction", "loop.session_manager")
    assert not any(item in source for item in forbidden)


def test_command_result_is_not_an_agent_control_union() -> None:
    source = _text("plugins/builtin/command/types.py")
    forbidden = ("ResumeAgent", "RewritePrompt", "Passthrough", "SendMessage", "Handled")
    assert not any(item in source for item in forbidden)


def test_retired_filter_module_is_gone() -> None:
    assert not (SRC / "services" / "agent_loop" / "runtime" / "hooks.py").exists()
