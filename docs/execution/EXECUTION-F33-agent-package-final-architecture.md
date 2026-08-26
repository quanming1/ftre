# EXECUTION-F33 Agent Package 终局架构

> 阶段：F33 · PRD：`docs/prd/PRD-F33-agent-package-final-architecture.md`
> 分支：`feature/F33-agent-package-final-architecture`（自 `develop` @ `195de23`）
> 安全说明：本文不含任何 API Key、凭据或运行时 session 内容。

## 1. 执行摘要

按 PRD F33 完成 Agent 终局分包：`AgentService`/`InboundMessage`/`AgentRunResult`/
Agent Hook/AgentConfig 抽取到新契约包 `packages/ftre-agent`；`AgentLoop`/`TurnExecutor`/
Core factory/CompletionRegistry 抽取到新 Provider 包 `packages/ftre-agent-runtime`，通过唯一
entry point `agent-runtime = ftre_agent_runtime.plugin:apply` 接入 Composition。Host 删除
旧 Agent Runtime Owner（`services/agent/{service,contracts,hooks,registry,plugin}.py` 与整个
`runtime/` 目录，含 AgentLoopDriver 适配层），TurnOutcome 一次性切换为 AgentRunResult，
无兼容 alias。全量 671 passed、ruff 全绿、diff check 通过、Gateway smoke 通过、wheel
洁净安装三场景验证通过。行为不变：Session/Inbox/Client wire、Core Hook 语义、Steering、
Compaction、Retry、Fallback、取消与生命周期协议全部保持。

F33.1 冻结的关键架构决策（写入实现，非临时方案）：

1. **能力参数化**：Runtime 源码零 import `ftre.services.*`；Host Service 以构造参数注入，
   Runtime 只按公开窄方法调用（AC21 由 AST 扫描 + wheel 元数据双重证明）。
2. **HookSpec 唯一来源**：类型族全部来自 `ftre_agent_core.hooks`；`ftre_agent.hooks` 对
   Core Hook（AGENT_BEFORE_REASONING_SPEC/AGENT_STOP_DECISION_SPEC）仅 re-export 同一
   对象，不复制定义。
3. **HookScopeCarrier 唯一定义迁至 `ftre_agent.registry`**：契约包不依赖 Host，而
   AgentRegistry.scope_carrier 必须构造该值类型；kernel/hooks/scope.py 只保留
   `context_for_scope` 机制函数按鸭子类型消费（key/identity/identities）。
4. **AgentConfig/LLMConfig 数据契约唯一在 `ftre_agent.config`**：Host `config.py` 收缩为
   磁盘加载 + 缓存，不再定义数据形状。
5. **TurnOutcome→AgentRunResult 一次性切换**：status 收敛为 completed/cancelled/failed
   （PUBLIC_RUN_STATUS 映射），无同义 alias。
6. **删除 AgentDriver/AgentLoopDriver Port**：AgentService 改为 `attach_runtime/
   detach_runtime` 直接绑定实现约定方法集（run_inbound/cancel_session/get_session_status/
   is_active_session/delete_session/resume_confirmation）的对象，幂等且拒绝第二 runtime。

## 2. 代码落点

| 变更 | 文件 |
|---|---|
| 契约包（新） | `packages/ftre-agent/src/ftre_agent/{__init__,contracts,service,registry,hooks,config,status}.py` |
| 契约包元数据 | `packages/ftre-agent/{pyproject.toml,README.md,README.zh.md}`（无业务 entry point） |
| Runtime 包（新） | `packages/ftre-agent-runtime/src/ftre_agent_runtime/{__init__,engine,turn_executor,factory,state,completion,plugin}.py` |
| Runtime 包元数据 | `packages/ftre-agent-runtime/{pyproject.toml,README.md,README.zh.md}`（entry point `agent-runtime`） |
| Host 窄方法 | `src/ftre/services/session/service.py`（build_user_content 等 4 个）、`messaging/bus/service.py`（publish_session_status）、`system_prompt/service.py`（assemble_agent_prompt，内含 Hook dispatch） |
| kernel 瘦身 | `src/ftre/kernel/hooks/{scope,__init__,runtime}.py`（删除 HookScopeCarrier 值类型，保留机制函数） |
| config 收缩 | `src/ftre/services/agent/config.py`（数据契约改 import ftre_agent，仅留磁盘加载） |
| Composition 切换 | `src/ftre/app/gateway/composition.py`（agents entry → `ftre_agent_runtime.plugin:apply`） |
| 旧 Owner 删除（git rm） | `src/ftre/services/agent/{service,contracts,hooks,registry,plugin}.py` + `runtime/` 整目录（engine/turn_executor/factory/provider/driver/completion） |
| 消费者切换 | `plugins/builtin/command/builtin.py`、`services/config/service.py`、`services/system_prompt/hooks.py`、`services/agent/profile/manager.py` + compaction/inbox/llm-recovery/messaging/task/team 包共 29 个文件 |
| 根 pyproject | `pyproject.toml`（dependencies/extras 加两包；pytest pythonpath 加两包 src） |

