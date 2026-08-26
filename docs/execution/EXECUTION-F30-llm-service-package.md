# F30 统一 LLM Service Package 收尾审计报告

## 范围与结论

- 仓库：`E:\ftre`
- 分支：`develop`（PR #59 合入后）
- 联动修改：`E:\ftre-agent-core`（仅将 LLM 事件入口收敛为 `ftre_llm.events`）
- 未修改：`E:\binn\ftre-desktop`
- 审计技能：`refactor-cleanup-audit`
- 结论：F30 代码、文档和 Git 交付均已收尾；质量门禁通过，阶段已验收，可进入依赖 F30 的 F31。

## Owner 与迁移证据

| 能力 | 唯一 Owner | 证据 |
|---|---|---|
| Adapter 契约 | `ftre_llm.contracts.LlmAdapter` | `packages/ftre-llm/src/ftre_llm/contracts.py` 仅有一个 `LlmAdapter` AST 类定义 |
| OpenAI 共享实现 | `ftre_llm.base.OpenAIAdapterBase` | 继承唯一 `LlmAdapter`，自身不声明 Adapter 契约 |
| Completions/Responses 注册 | `ftre_llm.adapters.plugin` | `inject=("llm",)`，通过 `ctx.llm.register_adapter()` 注册并绑定 Effect |
| LLM Service | `ftre.services.llm.plugin` + `ftre_llm.service.LlmService` | Host 只创建/provide `llm`，不 import concrete adapter |
| Agent LLM 调用 | Agent Runtime 注入 `llm`，使用 `ftre_llm.LlmServiceAdapter` | `turn_executor.py` 不再创建 Core LLM handler，也不做输出转换 |
| StreamChunk 协议 | `ftre_llm.events`（从 Core 迁入） | Core 旧 `llm.events` 已删除，运行时只有一个协议 Owner |
| Compaction/Title | 各自 Plugin 消费 `ctx.llm.stream()` | 两处均从稳定 `LLMConfig.provider` 构造请求 |

静态扫描结果：生产代码没有 `OpenAICompletionsAdapter`/`OpenAIResponsesAdapter` 的 Host import，没有第二个 `LlmAdapter`，没有 `configured` 伪 Provider fallback。

## Provider 与 Hook 生命周期

`LLMConfig.provider` 由 `build_llm_config(provider_name, model_id)` 写入，Hook payload、请求配置、日志和诊断使用同一逻辑 Provider。

`LlmService` 在以下事务完成后异步发布 `llm/adapters-updated`：

```text
register_adapter → operation=register
registration.replace → operation=replace
registration.dispose → operation=dispose
```

通知任务由 Service 持有，`close()` 会取消并等待未完成任务；监听器异常只记录，不回滚已完成的注册事务。专项测试覆盖三种事件和 Provider Plugin unload。

## 生命周期审计

| 资源 | 创建/注册 | 清理 | 验证 |
|---|---|---|---|
| `LlmService` | `llm-service` Provider Plugin | `llm:close` Effect | Composition startup/close |
| OpenAI adapters | `llm-providers` Package Plugin | 每个 registration 的 `dispose` Effect | restart/unload lifecycle test |
| Adapter update tasks | `LlmService._queue_adapters_updated` | `close()` cancel + gather | Service contract test |
| Compaction/Title streams | 各自调用 `ctx.llm.stream` | 请求取消/流 finally | 全量测试 |

## 验证记录

- `python -m pytest -q`：`612 passed`
- `python -m ruff check src tests packages`：通过
- `git diff --check`：通过
- `python -m build --wheel --no-isolation --outdir <temp> packages/ftre-llm`：成功，wheel 含 Provider Plugin entry point
- AST 重复类扫描：仅 `contracts.py:LlmAdapter`
- 缓存清理：清理仓库内 `__pycache__`、`.pytest_cache`、`.ruff_cache`；wheel 生成的 `build/` 和 `ftre_llm.egg-info/` 已删除

未删除：`.ftre-inbox` 下的空 Session 目录，它们属于运行时数据；`.git/refs/heads/feat` 属于 Git 内部目录，均不属于源码生成物。

## 历史阻塞（已解决）

以下记录保留当时的审计现场；随后已通过 PRD/TODO/CHANGELOG 更新和 GitHub PR 合并解决。

1. PRD/TODO 尚未切换为已验收/done。
2. feature 分支尚未提交、推送和合入 `develop`。
3. 当时未获得提交授权，故未执行 Git 交付动作。

## 2026-08-26 再审

