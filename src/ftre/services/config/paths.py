from pathlib import Path
import os
import sys

CONFIG_PATH = Path(os.environ.get("USERPROFILE", Path.home()) if sys.platform == "win32" else Path.home()) / ".ftre" / "config.json"

