"""ftre 最后一次 LLM 模型切换 Plugin 的公共入口。

本包不改变 Agent 的主模型，也不实现 Retry Loop。它包装每次 ``llm/stream``，前面的
attempt 原样交给 Core 重试；只有最后一次 attempt 在尚未提交任何输出时失败，才调用
配置中的备用模型。

外部加载器只需要使用 :func:`apply`、``inject`` 和 ``provide``，其余模块均为包内实现。
"""

from .plugin import apply, inject, provide

__all__ = ["apply", "inject", "provide"]
