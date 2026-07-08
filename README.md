# FTRE

FTRE 是本地运行的 AI 编程助手，由四个关联项目组成：

| 项目 | 仓库 | 职责 |
|---|---|---|
| ftre-agent-core | [quanming1/ftre-agent-core](https://github.com/quanming1/ftre-agent-core) | ReAct、LLM、Tool 与 tracing 核心（无状态纯算法库） |
| ftre | [quanming1/ftre](https://github.com/quanming1/ftre) | Gateway 后端：Session、Channel、MCP、插件、HTTP API |
| ftre-desktop | [quanming1/ftre-desktop](https://github.com/quanming1/ftre-desktop) | Electron + React 桌面客户端 |
| ftre-docs | [quanming1/ftre-docs](https://github.com/quanming1/ftre-docs) | 文档站（React + Vite） |

```
ftre-agent-core    Agent 核心库（无状态、纯算法）
      │              ReActAgent / LLMHandler / Tool 体系 / Runner
      │              被 ftre 后端 import 使用，不独立部署
      │
      ├── ftre-octo-plugin  Octo IM 外部插件
      │                     WuKongIM 桥接 / Octo Channel
      │
      ▼
ftre               Gateway 后端（有状态、长驻进程）
      │              Session 管理 / EventBus / Channel / 插件 / MCP
      │              内置插件：skill、mcp、context_govern、title_gen
      │              对 desktop 提供 WebSocket + HTTP API
      ▼
ftre-desktop        Desktop 客户端（Electron + React）
                     GUI 体验：聊天界面、编辑器、Inspector 面板、设置
                     通过 WebSocket 与后端通信
      ▼
ftre-docs          文档站（独立部署，不依赖后端）
```

## 启动

```powershell
py -3.12 E:\ftre\start.py
```

`start.py` 依次启动三个服务：

1. **Gateway 后端** → `ws://127.0.0.1:48650/`
2. **Desktop 前端** → Electron + Vite dev server
3. **Docs 文档站** → `http://localhost:48652/`

## 核心特性

### 内置工具

8 个内置工具（`src/ftre/tools/`）：bash、read、write、edit、set_workspace、cron、task、send_message。

- **read/write/edit** 返回 `(result_str, diff_metadata)` 元组，前端 Inspector 面板直接展示 diff 预览和文件快照
- **bash** 支持 RTK 自动重写（减少 token 消耗）、semble 语义代码检索集成
- **task** 子 Agent 模式，把任务派发给独立 session 同步执行
- 工具按 Agent 配置裁剪（`tools.allow` / `tools.deny`）

### 多 Agent 架构

每个 Agent 有独立配置目录 `~/.ftre/agents/<agent_id>/`，支持独立 LLM、工具、MCP、Skill、工作区配置。

### MCP 双层配置

| 层级 | 配置来源 | 注册位置 |
|------|----------|----------|
| 公共 MCP | `config.json` 的 `mcp` 段 | 全局 `tool_registry`（所有 Agent 共享） |
| 私有 MCP | `agent.config.json` 的 `mcp` 段 | per-agent `tool_registry` |

### Inspector 面板

Desktop 右侧扩展面板，支持：
- **文件预览**：Monaco 编辑器只读渲染，内容快照来自 read 工具 metadata
- **Diff 预览**：edit/write 工具点击打开，side-by-side diff 视图
- **文件树侧边栏**：工作区目录浏览，git 状态标记（协商缓存轮询），图片预览
- **Changes 节点**：平铺所有 git 变更文件，显示增删行数和状态标记

### Hook 系统

全异步 filter chain，两个挂点：
- `before_messages_build`：事件流治理 + AGENTS.md 注入
- `before_agent_run`：MCP/Skill 系统提示词注入 + 私有工具注册

### 插件体系

内置 4 个插件（随代码发布）：`skill`、`mcp`、`context_govern`、`title_gen`。外部插件目录 `~/.ftre/plugins/`。

## 配置

全局配置：`~/.ftre/config.json`

Agent 配置：`~/.ftre/agents/<agent_id>/agent.config.json`

文档：[ftre-docs](https://github.com/quanming1/ftre-docs)

## 技术栈

- **后端**：Python 3.12 + asyncio
- **前端**：TypeScript + React + Electron + Vite
- **编辑器**：Monaco Editor
- **LLM**：OpenAI 兼容 API（通过 ftre-agent-core 的 LLMHandler）

## Agent Tracing

Gateway 为每次 Agent 执行自动记录树状 Trace：

```
agent
├── llm
├── tool
└── llm
```

Trace 文件：`~/.ftre/traces/agent-traces.jsonl`

Desktop 左侧 **追踪** 面板可查看 Trace 列表、Run 树和完整详情。

只读 API：

- `GET /api/traces?limit=100`：最近 Trace 摘要
- `GET /api/traces/{trace_id}`：单个 Trace 的 Run 树
- `GET /api/traces/{trace_id}/runs/{run_id}`：单个 Run 的完整 payload

> Trace 包含完整提示词和工具输入输出。共享或归档 JSONL 文件前应检查敏感信息。
