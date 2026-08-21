# PRD-F10 Compaction Service Owner 收敛与 Port 删除

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F10 |
| 名称 | Compaction Service Owner 收敛与 Port 删除 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-22 |
| 定稿日期 | 2026-08-22 |
| 验收日期 | 2026-08-22 |
| 关联文档 | `docs/TODO.yaml` F10；`docs/prd/README.md`；`AGENTS.md`；`docs/PROCESS.md` |

## 1. 背景与目标

### 1.1 当前问题

当前上下文压缩的真实实现位于：

```text
features/compaction/service.py → CompactionService
services/compaction/contracts.py → CompactionPort
```

`AgentLoop`、`ContextGate` 和 Command 依赖 `CompactionPort`，由 Feature Plugin 创建
`CompactionService` 后以 `compaction` Service key 发布。这个设计虽然隔离了实现，但对
ftre 当前只有一个压缩实现的情况引入了额外概念和跨层跳转：

1. 核心公共能力位于 `features`，而调用方位于 `services`；
2. 同一个能力同时出现 Service、Port、Feature 三套命名；
3. Port 的方法容易与真实实现漂移（当前 `compact_now` 曾未在 Port 中声明）；
4. 维护者无法从 `services/compaction` 目录直接找到压缩实现；
5. 禁用压缩时的 No-op 行为与真实 Service 契约混在一起。

### 1.2 目标

将 Compaction 收敛为一个明确的公共 Service Owner：

```text
services/compaction/service.py → CompactionService（唯一真实实现）
services/compaction/plugin.py  → 创建并 provide("compaction")
features/compaction/plugin.py  → 只注册 compaction 相关 Hook
```

完成后：

- 删除 `CompactionPort` 和 `services/compaction/contracts.py`；
- `AgentLoop`、`ContextGate`、Command 和 Provider 直接使用 `CompactionService`；
- `CompactionService` 的实现、配置边界、状态和并发逻辑全部归 `services/compaction`；
- Feature 只贡献 `agent/pre-step`、`agent/request-error` 等行为 Hook，不拥有压缩状态；
- 公共 Service key 仍为 `compaction`，客户端和外部协议不变；
- 保留唯一 Composition/Plugin 装配路径，Service unload/restart 可逆。

### 1.3 非目标

- 不改变压缩算法、摘要 Prompt、压缩阈值、快速压缩语义或持久化消息格式；
- 不修改桌面端、客户端协议或 `E:\ftre-agent-core`；
- 不引入新的通用 Port、Adapter、Facade 或 Provider 抽象；
- 不在本阶段发布 cordis-py；
- 不重构其他 Service 的 Owner；
- 不改变 F8 Command Plane 的外部行为。

## 2. 术语与边界

### 2.1 CompactionService

ftre 的公共 Service，负责压缩任务的状态、并发、LLM 摘要、快速裁剪、事件发射器
绑定和取消。它是 `compaction` key 的唯一实际值类型。

### 2.2 Compaction Feature Hook

位于 `features/compaction/plugin.py` 的行为插件，只负责把 CompactionService 接入
Agent Hook 管线：

- `agent/pre-step`：执行水位检查和必要的压缩；
- `agent/request-error`：处理上下文溢出后的压缩重试；
- Hook 的注册和注销由 Cordis `ctx.effect` 管理。

Feature Hook 不创建 CompactionService，不持有独立压缩状态，不直接写 Session 或 WS。

### 2.3 No-op 行为

如果测试或特殊嵌入场景不提供压缩 Service，可以保留内部
`NullCompactionService` 作为显式 No-op 实现；它不是 Port、不是第二个 Owner，也不
作为公开依赖契约导出。默认 Gateway 的必选 `compaction` Plugin 必须提供真实
`CompactionService`。

## 3. 功能需求

### 3.1 Service Owner 迁移

- [x] **FR1：真实实现迁入 Service 层**
  - 将 `features/compaction/service.py` 迁移为
    `services/compaction/service.py`；
  - 保留 `CompactionService` 的公开行为、并发锁、任务表和事件出口；
  - 迁移后生产代码只能从 `ftre.services.compaction` 获取实现。

- [x] **FR2：删除 Port 契约**
  - 删除 `CompactionPort`、`services/compaction/contracts.py` 及其公开导出；
  - `AgentLoop`、`ContextGate`、Command、Provider 的类型标注改为
    `CompactionService` 或明确的可选 No-op 实现；
  - 生产代码和测试不得出现 `CompactionPort`。

