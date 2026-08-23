"""Cron Channel owned by the Schedule Feature.

Cron sessions are internal workers. Their outbound messages are intentionally
discarded here; agents can explicitly use ``send_message`` to reach a real
channel, while ChannelManager still has a valid sink for the cron route.
"""
# Cron Channel：承载内部定时任务的入站/出站适配。
# 出站默认静默丢弃，避免调度消息误发到客户端；但 ChannelManager 仍持有
# 这个合法 sink，cron 路由不会因缺少通道而报错。

from __future__ import annotations

from ftre.services.messaging.channel.base import Channel


class CronChannel(Channel):
    """Silent sink for messages produced by scheduled sessions."""

    def __init__(self, bus) -> None:
        super().__init__(channel_id="cron", name="Cron Channel", bus=bus)

    async def send(self, msg) -> None:
        """Do not echo internal cron output to an external client."""
        return
