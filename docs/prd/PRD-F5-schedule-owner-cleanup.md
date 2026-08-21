# PRD-F5-Schedule Owner 收敛与调度生命周期治理

> F4 已清理大部分迁移壳，但 Schedule 仍存在一组未完成的 Owner 迁移：
> `features/schedule/channel.py` 只是转发壳，`features/schedule/store.py` 只是重复导出，
> 真正的 `CronChannel`、`CronScheduler` 和 cron Tool 仍在 `services/tools/builtin/cron.py`，
> 并由 Gateway Bootstrap 手工创建。F5 将 Schedule 作为完整 Feature 收敛。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F5 |
| 名称 | Schedule Owner 收敛与调度生命周期治理 |
| 状态 | 草稿 |
| 创建日期 | 2026-08-21 |
| 定稿日期 | — |
| 验收日期 | — |
| 关联文档 | `docs/TODO.yaml` 阶段 F5；`AGENTS.md`；F4 PRD 与执行报告；`docs/PROCESS.md` |

## 1. 背景与目标

### 1.1 当前问题

| 文件 | 当前行为 | 架构问题 |
|---|---|---|
| `features/schedule/channel.py` | `from ftre.services.tools.builtin.cron import CronChannel` | Feature 只暴露转发壳，实际 Channel Owner 在 Tool 层 |
| `features/schedule/store.py` | 再导出 `ScheduleService` | 与 `service.py` 重复，没有独立职责 |
| `services/tools/builtin/cron.py` | 同时拥有 Store 函数、CronChannel、CronScheduler、cron Tool | 一个文件跨越存储、调度、Channel、Tool 四个层级 |
| `app/gateway/bootstrap.py` | 手工创建 `CronScheduler` 并启动 | Plugin 生命周期无法接管，关闭顺序和重复启动没有统一 Owner |
| `features/schedule/router.py` | 直接访问 `service.root` 和 JSON 文件 | Router 绕过 Service API，ScheduleService 不是唯一数据入口 |

### 1.2 目标

> Schedule Feature 独立拥有 Job Store、ScheduleService、CronScheduler、CronChannel、cron Tool
> 和 Router；Plugin 统一负责装配、启动、停止和 Effect 清理，Bootstrap 不再手工创建任何 Schedule 对象。

### 1.3 非目标

1. 不改变 cron JSON 文件格式、字段含义、默认目录和现有 HTTP `/api/cron` 协议。
2. 不引入新的调度库、数据库、分布式锁、跨进程调度或持久化队列。
3. 不修改 Desktop、`ftre-agent-core`、Octo 独立仓库或客户端协议。
4. 不在本阶段扩展 cron 表达式语法或新增调度产品能力。

## 2. 需求范围

### 2.1 功能需求

- [ ] **FR1：Schedule 公共契约冻结。** 明确 `ScheduleService` 的 Job CRUD、`CronScheduler` 的
  start/stop、`CronChannel` 的 Channel Contract 和 cron Tool 的注册接口；Router 不得访问
  文件系统或 Service 私有字段。
- [ ] **FR2：CronStore 单一持久化 Owner。** 将 `cron` JSON 的路径解析、读取、原子写入、删除、
  run_history 更新归入 `features/schedule/store.py` 的实际实现；`ScheduleService` 只编排
  Domain/Store，不再重复 `root.glob` 或直接写 JSON。
- [ ] **FR3：ScheduleService 完整 CRUD。** 提供 `list/get/create/update/delete/append_run` 等
  窄接口，校验 Job ID 和字段；Router、Tool、Scheduler 全部通过 Service 调用。
- [ ] **FR4：CronScheduler 迁入 Feature。** 将调度循环迁入 `features/schedule/scheduler.py`，
  只依赖 `ScheduleService`、`SessionService`、`MessageBusService`；不再依赖
  `services.tools.builtin.cron` 的全局 `CRON_DIR` 和函数。
