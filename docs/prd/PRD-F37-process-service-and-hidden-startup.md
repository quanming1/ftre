# PRD-F37 Process Service 与 Electron 后端启动收口

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F37 |
| 名称 | Process Service 与 Electron 后端启动收口 |
| 状态 | 开发中 |
| 创建日期 | 2026-08-28 |
| 定稿日期 | 2026-08-28 |
| 验收日期 | — |
| 关联文档 | `docs/TODO.yaml` F37；`docs/prd/PRD-E1-mac-client-packaging.md`；`AGENTS.md` |
| 关联仓库 | `E:\\ftre`、`E:\\binn\\ftre-desktop` |

## 1. 背景与目标

### 1.1 背景

当前桌面端在 `packages/electron/src/backend.ts` 中直接启动 bundled Python Gateway。Electron
已经设置 `windowsHide: true`，但 Windows 启动入口仍然是控制台子系统的 `python.exe`。在部分
系统、Electron/Node 版本和安全软件环境下，进程创建瞬间仍可能出现黑色控制台窗口。

即使主 Gateway 改为无窗口启动，Gateway 在运行期间还会启动 Git、cmd、PowerShell、MCP
stdio server、Playwright、终端和其他工具进程。父进程的隐藏设置不会可靠地传递给嵌套的
控制台子进程；未设置 `CREATE_NO_WINDOW` 的任一调用点都可能再次闪窗。正式启动如果经过
`.bat`、`cmd /c` 或 PowerShell，还会增加嵌套 shell 和退出清理的不确定性。

### 1.2 目标

建立唯一的本地进程管理边界：Electron 负责 bundled Gateway 的启动和生命周期，`ftre-process`
负责 Python 后端及插件子进程的跨平台启动策略，最终实现 Windows 正式客户端从启动、消息
处理、Tool/MCP/Git/浏览器调用、重启到退出全链路无可见控制台，并保留完整日志和诊断能力。

### 1.3 非目标

- 不修改 Agent、LLM、Tool、Session、Inbox 或 WebSocket 业务协议。
- 不把 Gateway 改成远程服务，不引入系统级 Windows Service 或登录自启动任务。
- 不在实现过程中 kill、重启或改写正在运行的 Gateway；只能使用新的独立进程或测试夹具验收。
- 不通过全局 setter、Service Bag、兼容 Facade 或第二套进程 Owner 解决问题。
- 不把 `.bat`、PowerShell 或 `cmd.exe` 作为正式客户端启动中间层。

## 2. 三阶段范围

### 阶段一：Electron BackendSupervisor，客户端启动无黑窗

阶段一只改桌面端的正式 Gateway 启动链，不触碰后端业务代码。

- Windows 使用 runtime 目录内的 `pythonw.exe`；macOS 使用 bundled POSIX Python。
- Electron 使用 `windowsHide: true`，并继续使用 `stdio: ["ignore", "pipe", "pipe"]`，
  将 stdout/stderr 转发到 LoadingScreen、客户端日志和诊断面板。
- `BackendSupervisor` 成为唯一的启动、停止、重启和崩溃恢复 Owner；`main.ts` 只调用
  Supervisor 公共生命周期，不再自己拼接命令或管理 ChildProcess。
- 启动与 UI 创建解耦：窗口可以立即显示 LoadingScreen，Supervisor 在 Gateway 可访问前
  发布 `starting`；完成 WebSocket/health ready 探测后发布 `ready`；失败发布带路径、退出码
  和最近日志的结构化错误。
- 正式启动不调用 `start-gateway.bat`；该脚本只保留给开发者手工诊断。

阶段一结束时，客户端在没有打开终端的情况下启动、自动重启和退出都不出现黑窗，且日志不丢失。

### 阶段二：`ftre-process` Package，后端子进程无黑窗

阶段二新增可独立复用的 Package，并由 ftre 内置 Provider Plugin 装配。

- Package 只依赖 Python 标准库，不依赖 Agent、Tool、Session、Gateway 或具体 MCP 实现。
  - `ProcessService` 是唯一对外服务；Tool、Git、MCP、Playwright、环境探测和其他插件只能
  通过 Inject 消费它。一次性异步命令使用 `run`/`run_shell`，长驻异步服务使用 `spawn`，
  CLI 后台进程管理的同步边界使用 `run_sync`/`spawn_sync`，不得再复制平台策略。
