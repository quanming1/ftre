"""可独立安装的 ftre 上下文压缩能力包。

公共入口只暴露 Service 和事件名称；Plugin 的 apply 由 Manifest/entry point
显式加载，未安装本包时 ftre 核心不会触碰这些导出。
"""

from .events import CompactEventName
from .service import CompactionService

__all__ = ["CompactEventName", "CompactionService"]
