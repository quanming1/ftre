# F19 执行报告：Session HTTP 路由 Plugin 化与 Inject 边界收敛

> 状态：已完成；F19.1–F19.4 已验收，未提交 Git。

## 1. 范围与 Owner 迁移

- 仓库：`E:\\ftre`；分支：`feature/F19-session-route-inject-boundary`。
- 未修改客户端、`E:\\ftre-agent-core`、`E:\\cordis-py` 和用户运行数据。
- 旧 Owner：`services/session/plugin.py` 同时创建 Session Service 和注册 HTTP Router，
  通过 `ctx.get("agents"/"inbox")` 迟查跨 Service 依赖。
- 新 Owner：`services/session/plugin.py` 只 provide `sessions`、`session_events`；
  `plugins/builtin/session_routes/plugin.py` 显式 inject `sessions`、`agents`、`inbox`、`http`，
  注册并清理原有 Session HTTP Router。

## 2. 完成内容

- `services/session/router.py` 删除 callable accessor 和 `current()` late lookup，改为接收
  稳定 Service 实例。
- Composition 新增 required `session-routes` Plugin，依赖就绪后注册路由。
- 新增架构门禁和生命周期测试：Session Provider 无 HTTP/动态跨 Service 查找；Route Plugin
  restart 不重复路由，unload 不关闭 Session/Agent/Inbox。
- 补充三个 F18 Tool Package 的最小行为回归，避免只验证文件/工厂存在。

## 3. 验证结果

| 检查 | 结果 |
|---|---|
| 全量测试 | `504 passed in 114.86s` |
| F19/HTTP/Package 专项 | 通过；F19 专项 6 项、Package 行为 10 项 |
| Ruff | `python -m ruff check src tests packages --no-cache` 通过 |
| Host wheel | `ftre-0.3.0`，184 文件，SHA256 `04369d6c51e78635232ef971c53afeb4b73645f098e1274131fae0756523b562`；无 tests/pyc/cache |
| Gateway smoke | `GET http://127.0.0.1:48665/api/health` 返回 `200 {"status":"ok"}`，正常 stop |
| `git diff --check` | 通过 |

## 4. 工程卫生与边界

- 最后一次测试/构建后清理 `__pycache__`、`.pyc`、`.pytest_cache`、build/dist/egg-info、
  临时 wheel 和空目录；最终复核数量均为 0。
- F15 仍保持 `in_progress`：其远程 CI（AC19）尚未在 GitHub Actions 触发，本报告不伪造
  CI 证据，也不把 F15 文档状态改成已验收。
- 当前工作树包含 F15/F16/F17/F18/F19 的累计未提交修改；本阶段未执行 commit、push、
  merge 或 release。
