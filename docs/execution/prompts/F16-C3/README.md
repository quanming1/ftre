# F16 / Agent Core C3 Hook 终局收敛提示词

本目录指导第二阶段：在 F15 已验收后，把 Agent Core 的 4 段 Tool Hook 收敛为 2 段，
将 `agent/turn-stopping` 改为 `agent/stop-decision`，最终使全系统 Hook 从 17 个变为 15 个。

这是配对阶段，需要两个仓库各自拥有 PRD 和 TODO：

- `E:\ftre-agent-core`：C3，拥有 HookSpec、DTO 和算法调用点；
- `E:\ftre`：F16，拥有依赖版本、Host HookRuntime 集成、Package/Plugin 消费和端到端验收。

当前第二阶段尚未正式立项，因此提示词 00 只建立/评审契约，不改生产代码。只有两个 PRD
都为 `approved`，并且 F15 已验收，才可执行 01-06。

## 执行顺序

| 批次 | 仓库 | 目标 |
|---|---|---|
| 00 | 两仓文档 | 建立 F16/C3 PRD、TODO、迁移矩阵和共同 AC，等待用户评审 |
| 01 | 两仓只读 + 测试基线 | 证明 4→2/改名可行，冻结无消费者、行为和版本边界 |
| 02 | Agent Core | 实现 `tool/before`、`tool/after`，删除四段旧协议 |
| 03 | Agent Core | 实现 `agent/stop-decision`，删除 turn-stopping 旧协议 |
| 04 | Agent Core | 全量验证、wheel、版本/发行候选和 Core 收尾 |
| 05 | ftre | 升级 Core 版本、迁移 Host/Package/测试，17→15 |
| 06 | 两仓 | 跨仓洁净安装、E2E、清理、PRD/TODO/报告最终验收 |

## 共同边界

- Core 保持无状态，不 import ftre、Cordis、Session、Inbox、Compaction、Channel。
- ftre 不复制 Core DTO，不保留旧名 alias、双 dispatch、桥接 adapter 或本地 `sys.path`。
- 不为两段 Tool Hook 增加 `Port`、Coordinator、Facade、Service Bag 或第二执行器。
- Core 发布/Tag/PyPI、push、PR、merge 需要用户明确授权；普通实现提示词只授权本地 commit。
- 中文注释解释 Hook 时机、可改写范围、取消、错误归一化和为什么删除 around/观察层。
- 每批清理死代码、旧导出、陈旧测试/文档、缓存、构建物和空目录，并记录可复现证据。