- [x] **FR3：Service Plugin 唯一装配**
  - 新增 `services/compaction/plugin.py`，声明
    `inject = ("sessions",)`、`provide = ("compaction",)`；
  - Plugin 创建 `CompactionService(session_manager=ctx.sessions)` 并发布唯一实例；
  - `features/compaction/plugin.py` 不得调用 `ctx.provide("compaction", ...)`。

### 3.2 Feature Hook 拆分

- [x] **FR4：Feature 只贡献 Hook**
  - Feature Plugin 通过 `inject = ("compaction", "sessions", "hook_runtime")`
    获取已存在 Service；
  - 保留 `agent/pre-step`、`agent/request-error` 语义和失败策略；
  - 所有 Hook Receipt、后台任务和关闭动作绑定 `ctx.effect`。

- [x] **FR5：Composition 清单收敛**
  - `compaction` Manifest 的 Service Owner 指向
    `ftre.services.compaction.plugin:apply`；
  - Feature Hook 使用稳定的独立 Manifest id（建议 `compaction-hooks`），并声明
    对 `compaction` 的依赖；
  - 必选 Service 启动失败阻止 Gateway，Hook Plugin 不能创建半成品 Service。

### 3.3 调用方迁移

- [x] **FR6：Agent 数据面直接使用 Service**
  - `AgentRuntimeServices.compaction`、`AgentLoop`、`ContextGate` 使用
    `CompactionService | None`；
  - `AgentLoopProvider` 只传递 Composition 提供的 Service，不导入 Feature；
  - `ContextGate` 的 peek/policy/claim 和压缩屏障行为不变。

- [x] **FR7：Command 直接使用 Service**
  - `/compact`、`/compress-fast` 直接调用注入的 `CompactionService`；
  - `/compact` 使用真实实现公开的 `compact_now`，并在类型契约中真实存在；
  - Command 不导入 Feature 私有模块、不通过 Loop 字段获取压缩对象。

### 3.4 清理与一致性

- [x] **FR8：旧文件与旧导出清理**
  - 删除 `features/compaction/service.py`、`services/compaction/contracts.py`；
  - 更新两个 `__init__.py`、架构测试、契约测试、文档和导入；
  - 不保留单行转发、兼容 re-export 或旧 Port 别名。

- [x] **FR9：生命周期可逆**
  - Service 创建、Hook 注册、事件发射器绑定和 Service close 有明确 Owner；
  - Service Plugin/Feature Hook unload、restart、失败回滚和重复 close 幂等；
  - 正在执行的压缩 Task 在 Gateway 停止时被等待或取消，不残留旧 Service 引用。

- [x] **FR10：文档与架构门禁同步**
  - 更新 F8/F9/PRD 总览中对 `CompactionPort` 的旧描述；
  - 增加“Compaction 只有一个 Service Owner、Feature 只拥有 Hook”的架构测试；
  - 更新 TODO、CHANGELOG 和本阶段执行报告。

## 4. 目标文件树

```text
src/ftre/
├─ services/
│  └─ compaction/
│     ├─ __init__.py          # 导出 CompactionService / 事件名
│     ├─ events.py            # 压缩领域事件名
│     ├─ service.py           # 唯一真实实现与并发状态
│     └─ plugin.py            # inject sessions → provide compaction
│
└─ features/
   └─ compaction/
      ├─ __init__.py
      └─ plugin.py             # 只注册 pre-step / request-error Hook
```

依赖方向：

```text
AgentLoop / Command / ContextGate
             ↓
services.compaction.CompactionService
             ↑
services.compaction.plugin

features.compaction.plugin
             → 注入 compaction Service
             → 注册 Agent Hook
```

禁止方向：

```text
services.agent_loop → features.compaction.service
features.compaction.plugin → ctx.provide("compaction", ...)
任何调用方 → CompactionPort
```

## 5. 接口与生命周期

### 5.1 公共 Service 方法

迁移后 `CompactionService` 至少保留以下公开方法，不新增 Port：

```python
await service.should_compact(...)
await service.compact(...)
await service.compact_now(...)
await service.compact_if_needed(...)
await service.compress_fast(...)
service.is_compacting(session_id)
await service.cancel_compact(session_id)
await service.cancel_all_compact_tasks()
service.bind_event_emitter(emit_event)
```

### 5.2 生命周期表

