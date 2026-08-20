from __future__ import annotations

from typing import Any


class UvicornServer:
    """Small owned resource wrapper; actual server creation happens at listen time."""

    def __init__(self, app: Any, host: str = "127.0.0.1", port: int = 48650) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.server = None

    async def start(self) -> None:
        import uvicorn

        self.server = uvicorn.Server(uvicorn.Config(self.app, host=self.host, port=self.port, log_config=None))
        await self.server.serve()

    async def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True

