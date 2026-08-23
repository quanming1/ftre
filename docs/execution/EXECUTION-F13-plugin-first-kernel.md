# F13 执行报告：Plugin-first 内核收敛与消息交接

## 结论

F13 已按 PRD 完成。核心结果是把 Gateway/Agent 的装配和业务路由从手工协调收拢到
Cordis Plugin/Fiber 生命周期，同时保持现有 `InboundMessage`、`request_id`、Inbox 和
WebSocket 语义不变。

本阶段没有新增 `AgentControlPort`、`CompactionPort`、Coordinator 或 Service Bag；
`ftre-inbox` 和 `ftre-compaction` 仍是可选能力，基础 Agent Runtime 不依赖它们的内部
Queue/Compaction 模型。

## 完成内容

### 1. 轻内核与唯一 Composition

- `src/ftre/platform/plugin_runtime/` 继续只提供 Context、Manifest、Discovery、Loader、
  Manager、Hook、Fiber/Effect 和诊断机制，不导入产品 Service。
- `src/ftre/app/gateway/composition.py` 只声明并装载 Plugin；不再创建业务对象图，
  不再手工注册业务 HTTP 路由。
- `src/ftre/app/gateway/bootstrap.py` 只负责 Host/Runtime 启停、监听地址覆盖和逆序关闭；
  不再 `new` Session、Bus、Channel、Agent、Tool、Command 或 AgentLoop。

### 2. Agent Runtime 与消息交接

- 新增 `src/ftre/services/agent_loop/plugin.py`，作为 AgentLoop 的唯一 Provider Plugin，
  通过 `AgentLoopProvider.from_context()` 读取公开 Context Service，绑定 `AgentService`
  Driver，并将 Loop/Driver 的关闭绑定到 Fiber Effect。
- Trace exporter 由 `TraceService.build_tracer()` 唯一创建并提供，AgentLoop 不再直接导入
  SQLite store 或创建第二个 exporter；仅保留一个 Trace 写入 Owner。
- 删除仅承载 `loop + driver` 的 `AgentLoopRuntime` DTO；Provider 创建 Loop，Provider Plugin
  创建 Driver，运行时公开句柄只保留实际生命周期能力。
- `AgentLoop.run_inbound(InboundMessage)` 先校验 Session/Channel、执行 before-turn Hook，
  再通过 SessionProjection 写入一次正式 `USER_MESSAGE`，随后才创建 TurnExecutor/LLM Task。
- `TurnExecutor` 删除普通 UserMessage 的重复持久化路径和 `persist_input` 参数；它只处理
  Turn/Reply/Tool 执行。
- QueueItem 仍只在 `packages/ftre-inbox` 内存在；Inbox claim 后才转换为
  `InboundMessage`，沿用同一个 `request_id` 关联历史和 ACK。

### 3. Provider/Feature 自有 HTTP 路由

路由注册已从 Composition Root 移到实际 Owner Plugin，并且每项注册均有可逆 Effect：

- Config、Session、Agent Profile、Command、Trace、Attachment Provider；
- Skill、MCP、Schedule Feature；
- WebSocket Provider 注册 `/` WebSocket Host surface；HTTP Provider 拥有 health 路由。

这样卸载一个 Plugin 会同时撤销它的 Route、Hook、Tool、Channel、Task 和 Service 资源；
冻结后的 Host 会通过 `restart_required` 明确提示需要重建。

### 4. 可选能力与架构债务

- Inbox 未安装时 Agent Runtime 仍可激活，普通输入返回稳定 `inbox-unavailable`，不会退回
  旧 Lane/Mailbox。
- Inbox Plugin 现在拥有自身的 start/attach/detach；`inbox/changed` 和
  `inbox/status-changed` Hook 让 WebSocket 每次解析当前 Inbox，restart/unload 不会留下旧
  Service 回调或旧 Loop 引用。
- Inbox Hook 的 HookRuntime 绑定改为正确的延迟 Effect，避免加载时立即解绑；Inbox 关闭时
  清理 worker、receipt、宿主回调和 Hook 引用。
- 内置工具不再从 Subagent Provider 私有模块读取 Channel 常量，统一使用
  `services/messaging/channel/names.py` 稳定名称。
- 删除/继续保持旧 Lane、MailboxStore、旧 Plugin Kernel、兼容入口和第二 Composition Owner
  不回归；空目录和生成缓存已在最终扫描中清理。

## 测试与验收证据

### 后端

```text
python -m pytest -q
439 passed in 91.86s

python -m ruff check --no-cache src tests packages/ftre-inbox
All checks passed!

git diff --check
passed
```

专项覆盖包含：

- `tests/architecture/test_f13_plugin_first.py`：Kernel、Composition、Agent/TurnExecutor、
  Service Owner、Trace 单一 exporter 和公共 Channel 名称门禁；
