# PRD-F9 Service 依赖注入与架构债务清理

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F9 |
| 名称 | Service 依赖注入与架构债务清理 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-21 |
| 定稿日期 | 2026-08-21 |
| 验收日期 | 2026-08-21 |
| 关联文档 | `docs/TODO.yaml` F9；`docs/prd/README.md`；`AGENTS.md`；`docs/PROCESS.md` |

## 1. 背景与目标

### 1.1 当前问题

ftre 已经具备 Cordis `inject/provide` 机制，但运行时仍存在大量“看起来是 Service，
实际上通过对象字段、闭包、手工构造或全局路径直接获取依赖”的情况。

已发现的典型问题：

```text
Command Handler
  → loop.compaction
  → loop.session_manager

TurnExecutor
  → loop.session_manager

Bootstrap
  → 手工构造多个 Service
  → 再把实例塞进 AgentLoop

Feature/Service
  → 直接导入其他 Feature/Service 的具体实现
```

这些问题会造成：

1. Service Owner 不清晰，调用方必须知道 AgentLoop 内部字段；
2. Plugin Manifest 的 `inject` 声明与真实依赖不一致；
3. 单元测试需要构造完整 Gateway/AgentLoop，无法替换窄依赖；
4. unload/restart 时依赖生命周期无法由 Cordis 统一管理；
5. 直接 import 具体实现形成隐藏耦合和循环依赖；
6. `Any`、动态 `getattr`、兼容 facade 和重复适配器掩盖真实依赖图。

### 1.2 目标

建立清晰的 Service 依赖规则：

```text
Plugin
  ├─ inject = ("sessions", "compaction", ...)
  ├─ 从 ctx 获取已注入 Service
  ├─ 创建自己的实现
  ├─ provide 自己的 Service key
  └─ 所有注册/监听/任务绑定 ctx.effect

Service
  └─ 只持有由 Plugin/Composition 注入的公开依赖

Composition Root / Provider
  └─ 负责装配，不承载业务规则
```

本阶段完成后：

- Service 之间的依赖可从 `inject`、Provider 构造参数或公开 Contract 直接读出；
- Service/Feature 不再通过 `AgentLoop.xxx` 获取其他 Service；
- 具体 Service 不在消费者内部自行实例化；
- 跨层依赖只引用稳定 Contract、Entity 或 Infrastructure Port；
- 依赖缺失由 Cordis Fiber/Diagnostics 报告，不在运行到深处才抛 `AttributeError`；
- 架构债务有扫描清单、禁止规则和自动化门禁。

### 1.3 非目标

- 不在本阶段改变业务行为、客户端协议或 Agent Core 算法；
- 不重新设计 Cordis `Context`、Fiber 和 Hook Runtime；
- 不强制所有内部纯函数都通过注入获取；
- 不禁止 Composition Root 和专属 Provider 构造它们自己拥有的实现；
- 不把所有模块改造成独立 Plugin；
- 不顺便处理 F6.12 cordis-py PyPI 发布。

## 2. 依赖规则

### 2.1 允许的依赖获取方式

1. **Plugin 依赖 Service**：在模块级声明 `inject`，在 `apply(ctx)` 中使用
   `ctx.<service_key>` 或等价的已注入句柄。
2. **Plugin 提供 Service**：在模块级声明 `provide`，通过 `ctx.provide(key, value)`
   发布唯一 Owner。
3. **Service 依赖 Service**：由所属 Plugin 注入后传入 Service 构造函数；Service 不主动
   从全局 Context、Loop 或模块单例查找依赖。
4. **AgentLoop Provider**：可以接收公开 Service 句柄来构造内部数据面，但必须使用
   明确的类型化 Provider 参数，不得让业务 Handler 反向访问 Loop 字段。
5. **纯内部依赖**：同一 Service/Feature 内部的 Entity、Repository、纯函数和数据转换
   可以直接导入，不要求为每个函数增加注入层。

### 2.2 禁止的依赖获取方式

