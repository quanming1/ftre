"""HTTP Service：路由贡献注册表，而不是 HTTP Server。

各 Plugin 只向这里注册路由；Gateway Host 在所有 Plugin 完成后调用
``build_app`` materialize FastAPI，并在此后 ``freeze``。这样路由冲突、卸载后需
重启等规则由一个 Owner 管理，Service 不偷偷启动 uvicorn。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from fastapi import APIRouter, Request

from .security import same_origin
from .types import RouteContribution


class RouteConflictError(ValueError):
    """不同 Owner 注册同一 method/path/kind 时的显式冲突。"""
    code = "http_route_conflict"


class HttpService:
    """在创建 FastAPI Host 前收集并校验路由贡献。"""
    key = "http"

    def __init__(self) -> None:
        self._routes: list[RouteContribution] = []
        self._frozen = False
        self.restart_required = False
        self._app = None

    @property
    def frozen(self) -> bool:
        """返回 Host 是否已经 materialize；冻结后新增路由需重启。"""
        return self._frozen

    @property
    def app(self):
        """Return the materialized FastAPI Host, when a Gateway created one."""
        return self._app

    def register_router(self, router: APIRouter, owner: str, prefix: str = "/api") -> Callable[[], bool]:
        """Register all paths in a router and return a Fiber-owned disposer."""
        additions = [
            RouteContribution(method=method, path=f"{prefix}{route.path}", owner=owner, kind="router", router=router)
            for route in router.routes
            for method in sorted(route.methods or {"GET"})
        ]
        self._check_conflicts(additions)
        self._routes.extend(additions)
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            if self._frozen:
                self.restart_required = True
            for addition in additions:
                try:
                    self._routes.remove(addition)
                except ValueError:
                    pass
            return True

        return dispose

    def register_health(self, owner: str = "gateway") -> Callable[[], bool]:
        """注册标准健康检查路由，并返回 Fiber disposer。"""
        async def health():
            return {"status": "ok"}

        return self.register_route("GET", "/api/health", health, owner)

    def register_websocket_path(
        self,
        path: str,
        owner: str,
        handler: Callable[..., Any],
    ) -> Callable[[], bool]:
        """Register a WebSocket endpoint owned by a Plugin.

        The HTTP Host materializes the endpoint together with the other route
        contributions.  Keeping the handler in the contribution removes the
        old post-materialization ``attach_app`` bridge from Bootstrap.
        """
        addition = RouteContribution(
            method="WS",
            path=path,
            owner=owner,
            kind="websocket",
            handler=handler,
        )
        self._check_conflicts([addition])
        self._routes.append(addition)
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            if self._frozen:
                self.restart_required = True
            try:
                self._routes.remove(addition)
            except ValueError:
                return False
            return True

        return dispose

    def register_route(self, method: str, path: str, handler: Callable[..., Any], owner: str, kind: str = "exact") -> Callable[[], bool]:
        """Register one exact handler while rejecting owner conflicts."""
        addition = RouteContribution(method=method.upper(), path=path, owner=owner, kind=kind, handler=handler)
        self._check_conflicts([addition])
        self._routes.append(addition)
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            if self._frozen:
                self.restart_required = True
            try:
                self._routes.remove(addition)
            except ValueError:
                return False
            return True

        return dispose

    def _check_conflicts(self, additions: Iterable[RouteContribution]) -> None:
        """Validate method/path/kind uniqueness before mutating the registry."""
        current = self._routes + list(additions)
        seen: dict[tuple[str, str, str], str] = {}
        for item in current:
            key = (item.method, item.path, item.kind)
            previous = seen.get(key)
            if previous is not None and previous != item.owner:
                raise RouteConflictError(f"{item.method} {item.path} owned by {previous} and {item.owner}")
            seen[key] = item.owner

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        """返回路由诊断快照，不暴露 handler/router 引用。"""
        return tuple({"method": r.method, "path": r.path, "kind": r.kind, "owner": r.owner} for r in self._routes)

    def build_app(self, *, title: str = "ftre", version: str = "0.2.4"):
        """Materialize current contributions; callers freeze the registry afterwards."""
        from fastapi import FastAPI

        app = FastAPI(title=title, version=version)
        included: set[int] = set()
        for route in self._routes:
            if route.router is not None:
                identity = id(route.router)
                if identity in included:
                    continue
                included.add(identity)
                app.include_router(route.router, prefix="/api")
            elif route.kind == "websocket" and route.handler is not None:
                app.websocket(route.path)(route.handler)
            elif route.handler is not None:
                app.add_api_route(route.path, route.handler, methods=[route.method])
        app.state.http_service = self
        self._app = app
        return app

    def freeze(self) -> tuple[dict[str, Any], ...]:
        """冻结路由贡献，返回最终快照供 Host 构建或日志记录。"""
        self._frozen = True
        return self.snapshot()

    @staticmethod
    def same_origin(request: Request) -> bool:
        """把来源校验委托给安全辅助模块，保持 HTTP Service 的单一入口。"""
        return same_origin(request)
