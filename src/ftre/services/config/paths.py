"""用户级 ftre 配置和 Agent profile 的跨平台路径常量。"""

import os
import sys
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("USERPROFILE", Path.home()) if sys.platform == "win32" else Path.home()) / ".ftre" / "config.json"
AGENTS_DIR = CONFIG_PATH.parent / "agents"
