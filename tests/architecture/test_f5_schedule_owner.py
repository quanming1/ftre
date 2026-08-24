from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "ftre"
SCHEDULE = SRC / "plugins" / "builtin" / "schedule"
LEGACY_CRON_MODULE = "ftre.services.tools.builtin." + "cron"


def test_schedule_has_real_owner_modules_and_old_cron_is_deleted() -> None:
    assert not (SRC / "services" / "tools" / "builtin" / "cron.py").exists()
    for name, symbol in {
        "store.py": "class CronStore",
        "channel.py": "class CronChannel",
        "scheduler.py": "class CronScheduler",
        "tool.py": "def build_cron_tool",
    }.items():
        source = (SCHEDULE / name).read_text(encoding="utf-8")
        assert symbol in source
        assert LEGACY_CRON_MODULE not in source
        assert "import *" not in source


def test_schedule_router_and_bootstrap_do_not_bypass_owner() -> None:
    router = (SCHEDULE / "router.py").read_text(encoding="utf-8")
    assert "service.root" not in router
    assert "json.loads" not in router
    assert "read_text" not in router
    bootstrap = (SRC / "app" / "gateway" / "bootstrap.py").read_text(encoding="utf-8")
    assert "CronScheduler" not in bootstrap
    assert LEGACY_CRON_MODULE not in bootstrap


def test_no_production_import_points_to_deleted_cron_module() -> None:
    for path in (SRC).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != LEGACY_CRON_MODULE, path
