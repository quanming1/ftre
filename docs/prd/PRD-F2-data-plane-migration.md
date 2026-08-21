# PRD-F2-核心数据面 Service 化迁移

> 本阶段承接 F1 的渐进式重构。F1 已建立 Composition、Service、Plugin 和生命周期边界；F2 负责把仍位于旧目录的核心数据面真实实现迁入新 Owner。迁移期间旧路径只允许作为兼容 re-export，不再承载第二份实现。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F2 |
| 名称 | 核心数据面 Service 化迁移 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-21 |
| 定稿日期 | 2026-08-21 |
| 验收日期 | 2026-08-21 |
| 关联文档 | `docs/TODO.yaml` 阶段 F2；`AGENTS.md`；`docs/PROCESS.md`；`docs/prd/PRD-F1-backend-plugin-refactor.md` |

## 1. 背景与目标

### 1.1 背景

F1 已经让新的 Composition Root、Cordis Plugin Runtime、公共 Service 和 Feature Plugin 进入实际启动路径，但 Session、AgentLoop、Mailbox、EventBus、Channel、Command 和部分 Tool 的真实实现仍位于旧目录。当前 Service 层中存在继承旧类、转发旧对象和 compatibility alias，能够保证兼容，却没有完成代码所有权迁移。

### 1.2 目标

> 在不改变 Desktop 协议、Session 数据格式、Agent Core 算法和 EventBus/WS 语义的前提下，把核心数据面实现迁入 `services` 的正式 Owner；旧目录降级为单向兼容 re-export，生产路径不再依赖旧目录的业务实现。

### 1.3 迁移原则

1. 一次只迁移一个能力边界：先建立契约，再迁 Provider，再迁 Consumer，最后删除旧实现。
2. 迁移期间只允许一份真实实现；旧路径只能导出新 Owner，不得复制代码或反向持有状态。
3. 保留公共 import 兼容，但新代码、Composition 和测试不得新增旧路径依赖。
4. 每个切片必须同时提供契约测试、旧数据回归、生命周期测试和导入边界测试。
5. 不借迁移机会重写算法；行为改变必须另开 PRD。

### 1.4 非目标

1. 不修改 `E:\ftre-agent-core`、Desktop、Octo 独立仓库或客户端协议。
2. 不更改 Session JSON、Mailbox pending、Agent 配置和 WebSocket payload 格式。
3. 不引入多进程、HMR、Marketplace、权限沙箱或新的数据库。
4. 不在本阶段重新设计 AgentLoop、SessionLane、Compaction 或 TurnExecutor 算法。

## 2. 需求范围

### 2.1 功能需求

- [x] **FR1：Session Provider 实迁移。** `SessionManager` 的真实实现、实体、消息、存储和搜索归入 `services/session`；`SessionService` 不再继承旧目录类。
- [x] **FR2：旧 Session 路径单向兼容。** `ftre.session.*` 只保留 re-export 或明确的兼容适配器；不得保留第二份 `SessionManager`、Repository 或 Store 实现。
- [x] **FR3：Workspace 消除旧管理器耦合。** Workspace、Session workspace 持久化和工具工作区访问只依赖 `sessions`/`workspaces` Service，不直接创建或保存旧 `SessionManager`。
- [x] **FR4：Agent Runtime Provider 实迁移。** AgentLoop、SessionLane、Mailbox、ContextGate、CompletionRegistry、TurnExecutor、Compaction 和 Agent Factory 的真实实现归入 `services/agent/runtime`。
- [x] **FR5：Agent Consumer 改用 Service。** AgentLoop 和 runtime 内部通过 `SessionService`、`MessageBusService`、`ToolService`、`CommandService` 和 `AgentProfileService` 的窄契约工作，不从旧目录导入 Manager 实现。
- [x] **FR6：MessageBus/Channel/Command/Tool Provider 实迁移。** 业务数据面实现归入 `services/messaging`、`services/command` 和 `services/tools`；旧目录仅保留兼容导出。
- [x] **FR7：HTTP/WS 去除旧聚合入口。** 按 Owner 拆出的 Router 和 WebSocket Provider 使用公共 Service；移除 `bind_legacy_api`、模块全局 setter 和 `ws_channel.py` 对旧 aggregate API 的依赖。
- [x] **FR8：旧内核和旧 Builtin 收尾。** 新生产路径不导入 `ftre.plugin.kernel` 或 `ftre.plugin.builtin`；本阶段只清理数据面旧实现，旧 Plugin Kernel/Builtin 作为 F1 兼容测试面保留，删除另开 Plugin Kernel 收尾阶段。
- [x] **FR9：每个迁移切片可逆。** Provider 的对象、任务、监听和注册通过 Composition/Fiber/Effect 关闭；重复 close 不产生异常或残留。
- [x] **FR10：迁移诊断。** 通过架构导入测试、模块 Owner 断言和 Composition 路由 Owner 快照，持续证明新生产路径不命中旧数据面实现；旧 Plugin Kernel 的命中计数留到后续 Plugin Kernel 收尾阶段。

