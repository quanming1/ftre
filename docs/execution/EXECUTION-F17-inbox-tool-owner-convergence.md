# F17 执行报告：Inbox 基础 Owner 收敛与 Agent Runtime 去队列透传

> 状态：已完成；F17.1–F17.5 已验收。

## 1. 范围与边界

- 仓库：`E:\ftre`；当前分支：`feature/F17-inbox-tool-owner`（本批尚未提交）。
- 只修改 ftre Host、`packages/ftre-inbox`、测试和文档；不修改客户端、`E:\ftre-agent-core`、`E:\cordis-py`。
- F17 是对 F16 审计发现的后续修复：不恢复 Agent Runtime 对 Queue 的所有权。

## 2. 迁移基线

| 旧位置/入口 | 问题 | F17 新 Owner | 删除证据 |
|---|---|---|---|
| `TurnExecutor._inbox` | 构造时没有传入，始终为 `None` 的死透传 | 无；Agent Runtime 不再保存 Inbox | `self._inbox` 与 `runtime_context["inbox"]` 删除，架构测试门禁 |
| `services/tools/builtin/send_message.py` | Tool 依赖 Inbox 却由通用基础工具工厂注册 | F18 `ftre-messaging` | F17 移出通用工厂；业务迁移不在 F17 完成 |
| `services/tools/builtin/task.py` | 同上 | F18 `ftre-task` | F17 移出通用工厂；业务迁移不在 F17 完成 |
| `services/tools/builtin/team.py` | team_say/team_add_agent/wait_agent 依赖 Inbox | F18 `ftre-team` | F17 移出通用工厂；业务迁移不在 F17 完成 |

## 3. 已完成变更

- Inbox Plugin 只 provide Inbox Service，注册 admission/claim Hook、Worker 和持久化句柄。
- 业务 Tool 的三个独立 Package 迁移由 F18 继续完成；F17 不再把业务职责归入 Inbox。
- 通用 `build_default_tools()` 只构建不依赖 Inbox 的基础工具和 Plugin Tool View。
- 删除 `TurnExecutor` 的 `inbox` 参数、`_inbox` 字段和 runtime context 透传。
- Inbox Manifest 改为默认 Composition 的 required Plugin；禁止通过配置禁用 required Plugin。
- 新增 F17 架构、启动和生命周期门禁；目标 Owner 测试已通过 7 项。

## 4. 当前验证

- `python -m ruff check src tests packages --no-cache`：通过。
- F17/架构/启动/生命周期专项：`145 passed`。
- ftre 全量：`489 passed`。
- Inbox Service 独立导入、Hook/Worker 生命周期和 Agent Runtime 去透传门禁：通过。
- Gateway smoke：后台启动端口 `48662`，`GET /api/health` 返回 `200 {"status":"ok"}`，正常 stop。
- Host wheel：`ftre-0.3.0-py3-none-any.whl`，185 个文件；SHA256
  `f7fbd4c2c71e606095d081dd3cdb62817f7159697dda285c91b5617c1bf24839`。
- Inbox wheel：`ftre_inbox-0.2.0-py3-none-any.whl`，16 个文件；SHA256
  `c3c934b263392d72c31d3c06fc3170589d1a27c0d48cbe47bfaaf98ec3391a1d`。
- 两个 F17 wheel 均无测试目录、缓存和字节码；业务 Tool wheel 边界由 F18 重新构建验收。

## 5. 最终收尾

- F12/F14/F16 已补充 F17 边界变更记录；历史无 Inbox 验收结果保留为历史快照。
- TODO F17 已标记 `done / 已验收`；CHANGELOG 已记录当前运行时必须安装 Inbox。
- 最终清理后两仓缓存、字节码、build/dist、egg-info 和空目录均为零。

## 6. F18 纠偏说明

F17 原执行记录曾把“依赖 Inbox 的业务 Tool”写成 Inbox Owner。该结论违反 Plugin-first
职责边界，已由 `PRD-F18-tool-package-boundaries.md` 纠正：Inbox 只拥有队列基础设施，
`send_message`、`task`、`team_*`/`wait_agent` 分别由三个独立 Package 拥有。F17 的
Agent Runtime 去透传和 Inbox 基础生命周期结果仍有效；原 Tool Owner 表和对应 wheel
描述仅作为历史记录，不作为当前架构事实。