- Windows 默认合并 `CREATE_NO_WINDOW`，不覆盖调用方已有的
  `CREATE_NEW_PROCESS_GROUP`、`DETACHED_PROCESS` 等标志；非 Windows 不改变原有行为。
- 默认 `shell=False`，参数使用 argv 数组传递；必须使用 shell 时由适配层显式声明并仍应用
  平台隐藏策略，禁止通过内联 `for /f`、嵌套 `cmd /c` 隐式创建新的控制台。
- 同时覆盖同步 `run`、异步 `open/spawn`、超时、取消、stdout/stderr 采集和进程树终止。
- 迁移所有 ftre 自有生产调用点并增加静态扫描，禁止新增裸 `subprocess.run/Popen` 或裸
  `asyncio.create_subprocess_*`。MCP 的 stdio 进程由第三方 `mcp`/AnyIO transport 创建，
  不伪造第二个 MCP ProcessService；ftre 只负责传递配置并验证该 transport 的
  `CREATE_NO_WINDOW`/Job Object 进程策略，第三方升级时由集成测试重新确认。

阶段二结束时，发送消息、执行命令、Git 探测、MCP stdio、Playwright 和环境探测均不产生
可见控制台，且结果、退出码、超时和日志行为不回归。

### 阶段三：生命周期收口、跨平台验收与发布门禁

阶段三处理启动链和子进程策略之外的最终一致性。

- Supervisor 状态固定为 `idle → starting → ready → stopping → stopped`，异常路径进入
  `failed`；重复 start/stop/restart 必须幂等。
- 停止优先使用 Gateway graceful shutdown；超过宽限期后 Windows 使用 `taskkill /T /F`，
  POSIX 使用进程组信号；所有路径最终清理子进程句柄、定时器和日志监听器。
- 重启使用 generation/token 防止旧 ChildProcess 的 `close` 事件清空新进程句柄；崩溃重试有
  次数上限、退避和可诊断错误。
- Supervisor 的 `start/stop/restart` 经过同一生命周期队列串行化；并发调用不会创建两个
  Gateway，也不会让停止过程与重启过程交叉覆盖状态。
- 打包产物记录启动器、runtime 架构、Gateway 版本和进程策略；Windows、macOS x64/arm64
  的构建链路保持一致的 ready、日志和退出语义。
- CI 增加静态扫描、单元测试、打包结构检查和 Windows 原生手动验收；不以 Linux CI 通过
  代替 Windows 无窗口证据。

## 3. 目标文件结构

### 3.1 ftre 后端

```text
E:\\ftre\\packages\\ftre-process\\
├─ pyproject.toml
├─ README.zh.md
├─ tests\\
│  └─ test_process_service.py
└─ src\\ftre_process\\
   ├─ __init__.py
   ├─ service.py          # ProcessService 唯一公开服务
   ├─ contracts.py        # ProcessSpec / ProcessResult / ProcessHandle
   ├─ policy.py           # Windows/POSIX 启动和隐藏策略
   └─ errors.py           # 启动、超时错误；取消沿用 asyncio.CancelledError

E:\\ftre\\src\\ftre\\plugins\\builtin\\process\\
├─ __init__.py
└─ plugin.py              # Provider Plugin：创建、provide、关闭 ProcessService

E:\\ftre\\tests\\
├─ architecture\\test_no_direct_subprocess.py
└─ integration\\test_f37_process_consumers.py
```

`src/ftre/services/process` 不保留第二份实现。Package 提供实现和契约，Provider Plugin 负责
在 Host Composition 中装配和清理；其他 Service/Plugin 只通过 Service key 注入。

### 3.2 Electron 客户端

```text
E:\\binn\\ftre-desktop\\packages\\electron\\src\\
├─ backend-supervisor.ts  # Gateway 生命周期唯一 Owner
├─ backend-runtime.ts     # runtime manifest、路径和启动器解析
├─ backend-readiness.ts    # health/WebSocket ready 探测
└─ main.ts                # 只装配 IPC、窗口和 Supervisor

E:\\binn\\ftre-desktop\\scripts\\
└─ start-gateway.bat       # 仅人工诊断，不进入正式启动链
```