### 2.2 非功能需求

- **兼容性**：现有 Python API、HTTP/WS 路径、Session JSON、Agent 并发不变量和工具返回值保持不变。
- **导入边界**：`services` 内部不得反向导入旧目录的业务实现；兼容层只能由旧路径导入新 Owner。
- **可回滚**：每个切片独立提交，测试失败时可以回退该切片而不回滚已完成的前一切片。
- **性能**：不增加跨进程通信、磁盘往返或数据面轮询；适配层不得在热路径重复复制大型消息。
- **可观测性**：旧路径兼容命中必须可在测试或诊断中确认，不能静默形成第二个事实源。

## 3. 技术方案

### 3.1 目标目录

```text
src/ftre/
├─ services/
│  ├─ session/
│  │  ├─ service.py              # SessionService 唯一真实实现
│  │  ├─ entity/                 # models/state
│  │  ├─ message/                # converter/multimodal/token
│  │  ├─ persistence/            # repository/json store
│  │  ├─ search.py
│  │  └─ plugin.py
│  ├─ agent/
│  │  └─ runtime/
│  │     ├─ factory.py
│  │     ├─ loop/
│  │     ├─ mailbox/
│  │     └─ compaction/
│  ├─ messaging/
│  ├─ command/
│  └─ tools/
├─ features/
└─ platform/
```

### 3.2 迁移顺序

1. **F2.1 Session/Workspace**：先迁持久化和 Session Service，旧 `ftre.session` 改为 re-export。
2. **F2.2 Agent Runtime**：迁移 AgentLoop 及其内部组件，建立 Runtime Provider；AgentService 只暴露稳定调用面。
3. **F2.3 MessageBus/Channel/Command/Tool**：迁移业务数据面实现，保留协议对象兼容导出。
4. **F2.4 HTTP/WS**：删除 legacy setter 和聚合 Router，按 Service/Feature Owner 装配全部路由。
5. **F2.5 清理**：删除旧实现、旧 Kernel、旧 Builtin；运行生产引用扫描和全量验收。

### 3.3 兼容层规则

允许：

```python
# src/ftre/session/manager.py
from ftre.services.session.service import SessionService as SessionManager

__all__ = ["SessionManager"]
```

禁止：

- 新目录继承旧目录实现。
- 新目录与旧目录各保留一份业务实现。
- 新代码继续从旧路径 import Manager、Store 或 Router。
- 兼容层持有全局单例、setter 或隐藏生命周期。

### 3.4 目标 Runtime 注入形态

```python
@dataclass(frozen=True)
class AgentRuntimeServices:
    sessions: SessionService
    message_bus: MessageBusService
    channels: ChannelService
    tools: ToolService
    commands: CommandService
    profiles: AgentProfileService


class AgentRuntimeProvider:
    def __init__(self, services: AgentRuntimeServices):
        self.services = services

    def build_loop(self) -> AgentLoop:
        return AgentLoop(services=self.services)
```

这段结构是目标形态；迁移过程中允许用 adapter 把现有对象映射成窄契约，但 AgentLoop 不得继续把旧 Manager 作为公共依赖类型。

## 4. 接口定义

### 4.1 Session Service

保留现有方法语义：`init/close/create/get/list/update/delete/fork`、mailbox admission、workspace 读写、search。所有返回模型从 `services.session.entity` 导出。

### 4.2 Agent Runtime

`AgentService` 继续提供 `submit/cancel/wait/status/list/get/is_busy`；Runtime 内部不得被 Feature Plugin 直接访问。Agent created/disposed 事件仍通过 Service/Cordis Event 暴露。

### 4.3 兼容导出

旧路径可以被外部历史调用方导入，但导入对象的 `__module__`、诊断信息和测试必须指向新 Owner；旧路径不得反向成为新代码依赖。

