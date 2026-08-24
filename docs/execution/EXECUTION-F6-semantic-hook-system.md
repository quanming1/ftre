# F6 官方 cordis-py 运行时与语义 Hook 系统执行报告

## 1. 执行结论

F6.1–F6.11 已完成验收。ftre 已切换到官方 `cordis-py==0.4.0`，完成统一
Composition、类型化 Hook Runtime、Agent scope、Agent 状态机 Hook、Tool/Session/
Prompt/LLM 管线、Compaction Feature、Command 接入层解析以及生命周期/故障测试。

F6.12（PyPI 发行物和脱离 `E:\cordis-py` 的洁净安装）是明确的后置任务，仍保持
`docs/TODO.yaml` 的 `todo`，不阻塞本次 F6 核心验收。

本阶段边界只包含 `E:\ftre`，未修改 Desktop、`E:\ftre-agent-core`、`E:\cordis-py`、
Octo 或其他客户端仓库。

## 2. 最终架构结果

```text
Gateway Composition Root
  ├─ official cordis.Context / Fiber / Registry
  ├─ platform/hooks: HookSpec + HookRuntime + scope + diagnostics
  ├─ services/agent: Agent identity、Registry、公开 Driver 契约
  ├─ services/agent_loop: AgentLoop Provider、SessionLane、TurnExecutor
  ├─ services/session: durable Session、Mailbox、Projection、lifecycle/flush Hook
  ├─ services/tools + infrastructure/agent_core: typed Tool Hook adapter
  ├─ services/system_prompt + services/llm: structured prompt / stream Hook
  ├─ plugins/builtin/compaction: CompactionService + pre-step/request-error Hook
  └─ plugins/builtin/command: ingress parse/dispatch；不进入 Inbox 或模型上下文
```

### 2.1 Owner 与生命周期

| 资源 | Owner | 注册/启动 | 停止/卸载 | 故障结果 |
|---|---|---|---|---|
| Hook Listener | 当前 Cordis Plugin Fiber | `HookRuntime.register` + companion Effect | Fiber unload/restart 自动注销；等待 `active_calls == 0` | 诊断保留 owner/scope/order，不记录 payload |
| Agent scope | `AgentRegistry` | identity + parent carrier | dispose 后旧 identity 不再命中新 Agent | 同 ID 重建隔离 |
| Session | `SessionService` | repository commit 后发布 `session/created` | 删除 commit 后发布 `session/disposed`，`flush()` 是唯一屏障 | 观察失败不回滚事实 |
| SessionLane | `SessionLaneRegistry` | pending admission 后启动 drain | close/stop 取消 worker、压缩和等待者 | Hook/压缩失败保留 pending 或进入 BLOCKED |
| Compaction | `plugins/builtin/compaction` | Composition 显式加载 Feature | `cancel_all_compact_tasks` 等待真实 Task 退出 | overflow 只有持久进展才 Retry |
| Command | `CommandService` + ingress | AgentLoop 消费 Bus 时 parse | control/SessionLane 完成后返回 ACK | 命令不进入 Mailbox |

### 2.2 关键行为

- Agent 数据面保持 `Channel → EventBus → AgentLoop → SessionLane → TurnExecutor`。
- `agent/pre-step` 固化 `peek → decision → claim`，Hook 异常、压缩失败或取消期间不提前领取。
- `agent/request-error` 只接受有 progress token 和次数上限的 `RetryRequest`。
- `agent/turn-stopping` 是稳定 serial 收尾屏障；业务层 `agent.steer()` 输入 API 留给后续 continuation 阶段。
- Command 在 Bus 接入层解析；普通 Command 通过 SessionLane admission lock 串行执行。
- 旧 `before_run`、`before_messages_build`、Filter 和 `services/agent_loop/runtime/hooks.py` 已删除。

## 3. 变更清单

### 3.1 核心实现

