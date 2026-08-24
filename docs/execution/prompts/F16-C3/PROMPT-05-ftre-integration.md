# 执行提示词 05：F16 Host 切换到 Core 五项 Hook

你正在 `E:\ftre` 执行 F16 配对迁移。前置条件：Core C3 已有可由洁净 CI 解析的受审版本或不可变
commit，Core 自己的 PRD/测试已完成。只有本地 wheel、dirty sibling 或未授权版本时停止。

## 一、依赖与契约迁移

- 更新 pyproject/lock/CI 的 Core 最低版本，保证普通安装和 CI 使用同一来源；
- 迁移所有 import、HookSpec、DTO、类型检查、错误文本、测试和文档：
  - tools/pre-execute/execute/post-execute/result → tool/before/after；
  - agent/turn-stopping → agent/stop-decision；
- 保持 F15 的 Host 10 个 Hook 不变，最终精确为全系统 15 个；
- Inbox 的 before-reasoning、Compaction 的 Host Hook、System Prompt、Session、Messaging 均不借机改造；
- 不保留 alias、双 dispatch、adapter、复制 Core DTO 或版本条件分支。

## 二、集成行为

使用真实 ftre HookRuntime 验证 Tool Allow/Deny/Arguments/After、权限、取消、并发、错误和结果投影；
验证 Stop/Continue/上限、next-step before-reasoning、LLM stream、after-run/Compaction 不回归。
Desktop wire、Session 数据、Inbox 持久格式、Command 和 Gateway 路由必须不变。

## 三、测试与卫生

更新架构清单为 Host 10 + Core 5；旧 Core Hook 名在 ftre 源码、Package、测试和当前文档清零，历史
PRD 若保留必须明确标注历史，不伪装当前契约。清理旧 helper、兼容导出、缓存、build/egg-info 和
临时 wheel 安装痕迹。

执行 Hook/Agent/Tool/Package/architecture/lifecycle/startup 专项、全量 pytest、ruff、diff check、
洁净安装和 Gateway smoke。更新 F16 PRD/执行报告/TODO，按依赖/迁移/测试/文档职责提交，不 push。

停止时汇报 Core 来源版本、15 项清单、测试数、删除项、提交和最终批输入。

