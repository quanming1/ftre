"""
ftre 核心内置工具集（bash / read / write / edit / set_workspace）。

工具是无状态的工厂产物。当前工作区是 sessions 表的一等字段，Agent 每次运行
通过 WorkspaceService 创建的 accessor 放入 runtime_context['workspace']，
同步外观，工具用 Injected("workspace") 拿到它后调 ws.get() / ws.set(...)
读写持久化的 cwd。

F34 起这五个工具由 core-tools Plugin 作为普通贡献注册（owner="core-tools"），
不再由 ToolService.prepare_view 硬编码构造；ToolContribution 记录因此包含
它们，卸载 Plugin 后贡献可逆消失。
"""
from .bash import create_bash_tool
from .edit import create_edit_tool
from .read import create_read_tool
from .set_workspace import create_set_workspace_tool
from .write import create_write_tool

__all__ = [
    "create_bash_tool",
    "create_edit_tool",
    "create_read_tool",
    "create_set_workspace_tool",
    "create_write_tool",
]