- [ ] **FR5：CronChannel 迁入 Feature。** 将静默 Channel 的真实实现放入
  `features/schedule/channel.py`，通过 `ChannelService` 注册；停止时必须可逆注销，不能留
  `services/tools` 的反向依赖。
- [ ] **FR6：cron Tool 迁入 Feature。** 将 cron Tool 的参数定义、校验和执行逻辑放入
  `features/schedule/tool.py`，由 Schedule Plugin 通过 `ToolService.register(..., owner="schedule")`
  注册，并在 unload 时移除。
- [ ] **FR7：Schedule Plugin 接管生命周期。** Plugin 注入 `message_bus/sessions/channels/tools`
  和必要的 Config，创建 Service/Channel/Scheduler，按“提供 Service → 注册 Channel/Tool →
  启动 Scheduler”的顺序装配；所有资源绑定 `ctx.effect`，重复 close 幂等。
- [ ] **FR8：Bootstrap 解耦。** 从 `app/gateway/bootstrap.py` 删除 `CronScheduler` import、手工
  构造、`start()` 和 `stop()`；Gateway 只通过 Composition 加载 Schedule Plugin。
- [ ] **FR9：Router Service-only。** `features/schedule/router.py` 只调用 Service 公共方法，
  不访问 `service.root`、`Path`、`json.loads` 或 JSON 文件。
- [ ] **FR10：旧实现与空壳删除。** 删除 `src/ftre/services/tools/builtin/cron.py`，并确保
  `features/schedule/channel.py`、`store.py`、`tool.py` 都是有实际职责的 Feature 模块，不得
  出现单行 re-export 或 `import *`。
- [ ] **FR11：并发与重复启动保护。** Scheduler 同一实例最多一个后台 Task；Plugin 重复激活、
  reload、unload 和 close 不产生重复 Channel、重复 Tool 或悬挂 Task。
- [ ] **FR12：测试与架构门禁。** 新增 Schedule Owner、生命周期、Store 安全、Router Service-only
  和 Bootstrap 无手工装配测试；禁止新代码从 `services.tools.builtin.cron` 导入 Schedule 实现。

### 2.2 非功能需求

- **兼容性**：保留 Job JSON 字段、`/api/cron` 路由、cron session channel_id=`cron` 和 Tool 行为。
- **生命周期**：所有 Task、Channel、Tool、Watcher 和文件资源都有明确 disposer。
- **可测试性**：Scheduler 接收可注入的 clock/scan interval；测试不得等待真实 30 秒。
- **可观测性**：启动、停止、触发和失败日志使用 Schedule Owner 命名空间。
- **安全性**：Job ID 只能定位 Schedule Store 根目录下的文件，禁止路径穿越。

## 3. 目标结构与技术方案

### 3.1 目标文件树

```text
src/ftre/features/schedule/
├─ __init__.py
├─ plugin.py                 # 唯一装配入口与生命周期
├─ service.py                # ScheduleService：公开 Job API
├─ store.py                  # CronStore：JSON 文件与原子持久化
├─ scheduler.py              # CronScheduler：周期扫描与投递
├─ channel.py                # CronChannel：静默 outbound sink
├─ tool.py                   # build_cron_tool：ToolService Contribution
└─ router.py                 # 只调用 ScheduleService 公共 API
```

### 3.2 依赖方向

```text
Schedule Plugin
  ├─ ScheduleService ──> CronStore
  ├─ CronScheduler ────> ScheduleService + SessionService + MessageBusService
  ├─ CronChannel ──────> ChannelService
  ├─ cron Tool ────────> ScheduleService + ToolService
  └─ Router ───────────> ScheduleService
```

禁止方向：

```text
features/schedule  -X-> services/tools/builtin/cron
app/gateway        -X-> CronScheduler()
router             -X-> service.root / JSON 文件
```

### 3.3 Plugin 生命周期

