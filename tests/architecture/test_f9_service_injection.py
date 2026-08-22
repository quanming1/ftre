"""F9 Service Inject/Provide and Owner-boundary architecture gates."""

import ast
from pathlib import Path

import pytest

from ftre.services.session import SessionService

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "ftre"


def _source(relative: str) -> str:
    return (SRC / relative).read_text(encoding="utf-8")


def test_command_and_feature_code_never_uses_loop_as_service_locator() -> None:
    roots = [SRC / "services" / "command", SRC / "features", SRC / "interfaces"]
    forbidden = ("loop.session_manager", "loop.compaction", "loop.commands", "_loop.session_manager")
    for root in roots:
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert not any(item in source for item in forbidden), path


def test_agent_runtime_provider_has_no_unbounded_service_any() -> None:
    source = _source("services/agent_loop/provider.py")
    assert "Any" not in source
    assert "AgentRuntimeServices" in source
    assert "SessionService" in source
    assert "CommandService" in source


def test_turn_executor_receives_data_plane_services_explicitly() -> None:
    source = _source("services/agent_loop/runtime/loop/turn_executor.py")
    for forbidden in (
        'getattr(loop, "agent_service"',
        'getattr(loop, "attachments"',
        'getattr(loop, "system_prompt"',
        'getattr(loop, "hooks"',
        "loop.session_manager",
    ):
        assert forbidden not in source
    assert "self._attachments" in source
    assert "self._system_prompt" in source
    assert '"sessions": self._sessions' in source


def test_plugins_declare_context_service_attributes() -> None:
    ignored = {"get", "provide", "effect", "events", "fiber", "parent", "scope"}
    plugin_paths = list((SRC / "services").rglob("plugin.py")) + list(
        (SRC / "features").rglob("plugin.py")
    )
    for path in plugin_paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        inject = set()
        provide = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in {"inject", "provide"}:
                        value = node.value
                        if isinstance(value, (ast.Tuple, ast.List)):
                            target_set = inject if target.id == "inject" else provide
                            target_set.update(
                                item.value
                                for item in value.elts
                                if isinstance(item, ast.Constant) and isinstance(item.value, str)
                            )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                continue
            if node.value.id != "ctx" or node.attr in ignored:
                continue
            assert node.attr in inject or node.attr in provide, (
                path,
                node.attr,
                inject,
                provide,
            )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not isinstance(node.func.value, ast.Name) or node.func.value.id != "ctx":
                continue
            if node.func.attr != "get" or not node.args:
                continue
            key = node.args[0]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                assert key.value in provide, (path, key.value, provide)


def test_builtin_command_owner_dependencies_are_explicit() -> None:
    source = _source("services/command/builtin.py")
    for symbol in ("SessionService", "AgentService"):
        assert symbol in source
    assert "CompactionService" not in source
    assert "CompactionPort" not in source
    assert "register_builtin_commands(manager, loop)" not in source


def test_builtin_tools_use_public_agent_service_key() -> None:
    root = SRC / "services" / "tools" / "builtin"
    for path in root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert 'Injected("agent_loop")' not in source, path
        assert 'Injected("session_manager")' not in source, path
        assert "submit_inbound" not in source, path


def test_title_plugin_disposes_background_workers() -> None:
    source = _source("services/session/title/plugin.py")
    generator = _source("services/session/title/generator.py")
    assert "ctx.effect(generator.close" in source
    assert "self._stopping" in generator


def test_mcp_feature_uses_injected_attachment_owner() -> None:
    source = (SRC / "features" / "mcp" / "adapter.py").read_text(encoding="utf-8")
    assert "ftre.services.attachment.store" not in source
    assert "attachment_service" in source


def test_websocket_attachment_persistence_uses_attachment_service() -> None:
    channel = _source("services/messaging/channel/providers/websocket/channel.py")
    plugin = _source("services/messaging/channel/providers/websocket/plugin.py")
    assert "ftre.services.attachment.store" not in channel
    assert "attachment_service.save_image" in channel
    assert '"attachments"' in plugin
    assert not (SRC / "services" / "attachment" / "store.py").exists()
    assert "ftre.services.attachment.codec" in _source("services/session/message/multimodal.py")


@pytest.mark.asyncio
async def test_session_command_lifecycle_log_is_durable(tmp_path) -> None:
    sessions = SessionService(sessions_dir=tmp_path / "sessions")
    await sessions.init()
    session_id = await sessions.create_session("ws")
    await sessions.append_command_event(
        session_id,
        {"type": "command/run", "command_id": "cmd-1", "name": "/hello"},
    )
    await sessions.append_command_event(
        session_id,
        {"type": "command/done", "command_id": "cmd-1", "kind": "success"},
    )
    assert [event["type"] for event in await sessions.get_command_events(session_id)] == [
        "command/run",
        "command/done",
    ]
    await sessions.close()
