# 执行提示词 02：F15.2 HookRuntime 生命周期与 awaited 语义

你正在 `E:\ftre` 执行 F15.2。先读取强制文档、F15 PRD、提示词索引、F15.1 提交与完整
执行报告；确认 F15.1 已完成且基线测试通过。只在 F15 feature 分支工作，不修改其他仓库。

## 一、本批目标

收敛 `src/ftre/kernel/hooks/` 的机制语义，但不在 Kernel 中加入 Agent、Session、Inbox、
Compaction 等业务判断：

1. 明确 `EMIT` 是 detached、可丢失遥测；业务清理和权威状态不得使用它。
2. `PARALLEL` 启动全部 listener 并等待全部结束；OBSERVE 失败全部诊断，不提前取消兄弟任务。
3. `SERIAL` 按稳定注册顺序等待；`WATERFALL` 保持 continuation 且每层最多调用一次 `next_()`。
4. Plugin 注册必须显式绑定当前 Cordis Context/Fiber；业务 Plugin 不得静默绑定根 Context。
5. 一次注册只有一个常规生命周期 Owner。删除 Plugin 手工 `ctx.effect(receipt.dispose)` 与
   Runtime companion Effect 重复拥有同一 disposer 的模式。
6. unload/restart 先拒绝新调用，再等待 active listener 归零；取消、listener 异常、重复 dispose
   均幂等且不泄漏 Task。
7. 收敛 `scope`/`global_listener` 等重复表达，按 PRD 使用真实 Context 与
   `all_agent_scopes`，诊断标签不得参与匹配。

不要重写 Cordis，不增加第二注册表、Service Locator、后台清理线程或兼容重载。

## 二、测试要求

补齐确定性单元与生命周期测试：

- 四种 mode 的零/单/多 listener、返回类型、异常和取消；
- PARALLEL 的全部启动、全部等待、多个异常诊断；
- Context 缺失、错误 scope、父子/兄弟 Agent、同 id Context 重建；
- unload 与正在执行的 WATERFALL/SERIAL/PARALLEL 竞态；
- 新调用被拒绝、旧调用 drain、重复 dispose、restart 后只命中新 listener；
- snapshot/diagnostics 只显示 name、owner、mode、真实 scope、顺序、active/disposed，不记录 payload。

使用 Event/Barrier/短超时同步，不用长 sleep 猜调度。

## 三、注释与清理

- 为并发锁、active_calls、drain barrier、LIFO Effect 和 detached EMIT 写中文“为什么”注释。
- 删除与新 API 冲突的旧参数、重复 helper、陈旧示例和无用 receipt wrapper。
- 扫描未 await coroutine、裸 `create_task`、循环引用、重复 Effect、缓存和调试输出。

## 四、验证与收尾

至少执行：

```powershell
python -m pytest -q tests/hooks tests/lifecycle tests/contracts
python -m pytest -q tests/architecture
python -m ruff check --no-cache src tests packages
git diff --check
```

更新执行报告、PRD 变更记录和受影响 AC；全部通过后将 F15.2 标 `done`。按“Runtime 机制 / 生命周期
测试 / 文档”职责提交，不 push。

停止前确认 Kernel 仍然只知道通用 Spec/Context/Fiber/Effect，生产 Plugin 尚未被半迁移，工作树
只保留明确的后续批次改动。汇报提交、测试和第 03 批需要迁移的新注册 API。

