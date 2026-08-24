# 执行提示词 05：F15.5 Package 与内置 Plugin 消费迁移

你正在 `E:\ftre` 执行 F15.5。读取强制文档、F15 PRD、前四批提交和执行报告。目标是让所有
生产消费者只使用 17 项目标 Hook，不保留旧名、双注册或二次 Effect。

## 一、ftre-inbox

- 监听 `messaging/route` 接管普通输入，保持 Command 优先级和 capability error。
- 继续监听 Core `agent/before-reasoning`，原子消费 next-step；不得改名或复制 Core DTO。
- 监听 awaited `session/disposed` 完成对应 Session Inbox 清理。
- 发布 before-claim/changed/status-changed；删除三种 mutation Hook 残留。
- 所有 listener 绑定 Package Plugin Context，由一个 Fiber Owner 清理。

## 二、ftre-compaction

- `agent/after-turn` 迁为 `agent/after-run`；`agent/request-error` 迁为 `agent/run-error`；
  `inbox/before-claim` 保持不变。
- 保持三条业务语义：领取前硬门控、Run 后预压缩、overflow 后有界恢复。
- 保持 progress token、pending 保留、取消、失败和卸载降级；Host 不 import 压缩私有实现。
- 删除 receipt 的第二 `ctx.effect`、旧常量、旧注释和旧测试名称。
- 注意执行前可能存在用户对 `ftre_compaction/hooks.py` 的独立注释修改：必须通过 git 基线识别，
  不得覆盖或误提交非 F15 内容。

## 三、内置 Plugin

- Command 监听 `messaging/route`，纯 Command 不进入 Inbox/Agent Run。
- WebSocket 使用 awaited Inbox 事实 Hook，wire 不变。
- Session Title 继续使用 `system-prompt/assemble`，不因本阶段增加新 Hook。
- 全部 Plugin 使用公开 HookSpec 和 Inject Service，不访问 HookRuntime 私有条目或其他 Owner 私有实现。

## 四、验证与清理

测试 load/unload/restart、依赖缺失/恢复、in-flight listener、禁用 Inbox/Compaction、压缩失败、
Command-first、next-step、权威快照和 Session 删除。扫描旧 Host Hook 名、双发、compatibility、
重复 disposer、Package 反向 concrete import、缓存和 build 产物。

至少执行：

```powershell
python -m pytest -q packages/ftre-inbox/tests packages/ftre-compaction/tests
python -m pytest -q tests/contracts tests/lifecycle tests/startup tests/architecture
python -m ruff check --no-cache src tests packages
git diff --check
```

同步有价值中文注释，解释 Package 缺失行为和卸载语义。更新执行报告/PRD/TODO F15.5，按 Inbox、
Compaction、内置消费者、测试分批提交，不 push。停止时工作树不得包含半迁移旧名。