原 `backend.ts` 中的 spawn、重启、退出和崩溃恢复逻辑迁移到 `BackendSupervisor` 后，
删除旧 Owner，不能保留一份并行实现或兼容壳。

## 4. 服务与边界

### 4.1 `ProcessService` 负责

- 将结构化 `ProcessSpec` 转成平台进程参数；
- 启动同步/异步外部进程并返回 `ProcessHandle`；
- 合并隐藏窗口、进程组、stdio、cwd、env 和 shell 策略；
- 统一收集 stdout/stderr、退出码、耗时、超时和取消结果；
- 按平台终止单进程或完整进程树；
- 提供可测试的 fake process backend，不连接真实 Gateway。

### 4.2 `ProcessService` 不负责

- Agent Turn、Tool 权限、MCP 协议、Git 业务、Session 或消息投影；
- 判断某个命令是否允许执行；权限判断仍由 Tool/Approval Service 负责；
- 维护全局进程注册表或替代 Gateway Supervisor；
- 向 Renderer 发送 UI 事件；日志和状态由调用方映射到各自协议。

### 4.3 `BackendSupervisor` 负责

- 只管理 Electron 自己启动的 bundled Gateway；
- 解析 runtime、构建环境、启动参数和日志管道；
- ready/health 探测、启动超时、重启和退出；
- 向 Renderer 暴露 `starting/ready/failed/stopping/stopped` 状态。

### 4.4 消费者边界

```text
ToolService / Git Plugin / MCP Plugin / Playwright Adapter / Environment Probe
                              │ inject
                              ▼
                    ftre-process.ProcessService
                              │
                              ▼
                  OS subprocess / async subprocess
```

Agent、LLM、Session、Channel 和 Renderer 不得直接依赖 `subprocess` 或 `BackendSupervisor`。
MCP stdio 是第三方 transport 的明确外部边界，不由 ftre 重复封装；其 Windows 无窗口和
子进程树回收能力必须通过依赖版本检查与集成验收确认。Gateway 只负责业务服务组合，
不负责启动自己所在的进程。

## 5. 关键流程

### 5.1 客户端启动

```text
app.whenReady()
  → 创建窗口 + LoadingScreen
  → BackendSupervisor.start()
  → resolve runtime manifest
  → Windows: pythonw.exe / POSIX: bundled Python
  → stdio pipe
  → Gateway health/WebSocket ready
  → state=ready
  → Renderer 建立正常业务连接
```

任何失败都必须停止当前 generation、保留最近日志并返回结构化诊断，不能静默回退到系统
Python、用户 PATH 或 `.bat`。

### 5.2 后端子进程

```text
业务 Plugin
  → ProcessSpec(argv, cwd, env, timeout, mode)
  → ProcessService
  → 平台 policy 合并 flags
  → spawn/run/open
  → ProcessResult 或 ProcessHandle
```

Windows 默认使用 `CREATE_NO_WINDOW`；需要后台脱离时追加明确的 detach flags。任何自定义
flags 都必须经过 OR 合并，不能覆盖隐藏策略。

### 5.3 停止与重启

```text
stop/restart
  → 标记 generation + state=stopping
  → 请求 Gateway graceful shutdown
  → 等待宽限期
  → 超时：Windows taskkill /T /F；POSIX 进程组信号
  → 等待 close，清理 listeners/timers/handles
  → restart 时创建新 generation
```

旧进程的迟到事件不得影响新进程；用户取消和应用退出不能留下 Python、MCP 或 shell 孤儿。

## 6. 接口契约

### 6.1 ProcessService

