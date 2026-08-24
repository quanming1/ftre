# 执行提示词 06：F16 / Core C3 跨仓最终验收

你正在进行 Hook 终局收敛的最终审计。分别读取两个仓库的强制文档、PRD、TODO、完整执行报告、
提交历史和工作树。不要默认前批正确；发现范围内问题就修复并重跑，不得只列债务后宣称完成。

## 一、终局事实

重新生成两仓 Hook 清单和调用图，证明：

- Core 5：tool/before、tool/after、llm/stream、agent/before-reasoning、agent/stop-decision；
- Host/Package 10：messaging/route、session/created/disposed、agent/before-run/after-run/run-error、
  system-prompt/assemble、inbox/before-claim/changed/status-changed；
- 合计 15，名称唯一、发布点真实、消费者/默认行为/Failure/Scope/Owner 可追踪；
- 旧 Tool 四段名、turn-stopping 和 F15 已删除 Host 名无当前实现残留；
- 没有 alias、双发、Bridge、版本分支、复制 DTO、dirty sibling 依赖或第二生命周期 Owner。

## 二、两仓完整验证

Core：专项 + 全量 pytest、ruff、build、twine/check（若工具链规定）、wheel 内容、洁净安装、import-origin。

ftre：architecture/contracts/hooks/lifecycle/startup、Inbox/Compaction 独立测试与 wheel、无包最小
Composition、全量 pytest、ruff、diff、Gateway health/WS/消息/Tool/Command/取消/优雅关闭 E2E。

使用全新临时 venv 从声明来源安装 Core 和 ftre，不能引用 `E:\ftre-agent-core` 源码目录。临时目录
必须是已验证的 repo 外路径，安全清理，不触碰工作区/用户数据。

## 三、全盘工程卫生

扫描死代码、旧导出、compatibility、Port/Facade、空目录、缓存、build/dist/egg-info、临时 venv/db、
调试输出、过时注释和文档。迁移中有价值的中文注释必须与最终 Owner/名称一致。

## 四、状态闭环与停止条件

- 两份 PRD 的 FR/AC 逐条附证据，全部通过才标已验收；
- 两仓 TODO、CHANGELOG、README/AGENTS、执行报告与实际 15 项一致；
- 两仓按职责分批 commit，工作树干净；不混入执行前无关修改；
- 未经用户授权不 push/PR/merge/tag/release；若发行尚缺授权，阶段保持未完成并精确报告 blocker；
- 最终汇报 Core/ftre 版本、测试数字、wheel/哈希、洁净安装/E2E、删除清单、提交、分支状态和用户
  仍需执行的 PR/发布动作。
