"""HTTP 路由贡献 Service；真正的 FastAPI Host 由 app 层创建。"""

from .service import HttpService, RouteConflictError

__all__ = ["HttpService", "RouteConflictError"]
