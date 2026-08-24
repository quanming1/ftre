# 执行提示词 01：Core C3 / F16 事实审计与契约基线

你正在执行跨仓 Hook 收敛的基线批次。确认：ftre F15 已验收，ftre F16 与 Core C3 两份 PRD
均为 `approved`/`开发中`，TODO 状态和 feature 分支正确。否则停止，不改生产代码。

## 一、仓库与分支

- Core：`E:\ftre-agent-core`，只在 `feature/C3-hook-surface-convergence`；
- Host：`E:\ftre`，只在 `feature/F16-core-hook-surface-convergence`；
- 分别读取强制文档、配对 PRD、TODO、执行报告、F15/C2/C1 历史证据；
- 记录两个工作树的执行前修改，禁止跨阶段混提交，不 push/merge/release。

## 二、事实审计

使用 AST、`rg`、调用链和运行时 fake dispatcher 证明：

- 7 个 Core Hook 的定义、发布点、消费者、DTO、default、mode、scope、failure；
- 四段 Tool Hook 在同步/异步 Tool、并发批次、权限 ask/deny、取消、异常时的精确顺序；
- 是否存在生产 `tools/execute` around 或 `tools/result` listener；
- Tool Result 与 Agent Event/Tracer 的关联，删除 result Hook 后观测是否缺失；
- turn-stopping 的自然停止边界、Stop/Continue、最大 continuation 和错误/取消分支；
- ftre 对 Core 名称/DTO/版本的所有 import、测试、文档和 Package 依赖。

若发现真实消费者使 PRD 的 4→2 丢失必要能力，停止生产迁移，更新执行报告和 PRD 评审项；不得
为了满足数字目标删除能力。

审计同时扫描旧桥接层、重复 DTO/导出、未使用 helper、缓存、构建物、临时脚本和陈旧注释；
本批只建立基线，不提前删除生产实现。新增扫描器和复杂测试用中文注释解释规则、证据来源和
误报边界，禁止以过宽 allowlist 固化债务。工程卫生只清理本批生成的缓存、临时报告和调试输出，
存量生产债务必须记录到所属后续批次，不能在基线批偷跑迁移。

## 三、迁移门禁测试

在 Core 建立能够先对旧契约通过、迁移后对新契约通过的行为测试；在 ftre 建立精确目标 15 项的
架构断言和禁止半迁移名称的门禁。测试必须覆盖 zero-listener 默认行为和错误类型。

执行两仓专项 pytest、ruff、diff check。更新两份执行报告和 PRD 变更记录；只在证据完整时更新
对应基线子任务。按仓库规范分别提交，不把两仓文件放进一个 commit，不 push。

停止时汇报消费者数量、调用顺序、是否满足删除前提、两仓提交和第 02 批精确输入。
