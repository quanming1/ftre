from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "ftre"


def test_no_import_time_default_config_or_direct_legacy_write() -> None:
    for path in ROOT.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "DEFAULT_CONFIG" not in source
        if "services/config" not in str(path).replace("\\", "/"):
            assert "os.replace(tmp_path, CONFIG_PATH)" not in source
