"""Route contribution registry; deliberately not an HTTP server."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from fastapi import APIRouter, Request

from .security import same_origin
from .types import RouteContribution


class RouteConflictError(ValueError):
    code = "http_route_conflict"


class HttpService:
    key = "http"

    def __init__(self) -> None:
        self._routes: list[RouteContribution] = []
        self._frozen = False
        self.restart_required = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register_router(self, router: APIRouter, owner: str, prefix: str = "/api") -> Callable[[], bool]:
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
        async def health():
            return {"status": "ok"}

        return self.register_route("GET", "/api/health", health, owner)

    def register_route(self, method: str, path: str, handler: Callable[..., Any], owner: str, kind: str = "exact") -> Callable[[], bool]:
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
        current = self._routes + list(additions)
        seen: dict[tuple[str, str, str], str] = {}
        for item in current:
            key = (item.method, item.path, item.kind)
            previous = seen.get(key)
            if previous is not None and previous != item.owner:
                raise RouteConflictError(f"{item.method} {item.path} owned by {previous} and {item.owner}")
            seen[key] = item.owner

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        return tuple({"method": r.method, "path": r.path, "kind": r.kind, "owner": r.owner} for r in self._routes)

    def router_objects(self) -> tuple[APIRouter, ...]:
        result: list[APIRouter] = []
        seen: set[int] = set()
        for route in self._routes:
            if route.router is not None and id(route.router) not in seen:
                seen.add(id(route.router))
                result.append(route.router)
        return tuple(result)

    def build_app(self, *, title: str = "ftre", version: str = "0.2.4"):
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
            elif route.handler is not None:
                app.add_api_route(route.path, route.handler, methods=[route.method])
        app.state.http_service = self
        return app

    def freeze(self) -> tuple[dict[str, Any], ...]:
        self._frozen = True
        return self.snapshot()

    @staticmethod
    def same_origin(request: Request) -> bool:
        return same_origin(request)
