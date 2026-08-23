# 执行提示词 05：F14.5 Builtin Plugin 目录与 Owner 迁移

你正在 `E:\ftre` 执行 F14.5。目标是让产品行为和 concrete adapter 在目录上明确表现为
Plugin，最终删除含义模糊的 `features/`。这不是把文件机械复制到新目录；每项能力必须同时完成
Owner、inject/provide、Effect、测试和旧路径清理。

## 一、前置检查

- 阅读强制文档、F14 PRD、Owner 基线、执行报告和最新 Composition。
- 确认 F14.1-F14.4 已完成，Messaging/Agent 边界已经稳定。
- 逐项检查 Command、WebSocket、Subagent、Session Title、Context Govern、Plan、Trace、MCP、
  Skill、Schedule、Team 的 Service、Route、Tool、Hook、Task、Store 和清理函数。
- 禁止修改仓库外项目，禁止 push/merge/release。

## 二、目标迁移

迁入 `src/ftre/plugins/builtin/`：

```text
command/
channels/websocket/
channels/subagent/
session_title/
context_govern/
plan/
trace/
mcp/
skill/
schedule/
team/
```

保留在 `services/`：Host 稳定 registry/base contract，例如 ChannelService、HttpService、
ToolService、SystemPromptService。Plugin 自带且只服务于该能力的 Service/Store/Router/Tool
跟随 Plugin 迁移，不制造同名 Host Service 壳。

## 三、逐能力执行清单

对每项能力逐一完成，不能只做前几个就宣称全部：

1. 明确唯一 Plugin id、entry、inject、provide 和 required/default_enabled。
2. Plugin 创建自己的 Service/Store/Adapter，并通过 Effect 管理 Router、Hook、Tool、Channel、
   Task、Watcher、连接和 dispose。
3. 消费其他能力时只 Inject 公开 Service/Hook，不 import 对方 private implementation。
4. concrete WebSocket/Subagent 是 Channel Plugin；Channel base/registry 不下放到 Plugin。
5. Command Service 跟随 Command Plugin，使用 F14.4 Messaging Hook，不回到 Agent Runtime。
6. Trace Plugin 唯一拥有 exporter/store/router，禁止 Agent 再创建 exporter。
7. Schedule 唯一拥有 store/scheduler/channel/tool/router；停止顺序能等待任务退出。
8. MCP/Skill/Team 等自有 Service 保持稳定 key，缺失时基础 Turn 正常。
9. 更新 Composition entry 和所有生产/测试/文档 import。
10. 删除 `src/ftre/features`、旧 provider 路径、重复文件、re-export 和 compatibility alias。

一次只迁移一个完整 Owner；该 Owner 的专项测试通过后再迁下一个，避免大爆炸式 diff 无法定位。

## 四、中文注释与工程可读性

- 每个 Plugin 的模块 docstring 用中文回答：拥有哪项能力、提供什么、注入什么、卸载后消失什么。
- Service/Store 内注释解释状态所有权、并发、落盘和失败恢复；Plugin 内注释解释生命周期顺序。
- 迁移时保留有价值的业务注释并修正路径/术语；删除“Feature 层”“旧目录兼容”等陈旧说明。
- 不用注释掩盖函数过长；先按 Owner 内部职责拆分，再给非显然边界写注释。

## 五、测试和清理

每项 Plugin 至少有：load、行为、unload、restart、缺失依赖/可选禁用测试。还要验证 Route/Tool/
Hook/Channel/Task/Store 清理和 restart 使用新实例。

最终执行：

```powershell
rg -n "ftre\.features|src/ftre/features" src tests packages docs
python -m pytest -q tests/architecture tests/contracts tests/lifecycle tests/startup
python -m pytest -q
python -m ruff check src tests packages
git diff --check
```

全盘删除本批死代码、重复 Owner、空目录、缓存、临时状态和旧文档路径。不要保留空包占位。

## 六、停止条件

- 所有列出的能力逐项迁移和验证；`features` 目录/生产 import 为零；
- 每项只有一个 Plugin Owner，资源全部由 Effect 管理；
- 缺失任一可选 Plugin 时基础链路符合 PRD；
- PRD/执行报告/Composition/README/TODO `F14.5` 一致；
- 按 Owner 分批提交，不 push，工作树无本批未提交修改；
- 汇报逐项迁移表、删除项、生命周期证据、测试和提交。
