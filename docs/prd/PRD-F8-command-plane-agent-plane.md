# PRD-F8 Command Plane 与 Agent Plane 解耦

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F8 |
| 名称 | Command Plane 与 Agent Plane 解耦及架构债务清理 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-21 |
| 定稿日期 | 2026-08-21 |
| 验收日期 | 2026-08-21 |
| 关联文档 | `docs/TODO.yaml` F8；`docs/prd/README.md`；`AGENTS.md`；`docs/PROCESS.md` |

## 1. 背景与目标

### 1.1 当前问题

ftre 已经在 `AgentLoop` 接入层识别 Command，普通 Command 也不会进入
`MailboxStore`。但是普通 Command 的执行仍然通过：

```text
SessionLane.dispatch_command()
  → CommandService.dispatch_inbound()
  → TurnExecutor.execute_command()
```

因此当前只完成了“解析层分流”，没有完成“执行层解耦”。

当前 `CommandResult` 还是一个混合协议：

- `SendMessage`：给客户端返回文本；
- `Handled`：命令短路结束；
- `ResumeAgent`：恢复 Agent；
- `RewritePrompt`、`Passthrough`：改变 Agent 执行路径。

同时，内置命令通过 `register_builtin_commands(manager, loop)` 捕获完整
`AgentLoop`，再间接访问：

```python
loop.compaction.compact_now(...)
loop.session_manager.fork_session(...)
```

这掩盖了真实的 Service Owner：

```text
CompactionService / CompactionPort
SessionService
```

### 1.2 目标

采用 DSH 风格的最小 Command 协议：

```text
CommandRuntime
  → Command Handler
  → CommandResult(success / error)
```

目标状态：

```text
普通 Agent 消息
  → Mailbox
  → AgentLoop
  → TurnExecutor

Command
  → CommandRuntime
  → 直接响应或领域事件
```

需要恢复 Agent 的 `/allow`、`/deny` 不增加新的 `AgentControlPort`、
`AgentEffect` 或 `ResumeRequest` 类型，而是复用已有的 Session Event 和确认事件
管线完成恢复。

本阶段完成后：

- Command 执行不再进入 `TurnExecutor`；
- `TurnExecutor` 不认识 Command 类型；
- `CommandResult` 只表示成功或失败；
- `/compact` 直接依赖公开 `CompactionPort`；
- `/fork` 直接依赖 `SessionService`；
- `/allow`、`/deny` 复用已有确认事件恢复 Agent；
- 旧的 Loop 间接引用、混合返回类型和命令状态机全部清理。

### 1.3 非目标

- 不修改桌面端、客户端协议消费者或 `E:\binn\ftre-desktop`；
- 不修改 `E:\ftre-agent-core`；
- 不在本阶段发布 cordis-py PyPI 包；
- 不重写 Agent Core 的 LLM、Tool、Compaction 算法；
- 不把所有 Command 迁移成 Agent Tool；
- 不引入新的通用事件总线；
- 不改变现有命令的业务语义，只改变执行平面和内部依赖方向。

## 2. 术语与边界

### 2.1 Command Plane

负责命令解析、匹配、串行执行、直接响应和命令生命周期记录。它可以调用公开
Service，但不创建 Agent Turn。

### 2.2 Agent Plane

负责消费已经进入 Mailbox 的 Agent 消息，并执行 Agent Hook、LLM、Tool 和 Turn 收尾。
它不识别 `/compact`、`/fork` 等命令文本。

### 2.3 Session Event 桥接

Command 需要影响 Agent 时，不扩展 `CommandResult`。Handler 写入已有的领域/Session
事件，例如 `UserConfirmResultEvent`，由现有 Session/Agent 恢复流程继续处理。

## 3. 需求范围

### 3.1 功能需求

- [x] **FR1：收敛现有 CommandContext**
  - 不新增 `CommandInvocation` 类型，直接收敛现有 `CommandContext`。
  - Context 显式提供 `raw`、`command`、`args`、`session_id`、`channel_id`、
    `request_id` 等必要字段。
  - 删除通过 `meta: Any` 反查 `BusMessage` 的隐式依赖。

