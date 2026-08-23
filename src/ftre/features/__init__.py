"""Complete product capabilities composed from public Service contracts."""
# 产品 Feature 聚合包：所有能力都通过公开 Service/Hook 接入，
# 不允许反向依赖 App 或其他 Feature 的私有实现（AGENTS.md 边界）。
# 子包划分：context_govern（工作区治理）、plan（计划工具）、skill（技能目录）、
# team（团队状态）、schedule（定时任务）、mcp（MCP 服务器连接与工具）。