- `tests/lifecycle/test_agent_runtime_plugin.py`：Agent Runtime/Channel Provider 装载、
  endpoint 配置、关闭和无 Inbox 降级；
- `tests/architecture/test_f2_http_owner.py`、`tests/features/schedule/test_plugin.py`：
  Owner Plugin 路由和 Effect 清理；
- `tests/test_turn_lifecycle.py`、`tests/test_inbox_service.py`、F10/F12 生命周期和协议回归。

### Gateway smoke

使用临时 Session 根目录和显式端口 `48659` 启动真实
`run_gateway_runtime()`，确认 Socket 监听成功，随后取消任务并完成 Plugin/Channel/Agent
逆序清理，退出码为 0。

### Desktop 联调门禁

在 `E:\binn\ftre-desktop`（只读验证，未修改客户端）执行：

```text
pnpm test
renderer: 48 files / 488 tests passed
platform: 7 tests passed
```

单独执行 `pnpm --filter @ftre/renderer exec tsc --noEmit` 时发现客户端已有测试夹具错误：
`src/stores/chat.test.ts:461` 构造 optimistic 队列对象缺少必填 `awaitingEcho`。该文件不属于
本阶段后端范围，未在 F13 中越界修改；`pnpm test` 自带的 Electron 类型构建和平台测试已通过。

## 最终工作区卫生

- 已清理本阶段测试生成的 `__pycache__`、`.pyc`、`.pytest_cache`、`.ruff_cache`、`build`、
  `dist` 和 `*.egg-info`；`src`、`tests`、`packages` 下没有空目录。
- 未执行 commit、push、merge、发布或跨仓库代码修改；当前用户累计改动保持原样，等待按
  分支规范分批提交。

## 后续明确边界

- F6.12 的 cordis-py PyPI 发行物和脱离 `E:\\cordis-py` 的洁净安装仍按原 TODO 保持 todo。
- Desktop renderer 的 `awaitingEcho` 测试夹具修复应单独建立客户端阶段，不混入 F13 后端提交。

## 2026-08-24 `refactor-cleanup-audit` 收尾

### 范围与基线

- 仓库：`E:\\ftre`；分支：`feature/F12-agent-before-reasoning`。
- 只审计 ftre 后端及已纳入本仓库的 `packages/ftre-inbox`、`packages/ftre-compaction`；
  未修改 Desktop、Agent Core、cordis-py 源仓库、用户 `.ftre` 数据和数据库。
- 工作区原本已有 F12/F13 累计未提交改动；本轮没有 commit、push、merge 或 release。

### Owner 审计与修复

| 审计项 | 当前唯一 Owner | 修复/证据 |
|---|---|---|
| Agent Loop 构造 | `AgentLoopProvider` + `agent-runtime` Plugin | 删除 `AgentLoopRuntime` 透传 DTO；Bootstrap 无业务构造 |
| Trace 写入与查询 | `TraceService` | `build_tracer()` 提供唯一 exporter；AgentLoop 不再 import trace store |
| Inbox admission/worker | `ftre-inbox` Plugin/Service | Inbox 自己 start/close/attach；QueueItem 不进入 AgentService |
| Inbox→Loop 绑定 | Inbox Plugin 的显式生命周期绑定 | restart/unload 清掉旧 Loop capability，回到 capability error |
| WebSocket queue/status 广播 | WebSocket Provider + Inbox Hook | `inbox/changed`、`inbox/status-changed`；每次从 Context 解析当前 Inbox |
| HTTP 路由 | 各 Service/Feature Provider Plugin | Composition 中无 `register_router` 或业务 router import |
| Subagent channel name | `services/messaging/channel/names.py` | Tools 不再 import Provider 私有模块 |

### 静态与生命周期证据

- AST 运行时扫描：`src/ftre` 对 `ftre_inbox`、旧根包和旧 Plugin/API 命名空间的生产 import
  为 **0**。
- `vulture src/ftre packages/ftre-inbox/src packages/ftre-compaction/src --min-confidence 90`
  无高置信度死代码输出。
- 架构测试覆盖旧路径、唯一 Owner、HTTP 路由 Owner、Trace exporter 和 Inbox 边界；生命周期
  覆盖 Plugin restart/unload、Agent runtime detach、Channel stop 和 WebSocket 当前 Inbox
  resolver。

### 最终门禁

```text
python -m pytest -q                         -> 439 passed in 91.86s
python -m ruff check --no-cache src tests packages/ftre-inbox
                                             -> All checks passed
python -m vulture ... --min-confidence 90   -> no findings
git diff --check                             -> passed
Gateway runtime smoke                        -> bind 48659 / cancel / cleanup OK
source/tests/packages cache scan             -> generated=0, empty_dirs=0
```

最终仍保留 Desktop renderer 独立 `tsc --noEmit` 的既有 `chat.test.ts:461` 缺少
`awaitingEcho` 测试夹具问题；它已在 F13 主报告记录，未越过本阶段后端边界修改。
