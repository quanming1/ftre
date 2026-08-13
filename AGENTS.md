<project>
后端路径：E:\ftre\src\ftre\
前端路径：E:\binn\ftre-desktop\
文档路径：E:\ftre-docs\
Agent 核心库：E:\ftre-agent-core\
Octo 插件路径：C:\Users\蒋全明\.ftre\plugins\octo_plugin\
配置目录：C:\Users\蒋全明\.ftre\
技术栈：Python 3.12 + TypeScript
日志：logging（Python）、console（前端）

MANDATORY 首次进入本仓库先读 3 份文档，之后每次 commit 前重读第 1 份：
1. docs/COMMIT.md — 提交规范唯一完整定义（type/scope/hook 机制）
2. docs/PROCESS.md — PRD 驱动开发流程（六步闭环）
3. docs/TODO.yaml — 阶段 id 唯一事实源（commit scope 校验依据）
</project>

<git_flow MANDATORY>

<basic_discipline>
- NEVER 私自 commit / push：除非用户明确要求（"commit"、"push"、"提交"），否则只改代码不提交
- 回滚需确认：回滚前告知内容/范围/影响，得到确认后再执行
- ALWAYS push 前先 commit；多仓库联动（改 core 验证后端，改前端验证 API）
- 跨仓库操作必须 set_workspace 显式切换：`cd A && git ...` 中的 cd 不改变 bash 工具工作区
</basic_discipline>

<branch_model>
master（仅发布，永不直接提交）← develop（默认基底，只接受 PR 合入）← feature/&lt;阶段id&gt;-&lt;name&gt; / prd-update / todos-update / release/&lt;ver&gt; / hotfix/&lt;name&gt;

- 默认工作分支是 develop
- NEVER 直接提交 master；NEVER 直接 commit 到 develop——**全 PR 流：develop 只接受 GitHub PR 服务器端合入，本地 develop 永远只 pull 同步**（pre-push hook 强制）
- MANDATORY feat/fix 分支名必须关联 TODO 阶段 id（如 feature/A2-config），提交 scope 与分支名阶段 id 必须一致（commit-msg hook 强制）
</branch_model>

<commit_format>
`&lt;type&gt;(&lt;scope&gt;): &lt;subject&gt;`，subject 中文
- type 白名单：feat / fix / prd / todos / docs / refactor / test / style / chore / perf
- feat/fix/prd/todos 的 scope 必须是 docs/TODO.yaml 中真实存在的阶段 id
- feat 额外强制：暂存必须包含对应阶段 PRD（docs/prd/PRD-&lt;scope&gt;-*.md）——行为变更必须同步 PRD 变更记录（无 PRD 的基建阶段跳过）
- perf 的 scope 必须带 FR 引用（如 perf(C2-FR6)），引用的 FR 编号必须真实存在于对应 PRD
- 其他 type 的 scope 用模块名（check_commit_msg.py 顶部「裁剪点」MODULE_SCOPES）
- 一条提交只做一件事；NEVER 写 fix stuff / update / misc 这类无意义 message
</commit_format>

