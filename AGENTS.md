## 项目约定

- 后端路径：E:\ftre\src\ftre\
- 前端路径：E:\binn\ftre-desktop\
- 文档路径：E:\ftre-docs\
- Agent 核心库：E:\ftre-agent-core\
- Octo 插件路径：C:\Users\蒋全明\.ftre\plugins\octo-plugin\
- 配置目录：C:\Users\蒋全明\.ftre\
- 使用 Python 3.12 + TypeScript
- 日志统一用 logging（Python）、console（前端）

## Git 规范

- **禁止私自 commit / push**：除非用户明确要求（如"commit"、"push"、"提交"），否则只改代码不提交
- **回滚需确认**：回滚前必须告知用户回滚的内容、范围和影响，得到确认后再执行
- **push 前先 commit**：不要把未 commit 的改动直接 push
- **多仓库联动**：改 core 后同步验证 ftre 后端，改前端后同步验证后端 API
- **操作不同仓库时用 `set_workspace` 显式切换**：`cd A && git ...` 组合命令中的 `cd` 不改变 bash 工具工作区，曾导致 `git init` 误在错误仓库执行

## 仓库关系

```
ftre-agent-core    Agent 核心库（无状态、纯算法）
      │              ReActAgent / LLMHandler / Tool 体系 / Runner
      │              被 ftre 后端 import 使用，不独立部署
      │
      ├── ftre-octo-plugin  Octo IM 外部插件（生态重要组成部分）
      │                     真实路径：C:\Users\蒋全明\.ftre\plugins\octo-plugin
      │                     Python + Node 混合项目：WuKongIM 桥接 / Octo Channel / octo_management Tool
      │                     通过 shim `C:\Users\蒋全明\.ftre\plugins\octo_channel.py` 被 Gateway 扫描加载
      ▼
ftre               Gateway 后端（有状态、长驻进程）
      │              Session 管理 / EventBus / Channel / 插件 / MCP
      │              内置插件：skill、mcp、context_govern、title_gen
      │              内置工具：bash / read / write / edit / set_workspace / cron / task / send_message
      │              对 desktop 提供 WebSocket + HTTP API
      ▼
ftre-desktop        Desktop 客户端（Electron + React）
      │              GUI 体验：聊天界面、编辑器、Inspector 面板、文件树、设置
      │              通过 WebSocket 与后端通信
      ▼
ftre-docs          文档站（React + Vite）
                     Markdown 源文件在 src/content/，侧边栏自动渲染
                     独立部署，不依赖后端
```

## 多 Agent 架构

每个 agent 有独立配置目录 `~/.ftre/agents/<agent_id>/`：

```
~/.ftre/agents/<agent_id>/
  ├── agent.config.json    # LLM、tools、workspace、mcp、plugins、disabled_skills
  ├── SOUL.md              # 人设（追加到全局 system_prompt 之后）
  ├── AGENTS.md            # 项目约定（context_govern 注入）
  ├── USER.md              # 用户偏好（追加到 SOUL.md 之后）
  └── skills/              # Agent 私有 Skill（同名覆盖全局）
```

### 配置合并规则（AgentManager.\_load_and_merge）

| 字段 | 合并策略 |
| --- | --- |
| llm | provider + model 可覆盖，api_key/base_url/vision 始终用全局 |
| tools | 整体替换（写了就用 agent 的，不写则全部可用） |
| workspace | Agent 的"家目录"（存放 prompt 文件的路径，不是对话 cwd） |
| mcp | 深度合并（按 server name 为 key，agent 覆盖全局） |
| plugins | 按 name 合并（同名 agent 覆盖全局，全局有但 agent 没提的保留） |
| disabled_skills | 整体替换（agent 写了就用 agent 的，不写则用全局） |

## Hook 系统

全异步 filter chain，`HookManager.trigger()` 为 `async def`，自动 `await` coroutine 返回值。

| 挂点 | 触发时机 | 上下文 | 典型用途 |
| --- | --- | --- | --- |
| `before_messages_build` | events 加载后、to_openai_messages 前 | `MessagesBuildContext`（可改 events/config） | context_govern：事件流治理 + AGENTS.md 注入 |
| `before_agent_run` | Agent 创建后、agent.run() 前 | `AgentRunContext`（可改 messages，含 agent_profile + agent_tool_registry） | MCP/Skill：系统提示词注入 + 私有 MCP 工具注册 |

调用点在 `loop.py`，两处均 `await self.hook_manager.trigger(...)`。

## 插件体系

内置插件（`src/ftre/plugin/builtin/`）随代码仓库发布，无需用户手动安装：

| 插件 | 职责 |
| --- | --- |
| `skill` | Skill 管理（loadSkill 工具、Skill CRUD API、system prompt 注入、per-agent 私有 skill 支持） |
| `mcp` | MCP 服务器管理（公共+私有双层配置、连接池、工具注册、CRUD API、config watcher） |
| `context_govern` | 上下文治理（AGENTS.md 双注入、工具事件配对/去重/悬挂清理） |
| `title_gen` | 标题生成（首条消息自动生成会话标题） |

插件通过 `FtrePluginApi` 注册能力：
- `self.api.tool_registry` — 注册工具（全局共享）
- `self.api.append_system_prompt(...)` — 注入 system prompt
- `self.api.register_router(APIRouter)` — 注册 HTTP 路由
- `self.api.register_hook(...)` — 注册 hook（所有 hook 回调必须为 `async def`）

外部插件目录 `~/.ftre/plugins/` 仍保留作为扩展点，`PluginManager` 先加载内置插件再扫描外部目录。

### MCP 双层配置