- `loop.session_manager`、`loop.compaction`、`loop.commands` 等跨 Service 间接引用；
- 在 Service/Feature 消费者内部执行 `SessionService()`、`CompactionService()` 等具体
  Service 构造；
- 用全局变量、模块缓存、单例注册表或 `getattr()` 代替 Service 注入；
- 必选依赖使用 `ctx.get(key, strict=False)` 后静默降级；
- Feature 导入另一个 Feature 的私有实现；
- HTTP/WS/Command Handler 直接访问 AgentLoop、SessionLane、TurnExecutor 私有字段；
- 用 `Any`、裸 `dict` 或字符串字段隐藏跨 Service 调用契约；
- 在 `bootstrap.py` 之外复制 Service 装配逻辑。

### 2.3 允许的特殊边界

- Composition Root 可以创建并提供基础 Service；
- Service 自己的 Plugin 可以创建该 Service 的唯一实现；
- AgentLoop Provider 是数据面装配适配器，可以将 Service 句柄传入内部 runtime；
- 可选依赖可以使用 `strict=False`，但必须在 PRD/注释中说明降级语义并有测试；
- 领域层可以调用自己的 Repository，不得借 Repository 绕过另一个 Service 的 Owner。

## 3. 需求范围

### 3.1 功能需求

- [x] **FR1：依赖图基线**
  - 扫描 `src/ftre/app`、`platform`、`services`、`features` 的 import、构造、字段和
    `ctx` 访问，生成 Service/Plugin 依赖清单。
  - 为每条跨模块依赖标记 `allowed`、`migration` 或 `forbidden`。
  - 基线必须包含 Owner、注入入口、生命周期 Owner 和测试覆盖。

- [x] **FR2：Inject/Provide 声明完整**
  - 每个 Provider Plugin 的 `inject` 与实际必选依赖一致。
  - 每个公共 Service 只有一个 `provide` Owner；重复提供必须被 Runtime 拒绝或诊断。
  - `apply(ctx)` 不得通过隐藏全局对象获得依赖。

- [x] **FR3：Service 构造边界**
  - Service 实现只能由自己的 Plugin、Composition Root 或明确的 Provider 创建。
  - 消费者只能接收公开 Service/Contract，不能在调用方法内部 `new` 另一个 Service。
  - 构造函数参数必须表达真实依赖，禁止用 `Any` 或任意 `**kwargs` 隐藏必选依赖。

- [x] **FR4：Command Service Owner 收敛**
  - `/compact`、`/compress-fast` 使用注入的 `CompactionService`。
  - `/fork` 使用注入的 `SessionService`。
  - Command Handler 删除对 `AgentLoop` 完整对象的闭包捕获。
  - 与 F8 的 Command Runtime 解耦保持一致。

- [x] **FR5：Agent 数据面依赖边界**
  - `AgentLoopProvider` 使用类型化的 `AgentRuntimeServices` 装配内部 runtime。
  - `TurnExecutor`、`SessionLane` 和 `ContextGate` 不通过 Loop 字段向外暴露 Service。
  - HTTP/WS/Feature 只使用 `AgentService`/公开 Driver，不直接调用 `AgentLoop`。
  - AgentLoop 保留数据面编排职责，不成为 Service Locator。

- [x] **FR6：Feature 依赖边界**
  - Compaction、Schedule、MCP、Skill、Plan、Team 等 Feature 只通过注入 Service、
    Hook Runtime 或公开 Contract 协作。
  - Feature 不导入其他 Feature 的私有 Service、Channel 或 Scheduler 实现。
  - Feature 注册的 Router、Tool、Channel、Hook 和后台任务绑定 `ctx.effect`。

- [x] **FR7：配置、存储与基础设施依赖收敛**
  - Config、Filesystem、Attachment、Trace、Session Repository 等基础设施的 Owner
    明确，业务 Service 不直接读取全局路径或环境变量替代 Config Service。
  - 允许的 Infrastructure 直连必须在 Contract/架构清单中登记。
  - 业务 Service 不跨层访问 Repository 私有字段。

