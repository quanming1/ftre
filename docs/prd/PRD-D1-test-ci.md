# PRD-D1-测试与 CI

> 状态生命周期：草稿 → 评审 → approved（定稿）→ 开发中 → 已验收
>
> 本文是对 TODO 中已完成 D1 阶段的反推 PRD。代码和测试已经存在，但本 PRD
> 的逐条验收证据需要在后续 CI/收尾流程中重新确认。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | D1 |
| 名称 | 测试与 CI（pytest + trace_store + 架构回归） |
| 状态 | 已验收（反推，待复核） |
| 创建日期 | 2026-08-13 |
| 定稿日期 | 2026-08-13 |
| 验收日期 | 2026-08-13 |
| 关联文档 | docs/TODO.yaml 阶段 D1；docs/PROCESS.md；AGENTS.md |

## 1. 背景与目标

- **背景**：ftre 已包含 Session、Bus、SessionLane、Projection、压缩、WebSocket、插件和工具等多层异步组件；单个单元测试通过并不能证明跨层生命周期正确。
- **目标**：建立可重复的 pytest 验证基线，覆盖正常路径、异常路径、取消、持久化恢复、协议校验和 trace 查询；让 PRD 的验收条目都有可执行证据。
- **非目标**：不在 D1 中新增业务功能，不替代各阶段 PRD 的领域验收。

## 2. 需求范围

### 2.1 功能需求

- [x] FR1：pytest 异步测试基础——覆盖 Python 3.12、pytest-asyncio 和临时 Session 目录。
- [x] FR2：Session/JSON 测试——覆盖 schema 校验、原子写入、损坏隔离、fork、并发锁和 mailbox 恢复。
- [x] FR3：AgentLoop/Turn 测试——覆盖 FIFO、ContextGate、取消、TurnOutcome、Projection 和 HITL 恢复。
- [x] FR4：Bus/WS 测试——覆盖 request/reply、Pydantic Payload、控制消息、附件和 attach snapshot 顺序。
- [x] FR5：Compact 测试——覆盖 summary、compress-fast、共享 Task、失败 fallback 和上下文锚点。
- [x] FR6：TraceStore——SQLite trace 写入、分页、按 Session 查询、payload 查询和旧表结构迁移。
- [ ] FR7：CI 门禁——在干净环境执行完整 pytest、ruff 和 PRD 验收矩阵；当前仓库需补充实际 CI 配置和运行记录。

### 2.2 非功能需求

- 测试必须隔离临时目录和环境变量，不读写用户真实 `~/.ftre/sessions`。
- 异步测试结束后不得留下未回收 Task；失败测试必须能定位到 session/request/turn。
- trace 测试不得依赖真实 LLM、MCP 或外部网络。

## 3. 技术方案

### 测试分层

| 层 | 代表测试 | 目标 |
|---|---|---|
| 数据层 | `test_session_state.py`、`test_session_json_store.py` | schema、原子落盘、损坏隔离 |
| 编排层 | `test_session_lane.py`、`test_turn_lifecycle.py` | FIFO、取消、压缩门控、Turn 终态 |
| 投影层 | `test_session_projection.py`、`test_event_stream_history.py` | Msg checkpoint、历史和 attach 恢复 |
| 协议层 | `test_bus_request_reply.py`、`test_ws_control_commands.py`、`test_ws_volatile_replay.py` | Bus/WS 契约和顺序 |
| 扩展层 | `test_plugin_tools.py`、`test_mcp.py`、`test_agent_manager.py` | 插件和多 Agent 配置 |
| 可观测层 | `test_trace_store.py` | trace 写入、查询和清理 |

### 关键回归矩阵

| 场景 | 必须验证的事实 |
|---|---|
| A 运行时提交 B/C | A 完成 → 必要时 compact → B → C，pending 不提前进入 context |
| cancel A | A 为 cancelled，B/C 仍按 FIFO 执行 |
| cancel queued B | B 从 pending 移除，A/C 不受影响 |
| Gateway 重启 | pending 恢复，active 不自动重放，已 checkpoint Msg 保留 |
| compact 失败 | 先复核/fast fallback，仍不安全则 blocked，队首保留 |
| WS attach + 实时事件 | snapshot 与 live event 不乱序、不重复 |
| request 重试 | 相同 request_id 不重复入队或执行 |

## 4. 接口定义

- 测试命令：`pytest -q`。
- 静态检查：`ruff check src tests`（若仓库启用 ruff）。
- trace 查询接口：`list_trace_summaries`、`get_trace`、`get_trace_run`。
- 测试结果必须记录日期、Python 版本、通过/失败数量和未覆盖项；不得只写“测试通过”。

## 5. 验收标准

- [x] AC1：核心单元测试覆盖 Session、Bus、Lane、Projection、Compact、Turn 生命周期。
- [x] AC2：trace_store SQLite 写入、查询、分页和历史结构读取有自动化测试。
- [ ] AC3：完整 `pytest -q` 在当前依赖环境通过（本轮只执行架构相关定向集合，尚未替代全量验收）。
- [ ] AC4：ruff/CI 门禁在干净环境通过并关联 PRD 阶段（待补 CI 配置）。

## 6. 测试计划

1. 先运行数据层和协议层测试，确认测试环境本身可用。
2. 再运行 Lane/Projection/Compact/Turn 跨层测试，重点观察异步 Task 清理。
3. 最后执行完整 pytest 和静态检查，按失败回到对应阶段 PRD 修订。
4. 对未覆盖的 send_message/team 路由、HTTP 删除队列、Gateway 停止竞态补测试后，才能把对应 PRD AC 从待补改为已验收。

本轮定向验证记录（2026-08-13）：

```text
python -m pytest -q tests/test_bus_request_reply.py tests/test_session_lane.py \
  tests/test_session_projection.py tests/test_compact_summary.py \
  tests/test_turn_lifecycle.py tests/test_turn_hitl.py tests/test_trace_store.py
48 passed in 9.20s
```

## 7. 变更记录

| 日期 | 变更 | 原因 |
|---|---|---|
| 2026-08-13 | 根据现有 tests/ 和 trace_store 代码反推 D1 PRD；明确已覆盖项与待补 CI/协作测试 | TODO 已将 D1 标记完成，但原仓库缺少对应 PRD 和逐条验收证据 |