- `src/ftre/kernel/hooks/runtime.py`：Fiber companion Effect、生命周期标记、in-flight quiescence、异步 emit observer 调度。
- `src/ftre/kernel/hooks/names.py`：补充 `session/created`、`session/disposed`。
- `src/ftre/services/session/`：Session lifecycle Hook、flush barrier、post-commit 事件。
- `src/ftre/services/agent_loop/`：AgentLoop Provider、SessionLane、TurnExecutor 和 Mailbox 数据面。
- `src/ftre/plugins/builtin/compaction/`：独立 CompactionService 与 Hook Feature。
- `src/ftre/plugins/builtin/command/`：接入层 parse/dispatch、CommandResult 路由。
- `src/ftre/infrastructure/agent_core/`：Tool/LLM 单向适配器。

### 3.2 测试

- `tests/hooks/`：五种 Cordis dispatch mode、waterfall、scope、诊断和 in-flight。
- `tests/contracts/`：Agent、Tool、Session、Prompt、LLM、Compaction、Command 契约。
- `tests/lifecycle/test_f10_lifecycle_faults.py`：Fiber reload、in-flight、scope 重建、取消、重试、压缩失败、pending 保留和去重执行。
- `tests/architecture/`：旧模块、旧 Hook、跨层私有依赖和唯一 Owner 门禁。

## 4. 验证证据

以下命令在最终代码状态执行：

```text
python -m pytest -q
383 passed

python -m pytest -q tests/hooks tests/contracts tests/architecture tests/lifecycle
100 passed

python -m ruff check --no-cache src tests
All checks passed!

git diff --check
通过
```

Gateway smoke：

```text
start_gateway(config={})
assert hook_runtime is not None
assert compaction is not None
composition.close()
GATEWAY START OK
GATEWAY CLOSE OK
```

最终只读复核：

- 生产源码旧 Hook 字符串命中数：`0`。
- `src`/`tests` 下 `__pycache__`：`0`。
- `src`/`tests` 下 `.pyc`：`0`。
- `src/ftre` 空目录：`0`。
- `docs/TODO.yaml` YAML 解析：通过。

## 5. PRD 验收对照

| 范围 | 结果 |
|---|---|
| PRE1–PRE10 官方 Cordis 基座 | 通过 |
| FR1–FR46（F6 核心范围） | 通过；FR24 明确限定为 serial 收尾屏障，业务 steer API 后置 |
| AC1–AC30 | 通过 |
| AC31–AC32 PyPI/洁净安装 | 后置 F6.12，未宣称完成 |

PRD 中的 FR/AC 已按上述结果更新；F6.12 的 PRE11–PRE16、AC31–AC32 保持未勾选。

## 6. 文档与 TODO 收尾

- PRD：`docs/prd/PRD-F6-semantic-hook-system.md` 状态更新为 `已验收`，补充验收日期、FR/AC 勾选和变更记录。
- TODO：F6 阶段及 F6.11 更新为 `done`；F6.12 保持 `todo`。
- CHANGELOG：`[未发布]` 增加 F6.11 最终验收条目。
- 执行报告：本文档。

## 7. Git 与已知边界

- 分支：`feature/F6-cordis-py-integration`。
- 本次未执行 commit、push 或 merge；工作区仍包含 F6.1–F6.11 的累计未提交改动。当前未发现额外生成缓存或空目录。
- F6.12 需要 `E:\cordis-py` owner 完成 TestPyPI/PyPI 发布后再执行；该任务不能在本仓库内伪造完成。
- 业务层 `agent.steer()` 的 continuation API 单独后置，不影响当前 serial `agent/turn-stopping` 屏障和现有 Agent 数据面。

## F11 后续变更

F11 在本报告的 F6.8–F6.11 基线上新增 `agent/after-turn`，并把压缩实现、Hook、命令
和算法迁入 `packages/ftre-compaction`；F6.8 中关于 `plugins/builtin/compaction` 或
`CompactionPort` 的路径描述是历史记录，不代表当前主包结构。
