"""Gateway startup facade used by the CLI and embedders."""

from __future__ import annotations

import asyncio
from typing import Any

from .composition import build_composition


async def start_gateway(*, config: dict[str, Any] | None = None, plugins_dir=None, initial_services=None):
    composition = await build_composition(config, plugins_dir=plugins_dir, initial_services=initial_services)
    http_service = composition.context.get("http")
    if http_service is not None:
        from .http.app import create_app

        composition.http_app = create_app(http_service)
        http_service.freeze()
    return composition


async def run_gateway(*, config: dict[str, Any] | None = None, plugins_dir=None, initial_services=None):
    composition = await start_gateway(config=config, plugins_dir=plugins_dir, initial_services=initial_services)
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await composition.close()
