# ftre-process

`ProcessService` 是 ftre 的跨平台外部进程边界，统一处理一次性命令、长驻进程、
stdout/stderr、超时、取消、进程组和 Windows 无控制台策略。ftre 自有 Tool、Gateway
和进程型适配器通过这个 Service 启动外部进程，不直接调用 `subprocess`。MCP stdio
由上游 `mcp`/AnyIO transport 创建，ftre 不重复实现传输层，而是在集成验收中确认其
Windows 无窗口和进程树回收策略。
