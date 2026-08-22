"""Public facade for the in-process business EventBus."""

from __future__ import annotations

from .bus import EventBus


class MessageBusService:
    """Expose the bus as a Service so channels do not construct global buses."""
    key = "message_bus"

    def __init__(self, bus: EventBus | None = None) -> None:
        self.bus = bus or EventBus()

    async def publish_inbound(self, message) -> None:
        """Publish a fire-and-forget inbound message through the owned Bus."""
        await self.bus.publish_inbound(message)

    async def request_inbound(self, message):
        """Submit an inbound request and wait for the AgentLoop admission ACK."""
        return await self.bus.request_inbound(message)

    def stop_inbound(self) -> None:
        """Close inbound admission during Gateway shutdown."""
        self.bus.stop_inbound()
