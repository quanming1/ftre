# F45 执行记录：ContextView Hook 与 Fork/回滚

## 范围

- 后端分支：`feature/F45-context-build-hook`
- 客户端分支：`feature/F45-context-build-hook`
- 本阶段交付：`agent/context-build`、压缩 Plugin ContextView、Snapshot Fork/回滚、
  AI 消息 Fork 和用户消息回滚回填。
- 未修改：LLM Provider、`llm/stream`/`llm/error`、Inbox 核心和 F44 WebSocket wire 协议。

## Owner 收口

| 能力 | 最终 Owner | 结果 |
|---|---|---|
| Event → 完整 Msg | `ftre-agent.session.derive` | 保留通用 fold，删除压缩裁剪函数和导出 |
| 本次 LLM ContextView | `ftre-agent-runtime` 调用 `agent/context-build` | 深拷贝、结果校验、Retry 复用；恢复重试刷新完整 Snapshot |
| summary/fast marker 解释 | `ftre-compaction` | `context.py` 只修改内存副本，不写 Session |
| Session 历史 | `SessionService.get_full_messages` | 完整 Msg 唯一事实来源；读侧 ContextView builder 可逆注入 |
| Fork | `SessionService.fork_session` + Session Router | `through_message_id`、稳定性屏障、子 Session 隔离 |
| 回滚 | `SessionService.rollback_session` + Session Router | 当前 Session 原地截断、request 索引清理、输入回填 |
| 客户端入口 | renderer `AssistantMessage` / `UserMessage` | Fork 创建子 Session；回滚刷新当前 Session 并回填文本 |

## 生命周期与一致性

- ContextBuild Hook 的 Receipt 绑定 Compaction Plugin Effect；卸载时移除监听器和
  ContextView builder，SessionService 恢复完整历史透传。
- 同一 Reasoning iteration 的 Provider Retry 不重复构建 ContextView；`agent/run-error`
  恢复先重新读取完整 Msg，再进入下一轮 Hook。
- Fork 前执行 `flush_log`，检查 Snapshot 水位和 Msg/Tool 状态；运行中的 Tool、审批和
  未完成目标明确拒绝。
- 子 Session 只复制完整 Msg/必要 metadata；Inbox pending、active run、approval、
  completion waiter 和 request 幂等索引均为空。
- rollback 保持当前 Session 身份，原子移除目标用户消息及之后的历史，不自动重跑副作用；
  被回滚用户内容通过 `prefill_content` 返回客户端输入框。

## 验证证据

| 检查 | 结果 |
|---|---|
| 后端全量 `py -3.12 -m pytest -q` | 通过，818 passed |
| 后端 Ruff（`src`、`packages`、`tests`） | 通过，All checks passed |
| 后端 `git diff --check` | 通过 |
| 新增 Fork/ContextView 专项 | 通过：路由、深拷贝、marker、未完成 Tool 拒绝、恢复刷新 |
| 三个 Package wheel 构建 | 通过：`ftre-agent`、`ftre-agent-runtime`、`ftre-compaction` |
| renderer `pnpm --filter @ftre/renderer test` | 通过，605 passed |
| renderer `pnpm --filter @ftre/renderer build` | 通过；仅有既有 CSS、动态 import 和 chunk 大小警告 |
| 旧压缩 Owner 搜索 | 通过：生产代码无 `derive_context_messages` / `_context_from_messages` 引用 |
| 旧回滚 UI 搜索 | 通过：无 `RollbackConfirmDialog`、`previewRollback`、`executeRollback` 引用 |
| 免安装版首轮 Turn 回归 | 通过：修复 `system-prompt/assemble` 对 typed `Msg` 的兼容边界，Prompt Hook 统一收到 JSON mapping |
| 回滚/Fork 语义回归 | 通过：`/fork` 只创建子 Session，`/rollback` 原地改写当前 Session，request 索引同步清理 |

## 文档同步

- `docs/prd/PRD-F45-context-build-hook.md`：状态更新为“已验收”，FR/AC/P0-P4 全部勾选。
- `docs/TODO.yaml`：F45 及 F45.1-F45.6 更新为 `done`。
- `docs/prd/README.md`：F45 更新为已验收。
- `CHANGELOG.md`：新增 `[未发布]` F45 条目。

## 提交状态

本次只完成工作区实现和验证，未执行 commit、push、merge 或 release；等待用户审查后
按 Git Flow 提交到 F45 分支并创建 PR。