- [x] **FR8：生命周期和可选依赖语义**
  - 必选依赖缺失时 Fiber 保持 pending，并产生稳定诊断；不得静默创建替代 Service。
  - 可选依赖必须有显式降级行为、日志和测试。
  - unload/restart 后所有注入的监听、任务、路由和资源可逆且不残留旧实例引用。

- [x] **FR9：架构债务清理**
  - 删除 `loop.<service>` 间接访问、重复 Service facade、未使用兼容导出、动态
    `getattr` 依赖和无生产引用的适配器。
  - 扫描并清理空目录、`__pycache__`、死模块、重复 `__init__` 导出和过时注释。
  - 每个保留的兼容入口必须有 owner、用途、删除条件和测试说明；没有说明的直接删除。

- [x] **FR10：架构门禁**
  - 新增 AST/import 规则，禁止 Service/Feature/Interface 访问上述禁用路径。
  - 新增 Plugin manifest 规则，校验 `inject/provide`、Owner 唯一性和必选依赖。
  - 依赖图和债务清单纳入测试，新增违规依赖必须先更新 PRD/TODO。

### 3.2 非功能需求

- **可测试性**：Service 可使用 fake/stub Contract 独立测试，不需要启动完整 Gateway。
- **可诊断性**：依赖缺失、重复提供、作用域错误和卸载残留有明确诊断。
- **可维护性**：通过目录、Manifest 和构造签名可以读出依赖方向。
- **生命周期安全**：依赖实例与 Plugin/Fiber 同生命周期，不保留已卸载对象。
- **性能**：注入发生在组合/启动阶段，热路径不新增全局查找。

## 4. 目标架构

### 4.1 Provider Plugin 模式

```python
inject = ("sessions", "hook_runtime")
provide = ("compaction",)


def apply(ctx: Context, config=None):
    service = CompactionService(
        session_manager=ctx.sessions,
        hook_runtime=ctx.hook_runtime,
    )
    ctx.provide("compaction", service)
    ctx.effect(service.close, label="compaction:close")
```

规则：

- `ctx.sessions` 是 Plugin 的注入依赖；
- `CompactionService` 不自己调用 `Context.get()`；
- Command、AgentLoop、其他 Feature 只消费 `CompactionService`；
- `CompactionService` 的实现 Owner 仍属于 Compaction Feature。

### 4.2 Composition Root 与 Provider 边界

```text
Composition Root
  → 声明 Plugin 顺序和初始 Service

Provider Plugin
  → inject 已存在 Service
  → 创建自己的实现
  → provide 自己的 key

AgentLoopProvider
  → 接收类型化 Service 句柄
  → 构造内部 AgentLoop/SessionLane/TurnExecutor

业务 Service / Feature
  ✕ 不反向查找 AgentLoop 或其他 Service 的内部字段
```

### 4.3 依赖方向

```text
interfaces
   ↓
features / application services
   ↓
public contracts / platform ports
   ↓
infrastructure implementations
```

同层协作优先通过：

1. `inject/provide` 的 Service key；
2. 已有 Hook/Event；
3. 明确的公开 Contract；

禁止通过私有模块 import、Loop 字段或全局单例穿透层级。

## 5. 当前债务初始清单

