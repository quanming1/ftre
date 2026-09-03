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
        # ``config`` 是该 Plugin 的 manifest 局部配置，不是完整的
        # ~/.ftre/config.json。ConfigService 必须自己读取配置文件；否则默认的
        # 空 manifest dict 会覆盖真实 providers/models，客户端就会看到空模型列表。
        service = ConfigService()
        ctx.provide("config", service)
    else:
        service = existing
    options = config if isinstance(config, dict) else {}
    start_watcher = getattr(service, "start_watcher", None)
    if callable(start_watcher):
        start_watcher(float(options.get("watch_interval", 1.0)))
    from .router import build_router

    disposer = ctx.http.register_router(build_router(service), owner="config")
    ctx.effect(lambda: disposer, label="http:config")
    close = getattr(service, "close", None)
    if callable(close):
        ctx.effect(lambda: close, label="config:close")
