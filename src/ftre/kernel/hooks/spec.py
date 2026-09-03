"""重新导出 ftre-agent 提供的 Hook 契约类型。

``ftre`` 不在 Kernel 里复制一份 ``HookSpec`` 或 Hook 模式枚举。真正的契约由
``ftre-agent`` 持有，Runtime 和 Gateway 因而使用同一套类型与校验规则；本文件
只是为了让 ftre 内部的业务 Owner 统一从 ``ftre.kernel.hooks`` 导入。

这里故意没有 ``class HookSpec``、转换函数或兼容别名。若在这里重新实现一份，
运行时很容易出现“看起来同名、实际不相等”的两套协议，破坏 Hook 注册和 dispatch。
"""

from ftre_agent.hooks import HookFailurePolicy, HookMode, HookScope, HookSpec

__all__ = ["HookFailurePolicy", "HookMode", "HookScope", "HookSpec"]