| 类别 | 代表位置 | 处理策略 |
|---|---|---|
| Loop 间接 Service 引用 | `services/command/builtin.py` | 迁移到直接注入公开 Service |
| TurnExecutor 穿透 Session | `services/agent_loop/runtime/loop/turn_executor.py` | 由 Provider/Runtime 明确传递窄依赖，删除 `loop.session_manager` 读取 |
| 手工 runtime 组装 | `app/gateway/bootstrap.py`、`services/agent_loop/provider.py` | 保留 Composition 装配职责，删除重复构造和动态回退 |
| `Any` Service 聚合 | `AgentRuntimeServices` | 改为公开 Contract/Protocol 或稳定 Service 类型 |
| 必选依赖宽松读取 | 多个 `plugin.py` 的 `ctx.get(..., strict=False)` | 必选依赖改为 Inject 失败诊断，可选依赖单独标注 |
| Feature 跨层具体实现 import | `features/mcp/adapter.py`、`features/plan/plugin.py` 等 | 改为公开 Service/Contract；保留纯数据类型直连 |
| 工具直接调用 AgentLoop | `tools/builtin/task.py`、`team.py` 等 | 改用 `AgentService`/公开 Driver |
| Tool 旧 Session 注入键 | `Injected("session_manager")` 与公开 `sessions` Service key 不一致 | 统一为 `Injected("sessions")`，运行时上下文只发布公开 Service key |
| 全局路径/配置读取 | Agent config、Trace、MCP 连接等 | 通过 Config/Filesystem Service 注入 |
| 动态兼容与死代码 | `getattr`、旧 facade、空目录、缓存 | 清理并增加架构门禁 |
| Title 后台线程 | `services/session/title/generator.py` 守护线程无 Fiber disposer | 增加 stop flag、worker registry、bounded join，并绑定 `ctx.effect(generator.close)` |
| 纯图片编码转换 | `services/attachment/codec.py` 被 Session 消息归一化直接调用 | 明确保留为无状态 Infrastructure Helper；它不持有 Service、路径或全局状态，不承担落盘 Owner |

此表是初始基线，F9.1 扫描后必须补充文件、Owner、风险和验收测试。

## 6. 实施计划

### F9.1 依赖图与债务基线

- 扫描 import、构造、字段访问、`ctx.get`、`getattr`、闭包和全局单例；
- 生成 Service Owner/依赖图/违规清单；
- 给每条债务标注保留、迁移或删除结论。

### F9.2 Inject/Provide 契约与门禁

- 冻结 Service key、Plugin `inject/provide` 规则和可选依赖语义；
- 增加重复 Owner、缺失依赖、必选依赖宽松读取测试；
- 建立 AST/import 架构测试。

### F9.3 Command、Compaction、Session 依赖迁移

- 配合 F8，删除 Command Handler 的完整 Loop 闭包；
- `/compact`、`/compress-fast` 注入 `CompactionService`；
- `/fork` 注入 `SessionService`；
- 直接引用公开 Contract，不引用 Feature 私有实现。

### F9.4 AgentLoop Provider 边界收敛

- 将 `AgentRuntimeServices` 的 `Any` 依赖改为稳定类型；
- Provider 负责一次性装配，运行时不再作为 Service Locator；
- TurnExecutor、SessionLane、ContextGate 只获得它们各自需要的窄依赖；
- 删除通过 `loop.<service>` 反查公共 Service 的路径。

### F9.5 Feature/Infrastructure 依赖迁移

- 清理 Feature 之间的具体实现 import；
- 配置、Filesystem、Attachment、Trace 和 Session Repository 依赖归位；
- 保留纯 Entity/Contract 直连，禁止业务模块穿透私有存储实现。

### F9.6 生命周期与作用域验证

- 验证 unload/restart 后无监听、Task、Router、Service 旧实例残留；
- 验证必选/可选依赖的 pending、降级和诊断；
- 验证不同 Agent scope 的 Service 实例不互相污染。

### F9.7 全盘债务清理

- 删除无生产引用的模块、兼容导出、空目录和 `__pycache__`；
- 更新测试 Fixture、文档、注释和依赖清单；
- 运行架构扫描，确保没有新增同类债务。

### F9.8 全量验收与收尾

- 全量 pytest、ruff、YAML、diff check 和 Gateway smoke；
- 更新 PRD、TODO、CHANGELOG 和执行报告；
- 每条债务必须有“已迁移/已删除/明确保留”结论。

## 7. 验收标准

- [x] **AC1：依赖图完整**
  - 所有 `services/`、`features/`、`app/` 的跨模块依赖都有 Owner、来源、目标、状态
    和测试证据。

