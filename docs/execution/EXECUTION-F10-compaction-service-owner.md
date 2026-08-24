# F10 执行报告：Compaction Service Owner 收敛与 Port 删除

## 1. 范围

- 仓库：`E:\ftre`
- 分支：`feature/F7-agent-core-hook-integration`
- 阶段：F10
- 未修改：桌面端、客户端、`E:\ftre-agent-core` 和其他仓库

本阶段按 PRD 将 Compaction 的真实实现从 Feature 层迁入 Service 层，并删除
`CompactionPort`。压缩算法、事件、命令和客户端协议保持不变。

## 2. Owner 迁移

| 迁移前 | 迁移后 | 结果 |
|---|---|---|
| `plugins/builtin/compaction/service.py` | `services/compaction/service.py` | 唯一真实 `CompactionService` |
| `plugins/builtin/compaction/plugin.py` 创建并 provide Service | `services/compaction/plugin.py` 创建并 provide | Service Plugin 唯一 Owner |
| Feature Plugin 同时创建 Service、注册 Hook | Feature Plugin 只注入 `compaction` 并注册 Hook | 行为与状态分离 |
| `services/compaction/contracts.py` 的 `CompactionPort` | 删除 | 不再增加额外接口层 |
| `AgentLoop/ContextGate/Command/Provider` 依赖 Port | 直接使用 `CompactionService` | 调用链更直观 |

最终结构：

```text
services/compaction/service.py  → CompactionService
services/compaction/plugin.py   → provide("compaction")
plugins/builtin/compaction/plugin.py   → agent/pre-step + agent/request-error Hook
```

`NullCompactionService` 仅保留为测试/禁用嵌入场景的显式 No-op，不是第二个 Owner，
也不再承担接口契约职责，且不从公共 `ftre.services.compaction` 导出。

## 3. 生命周期

- Service Plugin 创建 `CompactionService`，通过 `ctx.provide("compaction")` 发布。
- Service Plugin 通过 `ctx.effect(service.close)` 负责取消并等待所有压缩 Task。
- Feature Plugin 只注册两个 Hook Receipt，并通过 `ctx.effect` 逆序注销。
- AgentLoop Provider 绑定统一事件发射器；Gateway stop 时 Service close 等待实际 Task。
- Service 与 Hook Plugin 的重复 unload/close 由 Cordis Effect 和 Service close 保证幂等。
- Hook/Service 启动失败不会留下另一份 `compaction` 状态 Owner。

## 4. 测试与验证

专项验证：

```text
33 passed
ruff: All checks passed
```

全量验证：

```text
python -m pytest -q
405 passed in 36.89s

python -m ruff check src tests
All checks passed!

YAML parse docs/TODO.yaml
yaml: PASS

git diff --check
passed
```

覆盖内容：

- Service/Feature Plugin 分离与 Hook 注册；
- Service Owner、旧文件删除、Port 删除和 Composition Manifest 架构门禁；
- 自动压缩、手动压缩、快速压缩、LLM 摘要、事件投影和失败恢复；
- ContextGate、Command、AgentLoop Provider 和 Gateway Composition 回归；
- unload、close、压缩失败和 pending 保留语义。

## 5. 静态清理

- 生产代码中 `CompactionPort`：0；
- `plugins/builtin/compaction/service.py`：已删除；
- `services/compaction/contracts.py`：已删除；
- 生产代码不存在 `services.agent_loop → features.compaction.service` 私有导入；
- `compaction` Service key 只有 Service Plugin 提供；
- 旧导出、测试导入和架构门禁已同步。

## 6. 文档与交付状态

- PRD：F10 已标记 `已验收`；FR1-FR10、AC1-AC10 全部勾选；
- TODO：F10 及 F10.1-F10.8 均为 `done`；
- CHANGELOG：已加入 `[未发布]` F10 条目；
- PRD 总览、F8/F9 相关契约已同步为 `CompactionService`；
- 本阶段未执行 commit、merge 或 push，工作区状态由用户按 Git 流程处理。

## 7. 审计清理复核

本轮 `refactor-cleanup-audit` 最终复核结果：

- 最终全量测试：405 passed；
- 生产禁用引用扫描：`CompactionPort`、旧 Feature 实现路径、旧 contracts 路径均为 0；
- `compaction` Service key 只有 `services/compaction/plugin.py` 提供；
- 最终测试后清理 55 个测试/编译缓存目录，`.pyc` 为 0；
- `src/ftre` 空目录为 0，`tests` 空目录为 0；
- `git diff --check` 通过；
- 工作区未提交，保留本阶段改动，未声称为 clean branch。

## F11 后续变更

F11 已完成后续拆包：当前主包不再包含 `services/compaction` 或
`plugins/builtin/compaction`，压缩 Service、Hook、命令和算法位于
`packages/ftre-compaction`。本报告保留 F10 当时的验收证据；当前架构以 F11 PRD 和
执行报告为准。
