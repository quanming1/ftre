# 执行提示词 00：建立 F16 / Core C3 配对 PRD

你要为 Hook 终局收敛建立两个仓库的正式开发契约。本批只修改文档，不改生产代码、测试行为
或依赖版本；完成草稿后必须等待用户评审，不能自行把 PRD 从草稿跳到 approved。

## 一、强制阅读与仓库检查

分别在 `E:\ftre`、`E:\ftre-agent-core` 完整阅读各自 `AGENTS.md`、`docs/COMMIT.md`、
`docs/PROCESS.md`、`docs/TODO.yaml` 和 PRD 模板。检查分支、远端基底、工作树和未提交修改。
不得移动、覆盖、提交其他阶段的修改。

确认 ftre F15 已验收；若未验收，只能建立明确依赖 F15 的草稿，禁止开始实现。

## 二、创建配对契约

在 ftre 建立：

- `docs/prd/PRD-F16-core-hook-surface-convergence.md`；
- TODO F16，状态 `todo` 或评审时允许的状态；
- 执行报告壳 `docs/execution/EXECUTION-F16-core-hook-surface-convergence.md`。

在 Agent Core 建立：

- `docs/prd/PRD-C3-hook-surface-convergence.md`；
- TODO C3（先确认 C3 未被占用）；
- 执行报告壳 `docs/execution/EXECUTION-C3-hook-surface-convergence.md`。

两个 PRD 必须共同冻结：

1. 现状 Core 7 Hook → 目标 Core 5 Hook，全系统 17 → 15；
2. `tools/pre-execute/execute/post-execute/result` → `tool/before/after`；
3. `agent/turn-stopping` → `agent/stop-decision`；
4. 保持 `agent/before-reasoning`、`llm/stream`；
5. Tool before 的 Allow/Deny/Arguments、Tool after 的结构化最终结果；
6. 删除 around 替换执行能力和单独 result 广播的理由、替代观测面及非目标；
7. 取消、权限、并发 Tool、错误归一化、continuation 上限和零 listener 默认行为；
8. 无兼容 alias/双发/Bridge，Core 先产出可安装版本，ftre 后切换；
9. Core wheel/版本、ftre 洁净安装、两仓 CI、Gateway E2E 和回滚边界；
10. FR、AC、测试矩阵、分批任务、风险、中文注释和工程卫生要求。

## 三、迁移矩阵与评审问题

从真实代码生成发布点/消费者表，明确当前四段 Tool Hook 是否存在生产消费者；不要沿用旧审计
结论而不复核。PRD 必须回答：

- 删除 `tools/execute` 后谁调用真实 Tool Body？必须是 Core 私有直接调用，不新增公共层。
- 原 around listener 若未来出现，为什么应使用 Tool Service/Tracing 而不是恢复空 Hook？
- `tools/result` 的遥测由哪个现有 Event/Tracer 覆盖？若不能覆盖，停止并修订目标。
- tool/after 是否在 success/failed/cancelled/denied 全部触发，哪些状态允许改写？
- stop-decision 在自然停止的哪个精确边界发布，错误/取消是否不触发？
- Core 版本未发布时 ftre CI 如何验证，禁止依赖 sibling dirty worktree。

## 四、验证与停止

验证 YAML 可解析、阶段 id 唯一、PRD 链接存在、FR/AC 编号唯一、两个 PRD 术语和版本顺序一致，
执行 `git diff --check`。遵循各仓库文档提交规则；未经用户要求不 commit/push。

停止时只交付草稿路径、关键决策、未决问题和评审清单。两个 PRD 未经用户明确批准不得执行
提示词 01。

