"""System Prompt Service 的 Provider Plugin。

这里只创建 section registry；基础 prompt 文本仍来自 Agent profile/config，具体
Feature 通过注册贡献加入，不在 Provider 中硬编码产品提示词。
"""

from __future__ import annotations

from cordis import Context

from .service import SystemPromptService

provide = ("system_prompt",)
inject = ()


def apply(ctx: Context, config=None):
    """发布 Prompt registry；应用级 prompt 由 Agent 配置提供。"""
    if ctx.get("system_prompt", strict=False) is not None:
        return
    service = SystemPromptService()
    ctx.provide("system_prompt", service)
