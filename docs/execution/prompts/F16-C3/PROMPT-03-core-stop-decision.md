# 执行提示词 03：Core C3 Stop Decision 命名与语义收敛

你正在 `E:\ftre-agent-core` 完成 Core C3 的 Agent 停止决策契约。读取强制文档、C3 PRD、前两批
提交和报告；确认 Tool 两段模型已通过 Core 全量回归。

## 一、实现要求

- `agent/turn-stopping` 原子改名为 `agent/stop-decision`；删除旧常量、Spec、Payload/Result 名称、
  导出、错误文本、测试和文档，不保留 alias 或双 dispatch。
- 名称表达“Core 准备自然停止时的决策”，不是 Run 完成通知、错误通知或 finally 清理屏障。
- 零 listener 默认 Stop；Continue 携带普通 continuation message，并受既有最大次数/迭代预算约束。
- 错误、用户取消、权限中断和强制 shutdown 不伪装成自然停止，不进入错误的 continuation 循环。
- 保持 `agent/before-reasoning` 与 `llm/stream` 契约和调用顺序不变。

## 二、测试与代码质量

测试默认 Stop、一次/多次 Continue、达到上限、空/非法 continuation、取消、Hook 异常、Tool 后自然
停止和纯文本自然停止。证明 stop-decision 每个自然停止候选只触发一次，不与 ftre after-run 重复。

中文注释解释真实发布边界、预算和异常/取消为何不触发。扫描 turn-stopping 旧名、turn-stopped 混用、
compatibility、死 DTO、未使用导出、缓存和调试输出。

执行专项与 Core 全量 pytest、ruff、diff check；更新 C3 PRD/报告/TODO，按实现/测试/文档提交，不
push。停止时给出旧名清零证据和第 04 批版本输入。

