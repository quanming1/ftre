"""F12 独立 Inbox 所有权与退役旧队列的架构门禁。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
SRC = ROOT / "src" / "ftre"
PACKAGE = ROOT / "packages" / "ftre-inbox"


def test_inbox_package_is_complete_and_core_does_not_reverse_import_it() -> None:
    assert (PACKAGE / "pyproject.toml").exists()
    assert (PACKAGE / "README.md").exists()
    assert (PACKAGE / "src" / "ftre_inbox" / "service.py").exists()
    assert (PACKAGE / "src" / "ftre_inbox" / "repository.py").exists()
    assert (PACKAGE / "src" / "ftre_inbox" / "plugin.py").exists()
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path == SRC / "app" / "gateway" / "composition.py":
            assert "ftre_inbox.plugin:apply" in text
        else:
            assert "import ftre_inbox" not in text
            assert "from ftre_inbox" not in text


def test_retired_mailbox_runtime_tree_and_session_owner_are_gone() -> None:
    mailbox = SRC / "services" / "agent_loop" / "runtime" / "mailbox"
    assert not any(mailbox.glob("*.py"))
    for relative in (
        "services/agent/service.py",
        "services/agent/contracts.py",
        "services/session/service.py",
        "services/session/persistence/repository.py",
    ):
        source = (SRC / relative).read_text(encoding="utf-8")
        for symbol in (
            "MailboxState",
            "MailboxStore",
            "SessionLane",
            "get_mailbox_snapshot",
            "cancel_queued_message",
            "admit_inbound",
            "queue_position",
        ):
            assert symbol not in source, (relative, symbol)


def test_agent_service_contract_is_single_message_execution_boundary() -> None:
    source = (SRC / "services" / "agent" / "service.py").read_text(encoding="utf-8")
    assert "async def run(self, message: InboundMessage)" in source
    assert "self.driver.run(message)" in source
    assert "submit(" not in source
    assert "pending" not in source


def test_queue_wire_contract_has_no_legacy_frame_or_mailbox_alias() -> None:
    protocol = (SRC / "services" / "messaging" / "bus" / "protocol.py").read_text(encoding="utf-8")
    channel = (SRC / "services" / "messaging" / "channel" / "providers" / "websocket" / "channel.py").read_text(encoding="utf-8")
    assert "session/queue" in protocol
    assert "session/status" in protocol
    assert "mailbox_snapshot" not in protocol
    assert "frame_id" not in channel
    assert "mailbox" not in channel.lower()


def test_clean_import_does_not_load_ftre_gateway() -> None:
    package_src = PACKAGE / "src"
    code = (
        "import sys; sys.path.insert(0, r'" + str(package_src) + "'); "
        "import ftre_inbox; print(ftre_inbox.QueueTarget)"
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
