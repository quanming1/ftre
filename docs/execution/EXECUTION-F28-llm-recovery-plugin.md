# F28 执行报告：`ftre-llm-recovery` Package

## 结果

- 状态：已完成
- 范围：ftre LLM Hook 窄重导出、可选恢复策略 Package、默认 Manifest、配置、测试和文档。
- 未修改：客户端、Inbox、Session 持久化、Compaction 算法、Cordis；Stream Fallback 留给 F29。

## 实现证据

| 语义 | 代码位置 | 结果 |
|---|---|---|
| Core 契约复用 | `src/ftre/services/llm/hooks.py` | 重导出 Core 同一 `LLM_ERROR_SPEC`，无第二套 DTO |
| Package 入口 | `packages/ftre-llm-recovery/src/ftre_llm_recovery/plugin.py` | 只注入 `hook_runtime`，`provide=()`，按 Fiber 注册可逆 listener |
| 策略配置 | `packages/ftre-llm-recovery/src/ftre_llm_recovery/config.py`、`policy.py` | 按错误码返回 retry/stop；未知、非法、overflow/context_length 返回 `None` |
| 默认安装 | `src/ftre/app/gateway/composition.py`、根 `pyproject.toml` | 默认启用候选并声明包依赖，可通过配置禁用 |
| 生命周期 | `tests/lifecycle/test_f14_lifecycle_matrix.py` | restart 后仅保留一个 active listener，unload 后清零 |

## 验证

```text
python -m pytest -q
551 passed

python -m pytest -q packages/ftre-llm-recovery/tests
5 passed（另含生命周期组合测试）

python -m ruff check src tests packages
All checks passed

python -m build --wheel --sdist --outdir <临时目录>
ftre_llm_recovery-0.1.0 wheel/sdist 构建成功

洁净 venv（--no-index --no-deps）安装 Core 与 recovery wheel
entry point `llm-recovery = ftre_llm_recovery.plugin:apply` 可发现
```

## 收尾

PRD `PRD-F28-llm-recovery-plugin.md` 与 `TODO.yaml` 已同步为已验收。运行中的 Gateway 未被
kill 或重启；工作树中已有的用户修改保持不动，未执行 commit/push。

## Refactor Cleanup Audit

- **Owner**：`ftre-llm-recovery` 只拥有错误码策略和配置快照；Core 拥有实际 retry loop，
  `ftre-compaction` 仍拥有 overflow 恢复；没有重复 Service/Port/Coordinator。
- **边界**：Package 仅 import `ftre.services.llm.hooks` 的公开契约和 Cordis Context；AST
  扫描未发现跨 Owner 私有 import。Host 重导出与 Core Spec 身份断言通过。
- **生命周期**：默认 Composition 的 restart 后只保留一个 active listener，unload 后 active
  listener 为 0；无空目录、无路由/Task/Watcher 残留。
- **旧入口**：生产代码和测试没有旧 `llm/attempt-failed`/`llm/retry-decision` 引用；两处
  历史记录均已明确标记为废弃名称。
- **卫生**：最终清理了两仓源码/测试/文档范围内 81 个缓存或构建目录；ftre 剩余数量为 0，
  空目录为 0。两仓仍保留用户原有未提交修改，未执行 commit/push。
