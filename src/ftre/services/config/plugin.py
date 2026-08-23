"""配置 Service 的 Provider Plugin。

配置是许多 Plugin 的上游依赖，因此它必须先于业务 Plugin 发布；若 Composition
已提供测试/嵌入环境专用实例，Provider 不覆盖它。
"""

from __future__ import annotations

from typing import Any

from cordis import Context

from .service import ConfigService

inject = ("http",)
provide = ("config",)


async def apply(ctx: Context, config: dict[str, Any] | None = None):
    """Create the config owner; Composition-injected instances take precedence."""
    existing = ctx.get("config", strict=False)
    if existing is None:
        # PluginManager passes the composition snapshot directly. ConfigService
        # is the sole owner of that snapshot; bootstrap must not seed a copy.
        service = ConfigService(initial=config if isinstance(config, dict) else None)
        ctx.provide("config", service)
    else:
        service = existing
    from .router import build_router

    disposer = ctx.http.register_router(build_router(service), owner="config")
    ctx.effect(lambda: disposer, label="http:config")
