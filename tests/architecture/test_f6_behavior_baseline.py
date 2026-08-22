"""F6.3 行为基线清单。

这些测试不是新的实现，而是迁移期间不可删除的外部行为护栏。每个领域至少保留
一个真实状态机/持久化/协议测试；后续 Hook 化必须在同一套基线下逐片迁移。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]
BASELINE_TESTS = {
    "agent": (
        "tests/test_agent_manager.py",
        "tests/test_session_lane.py",
        "tests/test_turn_lifecycle.py",
    ),
    "tool": ("tests/test_plugin_tools.py", "tests/test_confirm_commands.py"),
    "session": (
        "tests/test_session_manager_baseline.py",
        "tests/test_session_json_store.py",
        "tests/test_session_projection.py",
    ),
    "command": ("tests/test_confirm_commands.py", "tests/test_ws_control_commands.py"),
    "compaction": (
        "packages/ftre-compaction/tests/test_compact_algo.py",
        "packages/ftre-compaction/tests/test_compact_summary.py",
        "tests/test_turn_lifecycle.py",
    ),
}


def test_each_f6_domain_has_a_preserved_behavior_baseline():
    missing = {
        domain: [path for path in paths if not (ROOT / path).is_file()]
        for domain, paths in BASELINE_TESTS.items()
    }
    missing = {domain: paths for domain, paths in missing.items() if paths}
    assert not missing, f"F6 behavior baseline files missing: {missing}"
