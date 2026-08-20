from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class ScheduleService:
    key = "schedule"

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or (Path.home() / ".ftre" / "cron")).expanduser().resolve()

    def list(self) -> list[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True)
        result = []
        for path in self.root.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(item, dict):
                    result.append(item)
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(result, key=lambda item: item.get("created_at", 0), reverse=True)

    def save(self, job: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{job['id']}.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")

    def delete(self, job_id: str) -> bool:
        path = self.root / f"{job_id}.json"
        if not path.exists():
            return False
        path.unlink()
        return True

