# F5 Schedule Owner 收敛与调度生命周期治理执行报告

## 1. 执行结论

F5 已完成。Schedule 不再把 Store、Channel、Scheduler 和 Tool 混在
`services/tools/builtin/cron.py` 中，也不再由 Gateway Bootstrap 手工启动。现在由
`features/schedule` 统一拥有数据持久化、Job API、调度投递、静默 Channel、cron Tool
和 Plugin 生命周期。

本阶段只修改 `E:\ftre`，未修改 Desktop、`ftre-agent-core`、Octo 或客户端。

## 2. 迁移结果

### 2.1 Schedule Owner 文件树

```text
src/ftre/features/schedule/
├─ plugin.py                 # 组合 Service、Channel、Tool、Scheduler
├─ service.py                # ScheduleService：唯一 Job 公共 API
├─ store.py                  # CronStore：路径安全、JSON、原子写入
├─ scheduler.py              # CronScheduler：扫描与 inbound 投递
├─ channel.py                # CronChannel：内部静默 sink
├─ tool.py                   # build_cron_tool：ToolService contribution
└─ router.py                 # 只调用 ScheduleService
```

### 2.2 数据与行为迁移

- `CronStore` 接管默认 `~/.ftre/cron`、Job ID 校验、JSON 损坏隔离、原子替换、删除和
  `run_history` 追加。
- `ScheduleService` 提供 `list/get/create/update/delete/append_run`，Router、Tool、
  Scheduler 均不再读写文件。
- `CronScheduler` 只依赖 Schedule、Session 和 MessageBus Service；同一实例重复
  `start/stop` 安全，并用 tick 锁防止并发重复触发。
- `CronChannel` 真实实现位于 Feature 内，通过 `ChannelService.register` 注册，卸载时
  可逆注销。
- cron Tool 由 Schedule Plugin 通过 `ToolService.register(..., owner="schedule")` 提供，
  卸载时移除，默认 Tool Builder 不再重复注册。

### 2.3 启动与清理

- `app/gateway/bootstrap.py` 已删除 CronScheduler import、构造、`start()` 和 `stop()`。
- Schedule Plugin 按“提供 Service → 注册 Channel/Tool → 启动 Scheduler”顺序装配，所有
  disposer 均绑定 Cordis Effect。
- 删除 `src/ftre/services/tools/builtin/cron.py` 以及 Schedule 的旧单行转发职责。

## 3. 分阶段提交

| 提交 | 内容 |
|---|---|
| `26c76f7` | F5 PRD 定稿（由 prd-update 分支回填） |
| `d8aad9c` | F5 TODO 进入开发（由 todos-update 分支回填） |
| `09c4da4` | Schedule Owner 实现迁移与旧 cron 删除 |
| `9a362e2` | Schedule Store/Service/Scheduler/Plugin/Router 测试与架构门禁 |
| `58ecf39` | 加固 Job ID、重复创建、损坏排序和 run_history 边界 |
| 收尾提交 | PRD、TODO、CHANGELOG 与执行报告同步 |

## 4. 自动化验证

```text
python -m pytest -q
327 passed

python -m ruff check src tests
All checks passed!

git diff --check
通过
```

新增验证覆盖：

- `tests/features/schedule/test_store.py`：路径穿越、损坏 JSON、原子写入、CRUD、run_history；
- `tests/features/schedule/test_service.py`：完整 CRUD 与字段校验；
- `tests/features/schedule/test_scheduler.py`：到期/禁用/并发 tick、重复 start/stop；
- `tests/features/schedule/test_plugin.py`：Channel、Tool、Scheduler 注册与卸载清理；
- `tests/features/schedule/test_router.py`：HTTP CRUD 仅通过 Service；
- `tests/architecture/test_f5_schedule_owner.py`：旧模块删除、Bootstrap 解耦、Owner 真实实现。

## 5. 验收对照

| 验收项 | 结果 |
|---|---|
| AC1 Owner 清晰 | 通过：Feature 模块均为实际实现 |
| AC2 旧实现删除 | 通过：旧 cron 文件和生产导入均不存在 |
| AC3 Store/Service 行为 | 通过：专项测试覆盖 |
| AC4 Scheduler 行为 | 通过：到期一次、禁用跳过、消息投递、停止清理 |
| AC5 Channel/Tool 生命周期 | 通过：Plugin 激活注册、卸载移除 |
| AC6 Router Service-only | 通过：无 root/Path/JSON 读取 |
| AC7 Bootstrap 解耦 | 通过：仅 Composition 加载 Schedule Plugin |
| AC8 并发安全 | 通过：tick 锁与幂等 start/stop |
| AC9 完整质量 | 通过：327 pytest、ruff、diff check |
| AC10 收尾 | 通过：PRD/TODO/CHANGELOG/执行报告已同步 |

## 6. 已知边界

- F5 保持原有 Job JSON 字段、`/api/cron` 路由、`cron` channel_id 和 Tool 文案行为，未
  引入数据库、分布式调度或新的 cron 语法。
- 调度器仍是单进程周期扫描器；跨进程抢占和持久化队列不在本阶段范围内。
- 测试运行会生成 Python 缓存；收尾步骤会在最后一次验证后删除这些非源码缓存，不再
  重新运行会生成缓存的命令。

## 7. 收尾状态

- 分支：`feature/F5-schedule-owner-cleanup`
- PRD：`docs/prd/PRD-F5-schedule-owner-cleanup.md`（已验收）
- TODO：阶段 F5（`done`）
- CHANGELOG：已追加 `[未发布]` F5 条目
- 最终工作区：收尾提交后清理非源码缓存并确认干净
