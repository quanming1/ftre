"""Built-in behavior and adapter Plugins."""
# 内置 Plugin 包：由 ftre 仓库维护、可出现在默认 Composition 的 Plugin 集合。
# "内置"仅表示随仓库发布，不表示绕过 Plugin Loader（PRD-F14 §5.3）——
# 每个能力仍走 Manifest/Discovery/Loader/Fiber 生命周期，可逆清理。
# 子目录即 Plugin Owner 清单：command（slash 指令）、channels（WS/Subagent 适配器）、
# core_tools（bash/read/write/edit/set_workspace 内置工具）、
# mcp/skill/schedule/plan/trace/session_title/context_govern（产品行为）、
# tool_audit（tool/after 审计日志）。业务工具另由仓内 packages 的
# ftre-{messaging,task,team} 拥有（PRD-F18/F34）。
