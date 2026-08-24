# F4 架构债务清理与单一 Owner 收敛执行报告

## 1. 执行结论

F4 已完成。F1/F2 迁移后遗留的旧数据面转发壳、Config/Trace/MCP 根目录实现、Feature
通配符模块、HTTP compat API、Gateway 进程错位和 Attachment 图片存储错位均已清理。
生产代码和测试统一使用 `app / platform / services / features` 及 `cordis` 公共路径。

本阶段只修改 `E:\ftre`，未修改 Desktop、`ftre-agent-core`、Octo 独立仓库或客户端。
由于本项目已明确不保留旧外部插件兼容，依赖已删除 `ftre.*` 路径的外部 Octo 测试入口
也一并移除；Octo 独立仓库本身未修改。

## 2. 迁移结果

### 2.1 旧数据面壳删除

删除以下旧 Python 包：

```text
ftre.agent
ftre.session
ftre.bus
ftre.channel
ftre.command
ftre.tools
```

所有测试改为从 `services/agent`、`services/session`、`services/messaging`、
`plugins/builtin/command`、`services/tools` 导入。`channel/test_channel.py` 不再放在生产包中。

### 2.2 Config 与 Trace

- `src/ftre/config.py` → `services/agent/config.py`；AgentConfig、LLMConfig、ContextConfig
  和 Agent 配置解析拥有明确 Owner。
- 原始 JSON 配置读取和 Gateway 地址读取归入 `services/config/loader.py`，路径归入
  `services/config/paths.py`。
- `src/ftre/trace_store.py` → `plugins/builtin/trace/store.py`；TraceService 不再
  反向依赖根模块。

### 2.3 MCP Feature

- `src/ftre/mcp/{config,manager,adapter}.py` 迁入 `plugins/builtin/mcp/{config,connection,adapter}.py`。
- MCP Plugin 负责创建连接管理器、注册工具和停止 watcher/连接。
- 删除根 `ftre.mcp` 包和 Feature 层通配符转发。

### 2.4 HTTP 与 Feature 空壳

- 删除 `ApiDependencies`、`register_compat_snapshot`、`register_compat_path` 和
  `kind="compat"` 路由记录。
- WebSocket `/` 使用正式 `register_websocket_path` 注册。
- 删除 Plan/Schedule/Team Feature 下无实际职责的通配符模块。
- 删除 ConfigService 未使用的 `replace_sync` 兼容入口。

### 2.5 App 与 Attachment 边界

- `GatewayRuntime`/`GatewayStatus` 归入 `app/gateway/process.py`。
- 删除 `ftre.gateway`、`app/cli` 的兼容转发文件。
- 图片存储实现归入 `services/attachment/store.py`，删除 `ftre.utils.image_store` 和
  `ftre.utils` 旧包；Session、Tool、WS、MCP 均从 Attachment Owner 使用。

## 3. 分阶段提交

| 切片 | 提交 | 内容 |
|---|---|---|
| F4.2 | `0b765d0` | 删除 Agent 与数据面旧路径壳，迁移测试导入 |
| F4.3 | `2a224b3`、`5015e41` | Config/Trace Owner 迁移及调用方收敛 |
| F4.4 | `6dc9836` | MCP 实现迁入 Feature Owner |
| F4.5 | `11285f2` | HTTP compat API 与 Feature 空壳清理 |
| F4.6 | `247c576` | Gateway Process 与 Attachment Store 归位 |
| F4.6 收尾 | `1d37ac2` | 删除依赖旧外部插件路径的测试入口 |
| F4.7 | 收尾提交 | PRD/TODO/CHANGELOG/验收报告同步 |

## 4. 自动化验证

```text
python -m pytest -q
313 passed

python -m ruff check src tests
All checks passed!

git diff --check
通过
```

新增/更新架构门禁：

- `tests/architecture/test_f4_no_legacy_packages.py`
- 禁止旧数据面导入、根 Config/Trace/MCP/Gateway/Utils Owner、通配符 re-export、
  `sys.modules[__name__]` 替换和 HTTP compat API。

## 5. 手动验证

通过 `start_gateway(config={})` 创建 Composition，并用 FastAPI `TestClient` 验证：

- `GET /api/health` 返回 200；
- WebSocket `/` attach 后收到 `reply_snapshot`；
- Gateway Process Owner 为 `ftre.app.gateway.process`；
- Composition 可正常关闭。

## 6. 已知边界

- F4 不改变 Desktop 协议字段、Session JSON、Trace SQLite、附件路径和 Gateway 状态文件格式。
- F4 不再支持旧 Python import 路径，也不为旧外部 Plugin/Octo 代码提供兼容层。
- `__pycache__` 等本地生成缓存不属于 Git 源码树，不作为架构模块保留。

## 7. 收尾状态

- 分支：`feature/F4-architecture-debt-cleanup`
- PRD：`docs/prd/PRD-F4-architecture-debt-cleanup.md`（已验收）
- TODO：阶段 F4（`done`）
- CHANGELOG：已追加 `[未发布]` F4 条目
- 最终工作区：干净
