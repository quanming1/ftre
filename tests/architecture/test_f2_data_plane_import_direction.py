from __future__ import annotations

import ast
from pathlib import Path

from ftre.bus import EventBus as LegacyEventBus
from ftre.channel.manager import ChannelManager as LegacyChannelManager
from ftre.command import CommandManager as LegacyCommandManager
from ftre.services.command import CommandManager
from ftre.services.messaging.bus import EventBus
from ftre.services.messaging.channel.manager import ChannelManager

ROOTS = (
    Path(__file__).parents[2] / "src" / "ftre" / "services" / "messaging",
    Path(__file__).parents[2] / "src" / "ftre" / "services" / "command",
    Path(__file__).parents[2] / "src" / "ftre" / "services" / "tools",
)


def test_legacy_data_plane_names_resolve_to_new_owners() -> None:
    assert LegacyEventBus is EventBus
    assert LegacyChannelManager is ChannelManager
    assert LegacyCommandManager is CommandManager


def test_new_data_plane_owners_do_not_import_legacy_packages() -> None:
    violations: list[str] = []
    for root in ROOTS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(("ftre.bus", "ftre.channel", "ftre.command", "ftre.tools")):
                    violations.append(f"{path}:{node.lineno}: from {node.module}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(("ftre.bus", "ftre.channel", "ftre.command", "ftre.tools")):
                            violations.append(f"{path}:{node.lineno}: import {alias.name}")
    assert violations == []
