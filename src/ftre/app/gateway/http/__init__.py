"""Gateway HTTP Host 的公开导出；应用工厂只负责把已注册路由物化成 FastAPI。"""

# 唯一的工厂入口 create_app：路由注册由 HttpService 负责，这里只做物化。
from .app import create_app

__all__ = ["create_app"]
