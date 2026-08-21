"""Public facade for the in-process business EventBus."""

from __future__ import annotations

from typing import Any

from ftre.bus import EventBus


class MessageBusService:
    """Expose the bus as a Service so channels do not construct global buses."""
    key = "message_bus"

    def __init__(self, bus: EventBus | None = None) -> None:
        self.bus = bus or EventBus()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.bus, name)