- [x] **FR2：CommandResult 简化**
  - 将现有 `Handled`、`SendMessage`、`ResumeAgent`、`RewritePrompt`、`Passthrough`
    收敛为一个最小结果协议：

    ```python
    CommandResult(
        kind="success" | "error",
        text: str = "",
        source_event_seq: int | None = None,
    )
    ```

  - `CommandResult` 只表达命令成功或失败，不表达 Agent 调度。
  - 命令 ID 只作为 Runtime 生命周期关联键，不新增对外的结果包装类型。

- [x] **FR3：CommandRuntime 独立执行**
  - `commands` Service 负责 parse、匹配、调用 Handler、结果校验、异常/取消和生命周期。
  - `SessionLane` 仍可在 admission lock 内调用 Command Runtime，以保持同 Session 顺序，
    但不得调用 `TurnExecutor`。
  - 未匹配文本不产生 Command 生命周期事件。

- [x] **FR4：Command 生命周期协议**
  - 已匹配命令生成唯一 `command_id`。
  - 执行前记录 `command/run`，结束后记录配对的 `command/done`。
  - `command/done` 至少包含 `command_id`、`kind`、可选 `text` 和
    `source_event_seq`。
  - Handler 异常、取消和结果校验失败都必须有明确终态。

- [x] **FR5：纯 Command 不创建 Agent Turn**
  - `/compact`、`/compress-fast`、`/fork` 和直接响应命令不产生 `turn/start`、
    `step/start`、LLM request、Agent Hook 或 Mailbox pending。
  - `persist_input` 只代表命令审计策略，不能触发 TurnExecutor 持久化边界。

- [x] **FR6：Session Event 恢复确认 Agent**
  - `/allow`、`/deny` 继续校验 ToolCallBlock 和确认状态。
  - Handler 写入已有 `UserConfirmResultEvent`，复用当前 Session/Agent 恢复管线。
  - 不新增 `AgentControlPort`、`AgentEffect`、`AgentResumeRequest` 或新的命令结果分支。
  - `reply_id`、`tool_call_id`、`request_id` 的现有幂等语义必须保持。

- [x] **FR7：内置命令只依赖真实 Service Owner**
  - `/compact` 直接依赖公开 `CompactionPort`；实现由 `CompactionService` 提供。
  - `/compress-fast` 直接依赖公开 `CompactionPort`。
  - `/fork` 直接依赖 `SessionService`。
  - `/cancel` 保持控制面语义，不将完整 `AgentLoop` 暴露给 Command Handler。
  - 删除 `register_builtin_commands(manager, loop)` 这种完整 Loop 闭包依赖。

- [x] **FR8：Session 命令边界**
  - `/cancel` 不进入 Mailbox、不写聊天历史。
  - 普通命令仍由 `SessionLane` admission lock 串行化，但只调用 Command Runtime。
  - Command 执行不得越过已 admission 的 Agent pending，也不得重复消费 pending。

- [x] **FR9：TurnExecutor 纯化**
  - 删除 `execute_command()`、`_command()`、`TurnStatus.COMMAND`、`Turn.command`、
    `Turn.command_name` 以及对 `ftre.services.command` 的导入。
  - `TurnExecutor.execute()` 只接受已由 Mailbox 领取的 Agent 工作项/普通 Agent 消息。

- [x] **FR10：架构债务清理**
  - 删除 `TurnExecutor` 中的 Command match-case、命令持久化分支和 `_send_command_message()`
    对命令结果的解释职责。
  - 删除 `CommandResult` 旧兼容类、旧注释、旧测试辅助和无生产引用的导出。
  - 将 `builtin.py` 对 `AgentLoop` 的引用改为已有公开 Service 依赖。
  - 生产代码、测试、Fixture 和文档不得继续引用 `loop.compaction`、
    `loop.session_manager` 作为 Command 的 Service 获取方式。
  - 清理命令路径产生的空壳、重复适配器、死代码和过时 `__pycache__`。

