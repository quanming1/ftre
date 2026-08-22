# F11 执行报告：上下文压缩门控 Hook 化与 SessionLane 解耦

## 结果

F11 已完成。核心 ftre 不再创建、导入或依赖 `CompactionService`；压缩实现、三条
Agent Hook 和两个压缩命令已收敛到可独立构建的 `packages/ftre-compaction`。

## 代码落点

- `src/ftre/platform/hooks/names.py`：新增稳定名称 `agent/after-turn`。
- `src/ftre/services/agent/hooks.py`：新增 `AfterTurnPayload` 和控制型 Hook Spec。
- `src/ftre/services/agent_loop/runtime/mailbox/lane.py`：只负责
  `peek → pre-step → claim → Turn → after-turn`，以通用 `MaintenanceOperation` 和
  `set_maintenance()` 提供状态快照桥，不理解压缩算法。
- `src/ftre/services/agent_loop/runtime/loop/context_gate.py`：删除。
- `src/ftre/services/session/events.py`：新增通用 Session event sink，供可选能力投影事件。
- `src/ftre/services/messaging/bus/service.py`：补齐公开 inbound 转发，保证未启用压缩包时
  Schedule 后台投递不经过缺失的 Service 方法。
- `packages/ftre-compaction/src/ftre_compaction/`：`CompactionService`、算法、Hook、命令、
  配置快照、Plugin 入口和事件别名。
- `src/ftre/app/gateway/composition.py`：默认清单删除 compaction；通过显式外部 Plugin 配置
  `ftre_compaction.plugin:apply` 启用。

## 行为保证

- pre-step 压缩成功前队首仍在 pending；失败返回 `RejectStep("keep")` 并进入 blocked。
- pre-step 压缩完成后由同一 Hook 重新调用 `should_compact`；若仍超过安全水位则继续
  `keep/block`，不会未经复查直接 claim。
- after-turn 在 CompletionRegistry 完成和快照发布后执行，即使 pending 为空也执行；Hook
  完成前不会领取下一条消息。
- Hook 维护期间客户端仍看到 `compacting`；失败不回滚已完成 Turn。
- 未安装/未启用压缩包时，Gateway、普通消息和 Agent Hook 默认继续；`/compact` 和
  `/compress-fast` 返回“命令不可用或未启用”，不进入 TurnExecutor。
- compaction Hook 收到取消时调用自身 Service 的 `cancel_compact(session_id)`；Plugin
  close 取消剩余共享任务。
- 压缩阈值、预算安全垫和摘要专用模型由 ftre-compaction/config.py 独立解析；
  核心 AgentConfig 只保留 mailbox_capacity，配置热更新在 Hook/Command 边界生成
  不可变快照。

## 验证证据

| 检查 | 结果 |
|---|---|
| 全量 pytest | 431 passed |
| 架构/契约/生命周期/启动专项 | 149 passed（包含 F11 可选包测试）；压缩包配置专项 58 passed |
| ruff | `python -m ruff check --no-cache src tests packages/ftre-compaction/src packages/ftre-compaction/tests` 通过 |
| 核心 Gateway smoke | 无 compaction Service，启动/关闭通过 |
| 显式启用压缩包 smoke | Service、`agent/pre-step`、`agent/after-turn`、`agent/request-error` 和两个命令注册成功 |
| 独立 wheel 构建 | `ftre_compaction-0.1.0-py3-none-any.whl` 构建成功；依赖为 `ftre>=0.2.5`，入口为 `ftre_compaction.plugin:apply` |
| 隔离安装 | 在 `E:\f11-clean-venv-nosystem` 中安装 root `ftre-0.2.5` wheel、`ftre-compaction` wheel 及已构建的 `cordis-py`/`ftre-agent-core` 支持 wheel；四个包的 import origin 均来自 venv，不含 `E:\ftre` 源路径 |
| 干净核心/可选模式 | 卸载 `ftre-compaction` 后核心 Composition 成功且无 compaction Service/压缩命令；重新安装并显式启用后 Service、三条 Hook、两个命令和 Plugin discovery 全部成功 |
| 架构扫描 | 核心不存在 compaction 目录、ContextGate、旧导入或 No-op fallback |

## 文档联动

- `docs/prd/PRD-F11-compaction-gate-hook.md`：状态更新为“已验收”，FR1–FR13、AC1–AC11
  已勾选，并记录状态桥接和取消语义决策。
- `docs/TODO.yaml`：F11 及 F11.1–F11.10 更新为 `done`。
- `CHANGELOG.md`：加入 `[未发布]` F11 条目。
- 本轮补充 F11.10：删除核心压缩配置 Owner 和无消费者历史配置示例，为可选包所有
  模块、生命周期和跨层边界补充中文注释。

## 已知边界

PyPI 上传仍属于 F6.12，不在本阶段执行；本阶段只验证独立 wheel/sdist 构建、元数据和
显式 Plugin 启用路径。
