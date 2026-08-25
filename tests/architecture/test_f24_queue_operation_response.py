"""F24 Queue Operation Response 的协议与 Owner 架构门禁。

这些断言保护的是“操作成功只由 Queue Response 结算”的边界，而不是某一条测试
样例。旧的 admission ACK 如果被重新加回生产 Channel，客户端又会回到双阶段状态机。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
CHANNEL = ROOT / "src" / "ftre" / "plugins" / "builtin" / "channels" / "websocket" / "channel.py"
INBOX_SERVICE = ROOT / "packages" / "ftre-inbox" / "src" / "ftre_inbox" / "service.py"
COMPOSITION = ROOT / "src" / "ftre" / "app" / "gateway" / "composition.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_queue_mutations_have_one_success_owner() -> None:
    """WebSocket 只包装 Inbox 的 wire snapshot，不再拥有第二种成功协议。"""
    source = _source(CHANNEL)
    tree = ast.parse(source)
    function_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_send_queue_response" in function_names
    assert "_send_admission_ack" not in function_names
    assert '"type": "session/queue"' in source
    assert '"value": {"accepted": accepted' in source  # 仅 session.cancel 控制 ACK


def test_wire_snapshot_exposes_persisted_revision_only() -> None:
    """revision 必须来自 Inbox 持久化快照，不能由 Channel 或客户端自行猜测。"""
    source = _source(INBOX_SERVICE)
    assert '"revision": snapshot.revision' in source
    wire_method = ast.parse(source).body
    assert any(
        isinstance(node, ast.ClassDef)
        and any(
            isinstance(member, ast.AsyncFunctionDef) and member.name == "wire_snapshot"
            for member in node.body
        )
        for node in wire_method
    )


def test_default_composition_requires_inbox_plugin() -> None:
    """默认 Gateway 没有 Inbox 时应启动失败，而不是悄悄退回旧队列实现。"""
    source = _source(COMPOSITION)
    assert 'PluginManifest("inbox", "ftre_inbox.plugin:apply", "builtin", True, True' in source