- [x] **FR11：错误、取消与重试**
  - Command 失败不得吞掉此前已 admission 的 Agent pending。
  - 压缩、fork、确认事件写入失败必须有明确错误结果和日志。
  - 相同 `request_id` 不得重复执行领域变更或重复写入确认事件。

### 3.2 非功能需求

- **简洁性**：Command 对外只暴露一个 Service、一个 Context 和一个 Result 协议。
- **依赖方向**：Command → 公开 Service；禁止 Command → AgentLoop 私有实现。
- **可回放**：`command/run`、`command/done` 通过 `command_id` 配对。
- **并发性**：不同 Session 可并行；同 Session 保持现有 admission 顺序。
- **生命周期**：Command 注册、注销和 Plugin unload 可逆、幂等。
- **兼容性**：保持现有 WS/HTTP 入站字段和客户端可见输出。

## 4. 目标架构

### 4.1 入站分流

```text
Channel / EventBus
        │
        ▼
CommandIngress
        │
        ├─ 未匹配 → AgentMailbox.submit(UserMessage)
        ├─ system command → CommandRuntime.execute(control lane)
        └─ ordinary command → SessionLane admission lock
                              → CommandRuntime.execute()
```

普通 Command 可以经过 SessionLane 保证顺序，但不能进入 Mailbox 或 TurnExecutor。

### 4.2 最小 Command 协议

```python
@dataclass(frozen=True)
class CommandResult:
    kind: Literal["success", "error"]
    text: str = ""
    source_event_seq: int | None = None
```

执行流程：

```text
CommandRuntime
  1. parse / match
  2. 生成 command_id
  3. 写 command/run
  4. 调用 Handler(CommandContext)
  5. 校验 CommandResult
  6. 写 command/done
  7. 返回 CommandResult
```

`command_id` 只用于生命周期事件和日志关联，不新增 `CommandExecution` 对外模型。

### 4.3 Service 依赖方向

```text
Command Plugin
   ├─ CompactionPort → CompactionService
   ├─ SessionService
   └─ 已有 Session Event 出口

Command Plugin  ✕→ AgentLoop
Command Plugin  ✕→ TurnExecutor
```

推荐装配形式：

```python
register_builtin_commands(
    manager,
    sessions=session_service,
    compaction=compaction_service,
)
```

而不是：

```python
register_builtin_commands(manager, agent_loop)
```

### 4.4 Command → Agent 的唯一桥接

```text
/allow 或 /deny
   │
   ▼
校验 ToolCallBlock
   │
   ▼
写入已有 UserConfirmResultEvent
   │
   ▼
现有 Session/Agent 恢复流程继续执行
```

该桥接不扩展 CommandResult，不新增 Agent 专用中间类型，也不直接调用
`TurnExecutor.execute_command()`。

## 5. 现有命令迁移矩阵

| 命令 | 当前实现 | F8 目标实现 | 直接输出 |
|---|---|---|---|
| `/cancel` | system command，Handler 捕获完整 Loop | 保持控制面，改为窄取消依赖 | 状态/快照 |
| `/compact` | `loop.compaction.compact_now()`，经过命令 Turn | `CompactionPort.compact_now()`，不创建 Turn | 成功/失败结果与压缩事件 |
| `/compress-fast` | `loop.compaction.compress_fast()`，经过命令 Turn | `CompactionPort.compress_fast()`，不创建 Turn | 成功/失败结果 |
| `/fork` | `loop.session_manager.fork_session()` | `SessionService.fork_session()` | `CommandResult.text` |
| `/allow` | 返回 `ResumeAgent`，由 TurnExecutor 解释 | 写入已有 `UserConfirmResultEvent` | Agent 后续回复 |
| `/deny` | 返回 `ResumeAgent`，由 TurnExecutor 解释 | 写入已有 `UserConfirmResultEvent` | Agent 后续回复 |

## 6. 架构债务清理清单

