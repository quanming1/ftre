"""OpenAI 协议 Provider Plugin 的内部实现目录。

具体 Adapter 由 ``plugin.py`` 注册到公开 ``llm`` Service；包根不再重新导出
concrete class，避免消费者绕过 Plugin 生命周期直接实例化协议实现。
"""

__all__ = []
