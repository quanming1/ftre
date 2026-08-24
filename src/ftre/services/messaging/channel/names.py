"""Stable protocol channel names shared by Providers and built-in tools."""
# 稳定的协议级 Channel 名常量：供 Channel Provider 与内置工具共享。
# 名字一旦发布即成为 wire 兼容面（PRD-F14 §10），改动必须走跨仓库评审。

# 内部 subagent 通道的固定 channel_id：task/team 工具投递消息时使用，
# 不暴露给外部客户端（WS 客户端 metadata 白名单不含它）。
SUBAGENT_CHANNEL_ID = "subagent"

__all__ = ["SUBAGENT_CHANNEL_ID"]