| 层级 | 配置来源 | 注册位置 | 连接管理 |
| --- | --- | --- | --- |
| 公共 MCP | `config.json` 的 `mcp` 段 | 全局 `tool_registry`（所有 agent 共享） | 启动时 `start_and_register` + config watcher 热重载 |
| 私有 MCP | `agent.config.json` 的 `mcp` 段 | per-agent `ctx.agent_tool_registry` | `BEFORE_AGENT_RUN` hook 中 `ensure_connections` 按需连接 |

连接池全局共享（`McpManager._connections`），按 server name 去重。`ensure_connection` 已连接且配置相同则复用，不二次加载。私有 MCP 工具注册到 per-agent registry，不污染全局。

HTTP API 通过 `?scope=global|private&agent_id=xxx` 区分操作目标。

### context_govern 事件流治理

新协议下 toolCall 嵌在 `assistant_message_complete` 的 `content[]` 中（不再独立事件），tool_result 是独立事件。治理三步：

1. **孤立刻重**：`_drop_orphan_tool_events` — 丢弃无匹配 toolCall 的 tool_result，从 content[] 移除无匹配 tool_result 的 toolCall block
2. **去重**：`_dedup_tool_events` — 同一 call id 只保留第一个 toolCall block 和第一条 tool_result
3. **悬挂丢弃**：`_drop_dangling_tool_results` — 裁剪后 toolCall 已被裁掉但 tool_result 残留的，丢弃

AGENTS.md 注入两份（叠加）：`agent_dir/AGENTS.md`（Agent 行为规则）+ `workspace/AGENTS.md`（项目约定）。

### 重要外部插件

- **Octo 插件**：`C:\Users\蒋全明\.ftre\plugins\octo-plugin\`
  - 这是 ftre 生态的重要组成部分，不是临时脚本目录
  - 改动 Octo IM / WuKongIM / 外部消息通道相关需求时，优先检查这里
  - 入口 shim：`C:\Users\蒋全明\.ftre\plugins\octo_channel.py`
  - 项目内也有自己的 `AGENTS.md` 和 `README.md`，进入该目录工作前先阅读
  - 该项目是独立 git 仓库，和 `E:\ftre` 主仓库分开管理

### 启动方式

两个终端：

```
# 终端 1 — 后端
ftre gateway

# 终端 2 — 客户端（ftre-desktop 仓库）
cd E:\binn\ftre-desktop && pnpm dev
```

打包模式：Electron 自动 spawn 内嵌 Python 后端，无需手动启动。

### CLI 入口点

`pyproject.toml` 注册了 `ftre = "ftre.main:app"`，`pip install -e .` 后在 `Scripts/` 生成 `ftre.exe`。

- `ftre.exe` 只是个启动器，内容固定就是 `from ftre.main import app; app()`，不随代码更新变化
- editable 模式通过 `site-packages/__editable__.ftre-0.1.0.pth` 指向 `src/` 源码目录，改代码直接生效
- **只有改 `pyproject.toml` 的 `[project.scripts]` 入口点名时才需要重新 `pip install -e .`**
- `ftre.exe` 所在的 `Scripts/` 目录需加入用户 PATH

### 内置工具

定义在 `src/ftre/tools/`，`build_default_tools()` 在 `agent_manager._build_agent()` 中按 Agent 配置构建 + 裁剪：

| 工具 | 说明 |
| --- | --- |
| `bash` | 执行 shell 命令，纯 cd 拦截持久切换工作区，RTK 自动重写减少 token，semble 语义检索集成 |
| `read` | 读取文件/图片/目录，返回 `(result_str, metadata)` 元组，metadata 含内容快照（file/content/start_line/end_line） |
| `write` | 创建/覆盖文件，保留原编码和换行风格，返回 `(result_str, diff_metadata)` |
| `edit` | 字符串模式 + 行号模式修改文件，返回 `(result_str, diff_metadata)`（before/after/diff/additions/deletions） |
| `set_workspace` | 切换 session 工作区（持久到 DB） |
| `cron` | 定时任务管理（`~/.ftre/cron/`，CronScheduler 30s 扫描） |
| `task` | 派发子任务到 subagent session 同步执行（防递归） |
| `send_message` | 跨 session 消息（notify 通知 / invoke 唤起） |

`ToolHandler.run_one()` 支持 `str` / `AgentEvent` / `tuple[str, dict]` 三种返回值，`react_runner` 在 `tool_result_event()` 中透传 `metadata=result.metadata`。

### Inspector 面板

Desktop 右侧扩展面板（`features/inspector/`），read/edit/write 工具完成后点击打开：

| Tab 类型 | 数据来源 | 展示方式 |
| --- | --- | --- |
| `file` | read 工具 `metadata.content` 内容快照 | Monaco 只读编辑器，不回读磁盘 |
| `diff` | edit/write 工具 `metadata.before`/`after` | Monaco side-by-side diff |
| `image` | 文件树点击图片文件 | `<img>` base64 data URL（IPC `fs:readImageBase64`） |

Tab 按 `toolCallId` 去重（per-tool，不是 per-file），重复点击跳转到已有 tab 并重新定位。

**文件树侧边栏**：tab bar 左侧按钮开关，懒加载目录（`fs:readDir`），vscode-icons 图标，git 状态标记（文件名/目录名按状态染色）。

**Git 轮询**：`git:poll` IPC 采用协商缓存设计——Phase 1 stat `.git/index` + `.git/HEAD` 拼 etag（<1ms），客户端带 `lastEtag` 比较；变了才走 Phase 2（`git status --porcelain` + `git diff --numstat`）。1 秒轮询，每 5 次强制走 Phase 2 兜底外部编辑器改文件。

**Changes 节点**：虚拟节点平铺所有 git 变更文件，显示状态字母（M/A/D/R/U/C）+ 增删行数（`+N -M`），点击 modified/added/deleted 打开 diff 预览。
