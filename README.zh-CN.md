# FTRE Gateway

[English](README.md) | 中文

FTRE 是本地优先的 AI 编程助手。本仓库是它的有状态 Python Gateway：负责组合运行时
Service 与 Plugin，管理会话和 Agent 执行，并通过 HTTP/WebSocket 为桌面端提供接口。

无状态的 Agent 契约与 ReAct Runtime 位于本仓库的 `packages/ftre-agent` 和
`packages/ftre-agent-runtime`；原独立 `ftre-agent-core` 仓库已退休，不再是运行时依赖。

## 一眼看懂架构

```text
CLI（ftre.main）
  └─ Gateway 启动器
      └─ Composition Root（app/gateway/composition.py）
          └─ cordis Context / Fiber
              ├─ Kernel：Hook/Plugin 机制、发现、加载、生命周期诊断
              ├─ Services：有状态公共能力
              ├─ Plugins：产品行为与具体适配器
              └─ HTTP Host + Channel + Agent 数据面
```

系统只有一个 Composition Root。它维护内置 Plugin 清单，应用配置，创建 Cordis Context，
注册启动路由，并拥有可逆的关闭流程。

### Service、Provider Plugin、Builtin Plugin

| 概念 | 位置 | 责任 |
| --- | --- | --- |
| Service | `src/ftre/services/<name>/service.py` | 带状态的公共能力，拥有稳定 key，例如 `sessions`、`tools`、`http`、`message_bus` |
| Process Package | `packages/ftre-process/` | 跨平台外部进程策略，由 Host 以 `process` Service key 注入消费者 |
| Provider Plugin | Service 目录旁的 `plugin.py` | 声明 `inject`/`provide`，创建或绑定 Service，并登记清理 Effect |
| Builtin Plugin | `src/ftre/plugins/builtin/<name>/` | Skill、MCP、Plan、Team、Schedule、上下文治理等可选产品行为 |
| Kernel Runtime | `src/ftre/kernel/plugins/` | Manifest 校验、entry point 发现、Cordis 加载、状态与失败诊断 |
| App Host | `src/ftre/app/` | 只负责进程边界：CLI、Gateway 启动、FastAPI 和 uvicorn |

`factory.py` 只是 Agent runtime 内部的对象组装辅助文件，不是 Service，也不是 Plugin
入口。Plugin 入口统一使用 `module:attribute`，通常指向 `apply(ctx, config)`。

## 项目文件树

```text
ftre/
├─ pyproject.toml                 # Host、extras 和 cordis-py 依赖
├─ config.example.json            # ~/.ftre/config.json 模板
├─ docs/                          # PRD、流程、TODO、执行记录
├─ src/
│  └─ ftre/
│     ├─ main.py                  # 薄 Typer 入口
│     ├─ app/
│     │  └─ gateway/
│     │     ├─ composition.py     # 唯一默认 Composition Root
│     │     ├─ bootstrap.py       # 启动和关闭编排
│     │     └─ http/               # FastAPI Host 与 uvicorn 适配
│     ├─ kernel/                   # 轻内核：Context 外围机制和 Plugin Loader
│     ├─ services/                 # 公共稳定 Service 和 Service Provider
│     │  ├─ config/ filesystem/ http/
│     │  ├─ messaging/{bus,channel}/
│     │  ├─ session/ tools/ workspace/
│     │  ├─ agent_profile/ attachment/
│     │  └─ system_prompt/
│     └─ plugins/builtin/          # 可逆产品行为和 concrete Channel Plugin
│        ├─ command/ trace/ session_title/
│        ├─ channels/{websocket,subagent}/
│        └─ skill/ mcp/ plan/ team/ schedule/ context_govern/
├─ packages/                       # 独立发行边界
│  ├─ ftre-inbox/                  # 可选持久队列 Plugin
│  └─ ftre-compaction/             # 可选压缩 Plugin
└─ tests/
   ├─ architecture/               # 导入边界和 Runtime 契约
   ├─ contracts/                   # Service 契约
   ├─ startup/ lifecycle/          # 组合与可逆清理
   └─ plugins/                     # 架构和生命周期门禁
```

生产代码不再保留 `platform`、`features`、`agent_loop` 或旧兼容入口。Kernel 只提供机制，
Host Service 只提供稳定能力，Plugin 负责行为和生命周期；能独立安装、卸载和发布的完整能力
才进入 `packages/`。

## 启动与生命周期

1. `ftre.main` 解析 CLI 参数，委托给 `app.gateway.bootstrap`。
2. `build_composition()` 构造默认 Manifest 清单，并在公共 Cordis `Context` 上创建 `PluginManager`。
3. 必选 Service Plugin 按依赖激活；启用的 Builtin/Package/外部 Plugin 随后激活。
4. Plugin 通过 `PluginContext` 贡献 Service、路由、Hook、工具或 Channel；所有副作用必须登记到
   `ctx.effect`。
5. 数据面绑定 Session/Agent/Bus/Channel provider，冻结 HTTP 注册表，并启动长驻 Gateway。
6. 关闭时按逆序释放 Fiber，所有 Task、Hook、Route、Channel 和 Store 随 Owner 清理；操作必须幂等。

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
Channel → MessageBus → messaging/inbound Hook
                         ├─ Command Plugin（控制面，直接返回 CommandResult）
                         ├─ Inbox Package（可选：pending → claim）
                         └─ AgentService.run(RuntimeInput) → ftre-agent-runtime → Session/LLM/Tool Hook
```

不同 Session 可以并行；同一 Session 同时最多一个 active turn；turn 与 compaction 不并发；
Inbox 未安装时普通 AgentService 仍可直接执行；安装后 Queue 只负责 admission/claim，Agent Runtime
完全不知道 pending、QueueItem、Command 和压缩。

## 内置能力

- **Services：**配置、文件系统策略/IO、HTTP 路由注册表、消息总线、Channel、Session、Agent/配置、
  工具、工作区、命令、附件、Trace 和系统提示词。
- **Builtin Plugins：**Skill、MCP、Plan、Team、Schedule、Context Govern、Trace、Command、Channel。
- **Optional Packages：**`ftre-inbox` 提供持久队列，`ftre-compaction` 提供压缩 Service/Hook/Command。

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

- `packages/ftre-agent` + `packages/ftre-agent-runtime` —— 无状态 Agent 契约与 Runtime
- [ftre-desktop](https://github.com/quanming1/ftre-desktop) —— Electron + React 客户端
- [ftre-docs](https://github.com/quanming1/ftre-docs) —— 文档站

## License

[MIT](LICENSE)