- 当前分支仍为 `feature/F30-llm-service-package`，未回退到 `develop`。
- AST 扫描再次确认 `LlmAdapter` 只有 `contracts.py` 一处定义；Host/Compaction/Fallback/Recovery 没有 concrete OpenAI Adapter import。
- Provider 伪值扫描未命中生产代码；Agent ID 使用的 `or "default"` 属于独立身份默认值，不是 LLM Provider fallback。
- `src/` 与 `packages/` 没有空源码目录；`.ftre-inbox` 运行时数据目录和 `.git` 内部目录未触碰。
- wheel 构建、全量 pytest、ruff、diff check 和缓存清理证据沿用上一轮；本轮新增的
  Core Runner 接线已在下方重新完成全量验证。
- F30 PRD/TODO/CHANGELOG 与 Git 分片提交仍是唯一未完成的交付项，不能标记阶段完成。

最终复审门禁：

- `python -m pytest -q`：`615 passed in 185.88s`
- `python -m ruff check src tests packages`：通过
- `git diff --check`：通过
- 本轮测试产生的 `__pycache__` 与 package build 临时目录属于忽略的生成物，未纳入
  Git 变更；源码门禁不依赖这些目录。

## 2026-08-26 Core 协议收敛复审

- `ftre_llm.events` 成为 StreamChunk 唯一实现；Core 已删除 `ftre_agent_core.llm.events`
  兼容模块，内部直接导入该协议，不再声明第二套 dataclass。
- 删除 `src/ftre/services/llm/core_bridge.py`；Agent Runtime 使用
  `ftre_llm.LlmServiceAdapter`，只组装 `LlmRequest`，不复制流事件；Core Runner
  在 Agent 构造阶段接收注入，不再由 Host 写入 `runner._llm`。
- LlmService 删除返回值无人消费的 `agent/request-error` 错误派发；Recovery Plugin 改接
  Core 唯一 `llm/error`，其 `LLMErrorDecision` 已实际控制 Core Retry/Stop。
- `ConfigService.resolve_llm()` 补齐 `context_window`、`vision`、
  `reasoning_effort_values` 模型能力快照。
- Core 全量测试：`265 passed`；ftre 全量测试：`615 passed`；两仓 ruff 与 diff check 通过。
- 当时仍未提交；该状态已由后续 PR #59 的分批提交和合并解决。

### 收尾审计结论（提交前现场）

- ftre 生产源码未命中 `runner._llm`、`core_bridge` 或旧 Core events 模块导入。
- `ftre-llm` 高置信度 vulture 扫描无死代码结果；Core 仅保留既有测试替身的
  unreachable-yield/参数告警，不影响生产路径。
- 代码门禁已通过；忽略的测试/构建生成物不属于 Git 交付内容，当时工作树仍保留
  执行前已有的大量未提交修改。
- 当前生成物盘点：ftre 源码/测试共 49 个 `__pycache__`，`packages/ftre-llm` 4 个，
  另有该包的 `build/` 与 `src/ftre_llm.egg-info/`；删除命令受当前执行环境策略阻止，
  未对工作区做宽泛递归删除。

## 2026-08-26 最终验收

### Git 交付

- Core：`feature/C7-ftre-llm-protocol-owner` → PR #17 → merge commit
  `97a99970ad739d96a46a230680b1b0678cf8488d`。
- ftre：`feature/F30-llm-service-package` → PR #59 → merge commit
  `6c6478e81fae87cb3096e9dfbf8aeafa6dd3f721`。
- ftre CI 首次暴露两个真实问题：未安装仓内 `ftre-llm`，以及 Core checkout 固定在旧提交；
  已分别通过 `8e79636`、`0abf76c`、`64aa867` 修复，最终 CI 通过。
- 两仓已同步本地 `develop`，未忽略文件工作树干净；客户端未修改。

### 最终门禁

| 门禁 | 结果 |
|---|---|
| Core 全量 pytest | `265 passed` |
| ftre 全量 pytest | `615 passed` |
| ftre CI（Python 3.12） | 通过 |
| Core/ftre ruff | 通过 |
| `git diff --check` | 通过 |
| `ftre-llm` wheel 与 entry point | `ftre_llm-0.1.0-py3-none-any.whl`，SHA256 `C82276B22849F8AE06E7AF95C1D02D492F4C9217E4E02D6891A45A9D62F046D9`；含 `ftre.plugins` Provider 入口 |
| `ftre-llm` 洁净 venv | 安装 `ftre-llm` + `cordis-py` wheel 后 `ftre_llm`/`cordis` import 成功，entry point 为 `ftre_llm.adapters.plugin:apply` |
| Gateway smoke | `GET http://127.0.0.1:48799/api/health` 返回 `200 {"status":"ok"}`，测试进程已停止 |
| 跨仓协议/安装基线 | Core `97a9997` 与 ftre `6c6478e` 已锁定并通过 CI |

最终状态：F30 PRD 状态为 `已验收`，TODO F30 及 F30.1–F30.7 均为 `done`，CHANGELOG、
执行报告与代码证据一致。测试产生的 `build/`、`egg-info/`、`__pycache__` 属于被忽略的本地
生成物，不进入 Git 交付。