### 2.1 测试

| 类型 | 文件 |
|---|---|
| 架构门禁（新） | `tests/architecture/test_f33_agent_package_boundaries.py`（8 个：AC1 子进程独立导入、AC3 旧 Owner 退出、AC4 entry point、AC15 元数据、AC21 双向依赖禁止、AC22 DSH 对照） |
| 契约（新） | `tests/contracts/test_f33_agent_contracts.py`（7 个：runtime 绑定幂等/结果契约/CompletionRegistry×2/scope carrier/config 契约） |
| 生命周期（新） | `packages/ftre-agent-runtime/tests/test_f33_runtime_lifecycle.py`（4 个：真实 Composition 装载卸载、幂等关闭、洁净解释器导入、kernel hook 兼容） |
| 既有测试切换 | 架构门禁 11 个文件路径改指 packages（f31/f6/f9×2/f12/f13/f14/f15/f17/f8/f34）；行为测试重写或修复 12 个（f2 provider、f6 agent service、turn lifecycle/hitl、after_run、runtime plugin、f10 faults、f31 contracts、f7 pipeline、hook runtime 等） |
| 措辞修正 | `test_turn_lifecycle.py` docstring 旧类型名改中性表述；inbox/task 包注释旧结果类型名同步 |

## 3. 关键实现说明

- **Runtime Plugin 唯一装配点**：`apply(ctx)` inject 10 个 Service key（attachments/traces
  以 `ctx.get(strict=False)` 显式解析为可选项），创建 AgentService + AgentLoop 并绑定同一
  Fiber；`ctx.effect` 注册 close（stop loop + detach runtime），重复关闭幂等。已存在
  `agents` Service 时 no-op，防止双装载。
- **TurnExecutor 能力参数化落点**：`_build_messages` 改调 `sessions.build_user_content/
  to_openai_messages` 窄方法；`_assemble_prompt` 改调 `system_prompt.assemble_agent_prompt`
  （SYSTEM_PROMPT_ASSEMBLE_SPEC dispatch 移入该 Host 方法，Hook 语义不变）；`_core_hook_
  binding` 增加 `registry.ensure(agent_id)` 修复未登记 identity 的 KeyError。
- **InboundMessage metadata duck-typing**：Runtime 对 metadata 只按 `hasattr(model_dump)`
  判定 dataclass/dict 两种形态，不 import Host 的 InboundMetadata 类型。
- **engine 状态发布**：`_publish_session_status_async` 改调 `message_bus.publish_session_
  status()` 窄方法；`_persist_inbound_user_message` 改调 `sessions.record_to_msg()`。
- **消费者批量迁移**：29 个文件用一次性脚本 `migrate_imports.py` 完成（脚本为临时产物，
  验证后删除，不入库）；3 个含循环依赖风险的文件（config/service、command/builtin、
  system_prompt/hooks）手工处理（TYPE_CHECKING + 延迟导入）。
- **AC16 验证方式（沙箱适配）**：`pip --target` 的 TEMP 中转目录间歇性被沙箱拒绝写入；
  场景一（仅契约包）改用 wheel 解压安装验证（纯 Python wheel 解压即安装，与
  `--no-deps --target` 语义等价）；场景二（Runtime + 公开依赖）以 `--target` 完成；
  场景三（full Host）由 Gateway smoke 承担。三场景均断言 `ftre` 不进入 `sys.modules`
  且模块 `__file__` 位于安装位置。

