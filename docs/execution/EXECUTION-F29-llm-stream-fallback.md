# F29 执行报告：`ftre-llm-fallback` Package

## 结果

- 状态：已完成
- 范围：ConfigService 模型解析、最后一次流 fallback Package、默认发行组合、生命周期和测试。
- 未修改：客户端、Inbox、Session、Compaction 算法、Core Retry Loop 和 Cordis。

## 实现证据

| 语义 | 代码位置 | 结果 |
|---|---|---|
| 模型解析 | `src/ftre/services/config/service.py` | `resolve_llm(provider, model)` 返回防御性 Adapter 配置快照 |
| fallback Owner | `packages/ftre-llm-fallback/src/ftre_llm_fallback/stream.py` | 只在 `attempt == max_attempts`、无已提交协议输出且错误码命中时切换 |
| 主流/备用流 | 同上 | 主错误 finish 在切换时抑制；备用成功原样转发；备用失败回传主错误且不递归 |
| 默认安装 | 根 `pyproject.toml`、`composition.py` | 默认依赖、extra、entry point 和可选 Manifest 已加入 |
| 生命周期 | `tests/lifecycle/test_f14_lifecycle_matrix.py` | restart 保持单 listener，unload 后 active listener 为 0 |

## 验证

```text
python -m pytest -q
566 passed

python -m pytest -q packages/ftre-llm-fallback/tests tests/test_config_resolve_llm.py
13 passed

python -m ruff check --no-cache src tests packages/ftre-llm-fallback
All checks passed
```

专项覆盖：前 N-1 次失败、最后一次成功 fallback、直接异常、错误 FinishChunk、部分正文、
取消、overflow/context_length、未知错误、备用失败、主流关闭、配置快照、真实 Core 集成，
以及默认 Composition 的启用、重启、卸载和禁用路径。

## 收尾

PRD `PRD-F29-llm-stream-fallback-plugin.md` 与 `TODO.yaml` 已同步为已验收。运行中的 Gateway
未被 kill/restart；未执行 commit、push 或发布。C6/F29 后续只需按仓库流程提交和发行，不再有
未完成的代码任务。

## Refactor Cleanup Audit（2026-08-25）

- **Owner**：`ConfigService.resolve_llm()` 是模型配置快照的唯一 Host Provider；
  `ftre-llm-fallback` 是 `llm/stream` 的唯一 fallback 行为 Owner；Composition Root 只声明
  Manifest，不持有 fallback 状态。Core 的 Retry/错误分类不被 Package 复制。
- **边界**：Package 源码 AST 扫描未发现对 `Agent Runtime`、`Session Repository`、Gateway 或
  Plugin Loader 私有实现的 import；仅依赖 `ftre.services.llm.hooks` 稳定 Hook 契约、ConfigService
  公开解析方法和 Core 公共 Adapter 工厂。生产代码没有旧 `llm/attempt-failed`、
  `llm/retry-decision` 或 `LLMRecoveryDecision` 引用，历史文档已明确标注废弃。
- **生命周期**：Hook Receipt 绑定 Package Fiber；restart 后保持一个 active listener，unload
  后为 0；主流在切换前关闭，备用 Adapter 使用 `max_retries=0`，不递归进入第二个 fallback/Retry
  生命周期。取消、已有协议输出、overflow 和备用失败均保持原错误路径。
- **卫生**：清理本轮测试生成的 `.pytest_cache`、`.ruff_cache`、`build` 和 Core 根级缓存；
  保留 ftre `data/sessions.db` 与 `.ftre-inbox` 下运行时数据。生成物/空目录扫描未发现需要删除的
  受版本控制源码目录；`git diff --check` 通过。
- **工作树**：当前 ftre `develop` 包含执行前已有的 F26/F27/F28 等修改及新增 Package；本审计
  未回滚、未提交、未 push，也未修改客户端或重启 Gateway。
