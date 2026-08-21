# FTRE Gateway

[English](README.md) | 中文

FTRE 是本地优先的 AI 编程助手。本仓库是它的有状态 Python Gateway：负责组合运行时
Service 与 Plugin，管理会话和 Agent 执行，并通过 HTTP/WebSocket 为桌面端提供接口。

无状态的 ReAct/LLM/Tool 算法位于独立的 `ftre-agent-core` 仓库；桌面端和文档站也都是
独立仓库，本仓库不修改它们。

## 一眼看懂架构

```text
CLI（ftre.main）
  └─ Gateway 启动器
      └─ Composition Root（app/gateway/composition.py）
          └─ cordis Context / Fiber
              ├─ Platform：插件发现、加载、生命周期诊断
              ├─ Services：有状态公共能力
              ├─ Features：产品行为 Plugin
              └─ HTTP Host + Channel + Agent 数据面
```

系统只有一个 Composition Root。它维护内置 Plugin 清单，应用配置，创建 Cordis Context，
注册启动路由，并拥有可逆的关闭流程。

### Service、Provider Plugin、Feature Plugin

| 概念 | 位置 | 责任 |
| --- | --- | --- |
| Service | `src/ftre/services/<name>/service.py` | 带状态的公共能力，拥有稳定 key，例如 `sessions`、`tools`、`http`、`message_bus` |
| Provider Plugin | Service 目录旁的 `plugin.py` | 声明 `inject`/`provide`，创建或绑定 Service，并登记清理 Effect |
| Feature Plugin | `src/ftre/features/<name>/` | Skill、MCP、Plan、Team、Schedule、上下文治理等可选产品行为 |
| Platform Runtime | `src/ftre/platform/plugin_runtime/` | Manifest 校验、显式发现、Cordis 加载、状态与失败诊断 |
| App Host | `src/ftre/app/` | 只负责进程边界：CLI、Gateway 启动、FastAPI 和 uvicorn |

`factory.py` 只是 Agent runtime 内部的对象组装辅助文件，不是 Service，也不是 Plugin
入口。Plugin 入口统一使用 `module:attribute`，通常指向 `apply(ctx, config)`。

## 项目文件树

```text
ftre/
├─ pyproject.toml                 # Python 包和 cordis-py 依赖
├─ config.example.json            # ~/.ftre/config.json 模板
├─ docs/                          # PRD、流程、TODO、执行记录
├─ src/
│  ├─ cordis/                     # 离线/开发环境使用的公共契约兼容实现
│  └─ ftre/
│     ├─ main.py                  # 薄 Typer 入口
│     ├─ app/
│     │  └─ gateway/
│     │     ├─ composition.py     # 唯一默认 Composition Root
│     │     ├─ bootstrap.py       # 启动和关闭编排
│     │     └─ http/               # FastAPI Host 与 uvicorn 适配
│     ├─ platform/
│     │  └─ plugin_runtime/       # Catalog → Discovery → Loader → Manager
│     ├─ services/                 # 公共有状态运行时能力
│     │  ├─ config/ filesystem/ http/
│     │  ├─ messaging/{bus,channel}/
│     │  ├─ session/ agent/ tools/ workspace/
│     │  ├─ command/ attachment/ observability/
│     │  └─ system_prompt/
│     ├─ features/                 # 可选产品 Plugin
│     │  ├─ skill/ mcp/ plan/ team/ schedule/
│     │  └─ context_govern/
│     └─ <旧目录>/                 # 迁移兼容，不再承载新生产代码
└─ tests/
   ├─ architecture/               # 导入边界和 Runtime 契约
   ├─ contracts/                   # Service 契约
   ├─ startup/ lifecycle/          # 组合与可逆清理
   └─ plugin/                      # 兼容层和内置行为
```

`agent`、`api`、`bus`、`channel`、`plugin`、`session` 等旧目录暂时保留，以兼容现有客户端
和已安装插件。新代码必须放在 `app`、`platform`、`services` 或 `features`，不得依赖
`ftre.plugin.kernel`。

## 启动与生命周期

1. `ftre.main` 解析 CLI 参数，委托给 `app.gateway.bootstrap`。
2. `build_composition()` 构造默认 Manifest 清单，并在公共 Cordis `Context` 上创建 `PluginManager`。
3. 必选 Service Plugin 按依赖激活；启用的 Feature 和外部 Plugin 随后激活。
4. Plugin 通过 `PluginContext` 贡献 Service、路由、Hook、工具或 Channel；所有副作用必须登记到
   `ctx.effect`。
5. 数据面绑定 Session/Agent/Bus/Channel provider，冻结 HTTP 注册表，并启动长驻 Gateway。
6. 关闭时按逆序释放 Fiber，再停止 AgentLoop、Channel、调度器和持久化资源；清理必须幂等。

外部模块在发现阶段不会被导入，只有用户在 `~/.ftre/config.json` 显式启用后才会解析入口：

```json
{
  "plugins": [
    {
      "id": "my-plugin",
      "entry": "my_plugin:apply",
      "enabled": true,
      "config": {}
    }
  ]
}
```

## Agent 数据面

```text
Channel → MessageBus → AgentLoop → SessionLane → ContextGate → TurnExecutor
                                      ├─ MailboxStore（只持久化 pending）
                                      ├─ CompactManager（不与 turn 重叠）
                                      └─ messages（持久化聊天历史）
```

不同 Session 可以并行；同一 Session 同时最多一个 active turn；turn 与 compaction 不并发；
pending 领取采用 at-most-once。SessionLane 和生命周期测试覆盖这些不变量。

## 内置能力

- **Services：**配置、文件系统策略/IO、HTTP 路由注册表、消息总线、Channel、Session、Agent/配置、
  工具、工作区、命令、附件、Trace 和系统提示词。
- **Features：**Skill 目录与加载、公共/私有 MCP、Plan 工具、Team 编排、Schedule 持久化、上下文治理 Hook。
- **兼容边界：**旧 `setup(ctx, config)` 插件和外部 Octo Channel 在 Runtime 边界适配；它们不定义新架构。

## 开发

```bash
python -m pip install -e .[dev]
python -m pytest -q
python -m ruff check src tests
ftre gateway
```

配置读取自 `~/.ftre/config.json`，可复制 `config.example.json` 开始。PRD 流程见
`docs/PROCESS.md`，提交规范见 `docs/COMMIT.md`。

## 关联仓库

- [ftre-agent-core](https://github.com/quanming1/ftre-agent-core) —— 无状态 Agent/LLM/Tool 核心
- [ftre-desktop](https://github.com/quanming1/ftre-desktop) —— Electron + React 客户端
- [ftre-docs](https://github.com/quanming1/ftre-docs) —— 文档站

## License

[MIT](LICENSE)
