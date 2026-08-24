"""ftre 公共运行时能力层。

子包中的 Service 是跨 Feature 共享的状态 Owner；外部只应依赖稳定 key/公开门面，
而不是导入 Provider 或数据面内部实现。
"""