```python
@dataclass(frozen=True)
class ProcessSpec:
    argv: tuple[str, ...]
    cwd: Path | str | None = None
    env: Mapping[str, str] | None = None
    timeout: float | None = None
    mode: Literal["capture", "stream", "detached"] = "capture"
    encoding: str | Sequence[str] = "utf-8"
    creationflags: int = 0


class ProcessService:
    async def run(self, spec: ProcessSpec) -> ProcessResult: ...
    def run_sync(self, spec: ProcessSpec) -> ProcessResult: ...
    async def wait(self, handle: ProcessHandle, timeout: float | None = None) -> ProcessResult: ...
    def wait_sync(self, handle: SyncProcessHandle, timeout: float | None = None) -> int: ...
    async def run_shell(self, command: str, *, cwd: Path | None = None,
                        env: Mapping[str, str] | None = None,
                        timeout: float | None = None) -> ProcessResult: ...
    async def spawn(self, spec: ProcessSpec) -> ProcessHandle: ...
    def spawn_sync(self, spec: ProcessSpec) -> SyncProcessHandle: ...


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: int
```

`ProcessHandle` 提供 `pid`、`wait`、`communicate`、`terminate`、`kill` 以及流式 stdout/stderr；
同步 CLI 边界使用同样语义的 `SyncProcessHandle`（`wait/communicate` 为同步方法）；
公开接口不暴露底层 Registry 或平台实现对象。`run_shell` 只在调用方明确选择时拼装
平台 shell，默认的 `ProcessSpec` 始终使用 argv 且不经过 shell。
Windows 调用方若已有 `creationflags`，ProcessService 只做 OR 合并，不覆盖调用方标志。

### 6.2 BackendSupervisor

```typescript
type BackendState =
  | "idle" | "starting" | "ready" | "failed" | "stopping" | "stopped";

interface BackendStatus {
  state: BackendState;
  pid?: number;
  generation: number;
  exitCode?: number | null;
  error?: { code: string; message: string; recentLogs: string[] };
}

class BackendSupervisor {
  start(): Promise<BackendStatus>;
  stop(): Promise<void>;
  restart(): Promise<BackendStatus>;
  status(): BackendStatus;
}
```

Renderer 只消费状态和日志 IPC，不获得 executable path、spawn 参数或停止进程权限。

## 7. 测试与验收

### 7.1 阶段一验收

- [ ] Windows 安装包从桌面快捷方式双击启动，Process Explorer 中 Gateway 为 `pythonw.exe`，
  无 `conhost.exe` 可见窗口。
- [ ] LoadingScreen 能收到 Python stdout/stderr；启动失败能展示 runtime 路径、退出码和最近日志。
- [ ] 自动重启、手动重启和正常退出均不调用 bat/cmd/PowerShell 中间层。
- [ ] macOS x64/arm64 仍启动 bundled Python，Windows 行为不回归。

### 7.2 阶段二验收

- [x] `ProcessService` 单元测试验证 Windows flags OR 合并、POSIX no-op、pipe、超时和取消。
- [x] 架构扫描确认生产代码无新增裸 `subprocess` 调用；Tool、Git、MCP、Playwright、环境
  探测均经过 ProcessService；MCP stdio 仅允许第三方 transport 的明确适配边界。
- [ ] Windows 原生执行命令、Git、MCP stdio、Playwright 和环境探测，无黑窗且输出完整；
  MCP 依赖版本的 `CREATE_NO_WINDOW` 和 Job Object 行为有集成证据。
- [ ] 权限拒绝、非零退出码、超时和取消仍映射为原有业务错误，不吞错。

### 7.3 阶段三验收

- [ ] 连续 start/stop/restart、Gateway 启动失败、Gateway 崩溃和应用强制关闭均幂等且可诊断。
- [ ] 退出后不存在由本客户端创建的 Python、MCP、cmd、PowerShell、Git 或 Playwright 孤儿进程。
- [x] `pytest`、`ruff`、架构扫描、客户端 `tsc`、平台测试通过；Windows 安装包冒烟待真实
  Windows 机器验收。
- [ ] 干净 Windows 环境无需预装 Python/Node，安装后离线启动 Gateway；日志、Session 和用户配置位置不变。
- [ ] 三阶段完成后删除旧 backend spawn Owner、正式链路脚本依赖和重复进程策略，提交 PR 合入 develop。

## 8. 交付顺序

