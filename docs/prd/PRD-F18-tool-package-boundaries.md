# PRD-F18：消息、任务与团队 Tool Package 边界收敛

| 字段 | 值 |
|---|---|
| 阶段 | F18 |
| 名称 | 消息、任务与团队 Tool Package 边界收敛 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-24 |
| 定稿日期 | 2026-08-24 |
| 验收日期 | 2026-08-24 |
| 关联文档 | `docs/TODO.yaml`、`AGENTS.md`、F14/F17 PRD |

## 1. 背景与目标

### 1.1 背景

F17 为了移除 Agent Runtime 中未接通的 `_inbox` 透传，把 `send_message`、`task` 和
`team_*` 全部放进了 `ftre-inbox`。这解决了死透传，却把“使用 Inbox”错误地当成了“属于
Inbox”，造成一个 Plugin 同时承担消息通信、Subagent 编排和团队协作三个不同业务职责。

Inbox 的唯一业务职责应是持久队列：admission、pending、claim、ack/retry、恢复和消费边界。
消息通信、任务编排和团队协作都应拥有自己的发布、配置、生命周期和可选安装边界。

### 1.2 目标

把三个 Tool 能力分别迁移到三个独立 Package/Plugin，同时让 `ftre-inbox` 只拥有 Inbox
Service、队列 Hook、Worker 和持久化；不改变客户端协议和工具对外行为。

目标结构：

```text
packages/
├─ ftre-inbox/       # 队列基础设施：InboxService、Repository、admission/claim Hook
├─ ftre-messaging/   # send_message：跨 Session notify/invoke
├─ ftre-task/        # task：Subagent 派发与精确等待
└─ ftre-team/        # team_*：团队成员、派活、状态、等待、解散
```

依赖方向：

```text
ftre-messaging ─┐
ftre-task       ├─→ ftre Host 公共 Service + inbox Service
ftre-team       ┘

ftre-inbox 不依赖上述三个业务 Package。
```

### 1.3 非目标

- 不修改客户端、`E:\\ftre-agent-core`、`E:\\cordis-py` 或 wire protocol。
- 不把 `send_message`、`task`、`team` 合并为一个“协作 Package”。
- 不把 Inbox 的 QueueItem、Worker、持久化或 claim 算法复制到三个业务 Package。
- 不为三个 Package 增加 `Port`、`Coordinator`、Facade、Service Bag 或新的转换层。
- 不为了目录对称新增重复 Team 数据 Service；清理当前未被工具消费的 TeamService 重复 Owner。

## 2. 需求范围

### 2.1 功能需求

- [x] FR1：新增 `ftre-messaging` Package，入口 `ftre_messaging.plugin:apply`，只注册
  `send_message`，工具行为保持 `notify` 和 `invoke` 协议不变。
- [x] FR2：新增 `ftre-task` Package，入口 `ftre_task.plugin:apply`，只注册 `task`，
  保持新建/复用 Subagent Session、Inbox 投递、request 精确等待和返回格式不变。
- [x] FR3：新增 `ftre-team` Package，入口 `ftre_team.plugin:apply`，只注册
  `team_create`、`team_add_agent`、`team_say`、`team_agent_status`、`team_delete`、
  `wait_agent`，保持团队 metadata、成员绑定、派活和等待行为不变。
- [x] FR4：三个 Package 各自声明准确的 `inject` 依赖并通过 `ctx.tools.register` 注册；
  每项 Tool 的 owner 分别为 `messaging`、`task`、`team`，注册和注销绑定当前 Fiber Effect。
- [x] FR5：`ftre-inbox` Plugin 删除对三个 Tool 工厂和 `tools`/`channels` 注入的依赖；只
  provide `inbox`，注册队列 Hook、Worker 和持久化资源。
- [x] FR6：删除 `src/ftre/plugins/builtin/team` 中未被实际团队 Tool 消费的重复
  `TeamService`/`teams` Provider；团队状态唯一保留在现有 Session metadata Owner 中。
- [x] FR7：Composition 对三个 Package 使用可选 Plugin Manifest；Package 未安装时 Host
  仍可启动，安装后可自动发现、加载、卸载和重启，不创建 Inbox 内的幽灵 Tool。
- [x] FR8：`ftre` extras 增加 `messaging`、`task`、`team` 组合，并让 `full` 表达四个
  能力 Package；各 Package wheel 不包含 Host 测试、缓存或其他 Package 源码。
- [x] FR9：F17 文档、README、CHANGELOG、Owner 表和架构测试改为“依赖 Inbox ≠ Inbox Owner”，
  不再宣称 Inbox 唯一拥有三个业务 Tool。

### 2.2 非功能需求

- **可卸载性**：卸载任一业务 Package 只移除该 Package 的 Tool 和资源，Inbox Service、
  Agent Turn 和其他业务 Package 继续运行。
- **独立发行**：每个 Package 可独立构建 wheel；安装顺序不依赖另两个业务 Package。
- **运行安全**：缺少 Inbox 时，依赖 Inbox 的 Package 不能静默启动；由 Plugin 注入门禁给出
  明确失败，普通 Host 的缺包行为必须可诊断。
- **边界安全**：Package 只能消费 Host 公共 Service/Hook 和 `inbox` Service，不 import
  `services/tools/builtin`、Agent Runtime、其他 Package 的私有模块。

## 3. 技术方案

### 3.1 Package/Plugin Owner