<merge_and_hooks>
- feature/* → develop 一律走 GitHub PR/MR（Code Review）：push 分支后提 PR，NEVER 本地 `git merge --no-ff` 合并回 develop
- develop 与 master 之间同样禁止本地 merge，一律走 PR：develop → master 走 release/* 提 PR；hotfix 回灌走 PR
- NEVER rebase 已推送历史
- 本地强制：.githooks/commit-msg（提交校验）+ .githooks/pre-push（master 双保护 + develop 三重保护：禁删 / 禁 feature 直推 / 本地领先即拒）
- merge:/revert: 开头系统提交跳过
- MANDATORY 首次在本仓库提交前，先完整阅读 docs/COMMIT.md（提交规范唯一完整定义，含 type/scope 规则与常见错误速查）
- 标准流程：checkout develop && pull → checkout -b feature/&lt;阶段id&gt;-&lt;task&gt; → 开发+测试 → commit → push origin feature/&lt;task&gt; → GitHub 提 PR 合入 develop → checkout develop && pull 同步
</merge_and_hooks>

</git_flow>

<prd_driven MANDATORY>
- MANDATORY 首次在本仓库开工前，先完整阅读 docs/PROCESS.md（PRD 驱动流程六步闭环）
- ALWAYS 先 PRD 后开发：TODO 阶段开工前先在 docs/prd/ 建 PRD（从 PRD-TEMPLATE.md 复制）并定稿 approved
- PRD 是唯一依据：需求/实现/测试/验收全部对照 PRD；验收按 PRD「验收标准」逐条核对
- 阶段 id 与状态见 docs/TODO.yaml（commit scope 的唯一事实源）
</prd_driven>

<architecture>

<repo_relations>
ftre-agent-core    Agent 核心库（无状态、纯算法）被后端 import，不独立部署
      ├── ftre-octo_plugin  Octo IM 外部插件（Python+Node：WuKongIM 桥接/Octo Channel/octo_management Tool）
      ▼                    （shim: ~/.ftre/plugins/octo_channel.py 被 Gateway 扫描加载）
ftre               Gateway 后端（有状态、长驻进程）：Session 管理 / EventBus / Channel / 插件 / MCP
      ▼
ftre-desktop       Desktop 客户端（Electron+React），WebSocket 与后端通信
      ▼
ftre-docs          文档站（React+Vite），独立部署
</repo_relations>

<agent_loop>
`Channel → EventBus → AgentLoop`；AgentLoop 内按 session_id 建独立 SessionLane（单 session actor：FIFO/取消/压缩门控/状态发布）。
协作组件：MailboxStore（持久化 pending）/ ContextGate（领取前水位检查）/ CompactManager（共享压缩）/ TurnExecutor（只执行已领取 turn 返回 TurnOutcome）/ CompletionRegistry（进程内精确等待）。

CRITICAL 不变量：
- 不同 session 可并行；同一 session 任意时刻最多一个 active turn
- turn 与 compaction 不并发；领取 at-most-once（pending 取走后崩溃不重放）
</agent_loop>

<multi_agent>
每个 agent 独立配置目录 ~/.ftre/agents/&lt;agent_id&gt;/（agent.config.json / SOUL.md / AGENTS.md / USER.md / skills/）。合并规则：

| 字段 | 合并策略 |
| --- | --- |
| llm | provider+model 可覆盖，api_key/base_url/vision 始终用全局 |
| tools / disabled_skills | 整体替换（写了就用 agent 的，不写则全部可用） |
| workspace | Agent 家目录 |
| mcp / plugins | 按 server name / name 深度合并（agent 覆盖全局） |
</multi_agent>

<hooks>
全异步 filter chain（回调必须 async def，自动 await coroutine）。调用点在 loop.py：
- before_messages_build：events 加载后、to_openai_messages 前；可改 events/config（context_govern：事件治理 + AGENTS.md 注入）
- before_agent_run：Agent 创建后、run() 前；可改 messages（MCP/Skill：提示词注入 + 私有 MCP 工具注册）
</hooks>

<plugins>
内置插件（src/ftre/plugin/builtin/）：skill（Skill 管理）/ mcp（MCP 双层配置）/ context_govern（AGENTS.md 双注入 + 工具事件配对去重）/ title_gen（标题生成）。外部插件目录 ~/.ftre/plugins/ 保留扩展点。

插件通过 FtrePluginApi 注册：tool_registry（工具）/ append_system_prompt（提示词）/ register_router（HTTP 路由）/ register_hook（hook）。

MCP 双层：公共（config.json mcp 段 → 全局 tool_registry，启动注册 + watcher 热重载）；私有（agent.config.json mcp 段 → per-agent registry，BEFORE_AGENT_RUN 按需连接）。连接池按 server name 全局去重复用；HTTP API 用 ?scope=global|private&agent_id=xxx 区分。

⚠️ Octo 插件（重要外部插件）：C:\Users\蒋全明\.ftre\plugins\octo_plugin\，独立 git 仓库；改 Octo IM / WuKongIM / 外部消息通道需求时优先看这里；入口 shim octo_channel.py；进入该目录前先读它的 AGENTS.md 和 README.md
</plugins>

</architecture>

<run_and_cli>
- 启动：两个终端——`ftre gateway`（后端）；`cd E:\binn\ftre-desktop && pnpm dev`（客户端）。打包模式 Electron 自动 spawn 内嵌后端
- CLI：pyproject.toml 注册 `ftre = "ftre.main:app"`，editable 安装改代码直接生效；只有改 pyproject.toml 入口点名才需重新 pip install -e .；ftre.exe 所在 Scripts/ 需在 PATH
</run_and_cli>

<tools>
定义在 src/ftre/tools/，build_default_tools() 按 Agent 配置构建 + 裁剪：

| 工具 | 说明 |
| --- | --- |
| bash | shell 执行，纯 cd 拦截持久切换工作区，RTK 自动重写减 token，semble 语义检索 |
| read | 读文件/图片/目录，返回 (result_str, metadata)，metadata 含内容快照（file/content/start_line/end_line） |
| write | 创建/覆盖文件，保留原编码和换行风格，返回 (result_str, diff_metadata) |
| edit | 字符串/行号模式修改，返回 (result_str, diff_metadata)（before/after/diff/additions/deletions） |
| set_workspace | 切换 session 工作区（持久到 DB） |
| cron | 定时任务管理（~/.ftre/cron/，CronScheduler 30s 扫描） |
| task | 派发子任务到 subagent session 同步执行（防递归） |
| send_message | 跨 session 消息（notify 通知 / invoke 唤起） |

ToolHandler.run_one() 支持 str / EventBase / tuple[str, dict] 三种返回值，react_runner 在 ToolResultEndEvent 中透传 metadata=result.metadata
</tools>

<anti_lazy>
- NEVER 用空函数、TODO、placeholder 假装完成
- NEVER 重复性任务做几个就声称全部完成——逐个执行，验证全部
- NEVER 跳过失败的步骤——修复后重新验证
- 同一问题反复改不好就停下：回到初始假设、复现路径和失败证据重新判断，换方向
- 收尾前通读改过的文件：确认连贯、无语法错误、无残留调试代码
- 违反以上任何一条：下一轮立即自纠
</anti_lazy>
