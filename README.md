# FTRE

FTRE 是本地运行的 AI 编程助手，由四个关联项目组成：

| 项目 | 路径 | 职责 |
|---|---|---|
| ftre-agent-core | `E:\ftre-agent-core` | ReAct、LLM、Tool 与 tracing 核心 |
| ftre | `E:\ftre` | Gateway、Session、Channel、MCP 与 HTTP API |
| ftre-desktop | `E:\binn\ftre-desktop` | Electron + React 桌面客户端 |
| ftre-docs | `E:\ftre-docs` | 文档站 |

## 启动

```powershell
py -3.12 E:\ftre\start.py
```

默认服务：

- Gateway：`ws://127.0.0.1:19470/`
- Desktop：Electron + Vite
- Docs：`http://localhost:5173/`

## Agent Tracing

Gateway 会为每次 Agent 执行自动记录树状 Trace：

```text
agent
├── llm
├── tool
└── llm
```

Trace 文件位于：

```text
C:\Users\<用户名>\.ftre\traces\agent-traces.jsonl
```

记录内容包括 Agent/LLM/Tool 输入输出、耗时、错误、token usage、
`finish_reason`、工具调用数量，以及 provider 返回的模型标识。

查看方式：启动 Gateway 和 Desktop，在 Desktop 左侧点击 **追踪**。页面每 3 秒自动刷新，
提供 Trace 列表、Run 树和完整详情，并单独标记 `stop / no tool` 情况。

只读 API：

- `GET /api/traces?limit=100`：最近 Trace 摘要
- `GET /api/traces/{trace_id}`：单个 Trace 的轻量 Run 树
- `GET /api/traces/{trace_id}/runs/{run_id}`：按需读取单个 Run 的完整 payload

Trace 包含完整提示词和工具输入输出。共享或归档 JSONL 文件前应先检查敏感信息。
