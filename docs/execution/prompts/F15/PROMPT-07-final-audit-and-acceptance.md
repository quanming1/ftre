# 执行提示词 07：F15.8-F15.9 全盘清理与最终验收

你正在 `E:\ftre` 执行 F15 最终批。不要相信“前面已经做完”；必须重新审计代码、测试、
文档、Package 和提交历史，修复 F15 范围内的全部缺口后再验收。

## 一、前置审计

1. 阅读强制文档、F15 PRD、七批提示词、完整执行报告和 F15 全部提交。
2. 核对当前分支、工作树和执行前遗留修改；禁止吞并不属于 F15 的用户改动。
3. 从 AST 和运行时 snapshot 重新生成 Hook 清单，必须精确为 Host 10 + Core 7。
4. 逐项对照 FR1-FR23、AC1-AC20；没有证据的条目保持未勾选并修复，不能文字豁免。

## 二、全盘清理

使用 `rg --files`、`rg`、AST/import 扫描和测试证据检查：

- 删除的 Host Hook 常量、DTO、dispatch、listener、导出、测试、README 和历史示例；
- alias、deprecated、compatibility、双发、no-op fallback、重复 HookSpec/Owner/Effect；
- 未 await coroutine、detached 业务 Task、旧闭包、listener/repository 生命周期泄漏；
- Plugin 跨 Owner private import、Package 反向 Host concrete import、Core DTO 复制；
- 空 `__init__` 兼容壳、死 helper、未使用 import、注释掉旧实现和失效 TODO；
- `__pycache__`、`.pyc`、`.pytest_cache`、build/dist/egg-info、临时 venv/db/queue、空目录和调试输出；
- AGENTS、PRD、TODO、CHANGELOG、README 中的旧 Hook 数量、名称和时机。

发现可证实的 F15 债务就修复并补测试，不能只列入报告。删除前验证目标绝对路径位于仓库内。

## 三、完整验证

至少执行并记录实际结果：

```powershell
python -m pytest -q tests/architecture
python -m pytest -q tests/contracts
python -m pytest -q tests/hooks
python -m pytest -q tests/lifecycle
python -m pytest -q tests/startup
python -m pytest -q packages/ftre-inbox/tests packages/ftre-compaction/tests
python -m pytest -q
python -m ruff check --no-cache src tests packages
git diff --check
```

同时完成两个 Package wheel/洁净安装、无包最小 Composition、Gateway health/WebSocket attach/
消息 admission/Agent reply/Command/queue/status/cancel/优雅关闭 smoke，以及生成物扫描。

## 四、文档与最终状态

- PRD：按证据勾选 FR/AC，追加变更记录、验收日期，状态仅在全部通过后改为 `已验收`；
- TODO：F15.8/F15.9 和阶段 F15 仅在全通过后标 `done`；
- CHANGELOG `[未发布]`、AGENTS/README、中文 Hook 文档与实际 17 项契约一致；
- 执行报告列出事实清单、删除项、生命周期矩阵、Package 结果、每条 AC 证据、全部提交和未完成项。

按“审计修复 / 测试 / 文档验收”职责提交，commit 前重读规范。不 push、PR、merge 或 release。

## 五、最终停止条件

- Host 10 + Core 7 精确成立，删除的 Host 旧名全盘清零；
- 全量测试、ruff、diff、wheel、洁净安装、无包和 Gateway smoke 全部通过；
- PRD/TODO/CHANGELOG/执行报告与代码一致；
- F15 修改全部分批提交，最终工作树干净；若有执行前遗留改动，明确证明来源且不得宣称干净；
- 最终只汇报实现结果、关键删除、测试数字、构建物、提交列表和需用户执行的 push/PR。
