## 项目约定

- 后端路径：E:\ftre\src\ftre\
- 前端路径：E:\binn\ftre-desktop\
- 文档路径：E:\ftre-docs\
- Agent 核心库：E:\ftre-agent-core\
- 配置目录：C:\Users\蒋全明\.ftre\
- 使用 Python 3.12 + TypeScript
- 日志统一用 logging（Python）、console（前端）

## Git 规范

- **禁止私自 commit / push**：除非用户明确要求（如"commit"、"push"、"提交"），否则只改代码不提交
- **回滚需确认**：回滚前必须告知用户回滚的内容、范围和影响，得到确认后再执行
- **push 前先 commit**：不要把未 commit 的改动直接 push
- **多仓库联动**：改 core 后同步验证 ftre 后端，改前端后同步验证后端 API

## 仓库关系

```
ftre-agent-core    Agent 核心库（无状态、纯算法）
     │              ReActAgent / LLMHandler / Tool 体系 / Runner
     │              被 ftre 后端 import 使用，不独立部署
     ▼
ftre               Gateway 后端（有状态、长驻进程）
     │              Session 管理 / EventBus / Channel / 插件 / MCP
     │              内置插件：skill、mcp、context_govern、title_gen
     │              对 desktop 提供 WebSocket + HTTP API
     ▼
ftre-desktop        Desktop 客户端（Electron + React）
     │              GUI 体验：聊天界面、设置、MCP 面板、TokenRing
     │              通过 WebSocket 与后端通信
     ▼
ftre-docs          文档站（React + Vite）
                    Markdown 源文件在 src/content/，侧边栏自动渲染
                    独立部署，不依赖后端
```

## 插件体系

内置插件（`src/ftre/plugin/builtin/`）随代码仓库发布，无需用户手动安装：

| 插件 | 职责 |
| --- | --- |
| `skill` | Skill 管理（loadSkill 工具、Skill CRUD API、system prompt 注入） |
| `mcp` | MCP 服务器管理（连接、工具注册、CRUD API、config watcher） |
| `context_govern` | 上下文治理（AGENTS.md 注入、工具事件配对校验） |
| `title_gen` | 标题生成（首条消息自动生成会话标题） |

插件通过 `FtrePluginApi` 注册能力：
- `self.api.tool_registry` — 注册工具
- `self.api.append_system_prompt(...)` — 注入 system prompt
- `self.api.register_router(APIRouter)` — 注册 HTTP 路由
- `self.api.register_hook(...)` — 注册 hook

外部插件目录 `~/.ftre/plugins/` 仍保留作为扩展点，`PluginManager` 先加载内置插件再扫描外部目录。

### 一键启动

`E:\ftre\start.bat` 依次启动三个服务：
1. Gateway 后端 → `ws://127.0.0.1:19470/`
2. Desktop 前端 → Electron pnpm dev
3. Docs 文档站 → `http://localhost:5173/`