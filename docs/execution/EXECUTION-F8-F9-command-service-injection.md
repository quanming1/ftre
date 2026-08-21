# F8/F9 执行报告：Command Plane、Service 注入与架构债务收尾

> 2026-08-22 按 `refactor-cleanup-audit` 重新复核并补充本报告。

## 1. 执行范围

本次只修改 `E:\ftre` 后端仓库，未修改桌面端、客户端协议消费者或
`E:\ftre-agent-core`。执行内容对应：

- `docs/prd/PRD-F8-command-plane-agent-plane.md`
- `docs/prd/PRD-F9-service-injection-and-debt-cleanup.md`
- `docs/TODO.yaml` 的 F8/F9 阶段

审计依据：`refactor-cleanup-audit` Skill 及其 cleanup checklist，按“基线 → Owner
迁移 → 生命周期 → 入口/静态扫描 → 测试 → 生成物清理 → 最终门禁”的顺序执行。

## 2. 结果摘要

结论：F8、F9 已完成，PRD 的 FR/AC 已逐条勾选，TODO 状态为 `done`。

主要结果：

1. Command 与 Agent 完全分平面。普通 Command 在 SessionLane 内直接调用
   `CommandRuntime`，不进入 Mailbox、TurnExecutor、LLM 或 Agent Hook。
2. `CommandResult` 收敛为 `success/error`；需要恢复 Agent 的 `/allow`、`/deny`
   复用已有 `UserConfirmResultEvent`，没有引入 `AgentControlPort` 等额外类型。
3. `/compact`、`/compress-fast`、`/fork` 分别使用 `CompactionPort`、
   `SessionService` 等真实 Owner；Command Handler 不再捕获完整 AgentLoop。
4. TurnExecutor、WebSocket、MCP、Schedule 和内置 Tool 的 Service 依赖改为显式
   Provider 参数或 `inject/provide`；AgentLoop 不再作为 Service Locator。
5. 删除旧 Command 结果分支、TurnExecutor Command 状态机、重复 facade、附件旧
   `store.py` 和无效 Trace 生命周期动态回调。
6. 保留的 `attachment.codec` 是无状态图片编码 Helper，只负责文件到 data URL 的
   协议转换，不创建、持有或绕过 AttachmentService。
7. 审计复核进一步统一 Tool 的 `sessions` 运行时注入键，并为 Session Title 的后台
   线程增加 stop flag、线程登记、bounded join 和 Fiber disposer。

## 3. Owner 与依赖迁移证据

| 能力 | 迁移前 | 迁移后 | 证据 |
|---|---|---|---|
| Command 执行 | `TurnExecutor.execute_command()` | `CommandRuntime.dispatch_inbound()` | `tests/contracts/test_f9_command_ingress.py` |
| 压缩 | `loop.compaction` | `CompactionPort` → Compaction Feature | `tests/architecture/test_f8_compaction_boundaries.py` |
| 会话 fork | `loop.session_manager` | `SessionService.fork_session()` | `tests/test_confirm_commands.py` |
| 确认恢复 | `ResumeAgent` 结果分支 | `UserConfirmResultEvent` + AgentService | `tests/test_confirm_commands.py`、`tests/test_turn_hitl.py` |
| Agent 数据面 Service | `TurnExecutor` 动态读取 Loop 字段 | Provider 显式传入 agents/attachments/system_prompt/hooks | `tests/architecture/test_f9_service_injection.py` |
| WS 附件 | `attachment.store.save_image()` | 注入 `AttachmentService.save_image()` | `tests/test_ws_attachment_persist.py` |
| MCP 附件 | Adapter 直接访问 store | `McpManager.attachment_service` | `tests/architecture/test_f9_service_injection.py` |
| 图片编码 | 旧 `attachment/store.py` | 无状态 `attachment/codec.py` | `tests/test_image_store.py`、`test_multimodal_path.py` |
| Tool Session 依赖 | `Injected("session_manager")` | `Injected("sessions")` | `tests/architecture/test_f9_service_injection.py` |
| Title Worker | 无可逆关闭 | `TitleGenPlugin.close()` + `ctx.effect` | `tests/test_title_gen.py` |

