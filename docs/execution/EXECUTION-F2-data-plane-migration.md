# F2 核心数据面 Service 化迁移执行报告

## 1. 执行结论

F2 已在 `feature/F2-data-plane-migration` 分支完成并通过验收。迁移采用渐进式策略：真实实现迁入 `services` Owner，旧目录保留单向兼容 shim；生产启动路径不再依赖旧数据面目录、旧 aggregate HTTP Router 或 `bind_legacy_api`。

本阶段没有修改 Desktop、`ftre-agent-core`、Octo 独立仓库或其他仓库。

## 2. 迁移结果

### 2.1 Session 与 Workspace

- `SessionService`、实体模型、消息转换、JSON Store、Repository、搜索和 Projection 归入 `services/session`。
- `SessionService` 不再继承旧 `SessionManager`。
- `ftre.session.*` 仍可导入，但只指向新 Owner；没有第二份 Session 实现。
- Workspace、工具和 Agent Runtime 改用新 Session Service 类型。

### 2.2 Agent Runtime

- AgentManager 归入 `services/agent/profile/manager.py`。
- AgentLoop、SessionLane、Mailbox、ContextGate、CompletionRegistry、TurnExecutor、Compaction 归入 `services/agent/runtime`。
- SessionProjection 归入 `services/session/projection.py`。
- `AgentRuntimeProvider` 负责把公共 Service handle 映射到内部 AgentLoop 构造参数，Gateway 不再手工直接组装 Loop。
- Hook Context 归入 `services/agent/runtime/hooks.py`，新 Runtime 不再导入旧 `ftre.plugin` Kernel。

### 2.3 Bus、Channel、Command、Tool

- EventBus、Bus message/payload/protocol 归入 `services/messaging/bus`。
- Channel base/manager、WebSocket、Subagent Provider 归入 `services/messaging/channel`。
- Command manager/types/builtin 归入 `services/command`。
- 内置 Tool catalog 和工具实现归入 `services/tools/builtin`。
- 旧 `bus/channel/command/tools` 目录只保留兼容模块别名，历史 import 和 monkeypatch 行为保持可用。

### 2.4 HTTP 与 WebSocket

- 新增 Session、Agent、Trace、Attachment、Command Router。
- Schedule Router 补齐 cron CRUD。
- Composition 直接按 Owner 注册路由，不再导入 `ftre.api.routes` 聚合 Router。
- `bind_legacy_api` 已移除。
- WebSocket Channel 只贡献 `/` 协议端点，复用 Composition 创建的 FastAPI Host，不再挂载旧 API Router。
- 旧 `ftre.api.routes` 仅保留给历史兼容测试；生产路径没有引用。

## 3. 分阶段提交

| 切片 | 提交 | 内容 |
|---|---|---|
| F2.1 | `f270576` | Session/Workspace 真实实现迁移与兼容 shim |
| F2.2 | `2a2988e` | Agent Runtime、Agent profile、Runtime Provider 迁移 |
| F2.3 | `7544134` | Bus、Channel、Command、内置 Tool 迁移 |
| F2.4 | `6214729` | HTTP/WS Owner Router 与 Host 迁移 |
| F2.5 | `a3909fc` | Hook 边界、旧 shim 审计、文档和最终验收 |

## 4. 自动化验证

```text
python -m pytest -q
376 passed, 1 warning

python -m ruff check src tests
All checks passed!

git diff --check
通过
```

唯一 warning 是既有 `src/ftre/channel/test_channel.py` 中 `TestChannel` 带自定义构造器导致的 Pytest collection warning。

新增架构门禁覆盖：

- 新 Owner 不导入旧 Session/Agent/Bus/Channel/Command/Tool 包。
- 旧目录没有第二份业务实现，只能指向 `services`。
- Composition 不导入 aggregate API 或 legacy setter。
- Session、Runtime、Bus、Channel、Command、Tool 的旧 import identity 保持一致。

## 5. 手动验证

- `start_gateway(config={})` 创建 Composition 成功。
- `/api/health` 返回 `200 {"status": "ok"}`。
- 路由快照包含 `/api/traces`、`/api/sessions`、`/api/config`、`/api/cron`、`/api/commands`、`/api/images/{filename}`、`/api/agents`、`/api/skills`、`/api/mcp`。
- 使用 Composition Host 创建 WebSocket Channel，`/` 可以建立连接并完成 attach。
- Composition dispose 可重复执行，无残留异常。

## 6. 兼容边界与后续阶段

本阶段刻意没有删除旧 `ftre.plugin.kernel`、`ftre.plugin.builtin` 和 `ftre.api.routes`，因为它们仍被历史插件和兼容测试直接导入；它们已经不在新数据面生产路径中。删除旧 Plugin Kernel/API 兼容面应另开 F3 PRD，继续遵循“先迁 Provider/Consumer，再删旧入口”的策略。

## 7. 收尾状态

- 分支：`feature/F2-data-plane-migration`
- 基线：F1 已验收提交
- 范围：仅 `E:\ftre`
- PRD：`docs/prd/PRD-F2-data-plane-migration.md`
- TODO：阶段 F2 已标记 `done`
- 最终工作区：干净