| Package | Plugin id | 注册 Tool | 主要注入 | 唯一 Owner |
|---|---|---|---|---|
| `ftre-inbox` | `inbox` | 无业务 Tool | `sessions`、`agents`、`hook_runtime` | InboxService、Queue Hook/Worker |
| `ftre-messaging` | `messaging` | `send_message` | `channels`、`tools`、`inbox` | 跨 Session 通信 Tool |
| `ftre-task` | `task` | `task` | `channels`、`tools`、`inbox` | Subagent 派发/等待 Tool |
| `ftre-team` | `team` | `team_*`、`wait_agent` | `sessions`、`agents`、`channels`、`tools`、`inbox`、`agent_profiles` | 团队协作 Tool |

`inbox` 是 Host 当前 Gateway 的必选基础 Plugin；三个业务 Package 是可选能力。它们通过
`inject("inbox")` 使用 Inbox，不反向进入 Inbox Plugin 的注册代码。

### 3.2 生命周期

每个业务 Plugin 的 `apply` 只做三件事：

1. 从 Context 注入公开依赖；
2. 创建自己的 Tool 工厂产物并注册到 `ToolService`；
3. 将 disposer 绑定 `ctx.effect`，卸载时撤销全部 Tool。

Inbox Plugin 不负责业务 Tool 的注册。`unload("task")` 不得关闭 Inbox；
`unload("inbox")` 只在依赖它的业务 Plugin 已被卸载或显式失败时执行。

### 3.3 Team 数据边界

现有团队 Tool 的真实数据写入是 `SessionService` 的 `metadata["teams"]` 和成员
`metadata["team_member"]`。成员 profile 的目录和校验由公开 `AgentProfileService` 负责，
Team Package 不 import Agent Profile 私有 helper。本阶段将删除未被消费的内存 `TeamService`，
不迁移数据格式，避免同时存在两个团队状态 Owner。

## 4. 接口定义

三个 Package 的入口统一为：

```python
inject = ("tools", "inbox")

async def apply(ctx, config=None):
    tool = create_tool(..., ctx.inbox)
    disposer = ctx.tools.register(tool, owner="<package-id>", source="package")
    ctx.effect(lambda: disposer, label="<package-id>:tool:<name>")
```

具体工具对 Agent 的名称、参数、返回字符串和 Inbox 调用方法保持现有公开行为。

## 5. 验收标准

- [x] AC1：三个 Package 的目录、`pyproject.toml`、entry point、README 和最小 import 均存在，
  且各自 wheel 不包含其他 Package、Host tests、缓存或临时文件。
- [x] AC2：架构扫描证明 `ftre-inbox` 不 import 三个业务 Tool 工厂；三个业务 Package 不
  import `ftre.services.tools.builtin` 或 Agent Runtime 私有模块；Tool owner 与 Package 一一对应。
- [x] AC3：`send_message`、`task`、全部 `team_*`/`wait_agent` 行为回归通过；其注册/注销
  不改变现有参数、返回格式、request_id 和 Session metadata。
- [x] AC4：分别 unload/restart `messaging`、`task`、`team`，只出现对应 Tool 的增删，
  Inbox、Agent、Session 和其他 Tool 无残留、无重复、无错误关闭。
- [x] AC5：禁用或未安装三个业务 Package 时，默认 Host 仍能启动；未安装 Inbox 时，
  Inbox 及其依赖 Package 的状态为明确失败/未启用，不产生静默半成品。
- [x] AC6：`ftre[inbox,messaging,task,team,full]` 的依赖组合可解析；四个 Package 可
  在洁净临时虚拟环境构建和安装，entry point 能被 Discovery 发现。
- [x] AC7：全量 pytest、architecture/contracts/startup/lifecycle、ruff、wheel、Gateway
  smoke、`git diff --check` 和生成物/空目录扫描全部通过。
- [x] AC8：F17 PRD/TODO/README/CHANGELOG/执行报告不再把 Inbox 描述为三个业务 Tool 的 Owner，
  F18 FR/AC 状态与实际证据一致。

## 6. 测试计划

- **架构测试**：Package 文件归属、entry point、唯一 Tool Owner、禁止跨 Owner 私有 import、
  Inbox 不注册业务 Tool、旧 TeamService 不存在。
- **行为测试**：分别加载三个 Plugin，执行 Tool 工厂的既有单元/回归场景。
- **生命周期测试**：单独 activate/unload/restart 三个 Plugin，检查 ToolRegistry 快照和
  Inbox/Session/Agent 状态。
- **启动测试**：无三个业务 Package、仅 Inbox、全 Package 三种 Composition；覆盖禁用必选
  Inbox 和缺失依赖诊断。
- **发行测试**：四个 wheel 构建、文件清单检查、洁净临时 venv 安装和 entry point 发现。

## 7. 批次计划

1. F18.1：Owner 基线、PRD/TODO 定稿和 F17 文档纠偏。
2. F18.2：新建 `ftre-messaging` 与 `ftre-task` Package，迁移并接线工具。
3. F18.3：新建 `ftre-team` Package，迁移 Team 工具并清理重复 TeamService。
4. F18.4：Inbox 收窄、Composition/extras/Discovery 与生命周期接线。
5. F18.5：架构/行为/生命周期/发行测试和旧引用清理。
6. F18.6：全量验收、执行报告、CHANGELOG 和工程卫生。

## 8. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-24 | 初版：将 `send_message`、`task`、`team` 拆为三个独立 Package，Inbox 仅保留队列职责 | F17 将“使用 Inbox”误判为“属于 Inbox”，造成错误的单一 Owner |
| 2026-08-24 | 完成 F18.1–F18.6；FR1–FR9、AC1–AC8 全部验收 | 497 项测试通过，四个 Package wheel、洁净 venv、未安装业务包启动、Gateway smoke、ruff 和工程卫生门禁通过 |