| 资源 | 创建者 | 注册/绑定 | 停止/注销 | 失败回滚 |
|---|---|---|---|---|
| `CompactionService` | Service Plugin | `ctx.provide("compaction")` | Service Plugin close | Fiber 失败时撤销提供和任务 |
| Agent Hook Receipt | Feature Hook Plugin | `ctx.hook_runtime.register` | `ctx.effect(receipt.dispose)` | 任一 Hook 注册失败时回滚已注册 Receipt |
| Event emitter | AgentLoop Provider | `bind_event_emitter` | Service close/Loop stop | 不持有已卸载 Loop |
| Compact Task | CompactionService | `asyncio.create_task` | cancel_all + await | 失败保留 pending，不丢队首 |

## 6. 测试计划

### 6.1 架构测试

- `services/compaction/service.py` 存在且包含真实实现；
- `features/compaction/service.py` 和 `services/compaction/contracts.py` 不存在；
- 生产代码没有 `CompactionPort`；
- 只有 Service Plugin 提供 `compaction`；Feature Plugin 只声明 Inject；
- AgentLoop/Command/ContextGate 不导入 Feature 实现。

### 6.2 契约与行为测试

- `compact`、`compact_now`、`compact_if_needed`、`compress_fast` 行为保持；
- `/compact` 和 `/compress-fast` 通过 Service 调用；
- 压缩事件仍通过统一 Session Event 出口投影和广播；
- 阈值、压缩失败、快速压缩和取消语义不变。

### 6.3 生命周期与启动测试

- Service Plugin activate/settle/unload/restart；
- Feature Hook Plugin activate/settle/unload/restart；
- Service 创建失败、Hook 注册失败和压缩 Task 失败回滚；
- Composition required Plugin、路由快照和 Gateway shutdown；
- 全量 pytest、ruff、YAML、`git diff --check` 和生产旧引用扫描。

## 7. 验收标准

- [x] **AC1：唯一真实 Owner**
  - `CompactionService` 位于 `services/compaction/service.py`，Feature 目录不再包含
    压缩实现；生产代码没有第二个状态 Owner。

- [x] **AC2：Port 完全删除**
  - `CompactionPort`、`contracts.py`、旧导出、旧测试和旧文档引用全部删除。

- [x] **AC3：Service Plugin 装配正确**
  - `services/compaction/plugin.py` 是唯一 `provide("compaction")` 的入口；
    Composition 可在依赖缺失时给出稳定诊断。

- [x] **AC4：Feature Hook 纯化**
  - Feature Plugin 只注入 Service 并注册 Hook，不创建 Service、不拥有压缩状态。

- [x] **AC5：调用方直接使用 Service**
  - AgentLoop、ContextGate、Command、Provider 的类型和导入均指向
    `CompactionService`，不存在 `CompactionPort` 或 Feature 私有导入。

- [x] **AC6：行为保持**
  - 自动压缩、手动 `/compact`、`/compress-fast`、溢出重试、事件投影和 pending
    保留行为与迁移前一致。

- [x] **AC7：生命周期可逆**
  - unload/restart/close 后无 Hook、Task、Event emitter 或旧 Service 引用残留；
    重复 close 安全。

- [x] **AC8：协议与客户端不变**
  - Service key `compaction`、命令文本、Session Event 和 WS/HTTP 输出不变。

- [x] **AC9：架构债务清理**
  - 旧实现、Port、兼容导出、空目录、生成缓存和过时文档引用完成清理。

- [x] **AC10：质量门禁**
  - `python -m pytest -q`、`python -m ruff check src tests`、YAML、
    `git diff --check`、Composition smoke 和生产旧引用扫描全部通过。

## 8. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-22 | 创建 F10 草稿：将 Compaction 实现迁入 Service，删除 CompactionPort，Feature 只保留 Hook 行为 | 当前只有一个压缩实现，Port 增加了不必要的跨层抽象和维护成本 |
| 2026-08-22 | 完成 F10：Service Owner 迁移、Feature Hook 拆分、Port/旧实现删除；404 项全量测试、ruff、YAML、diff check 和 Composition 回归通过 | 让压缩能力回到公共 Service 层并减少抽象层级 |
| 2026-08-22 | 收尾审计将 `NullCompactionService` 收紧为 Service 模块内部 fallback，并补充 unload 取消 in-flight Task 测试；最终全量 405 项通过 | 避免 No-op 类型成为第二个公共 Owner，并补齐生命周期证据 |
