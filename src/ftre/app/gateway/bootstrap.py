"""Gateway startup facade used by the CLI and embedders."""
# Gateway 启停边界：创建唯一 Composition、构建 FastAPI Host，并在关闭时按依赖逆序释放资源。
#
# 与 composition.py 的分工：
#   - composition.py 负责"装配"（Plugin 清单 + Service 生命周期）；
#   - 本模块只负责进程级 Host/Runtime 启停和逆序关停，不创建业务 Service。

from __future__ import annotations

import asyncio
from typing import Any

from .composition import build_composition


async def start_gateway(*, config: dict[str, Any] | None = None, plugins_dir=None, initial_services=None):
    """Build a composition and materialize its HTTP Host for embedders/tests."""
    # 轻量启动入口：只做"组装 + 物化 HTTP"，不开数据面（AgentLoop/WS）。
    # 供嵌入式场景（测试、需要自管数据面的宿主）使用：
    #   1. 调 build_composition 完成 Plugin 装载（不监听端口）；
    #   2. 若 http 服务存在，把路由物化成 FastAPI 并 freeze（冻结后不可再注册路由）；
    #   3. 返回 Composition 句柄，由调用方决定何时 close()。
    composition = await build_composition(config, plugins_dir=plugins_dir, initial_services=initial_services)
    http_service = composition.context.get("http")
    if http_service is not None:
        from .http.app import create_app

        composition.http_app = create_app(http_service)
        channels = composition.context.get("channels", strict=False)
        websocket = channels.manager.get("ws") if channels is not None else None
        if websocket is not None and hasattr(websocket, "attach_app"):
            websocket.attach_app(composition.http_app)
        http_service.freeze()
    return composition


async def run_gateway(*, config: dict[str, Any] | None = None, plugins_dir=None, initial_services=None):
    """Keep an embedded Gateway alive until cancellation, then close it."""
    # 最简单的驻留模式：组装后无限 sleep，直到外部取消（Ctrl+C / task.cancel），
    # finally 里逆序关停所有 Plugin Fiber。适用于测试与最小嵌入式宿主。
    composition = await start_gateway(config=config, plugins_dir=plugins_dir, initial_services=initial_services)
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await composition.close()


async def run_gateway_runtime(*, port: int | None = None, host: str | None = None, config: dict[str, Any] | None = None, plugins_dir=None):
    """Start the real Gateway data plane through the new Composition Root.

    The existing AgentLoop and WebSocket protocol are retained as runtime
    providers; they receive public Service handles from Composition and are
    stopped by the returned root cleanup path.
    """
    # 真实 Gateway 数据面启动入口（CLI `ftre gateway` 走这里）。
    #
    # 运行时只读取 Composition Context 的公开 Service；全程 try/finally，
    # 即使中途失败，已启动的 Runtime/Plugin 资源也全部逆序释放。
    from ftre.services.config.loader import load_config_file

    # 加载配置（外部传入或读 ~/.ftre/config.json）
    config_data = dict(config) if config is not None else load_config_file()
    # 监听地址属于 WebSocket Provider 的配置，不再由 Bootstrap 持有一个
    # 第二份 Channel 对象；这里仅把 CLI 覆盖值写入该 Plugin 的 manifest。
    servers = config_data.get("servers")
    gateway = servers.get("gateway") if isinstance(servers, dict) else None
    gateway = dict(gateway) if isinstance(gateway, dict) else {}
    configured_host = gateway.get("host") if isinstance(gateway.get("host"), str) else "127.0.0.1"
    configured_port = gateway.get("port") if isinstance(gateway.get("port"), int) else 48650
    ws_config = {"host": host or configured_host, "port": port or configured_port}
    plugin_entries = [dict(item) for item in config_data.get("plugins", ()) if isinstance(item, dict)]
    for item in plugin_entries:
        if str(item.get("id") or item.get("name")) == "websocket-channel":
            item["config"] = {**dict(item.get("config") or {}), **ws_config}
            break
    else:
        plugin_entries.append({"id": "websocket-channel", "config": ws_config})
    config_data["plugins"] = plugin_entries
    # 预置变量：None 哨兵供 finally 判断"是否已创建、是否需要释放"
    channel_service = None
    composition = None
    try:
        # Plugin 负责创建并初始化业务 Service；Bootstrap 只取得公开句柄并
        # 启动 Host/Runtime，不再手工 new Session、Bus、Channel、Agent 或 Tool。
        composition = await start_gateway(
            config=config_data,
            plugins_dir=plugins_dir,
        )
        context = composition.context
        channel_service = context.channels
        # Agent Provider Plugin 已完成 AgentService 与私有 Runtime 装配；
        # Bootstrap 只启动 Host 通道，不保存第二份 Agent 对象图。
        await channel_service.start_all()
        composition.context.get("http").freeze()  # 冻结路由：此后不可再注册
        # 常驻：直到外部取消（Ctrl+C / 进程信号）
        while True:
            await asyncio.sleep(1)
    finally:
        # ── 逆序关停（与启动顺序相反，保证依赖方先停）──
        if channel_service is not None:
            await channel_service.stop_all()  # 停通道接收循环
        if composition is not None:
            await composition.close()  # 逆序释放全部 Plugin Fiber（effect 清理）
