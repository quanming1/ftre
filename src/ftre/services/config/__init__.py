"""用户配置 Service、路径常量和加载辅助函数。"""

from .loader import load_config_file, load_gateway_address
from .paths import AGENTS_DIR, CONFIG_PATH
from .service import ConfigConflictError, ConfigService, ConfigSnapshot

__all__ = [
    "AGENTS_DIR",
    "CONFIG_PATH",
    "ConfigConflictError",
    "ConfigService",
    "ConfigSnapshot",
    "load_config_file",
    "load_gateway_address",
]
