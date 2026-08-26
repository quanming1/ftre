"""Core tools Provider Plugin：五个内置工具的唯一 Owner。

bash/read/write/edit/set_workspace 在这里作为普通 ToolContribution 注册
（owner="core-tools"），与 skill/plan/schedule 等行为 Plugin 完全同构：
注册走 ToolService.register，清理走各自 Fiber 的 effect，卸载后贡献消失。
"""

from __future__ import annotations

from cordis import Context

from . import (
    create_bash_tool,
    create_edit_tool,
    create_read_tool,
    create_set_workspace_tool,
    create_write_tool,
)

inject = ("tools",)
provide = ()


def apply(ctx: Context, config=None):
    """注册五个内置工具并把每个贡献的清理绑定到当前 Fiber。"""
    # 工厂每次调用产出新实例，view 之间不共享可变状态；label 用工具名
    # 便于在 Fiber 诊断里定位是哪一项贡献的清理。
    for name, factory in (
        ("bash", create_bash_tool),
        ("read", create_read_tool),
        ("write", create_write_tool),
        ("edit", create_edit_tool),
        ("set_workspace", create_set_workspace_tool),
    ):
        disposer = ctx.tools.register(factory(), owner="core-tools", source="builtin")
        ctx.effect(lambda d=disposer: d, label=f"core-tools:{name}")
