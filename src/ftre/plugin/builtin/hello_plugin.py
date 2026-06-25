"""
示例 Plugin — Hello Channel

演示如何通过 Plugin 注册一个自定义 Channel。
将此文件放到 plugins_dir 目录下即可自动加载。

这个 Channel 不做实际通信，只是在启动时打印一条日志，
证明 Plugin 体系能正常工作。
"""
import logging

from ftre.plugin import Plugin
from ftre.channel import Channel
from ftre.bus import BusMessage

logger = logging.getLogger(__name__)


class HelloChannel(Channel):
    """一个什么都不做的示例 Channel"""

    def __init__(self, bus, greeting: str = "Hello from Plugin!"):
        super().__init__(channel_id="hello", name="Hello Channel", bus=bus)
        self.greeting = greeting

    async def start(self) -> None:
        logger.info(f"[hello-channel] {self.greeting}")

    async def send(self, msg: BusMessage) -> None:
        # 示例：只打印，不实际发送
        logger.info(f"[hello-channel] 收到 outbound: {msg.type} → {msg.to_session}")


class HelloPlugin(Plugin):
    """示例插件：注册一个 Hello Channel"""
    name = "hello"
    version = "0.1.0"

    def setup(self) -> None:
        greeting = self.api.config.get("greeting", "Hello, ftre Plugin System!")
        channel = HelloChannel(bus=self.api.bus, greeting=greeting)
        self.api.register_channel(channel)

    def teardown(self) -> None:
        logger.info("[hello-plugin] 已卸载")