## 4. 验证证据

```text
12.4-1 契约包导入：python -c "from ftre_agent import AgentService, InboundMessage" → agent contract ok
12.4-2 Runtime 导入：python -c "from ftre_agent_runtime import apply" → runtime package ok
12.4-3 专项门禁：pytest -q tests/architecture tests/contracts tests/lifecycle tests/startup → 251 passed（140.73s）
12.4-4 全量：pytest -q → 671 passed（189.57s）
        ruff check src tests packages → All checks passed!
        git diff --check / git diff --cached --check → exit 0
Gateway smoke：ftre gateway --port 48772 --background → GET /api/health → {"status":"ok"}；
        日志全链路装载无错误；ftre gateway stop → [OK] Gateway stopped.
AC16 场景一：仅 ftre_agent wheel 解压安装 → 导入 ok + 'ftre' not in sys.modules
AC16 场景二：ftre_agent + ftre_agent_runtime wheel --target 安装 → 导入 ok + 断言同上
AC16 场景三：full Host editable 组合 → Gateway smoke 通过
AC17 wheel 内容：zipfile 检查两 wheel 目录 → 无 Host 源码/tests/__pycache__（violations: []）
残留搜索：AgentLoopDriver/AgentDriver/TurnOutcome/core_bridge/旧 import 路径在
        src+packages+tests 生产代码零残留（剩余命中均为门禁负向断言与历史文档）
editable 安装：pip install -e packages/ftre-agent packages/ftre-agent-runtime（本机环境复装）
```

AC1–AC22 已逐条通过并勾选于 PRD；三联动收尾完成（PRD 已验收、TODO F33 done、
CHANGELOG 已追加）。push feature 分支与 PR 合入按 AGENTS.md 约定待用户明确指示后执行。

## 5. 遗留与移交

### 5.1 执行事故与恢复记录

收尾阶段的行尾规范化命令两次犯了同一个求值顺序错误（`open(f,'wb').write(
open(f,'rb').read()...)` 中写句柄先截断文件，读到空内容）：
第一次清空三个未提交文件（契约测试、架构门禁、本报告）；第二次在提交前规范化
Runtime 生命周期测试时再次触发，且空文件进入了 refactor 提交后由提交复验发现
（全量测试数 667 与预期 671 不符、collect-only 显示该目录 no tests collected）。恢复方式：
契约测试与生命周期测试从会话上下文全文重写；架构门禁从 `__pycache__` 的
pytest 编译产物（pyc）提取 code object 结构（docstring/常量/断言模板/字节码/行号）
逐函数重建；执行报告从本次会话完整重写。后续行尾规范化改为“先读入变量、转换、
写回、带非空断言”的分离步骤，并修复提交（soft reset 重做 refactor/docs 两批）。
恢复后专项验证 19+4 passed、全量复验 671 passed；教训已固化在验证流程中。

- `AgentService.registry`（AgentRegistry）公开属性自 F32 登记为独立债务，F33 将其随
  契约包迁移（Owner 转移为 ftre-agent 包），边界语义未变，债务本身未消解。
- Runtime 的 `get_session_status/is_active_session` 在 runtime 未绑定时返回安全空值
  （idle/False），无 runtime 的嵌入式 Host 需自行装配 Provider——Composition 默认必选，
  不装载时启动诊断会报 required 缺失（无旧 Lane 回退）。
- AGENTS.md 目录树为缩略示意（F34 已登记），`services/agent/` 仍真实存在（config +
  profile 归 Host），无需失实修正；packages 清单历史上未列入，维持惯例。
- pip 的 TEMP 中转目录在沙箱下间歇被拒（本机开发环境限制，非仓库问题）；洁净 CI 环境
  不受影响。site-packages 存在历史遗留的损坏发行版 `~tre`（pip WARNING），与本仓库无关。
- 用户环境无遗留 gateway 实例（smoke 前已确认 Running: no，验证实例已停止）。

## 6. 下一步

F33 后 Agent 分层终局已冻结（契约/Runtime/Host Service/Plugin/Core 唯一 Owner）。后续
候选：F6.12（cordis-py PyPI 发行物切换）、guard 门禁阶段（F34 遗留）或按 TODO 排期
推进；由用户决定。
