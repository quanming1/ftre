## 项目约定

- 后端路径：E:\ftre\src\ftre\
- 前端路径：E:\binn\ftre-desktop\
- 文档路径：E:\ftre-docs\
- Agent 核心库：E:\ftre-agent-core\
- 配置目录：C:\Users\蒋全明\.ftre\
- 使用 Python 3.12 + TypeScript
- 日志统一用 logging（Python）、console（前端）

## 仓库关系

```
ftre-agent-core    Agent 核心库（无状态、纯算法）
     │              ReActAgent / LLMHandler / Tool 体系 / Runner
     │              被 ftre 后端 import 使用，不独立部署
     ▼
ftre               Gateway 后端（有状态、长驻进程）
     │              Session 管理 / EventBus / Channel / 插件 / MCP
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

### 一键启动

`E:\ftre\start.bat` 依次启动三个服务：
1. Gateway 后端 → `ws://127.0.0.1:19470/`
2. Desktop 前端 → Electron pnpm dev
3. Docs 文档站 → `http://localhost:5173/`

按任意键停止所有进程。
