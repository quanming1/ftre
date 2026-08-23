# 执行提示词 07：F14.8-F14.10 生命周期、清理与最终验收

你正在 `E:\ftre` 执行 F14 最终批次。不要默认前六批已经正确；必须从代码、测试和运行结果
重新审计。你的任务是补齐生命周期与故障覆盖，清除全部迁移债务，逐条验证 PRD，并留下诚实、
可复现的最终执行报告。

## 一、开工与范围

1. 完整阅读 `AGENTS.md`、提交/流程/TODO、F14 PRD、七批提示词和完整执行报告。
2. 检查正确 feature 分支、提交历史和工作树；列出前六批承诺与实际代码的差异。
3. 只改 `E:\ftre`。Desktop/Core/Cordis 的验证可以只读运行；未经明确授权不得修改、提交、
   push、merge 或发布这些仓库。
4. 本批授权修复 F14 范围内发现的问题并分批 commit；不授权 push/PR/merge/release。

## 二、生命周期与故障矩阵

逐项建立或补齐真实测试：

- 最小 Agent Composition、Gateway Composition、默认完整 Composition；
- 每个 Host Provider 和 Builtin/Package Plugin 的 load/unload/restart；
- 依赖缺失进入 pending、依赖恢复后激活、required 失败阻止启动；
- in-flight Hook、LLM stream、Tool call、Inbox claim、Command、Channel send 的取消/drain；
- Session 删除、Gateway stop、WS 断连、Package restart、配置 watcher、SQLite/JSON store 关闭；
- restart 后 Hook/Route/Tool/Channel/consumer 只引用当前 Service，不保留旧闭包；
- 无 Inbox、无 Compaction、无 Command、无各可选 Plugin 的降级行为；
- pending 不丢失、不重复消费，Command 不创建 Turn，基础 Agent Turn 不依赖可选 Package。

测试必须有超时和确定性同步，禁止用长 sleep 掩盖竞态。

## 三、全盘重构清理审计

使用 `rg --files`、`rg`、AST/import 扫描、测试覆盖证据和可用的死代码工具，至少检查：

- `ftre.kernel`、`ftre.features`、`ftre.services.agent_loop` 旧引用和旧目录；
- compatibility/re-export/legacy/fallback/no-op/bind setter/Service Bag/Locator；
- 重复 Plugin id、Service key、Owner、HookSpec、Route、Tool、Command、Channel、Exporter；
- Plugin A import Plugin B 私有 Repository/Runtime/Adapter；
- Agent Runtime 中的 Queue/Command/Compaction/WebSocket 业务类型；
- 未受 Effect 管理的 Task、Thread、Watcher、listener、route、connection、store；
- 空 `__init__` 兼容包、空目录、临时脚本、调试日志、注释掉的旧实现；
- `__pycache__`、`.pyc`、`.pytest_cache`、build、dist、egg-info、临时 DB/queue/venv；
- README、AGENTS、PRD、TODO、CHANGELOG 中陈旧路径和过时流程。

发现可证实的 F14 债务就修复并补回归测试，不能只列清单。清理文件前确认绝对路径位于仓库内，
保留用户数据和执行前不属于 F14 的修改。

## 四、注释最终审查

逐个通读本阶段改动文件，不只运行格式化：

- 公共 Service/Plugin/HookSpec 有中文职责和缺失行为说明；
- 并发、取消、重试、落盘、claim、in-flight drain、逆序 dispose 有“为什么”注释；
- 注释中的路径、Owner、Hook 名、阈值和顺序与代码一致；
- 删除逐行翻译、重复标题分隔线、旧架构说明和已失效 TODO；
- 不用注释代替测试或为复杂坏设计辩护。

## 五、完整验证

至少执行并在报告保留实际结果：

```powershell
python -m pytest -q tests/architecture
python -m pytest -q tests/contracts
python -m pytest -q tests/lifecycle
python -m pytest -q tests/startup
python -m pytest -q
python -m ruff check src tests packages
git diff --check
```

另外执行：

- `ftre-inbox`、`ftre-compaction` 各自独立测试；
- 两个 Package wheel build 与洁净安装；
- 无可选 Package 的最小 Composition smoke；
- `ftre gateway` 启动、连接、消息/Command/取消、优雅关闭 smoke；
- 源码/测试/Package 生成缓存与空目录扫描；
- `git status --short` 和提交历史核对。

任何失败都回到实现修复并重跑相关门禁。不能把失败标成“环境问题”后勾选 AC，除非有确凿
外部证据且 PRD 明确允许；此时 F14 保持未完成。

## 六、文档与状态闭环

更新：

- F14 PRD：FR/AC 只按证据勾选，追加变更记录和验收日期；
- TODO：F14.8-F14.10 及 F14 仅在全部通过后标 `done`；
- `AGENTS.md`、README、Service/Plugin 文档：只描述最终实际架构；
- CHANGELOG `[未发布]`；
- `docs/execution/EXECUTION-F14-final-plugin-first-architecture.md`。

执行报告必须包含：目标树实际快照、Owner 表、依赖图、各批提交、删除清单、生命周期矩阵、
Package 构建结果、每条 AC 的证据、未完成项和跨仓库验证边界。

## 七、提交与最终停止条件

- 按“故障测试 / 债务修复 / 文档验收”分批 commit；commit 前重读规范。
- 禁止把无关修改、用户配置、缓存、临时测试数据提交。
- 不 push、不创建 PR、不 merge develop、不发版。
- 最终 `git status --short` 必须为空；如果存在执行前遗留修改，必须证明来源并保持未触碰，
  此时不能宣称“F14 分支干净”。
- 只有代码、测试、文档、TODO、CHANGELOG、执行报告和提交历史全部闭环时才汇报完成。
- 最终汇报应简洁列出：实现结果、关键删除、测试数字、wheel/clean-install/Gateway 结果、提交列表、
  分支状态，以及仍需用户执行的 push/PR 动作。
