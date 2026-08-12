## 项目约定

- 后端路径：E:\ftre\src\ftre\
- 前端路径：E:\binn\ftre-desktop\
- 文档路径：E:\ftre-docs\
- Agent 核心库：E:\ftre-agent-core\
- Octo 插件路径：C:\Users\蒋全明\.ftre\plugins\octo_plugin\
- 配置目录：C:\Users\蒋全明\.ftre\
- 使用 Python 3.12 + TypeScript
- 日志统一用 logging（Python）、console（前端）

## Git Flow 规范（强制）

- **禁止私自 commit / push**：除非用户明确要求（如"commit"、"push"、"提交"），否则只改代码不提交
- **回滚需确认**；**push 前先 commit**；**多仓库联动**（改 core 后验证后端，改前端后验证 API）
- **跨仓库操作必须 `set_workspace` 显式切换**：`cd A && git ...` 中的 `cd` 不改变 bash 工具工作区

分支模型：`master`（仅发布，永不直接提交）← `develop`（默认基底）← `feature/<阶段id>-<name>` / `prd-update` / `todos-update` / `release/<ver>` / `hotfix/<name>`

- 默认工作分支是 **develop**；master 永不直接提交；develop 禁止直接 commit，只接受 `feature/*` → `git merge --no-ff` 合入
- **feat/fix 分支名必须关联 TODO 阶段 id**（如 `feature/A2-config`），提交 scope 与分支名阶段 id 必须一致（commit-msg hook 强制）
- 提交格式 `<type>(<scope>): <subject>`，subject 中文；feat/fix/prd/todos 的 scope 必须是 `docs/TODO.yaml` 中真实存在的阶段 id；其他 type 的 scope 用 `.githooks/.scopes` 白名单模块名
- 合并：`feature/* → develop` 用 `--no-ff`；develop → master 走 `release/*`；**禁止 rebase 已推送历史**
- 本地强制：`.githooks/commit-msg`（提交校验）+ `.githooks/pre-push`（master 保护 + develop merge-only）；`merge:`/`revert:` 开头系统提交跳过；hook 完整规则见 `docs/COMMIT.md`
- 标准流程：`checkout develop → checkout -b feature/<阶段id>-<task> → 开发+测试 → commit → merge --no-ff → push develop`

## PRD 驱动开发（强制）

- **先 PRD，后开发**：TODO 阶段开工前先在 `docs/prd/` 建 PRD（从 `PRD-TEMPLATE.md` 复制）并定稿 `approved`
- **PRD 是唯一依据**：需求/实现/测试/验收全部对照 PRD；验收按 PRD「验收标准」逐条核对，全部通过才算完成
- 流程详见 `docs/PROCESS.md`；阶段 id 与状态见 `docs/TODO.yaml`

## 仓库关系

```
ftre-agent-core    Agent 核心库（无状态、纯算法）被后端 import，不独立部署
      ├── ftre-octo_plugin  Octo IM 外部插件（Python+Node：WuKongIM 桥接/Octo Channel/octo_management Tool）
      ▼                    （shim: ~/.ftre/plugins/octo_channel.py 被 Gateway 扫描加载）
ftre               Gateway 后端（有状态、长驻进程）：Session 管理 / EventBus / Channel / 插件 / MCP
      ▼
ftre-desktop       Desktop 客户端（Electron+React），WebSocket 与后端通信
      ▼
ftre-docs          文档站（React+Vite），独立部署
```

## 核心架构

### AgentLoop SessionLane（后端消息处理）

`Channel → EventBus → AgentLoop`；`AgentLoop` 内按 session_id 建独立 `SessionLane`（单 session actor，负责 FIFO/取消/压缩门控/状态发布）。协作组件：`MailboxStore`（持久化 pending）、`ContextGate`（领取前水位检查）、`CompactManager`（共享压缩）、`TurnExecutor`（只执行已领取 turn，返回 `TurnOutcome`）、`CompletionRegistry`（进程内精确等待）。

不变量：不同 session 可并行；**同一 session 任意时刻最多一个 active turn，且 turn 与 compaction 不并发**。领取 at-most-once：pending 被取走后崩溃不重放。

