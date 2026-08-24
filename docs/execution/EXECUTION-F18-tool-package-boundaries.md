# F18 执行报告：消息、任务与团队 Tool Package 边界收敛

> 状态：已完成；F18.1–F18.6 已验收，未提交 Git。

## 1. 范围与边界

- 仓库：`E:\\ftre`；分支：`feature/F18-tool-package-boundaries`。
- 允许修改：ftre Host、仓内 `packages/`、测试和文档。
- 明确未修改：客户端、`E:\\ftre-agent-core`、`E:\\cordis-py`。
- 目标：Inbox 只拥有队列基础设施；三个业务 Tool 各自拥有独立 Package/Plugin。

## 2. Owner 迁移基线

| 能力 | F17 临时 Owner | F18 目标 Owner | 当前状态 |
|---|---|---|---|
| Inbox Service/Queue Hook/Worker | `ftre_inbox.plugin` | `ftre_inbox.plugin` | 已收窄 |
| `send_message` | `ftre_inbox.plugin` | `ftre-messaging` / `messaging` | 已迁移 |
| `task` | `ftre_inbox.plugin` | `ftre-task` / `task` | 已迁移 |
| `team_*`、`wait_agent` | `ftre_inbox.plugin` | `ftre-team` / `team` | 已迁移 |
| 内存 `TeamService` | `plugins/builtin/team` | 无；Session metadata 是唯一状态 Owner | 已删除 |

## 3. 已完成工作

- 新增 `packages/ftre-messaging`、`packages/ftre-task`、`packages/ftre-team`，各自包含
  `pyproject.toml`、README、entry point、Plugin 和 Tool 工厂。
- `ftre-inbox.plugin` 删除业务 Tool 导入/注册，只保留 Inbox Service、Hook、Worker 和
  持久化生命周期。
- `ftre-team` 通过注入公开 `agent_profiles` Service 管理成员 profile，不再 import
  `ftre.services.agent.profile` 私有 helper。
- Composition 声明三个可选 Package Plugin；未安装可选业务包不阻止 Host 启动。
- 删除未被 Team 工具消费的 `src/ftre/plugins/builtin/team` 重复 Provider。
- `ftre` extras 增加 `messaging`、`task`、`team`，`full` 覆盖四个业务 Package 与
  Compaction。
- F17 PRD、执行报告、TODO、README 和 CHANGELOG 已纠正“依赖 Inbox 即 Inbox Owner”的错误描述。

## 4. 验证结果

| 检查 | 结果 |
|---|---|
| 全量 pytest | `497 passed in 160.79s` |
| 架构/启动/生命周期/Package 专项 | 通过 |
| Ruff | `python -m ruff check src tests packages --no-cache` 通过 |
| F10 Inbox Provider 重启 | 先卸载依赖业务 Plugin 后通过；避免 Provider 替换与消费者重载并发 |
| Host wheel | `ftre-0.3.0`，182 文件，SHA256 `ba52f84b1958cb25c42bd307eb2b9bd5bc6a9f635b28bdc7ab1687391b1f72a5` |
| Inbox wheel | `ftre_inbox-0.2.0`，12 文件，SHA256 `0e6d9edb3642d4ce74bc9114611721404856f7910035045d76bfe9908cb54656` |
| Messaging wheel | `ftre_messaging-0.1.0`，8 文件，SHA256 `565610115ad2b0bc67bc0afddb3b1c9663ccf1770b86cd503b1042e26f1578b9` |
| Task wheel | `ftre_task-0.1.0`，8 文件，SHA256 `31a05c223b283f3a32641ccbe3a2e68b0d1b0522f3cf265065f41ba64b3f47aa` |
| Team wheel | `ftre_team-0.1.0`，8 文件，SHA256 `1b24c66da997f98466a484d94fa08cabf6c059ca1b00d9289aac167ccb3b12fa` |
| wheel 内容门禁 | 全部无 tests、pyc、`__pycache__`、build 或其他 Package 源码 |
| 未安装业务 Package | Inbox ACTIVE；messaging/task/team 明确 `entry_import_failed`，Host 仍可组合 |
| 洁净 venv 安装 | 版本 `ftre 0.3.0`、Inbox `0.2.0`、三业务包 `0.1.0`；四 Plugin ACTIVE，Tool owner 正确 |
| Gateway smoke | `GET http://127.0.0.1:48664/api/health` 返回 `200 {"status":"ok"}`，正常 stop |
| `git diff --check` | 通过 |

## 5. 工程卫生

- 已清理 F18 构建生成的 `build/`、`dist/`、`*.egg-info`、测试缓存和临时 wheel/venv。
- 已确认旧 `src/ftre/plugins/builtin/team` 目录、Inbox Tool 工厂和 Host Tool 旧路径均无残留。
- F18 只修改 `E:\\ftre`；Agent Core、cordis-py 和客户端未修改。
- 当前工作树仍包含此前 F16/F17 的未提交修改；本阶段未执行 commit、push、merge 或 release。
