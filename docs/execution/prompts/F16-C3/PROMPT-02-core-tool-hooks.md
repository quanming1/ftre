# 执行提示词 02：Core C3 Tool Hook 四段收敛为两段

你正在 `E:\ftre-agent-core` 实现 Core C3 的 Tool Hook 收敛。先读取 Core 强制文档、C3 PRD、
基线提交和执行报告；确认第 01 批证明没有必须保留的 around/result 生产消费者。

## 一、目标协议

- `tool/before`：在任何 Tool Body 副作用前执行，输入稳定 call identity、不可变 arguments、取消
  信号；输出 Allow、Deny 或替换 Arguments。
- `tool/after`：Tool 形成统一结构化结果后执行，输入 identity、实际 arguments、success/failed/
  cancelled/denied 结果；输出最终结构化 ToolExecutionResult。
- Tool Body 由 Core 私有代码直接调用，不公开 invoke continuation，不新增 Port/Executor/Facade。
- 删除四个旧 Spec、常量、Payload、默认函数、导出、dispatch 和兼容 alias。
- Tracer/Event 继续提供事实观测；不为替代 `tools/result` 新增另一个同义 Hook。

## 二、行为不变量

- Deny 时 Body 不执行，after 的 denied 语义按 PRD 唯一决定；
- Arguments 替换使用副本，不污染并发 sibling call；
- success、Tool 抛错、参数错误、取消和权限确认都归一为既有 ToolResult；
- after 不能导致 Tool Body 再执行；非法返回类型 fail loud；
- 并发 Tool 调用 identity、结果和取消不串线；
- 零 listener 与迁移前用户可见行为一致。

## 三、测试、注释和清理

覆盖 Allow/Deny/Arguments、同步/异步、success/failed/cancelled、after 改写、非法类型、多 listener、
并发和取消。中文注释解释 before/after 的副作用边界、为什么 Body 私有直调、为什么删除 around
和 result。删除旧测试 helper、re-export、类型转换、死代码、缓存和 build 产物。

执行 Core Hook/Tool 专项、全量 `pytest`、`ruff check .`、`git diff --check`。更新 C3 PRD/执行报告/
TODO 子任务，按协议/实现/测试职责提交，不 push。随后在 ftre 只读运行兼容性扫描，预计失败点必须
与 F16 迁移表一致，不能临时改 ftre 让本批变绿。

