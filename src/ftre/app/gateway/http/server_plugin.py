"""Optional uvicorn provider; importing this module never starts a socket."""

from __future__ import annotations

from cordis import PluginContext

from .server import UvicornServer

inject = ("http",)
provide = ()


async def apply(ctx: PluginContext, config=None):
    """Build the server lazily and start it only when ``listen`` is explicit."""
    options = config if isinstance(config, dict) else {}
    app = ctx.http.build_app()
    server = UvicornServer(app, options.get("host", "127.0.0.1"), int(options.get("port", 48650)))
    # Server startup is explicitly opt-in; tests and embedders can build a
    # frozen app without opening a socket.
    if options.get("listen", False):
        import asyncio
        task = asyncio.create_task(server.start())
        ctx.effect(lambda: (setattr(server.server, "should_exit", True) if server.server else task.cancel()), label="http:server")