| 债务 | 当前证据 | 清理动作 |
|---|---|---|
| Command 进入 TurnExecutor | `SessionLane.dispatch_command()` 调用 `execute_command()` | CommandRuntime 直接返回结果，删除调用 |
| TurnExecutor 命令状态机 | `COMMAND`、`_command()`、`command_name` | 删除命令状态和字段 |
| 混合 CommandResult | `Handled`、`SendMessage`、`ResumeAgent` 等 | 收敛为 success/error |
| 完整 Loop 闭包 | `register_builtin_commands(manager, loop)` | 直接注入 `CompactionPort`、`SessionService` 等已有依赖 |
| Compaction 间接 Owner | `loop.compaction` | 改为公开 `CompactionPort` |
| Session 间接 Owner | `loop.session_manager` | 改为 `SessionService` |
| Command 隐式恢复 Agent | `ResumeAgent` match-case | 复用已有 Session Event |
| 直接 Bus 文本适配 | `_send_command_message()` 位于 TurnExecutor | 由 Command Runtime/接入层统一发送结果 |
| 死代码与兼容壳 | 无生产引用的旧结果类、测试辅助、缓存 | 全盘扫描并删除，保留必要测试证据 |

## 7. 模块调整计划

### F8.1 最小协议冻结

- 收敛现有 `CommandContext`；
- 将 `CommandResult` 改为 success/error；
- 保留 `command_id` 作为内部生命周期关联键；
- 删除 Agent 专用 Command 结果类型。

### F8.2 CommandRuntime 独立执行

- 将 `CommandManager` 的匹配职责与执行职责收敛到 `commands` Service；
- 实现 `command/run`、`command/done`；
- 统一结果校验、异常、取消和日志。

### F8.3 SessionLane 接入调整

- 保留 admission lock 和同 Session 顺序；
- `dispatch_command()` 只调用 CommandRuntime；
- 删除 `executor.execute_command()`；
- 正确完成 `CompletionRegistry` 和 Session 快照。

### F8.4 确认事件恢复

- 迁移 `/allow`、`/deny` 的确认事件写入；
- 复用已有 Session/Agent 恢复流程；
- 不新增 Agent Control/Effect/Resume 类型。

### F8.5 内置命令 Service Owner 收敛

- `/compact`、`/compress-fast` 注入 `CompactionPort`；
- `/fork` 注入 `SessionService`；
- `/cancel` 移除完整 Loop 闭包；
- 删除 `register_builtin_commands(manager, loop)`。

### F8.6 TurnExecutor 与命令债务清理

- 删除 Command import、Command 状态、命令字段和命令分支；
- 删除 `_send_command_message()`；
- 将普通 Agent 消息持久化逻辑留在 Agent 数据面；
- 清理旧兼容类型、重复适配器、空壳和缓存。

### F8.7 专项测试与架构门禁

- 添加 Command 无 Turn、无 LLM、无 Mailbox 污染测试；
- 添加 Session Event 恢复确认 Agent 测试；
- 添加 Service Owner 依赖方向测试；
- 添加失败、取消、重试、同 Session 顺序和 in-flight 测试。

### F8.8 全量验收与收尾

- 全量 pytest、ruff、YAML、diff check 和 Gateway smoke；
- 更新 PRD、TODO、CHANGELOG 和执行报告；
- 逐条核对 FR/AC，未通过项不得标记完成。

## 8. 验收标准

- [x] **AC1：TurnExecutor 依赖门禁**
  - `turn_executor.py` 不导入 `ftre.services.command`，不存在 `execute_command()`、
    `TurnStatus.COMMAND`、`Turn.command`、`Turn.command_name` 和 `_command()`。

- [x] **AC2：最小 CommandResult**
  - 生产代码只保留 success/error 两种结果；`RewritePrompt`、`Passthrough`、
    `ResumeAgent`、`Handled`、`SendMessage` 不再作为 CommandResult 类型。

- [x] **AC3：纯 Command 无 Turn**
  - `/compact`、`/compress-fast`、`/fork` 和直接响应命令不产生 `turn/start`、
    `step/start`、LLM request、Mailbox pending 或 Agent Hook。