静态审计结果：生产代码中不存在 `loop.session_manager`、`loop.compaction`、
`Injected("agent_loop")`、`execute_command`、旧 CommandResult 类型或
`ftre.services.attachment.store` 导入。唯一保留的直接模块依赖是上表明确登记的
纯编码 Helper。

## 4. 生命周期与并发审计

- Command Plugin 的注册、生命周期观察者和注销器都绑定 Cordis Effect；重复 unload
  不重复注销。
- SessionLane 仍是同一 Session 的串行边界；Command 不占用 Agent active turn。
- `request_id` 结果缓存保证同一命令请求不重复执行业务变更。
- `/allow`、`/deny` 先持久化现有确认事件，再通过 AgentService 恢复；重复确认不会
  新建另一套命令结果协议。
- AgentLoop stop 会等待 Lane、压缩任务和 Agent scope 结束；TurnExecutor 不再拥有
  Command 或 Compaction 生命周期。
- Session Plugin 对默认 `SessionService` 注册 post-commit lifecycle 与 close Effect；
  外部提供窄 Session Contract 时不强行调用实现私有生命周期方法。

## 5. 测试与烟测

基线为本轮改造前 389 项测试；迁移后新增契约/架构/生命周期覆盖，最终全量为：

```text
python -m pytest -q
404 passed
```

专项验证包括：

- `tests/architecture/`、`tests/contracts/`、`tests/lifecycle/`、`tests/startup/`
  全部通过；覆盖 Inject/Provide、Owner、Command 无 Turn、生命周期和 Gateway
  Composition。
- Command smoke：4 个内置命令执行、8 条成对 lifecycle 记录通过。
  `command-smoke: PASS commands=4 lifecycle_records=8`
- Attachment/WS smoke：21 项附件、消息归一化和注入 Owner 测试通过。
- Gateway Composition startup：默认 required Plugin、公开 Service、路由和 unload
  测试通过。
- 代码质量：

```text
python -m ruff check src tests       # All checks passed
git diff --check                     # passed
YAML parse docs/TODO.yaml            # yaml: PASS
```

## 6. 生成物与空目录清理

本轮最终测试完成后，按 Skill 要求清理了仓库内 55 个 `__pycache__`/`.pytest_cache` 等
生成目录；剩余 `.pyc` 为 0，`src/ftre` 与 `tests` 下空目录为 0。只删除了位于本
仓库内的生成物，没有触碰用户配置目录或其他仓库；清理后不再运行会重新生成缓存的
测试命令。

## 7. 文档与工作区状态

- PRD：F8/F9 状态均为 `已验收`，FR/AC 全部勾选。
- TODO：F8/F9 及其 F8.1-F8.8、F9.1-F9.8 均为 `done`。
- CHANGELOG：增加 F8/F9 未发布条目。
- 工作区保留用户已有的 `AGENTS.md` 修改；本阶段不执行 commit、merge 或 push，
  因仓库协作规则要求这些操作必须由用户明确授权。

## 8. 复核结论与已知边界

本轮审计修复了两项残余债务：

- Tool 的 Session 依赖由旧命名 `Injected("session_manager")` 统一为公开运行时 key
  `Injected("sessions")`，TurnExecutor 只发布 `"sessions"`。
- Session Title Plugin 的后台线程现在受 stop flag、worker registry 和 bounded join
  管理，并通过 `ctx.effect(generator.close)` 在 unload 时清理。

入口边界仍保持现有设计：`run_gateway_runtime` 在 App Host 阶段创建并绑定真实
WebSocket/Subagent Channel，因为 WebSocket 必须使用 Composition 创建后的 FastAPI
Host；这属于 App Host 适配，不是第二个 Service Owner。AgentLoop 的核心组装仍唯一归
`services/agent_loop/provider.py`。

最终证据：

- 全量测试：`404 passed`；ruff、YAML、`git diff --check` 全部通过。
- 生产代码禁用引用扫描：`loop.<service>`、旧 Tool Session key、旧 Command 状态、
  `attachment.store` 等均为 0。
- 生成物复核：`cache_dirs=0`、`pyc=0`、`empty_src_dirs=0`、`empty_test_dirs=0`。
- 提交：本轮未创建 commit；当前工作区为非干净状态，包含本阶段代码/文档/测试改动和
  用户原有的 `AGENTS.md` 修改，等待用户按 Git 流程分片提交。
