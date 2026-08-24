# 执行提示词 06：F15.6-F15.7 故障矩阵、Package 门禁与 Core 冻结

你正在 `E:\ftre` 执行 F15.6-F15.7。不要默认前五批正确；从实际调用链、Hook snapshot、
Package metadata 和测试重新审计。本批可修复 F15 范围内发现的问题，不得修改 Agent Core。

## 一、并发与故障矩阵

逐项建立确定性测试：

- before-run 拒绝、run-error 恢复成功/失败/重复 token/取消；
- after-run 在成功、错误、取消后 exactly-once，慢 listener 不造成消息重复领取；
- Session dispose 与 Inbox 清理、Gateway stop 与 listener drain；
- Inbox admission/claim/discard/恢复/容量/取消和连续 revision；
- Compaction before-claim、after-run、run-error 的失败和卸载；
- Plugin unload/restart 时 WATERFALL/SERIAL/PARALLEL in-flight 行为；
- WebSocket 慢消费者、断连和重连不产生旧状态覆盖；
- 零 listener、listener 抛错、listener 自取消、Runtime shutdown。

所有竞态使用 Event/Barrier/timeout，不使用不确定长 sleep。

## 二、Package 独立发行门禁

分别审计 Inbox/Compaction：

- pyproject build-system、依赖、版本、entry point、README、公共 `__init__`；
- wheel 不夹带 Host 私有源码、缓存、测试数据库、队列、build/dist/egg-info；
- Host 未安装 Package 时可 import、最小 Composition 可启停、直接 Agent Run 可执行；
- 安装后 discovery/load/unload/restart 和配置/数据目录正确；
- Package 只依赖公开 Service/Hook 契约，不 concrete import Host 私有实现。

在明确的 repo 外临时目录创建洁净 venv；验证绝对路径后再清理，禁止递归删除工作区或用户目录。

## 三、Core 冻结门禁

通过 ftre 的依赖解析、import/AST 和集成测试证明：

- 仍精确使用 `tools/pre-execute/execute/post-execute/result`、`llm/stream`、
  `agent/before-reasoning/turn-stopping`；
- 没有 `tool/before`、`tool/after`、`agent/stop-decision` 半迁移；
- 没有复制 Core DTO、compatibility adapter、本地 `sys.path`、sibling dirty dependency；
- Core 现有 Hook 兼容回归通过，但不在 `E:\ftre-agent-core` 写文件或提交。

## 四、验证、提交与停止

执行专项、两个 Package 独立测试、wheel build、洁净安装、无包 smoke、全量前置回归、ruff、
diff check。实际命令和结果写入执行报告，不能只写“通过”。

全部证据成立后更新 PRD 受影响 AC 和 TODO F15.6/F15.7。按“故障测试 / Package 修复 / 架构门禁”
分批提交，不 push。停止时汇报测试数、wheel 名/哈希、洁净安装结果、Core 冻结证据和第 07 批
仍需清理的精确列表。

