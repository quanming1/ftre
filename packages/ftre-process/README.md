# ftre-process

`ProcessService` 是 ftre 的跨平台外部进程边界。它统一处理一次性命令、长驻子进程、
stdout/stderr、超时、取消、进程组和 Windows 无控制台策略；ftre 自有 Tool、Gateway
和进程型 Host Plugin 通过服务接口消费它，不直接依赖 `subprocess`。MCP stdio 保留
上游 `mcp`/AnyIO transport 作为显式外部边界，并通过集成测试验证其 Windows 策略。
