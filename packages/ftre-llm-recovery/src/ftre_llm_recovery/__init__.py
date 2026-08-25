"""ftre LLM 失败恢复策略 Plugin 的公共入口。

这个 Package 不调用 LLM，也不自己执行重试。它只监听 Core 发布的
``llm/error`` Hook，并根据配置返回 ``retry`` 或 ``stop`` 建议。真正的尝试次数、
等待、取消和再次调用始终由 Agent Core 的 Retry Loop 负责。

外部只需要通过 ``ftre.plugins`` entry point 加载 :func:`apply`，不应直接依赖
本包内部的 ``config`` 或 ``policy`` 模块。
"""

from .plugin import apply, inject

__all__ = ["apply", "inject"]