## 5. 验收标准

- [x] **AC1：Session 真实实现位于 `services/session`。** `SessionService` 不继承 `ftre.session.manager.SessionManager`；生产代码只存在一份 Session Manager/Repository/Store 实现。
- [x] **AC2：旧 Session import 兼容。** 现有测试和历史 import 仍通过，但 `ftre.session.*` 仅 re-export 新 Owner。
- [x] **AC3：Workspace 边界成立。** Workspace、工具和 Agent Runtime 不直接 import 旧 `SessionManager`。
- [x] **AC4：Session 回归通过。** CRUD、恢复、fork、mailbox、search、workspace 持久化测试全部通过。
- [x] **AC5：Agent Runtime 新 Owner。** `services/agent/runtime` 包含真实 Loop/Lane/Mailbox/Turn/Compaction 实现，旧 `ftre.agent` 只保留兼容导出。
- [x] **AC6：Agent 数据面不变量保持。** 同 Session 最多一个 active turn；turn 与 compaction 不并发；不同 Session 可并行；pending at-most-once。
- [x] **AC7：业务数据面 Provider 化。** Bus、Channel、Command、Tool 的真实实现位于 `services`，旧路径无第二份实现。
- [x] **AC8：HTTP/WS 旧聚合入口移除。** `bind_legacy_api`、模块全局 setter 和生产路径 `ftre.api.routes` 引用为零。
- [x] **AC9：旧 Kernel/Builtin 不再被生产导入。** 数据面迁移完成后，生产路径不导入旧 Kernel/Builtin；兼容测试面必须有明确文档和架构测试，删除动作另开阶段。
- [x] **AC10：生命周期可逆。** Composition dispose 后 Session、Agent Runtime、Channel、Task、Tool/Command/Router Contribution 无残留。
- [x] **AC11：完整质量门禁。** `python -m pytest -q`、`python -m ruff check src tests`、`git diff --check` 全部通过；Gateway health/WS/Session 手动回归通过。
- [x] **AC12：工作区干净。** F2 所有代码按切片提交，最终分支无未提交文件。

## 6. 测试计划

- `tests/contracts/test_f2_session_owner.py`：验证 SessionService 新 Owner、兼容导出和方法语义。
- `tests/architecture/test_f2_import_direction.py`：禁止新 services/features 导入旧业务实现，允许旧路径单向导出。
- `tests/startup/test_composition.py`：验证 Composition 使用新 Provider，不改变路由和 Plugin 状态。
- `tests/lifecycle/test_f2_runtime_cleanup.py`：验证 Runtime/Channel/Task/Contribution 的逆序清理和幂等性。
- 现有 Session、Agent、Bus、Channel、HTTP 回归测试全部保留并执行。
- 手动：启动 Gateway，health、WS attach/admission/cancel、Session create/fork/search、正常退出。

## 7. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-21 | 初始定稿；把 F1 的兼容窗口明确拆成 F2 数据面迁移，并规定旧路径只能单向 re-export | F1 已验证控制面，但核心数据面需要渐进迁移，避免一次性重写造成行为回归 |
| 2026-08-21 | 完成 F2.1 Session/Workspace 基础迁移与 F2.2 Agent Runtime 迁移；旧路径改为模块别名，Gateway 使用 AgentRuntimeProvider | 保持旧 import 和测试兼容，同时让真实实现和 Composition 依赖指向新 Owner |
| 2026-08-21 | 完成 F2.3 Bus、Channel、Command 和内置 Tool 实现迁移；新数据面不再导入旧包，旧包保留兼容模块别名 | 让 Agent Runtime 和 Gateway 的数据面依赖统一指向 services Owner，同时保持历史 import 与协议测试 |
| 2026-08-21 | 完成 F2.4 HTTP/WS 路由 Owner 迁移；Composition 直接注册 Session、Agent、Trace、Attachment、Command 和 Feature Router，WebSocket 复用 Composition Host | 移除生产启动路径的 aggregate API、全局 setter 和 legacy bind，同时保留旧 API 仅供历史兼容测试 |
| 2026-08-21 | F2.5 收尾口径明确为“数据面旧实现清零、旧 Plugin Kernel/API 兼容面隔离”；后者不在本阶段删除 | AC16 的兼容窗口用于避免一次性迁移破坏历史插件测试，Plugin Kernel 删除另开阶段 |