- [x] **AC2：Inject/Provide 门禁**
  - 必选 Service 依赖均在 `inject` 或 Provider 参数中声明；重复 Owner、缺失依赖和
    未声明依赖均能被测试发现。

- [x] **AC3：禁止 Loop Service Locator**
  - Command、Feature、HTTP/WS、Tool 不访问 `loop.<service>`；
  - `loop.session_manager`、`loop.compaction` 等路径在生产依赖扫描中清零。

- [x] **AC4：Service Owner 正确**
  - `/compact` 使用 `CompactionService`；`/fork` 使用 `SessionService`；不再通过完整
    `AgentLoop` 闭包获得 Service。

- [x] **AC5：Provider 类型化**
  - `AgentRuntimeServices` 不使用无边界 `Any` 隐藏必选 Service；Provider 是唯一的
    AgentLoop 装配入口。

- [x] **AC6：跨层依赖清理**
  - Feature 不 import 其他 Feature 私有实现；业务 Service 不访问 Repository 私有字段；
    配置、Filesystem、Attachment、Trace 的 Owner 清晰。

- [x] **AC7：生命周期可逆**
  - Plugin unload/restart 后无旧 Service、Hook、Task、Router、闭包或 Event Listener
    残留；同一 Scope 重建后状态隔离。

- [x] **AC8：债务清理完成**
  - 死代码、重复 facade、兼容导出、动态依赖、空目录和生成缓存完成扫描；保留项均有
    生产引用或测试用途说明。

- [x] **AC9：行为不变**
  - Session、Agent、Command、Compaction、Tool、HTTP/WS 现有行为回归通过；客户端无需
    修改。

- [x] **AC10：质量门禁**
  - `python -m pytest -q`、`python -m ruff check src tests`、YAML 校验、
    `git diff --check` 和 Gateway 启停 smoke 全部通过。

## 8. 测试计划

### 8.1 架构静态测试

- AST/import 扫描 Service/Feature/Interface 的禁止 import 和 `loop.<service>`；
- 扫描 `ServiceClass()` 的创建位置；
- 校验 `inject/provide` 与依赖图一致；
- 校验 `Any`、`getattr`、全局单例和旧兼容入口的允许清单。

### 8.2 契约测试

- 缺失必选依赖时 Fiber 状态和诊断正确；
- 重复提供同一 Service key 被拒绝；
- Plugin 通过 `ctx.effect` 注册的资源可逆清理；
- Service 使用 fake Contract 时可独立运行。

### 8.3 生命周期与作用域测试

- Plugin load/unload/restart；
- Service 实例替换后旧实例不再接收事件；
- Hook、Router、Tool、Channel、后台 Task 无残留；
- Agent scope 同 key 重建后依赖隔离。

### 8.4 回归测试

- `/compact`、`/fork` 和 Agent 数据面完整流程；
- Schedule、MCP、Skill、Plan、Team Feature 启停；
- Gateway 启动、关闭、重启和真实 WS smoke；
- 全量 `pytest` 和 `ruff`。

## 9. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-21 | 创建 F9 草稿，定义 Service Inject/Provide 规则和架构债务清理范围 | 现有 Service 之间存在 Loop 间接访问、手工构造、隐藏依赖和跨层具体实现引用 |
| 2026-08-21 | 完成 F9 实施与验收：AgentLoop/TurnExecutor 显式注入、Feature/Attachment Owner 收敛、动态依赖与重复 facade 清理；架构门禁与 402 项全量测试通过 | 建立可读、可替换、可逆的 Service 依赖图 |
| 2026-08-21 | 审计复核统一 Tool 的公开 `sessions` 注入键，并补齐 Session Title 后台线程的 Fiber 生命周期清理 | 消除旧命名残留和 unload 后后台线程继续写 Session 的风险 |
| 2026-08-21 | 审计修复后的联合全量门禁为 404 项通过；最终缓存、字节码和空目录均清零 | 以最终工作区状态作为交付证据 |