- [x] **AC4：Command 生命周期完整**
  - 每个已匹配命令都有配对 `command/run`、`command/done`；异常、取消和协议失败也有终态。

- [x] **AC5：Service Owner 正确**
  - `/compact` 只依赖 `CompactionPort`；`/fork` 只依赖 `SessionService`；Command
    Handler 不捕获完整 `AgentLoop`，不使用 `loop.compaction` 或 `loop.session_manager`。

- [x] **AC6：确认事件恢复**
  - `/allow`、`/deny` 能通过已有 `UserConfirmResultEvent` 恢复确认流程；重复 request
    不重复写入确认事件；不经过 `TurnExecutor.execute_command()`。

- [x] **AC7：Session 串行性**
  - 同一 Session 中 Command 与 Agent pending 保持 admission 顺序；不同 Session 可并行；
    Command 不占用 Agent active turn。

- [x] **AC8：架构债务清理**
  - 旧 Command 结果类、完整 Loop 闭包、重复 Bus 文本适配器、无生产引用的命令分支、
    空壳和生成缓存均完成扫描；保留文件均有生产引用或测试用途说明。

- [x] **AC9：错误与幂等**
  - Command 失败不得丢失已 admission pending；压缩、fork、确认事件写入失败有明确
    error 结果；相同 `request_id` 不重复执行领域变更。

- [x] **AC10：质量门禁**
  - `python -m pytest -q`、`python -m ruff check src tests`、YAML 校验、
    `git diff --check` 和 Gateway 启停 smoke 全部通过。

## 9. 测试计划

### 9.1 单元测试

- Command 解析、最长前缀、参数保留、未匹配和 Result 校验；
- Command Runtime 的 run/done 配对、异常、取消和幂等；
- `/compact`、`/compress-fast`、`/fork` 的 Service 调用和结果；
- `/allow`、`/deny` 的确认事件构造和重复请求处理。

### 9.2 契约与架构测试

- `CommandContext` 不通过 `meta: Any` 反查 BusMessage；
- `TurnExecutor` 不导入、不调用、不解释 Command；
- builtin Command 不引用 `AgentLoop`、`loop.compaction` 或 `loop.session_manager`；
- 纯 Command 不产生 Turn、LLM、Mailbox 或 Agent Hook；
- 生产代码不存在旧 Command Result 类型和无生产引用的兼容分支。

### 9.3 生命周期与并发测试

- Command Plugin 注册、注销、unload、restart；
- Handler 超时、取消、异常和 lifecycle 写入失败；
- 同 Session Command 与 pending Agent 消息顺序；
- Session Event 恢复期间的重复确认、Gateway 关闭和 in-flight 清理。

### 9.4 集成测试

- WS 入站 `/compact`、`/fork`、`/allow`、普通文本的完整事件序列；
- `/compact` 不写入普通 UserMessage、不创建 Turn；
- `/fork` 结果通过现有 Channel 返回；
- `/allow`、`/deny` 通过已有确认事件恢复 Agent；
- 客户端现有入站/出站协议不变。

## 10. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-21 | 创建 F8 草稿，定义 Command Plane 与 Agent Plane 解耦 | 当前 Command 虽已在接入层解析，但仍依赖 TurnExecutor 执行 |
| 2026-08-21 | 收敛为最小 CommandResult；删除 AgentEffect、AgentControlPort、AgentResumeRequest 设计；改为复用已有 Session Event；增加 Compaction/Session Owner、Loop 闭包和兼容分支清理 | 避免过度设计，保持 Command 协议小而稳定 |
| 2026-08-21 | 完成 F8 实施与验收：CommandRuntime 独立执行、TurnExecutor 脱离 Command、确认事件恢复和 Service Owner 收敛；402 项全量测试与架构门禁通过 | 按最小协议完成 Command Plane/Agent Plane 解耦 |
| 2026-08-21 | 审计复核后补充 Service 注入键与后台线程生命周期门禁，F8/F9 联合全量验证更新为 404 项通过 | 收尾审计发现并修复残余依赖命名和资源清理债务 |
