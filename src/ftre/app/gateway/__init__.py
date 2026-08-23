"""Gateway 公共导出：提供 Composition 和默认 Plugin 清单，供 CLI 与嵌入式启动方使用。"""

# 对外只暴露组装根与清单两个入口，装配细节留在 composition.py 内部。
from .composition import Composition, default_manifests

__all__ = ["Composition", "default_manifests"]
