# F7 / C1 执行报告：Agent Core 直接消费 ftre Hook 协议

日期：2026-08-21

## 1. 结果摘要

F7 已完成。ftre-agent-core 在独立 `feature/C1-core-hook-integration` 分支实现无状态
`HookDispatcher` 和 Core-facing typed contracts；ftre 通过 Cordis `HookRuntime` 直接注入
Dispatcher 与 Agent scope。Tool、LLM stream、turn-stopping 不再经过桥接适配器。

本次只修改 ftre 与 ftre-agent-core，未修改客户端或其他仓库；Core 原有 B2 未提交修改保留。
本轮按用户要求未执行 commit/push，两个工作区仍保留可审查的未提交变更。

## 2. 主要改动

### Core（`E:\ftre-agent-core`）

- 新增 `HookDispatcher` Protocol、`HookSpec`/mode/failure/scope 和共享 payload/result：
  - `tools/pre-execute`
  - `tools/execute`
  - `tools/post-execute`
  - `tools/result`
  - `llm/stream`
  - `agent/turn-stopping`
- `ReActAgent` 接受 `hooks` 与 opaque `hook_context`，不再创建进程内 Hook 注册表。
- `ToolHandler` 直接执行四段 Tool pipeline；`ReasoningExecutor` 直接包裹原始 LLM stream；
  `ExitExecutor` 在 finalize 前做 Stop/Continue 决策并强制 continuation budget。
- 删除 `FtreCoreHookManager`、旧 `HookInput/HookOutput` 和 `ON_*` 注册入口。
- 新增 Core 合同/管线测试，更新过时的旧 LLM 导入测试和静态质量门禁。

### ftre（`E:\ftre`）

- `services/tools/hooks.py`、`services/llm/hooks.py`、`services/agent/hooks.py` 改为
  Core 合同的稳定业务重导出，消除重复数据类和 Spec Owner。
- `TurnExecutor` 直接把 `HookRuntime` 与 Agent scope context 传给 Core；删除
  `ToolHookBridge`、`HookedToolRegistry`、`HookedLLMAdapter`。
- 删除空的 `src/ftre/infrastructure/agent_core` 目录及其生成缓存。
- 新增 `tests/architecture/test_f7_agent_core_direct.py`，禁止旧桥接层回流。

## 3. 验收证据

| 门禁 | 结果 |
|---|---|
| Core 全量 pytest | `234 passed` |
| ftre 全量 pytest | `389 passed` |
| Core `python -m ruff check --no-cache .` | 通过 |
| ftre `python -m ruff check --no-cache src tests` | 通过 |
| 双仓库 `git diff --check` | 通过 |
| Core 旧 Hook/ON_* 引用扫描 | 无生产/测试引用 |
| ftre 旧桥接类引用扫描 | 无生产/测试引用 |
| Gateway smoke | `GATEWAY START OK` / `GATEWAY CLOSE OK` |
| Core 导入路径 | ftre 使用 `E:\ftre-agent-core\src\ftre_agent_core` |

专项测试包括：

- Core `tests/test_direct_hook_pipeline.py`：Tool 四段 Hook 与 LLM stream 直连；
- Core `tests/test_hooks.py` / `test_execute_exit.py`：Stop/Continue、预算耗尽、默认无 Hook；
- ftre `tests/contracts/test_f7_hook_pipeline.py`：Core/ftre Spec 与 payload identity、Cordis scope、
  Tool/turn-stopping 集成；
- ftre `tests/architecture/test_f7_agent_core_direct.py`：依赖方向与桥接删除门禁；
- 既有生命周期、取消、重试、权限恢复、Session flush 与 pending 消费测试。

## 4. 清理审计

- 旧 Core Hook Manager、适配器和空 `infrastructure/agent_core` Owner 已删除；
- `agent`、`api` 空目录不存在；
- 生产代码不再出现 `FtreCoreHookManager`、`hook_manager`、`ToolHookBridge`、
  `HookedToolRegistry`、`HookedLLMAdapter`；
- 测试完成后清理 Python 缓存、pytest/ruff 缓存与空迁移目录；清理后仅保留源代码、测试和文档。

## 5. 未包含事项

- PyPI 发布与洁净安装仍属于 TODO 中的 F6.12 后置任务；
- 本报告不代表两个仓库已经提交或合并到 develop；提交/PR 由后续显式流程执行。