1. 声明 `inject = ("message_bus", "sessions", "channels", "tools")`、`provide = ("schedule",)`。
2. 创建 `CronStore` 与 `ScheduleService`，通过 `ctx.provide("schedule", service)` 发布。
3. 注册 `CronChannel` 和 cron Tool，并把两个 disposer 放入 Fiber Effect。
4. 创建 `CronScheduler`，启动一个后台 Task；`ctx.effect(scheduler.stop, label="schedule:stop")`。
5. unload/close 时按逆序停止 Scheduler、移除 Tool、注销 Channel、释放 Service。

## 4. 接口定义

### 4.1 ScheduleService

```python
class ScheduleService:
    key = "schedule"

    def list(self) -> list[dict]: ...
    def get(self, job_id: str) -> dict | None: ...
    def create(self, payload: dict) -> dict: ...
    def update(self, job_id: str, patch: dict) -> dict: ...
    def delete(self, job_id: str) -> bool: ...
    def append_run(self, job_id: str, timestamp: float | None = None) -> None: ...
```

### 4.2 CronScheduler

```python
class CronScheduler:
    def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def tick(self, now: float | None = None) -> int: ...
```

`tick()` 返回本次触发数量，测试可直接调用；后台循环只负责按 interval 调用 `tick()`。

## 5. 验收标准

- [ ] **AC1：Owner 清晰。** `features/schedule/channel.py`、`store.py`、`tool.py` 均包含实际
  实现；不存在单行 re-export、`import *` 或对 `services.tools.builtin.cron` 的导入。
- [ ] **AC2：旧实现删除。** `src/ftre/services/tools/builtin/cron.py` 不存在；生产代码和
  测试不再导入该路径。
- [ ] **AC3：Store/Service 行为。** Job list/get/create/update/delete、run_history、非法 ID
  和 JSON 损坏处理测试通过，文件格式保持兼容。
- [ ] **AC4：Scheduler 行为。** 到期 Job 只触发一次、disabled Job 跳过、无效 cron 跳过、
  session/message bus 投递正确、stop 能结束后台 Task。
- [ ] **AC5：Channel/Tool 生命周期。** Schedule Plugin 激活后存在 cron Channel 和 cron Tool，
  unload 后两者均消失且无重复注册。
- [ ] **AC6：Router 边界。** Router 测试通过 AST/Mock 证明只调用 ScheduleService，不读文件或
  私有 root。
- [ ] **AC7：Bootstrap 解耦。** `bootstrap.py` 不导入或构造 `CronScheduler`；Composition 默认
  Schedule Plugin 可激活并负责启动/停止。
- [ ] **AC8：并发安全。** 重复 start/stop、重复 Plugin settle/close、并发 tick 不创建重复
  Task 或重复 Job 触发。
- [ ] **AC9：完整质量。** `python -m pytest -q`、`python -m ruff check src tests`、
  `git diff --check`、Gateway health、WebSocket attach 和正常 dispose 全部通过。
- [ ] **AC10：收尾。** F5 分片提交，PRD/TODO/CHANGELOG/执行报告同步，分支干净。

## 6. 测试计划

- `tests/features/schedule/test_store.py`：Store 路径安全、CRUD、损坏文件、原子写入。
- `tests/features/schedule/test_service.py`：Service 公共接口和字段校验。
- `tests/features/schedule/test_scheduler.py`：可注入 clock 的 tick、去重、disabled、投递和 stop。
- `tests/features/schedule/test_plugin.py`：Plugin 激活、Tool/Channel 注册、unload cleanup。
- `tests/features/schedule/test_router.py`：Router 只通过 Service API。
- `tests/architecture/test_f5_schedule_owner.py`：旧 cron 模块、Bootstrap 手工装配和转发壳门禁。
- `tests/startup/test_composition.py`、`tests/lifecycle/test_effect_cleanup.py`：启动与生命周期回归。

## 7. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-21 | 初始草案：将 Schedule 的 Store、Scheduler、Channel、Tool 和 Plugin 生命周期收敛到 Feature | F4 后发现 Schedule 仍存在跨层实现和空转发壳 |