1. 先完成阶段一并在独立桌面 feature 分支验收；不修改正在运行的 Gateway。
2. 再完成阶段二，在 ftre 新增 `ftre-process` Package 和 Provider Plugin，逐个迁移消费者。
3. 最后完成阶段三的生命周期、跨平台和发布门禁；每阶段单独提交、测试和 PR。
4. 三阶段均通过后，更新 F37 PRD 为“已验收”、TODO 为 `done`，追加 CHANGELOG，再走发布流程。

## 9. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-28 | 创建 F37 三阶段 PRD：Electron 启动无黑窗、`ftre-process` 后端子进程策略、生命周期与发布验收 | Windows 正式客户端存在控制台闪现风险，需要同时治理主 Gateway 和其后代进程 |
| 2026-08-28 | 阶段二实现采用 `run`/`run_shell`/`spawn` 与 CLI 专用 `run_sync`/`spawn_sync`；Bash Tool 改为 async 并通过 `Inject("process")` 调用 | 现有 Bash Tool 是同步 callable，后台 Gateway CLI 也是同步入口，必须在不复制平台策略的前提下覆盖两类调用 |
| 2026-08-28 | 明确 MCP stdio 是第三方 `mcp`/AnyIO transport 边界；ftre 不创建重复传输实现，只验证其 Windows 无窗口与 Job Object 策略 | 上游 transport 已负责创建和回收 MCP 进程，重复封装会造成两个生命周期 Owner |
| 2026-08-28 | 阶段一、二和自动化阶段三实现完成：后端 pytest 729 passed，ftre ruff 通过；客户端 renderer 537 passed、Electron 平台测试 16 passed、TypeScript 编译通过 | 代码验收已完成；Windows 原生安装包和 Process Explorer 无黑窗证据需在目标系统补验 |
| 2026-08-28 | BackendSupervisor 增加统一生命周期队列，串行化并发 `start/stop/restart`；bundle 增量同步删除已移除的 Python Package 目录 | 补齐幂等性和旧 `ftre_agent_core` 产物残留风险 |
| 2026-08-28 | 使用隔离临时用户目录、独立端口和旧安装包依赖运行当前 ftre 源码的 `pythonw.exe` smoke，`/api/health` 返回 200，测试进程已回收 | 在不触碰现有 Gateway 的前提下验证 Windows 无控制台启动器和 ready 链路；完整安装包仍需依赖可用时再验收 |
| 2026-08-28 | 在临时 resources/home 和独立端口上执行 BackendSupervisor 生命周期 smoke：首次 start ready、重复 start 复用 generation、restart generation+1、stop 后无监听进程 | 验证 Supervisor 的 ready、幂等和 generation 语义，避免使用现有 Gateway 做破坏性测试 |
| 2026-08-28 | 将当前 `E:\\ftre` 源码复制到隔离 resources 后再次执行同一 Supervisor smoke；当前代码在 `pythonw.exe` 下 ready、restart、stop 全部通过，端口和子进程均已回收 | 排除仅验证旧业务 bundle 的间接证据 |
| 2026-08-28 | 使用官方 PyPI 完成 Windows x64 依赖 bundle（78.4 MB），导入 `cordis`、`ftre`、`ftre_agent`、`ftre_agent_runtime`、`ftre_process` 通过，manifest 指向 `pythonw.exe`；unpacked 包构建通过后清理 `release/backend` 生成物 | 证明发布链路可构建且不携带旧 Core；最终 GUI/Process Explorer 交互仍需在可启动 Electron 的原生验收环境执行 |
| 2026-08-28 | 修复 F37.2 接线遗漏：`process` Provider 通过 Agent Runtime 注入并进入每个 Turn 的 `runtime_context`，补充 Bash 实际消费回归测试 | Windows 包中 Bash 连续返回 `runtime_context.process 未注入`；Provider 已注册但 Runtime 上下文未转发，导致所有 shell 命令未执行 |
| 2026-08-28 | 修复 Bash 异步工具调用同步 `WorkspaceAccessor.get/set` 造成的事件循环死锁；新增 `aget/aset` 异步入口并补充超时回归测试 | 工具执行期间 `/api/health` 与会话路由会同时无响应，根因是事件循环线程同步等待自身提交的协程 |