### 多 Agent 架构

每个 agent 独立配置目录 `~/.ftre/agents/<agent_id>/`（agent.config.json / SOUL.md / AGENTS.md / USER.md / skills/）。配置合并规则：

| 字段 | 合并策略 |
| --- | --- |
| llm | provider+model 可覆盖，api_key/base_url/vision 始终用全局 |
| tools / disabled_skills | 整体替换（写了就用 agent 的，不写则全部可用） |
| workspace | Agent 家目录 |
| mcp / plugins | 按 server name / name 深度合并（agent 覆盖全局） |

### Hook 系统

全异步 filter chain（回调必须 `async def`，自动 await coroutine）。调用点在 `loop.py`：
- `before_messages_build`：events 加载后、to_openai_messages 前；可改 events/config（context_govern：事件治理 + AGENTS.md 注入）
- `before_agent_run`：Agent 创建后、run() 前；可改 messages（MCP/Skill：提示词注入 + 私有 MCP 工具注册）

### 插件体系

内置插件（`src/ftre/plugin/builtin/`）：`skill`（Skill 管理）/ `mcp`（MCP 双层配置）/ `context_govern`（AGENTS.md 双注入 + 工具事件配对去重）/ `title_gen`（标题生成）。外部插件目录 `~/.ftre/plugins/` 保留扩展点。

插件通过 `FtrePluginApi` 注册能力：`tool_registry`（工具）、`append_system_prompt`（提示词）、`register_router`（HTTP 路由）、`register_hook`（hook）。

MCP 双层配置：公共（config.json `mcp` 段 → 全局 tool_registry，启动注册 + watcher 热重载）；私有（agent.config.json `mcp` 段 → per-agent registry，`BEFORE_AGENT_RUN` 按需连接）。连接池按 server name 全局去重复用，配置相同不二次加载；HTTP API 用 `?scope=global|private&agent_id=xxx` 区分。

**Octo 插件**（重要外部插件）：`C:\Users\蒋全明\.ftre\plugins\octo_plugin\`，独立 git 仓库；改 Octo IM / WuKongIM / 外部消息通道需求时优先看这里；入口 shim `octo_channel.py`；进入该目录前先读它的 AGENTS.md 和 README.md。

## 启动方式

两个终端：`ftre gateway`（后端）；`cd E:\binn\ftre-desktop && pnpm dev`（客户端）。打包模式 Electron 自动 spawn 内嵌 Python 后端，无需手动启动。

## CLI 入口点

`pyproject.toml` 注册 `ftre = "ftre.main:app"`，editable 安装改代码直接生效；**只有改 pyproject.toml 入口点名时才需重新 `pip install -e .`**；`ftre.exe` 所在 `Scripts/` 目录需加入 PATH。

## 内置工具

定义在 `src/ftre/tools/`，`build_default_tools()` 按 Agent 配置构建 + 裁剪：

| 工具 | 说明 |
| --- | --- |
| `bash` | 执行 shell 命令，纯 cd 拦截持久切换工作区，RTK 自动重写减 token，semble 语义检索 |
| `read` | 读文件/图片/目录，返回 `(result_str, metadata)`，metadata 含内容快照（file/content/start_line/end_line） |
| `write` | 创建/覆盖文件，保留原编码和换行风格，返回 `(result_str, diff_metadata)` |
| `edit` | 字符串/行号模式修改，返回 `(result_str, diff_metadata)`（before/after/diff/additions/deletions） |
| `set_workspace` | 切换 session 工作区（持久到 DB） |
| `cron` | 定时任务管理（`~/.ftre/cron/`，CronScheduler 30s 扫描） |
| `task` | 派发子任务到 subagent session 同步执行（防递归） |
| `send_message` | 跨 session 消息（notify 通知 / invoke 唤起） |

`ToolHandler.run_one()` 支持 `str` / `EventBase` / `tuple[str, dict]` 三种返回值，`react_runner` 在 `ToolResultEndEvent` 中透传 `metadata=result.metadata`。
