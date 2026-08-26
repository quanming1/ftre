# PRD-F34-ToolService Final Runtime

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F34 |
| 名称 | ToolService 最终运行时边界 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-26 |
| 定稿日期 | 2026-08-26 |
| 验收日期 | 2026-08-26 |
| 关联文档 | docs/TODO.yaml 阶段 F34；AGENTS.md；docs/execution/HANDOFF-F30-F32-agent-architecture.md |

## 1. 背景与目标

### 背景

F32 验收后 Agent Runtime 已只消费公开 Service，但 Tool 领域仍有六项结构性债务（立项前代码调研结论）：

1. **内置工具绕过贡献通道**：`ToolService.prepare_view()`（service.py:127-141）每次 Turn 硬编码构造
   bash/read/write/edit/setWorkspace 五个工具，不产生 `ToolContribution` 记录——
   `snapshot()`/`schemas()` 看不到内置工具、无 owner 元数据、无 Plugin 生命周期；
   与 `builtin/__init__.py::build_default_tools()` 逻辑重复（后者生产零调用，仅测试引用）。
2. **`registry` 公开属性**（service.py:25）：生产代码已零外部消费者（F32 清理后），
   仅 2 处测试直接访问，可安全私有化。
3. **`execute()` 绕过作用域**：直接打全局 registry，不做 agent 可见性校验；
   `schemas()` 与 `execute()` 生产零调用，语义未冻结。
4. **view preparer 签名绑死 MCP 语义**（service.py:123）：第三参数名 `mcp_config`，
   任何新 preparer 都被迫接收 MCP 业务参数。
5. **Tool Hook 生产侧零消费者**：`tool/before`、`tool/after` 由 Core 发布
   （tool_handler.py:119/188），Host 无任何 Plugin 监听；`ToolDeny`/`ToolAllow` 契约
   备而未用，`PermissionContext` 默认 ALLOW 空转。
6. **两套过滤语义不一致**：MCP `restrict(deny)` 与 profile `filter_tools(allow/deny)`
   分别作用于不同集合（前者保留内置工具、后者连内置工具一起过滤），语义未冻结。

### 目标

ToolService 成为 tools key 的唯一真实 Owner：内置工具经 Plugin 贡献、registry 隐藏、
公开方法语义冻结、`tool/after` 拥有真实消费者，Agent Runtime 不接触具体 Registry。

### 非目标

- **不做 guard 门禁**：`tool/before` + `ToolDeny` 的策略性拒绝另立后续阶段（本阶段
  只落地 `tool/after` 审计消费者）。
- 不新增 `execution_mode`、工厂型贡献、ToolService Port/Facade/DTO。
- 不修改客户端、Inbox wire、Session wire、Cordis Kernel、E:\ftre-agent-core、cordis-py。
- 不改变 LLM 请求中工具 schema 的 wire 形状（`to_openai_dict()` 原样）。

## 2. 需求范围

### 2.1 功能需求

- [x] FR1 core-tools Plugin：新建 `src/ftre/plugins/builtin/core_tools/`，把
  bash/read/write/edit/set_workspace 五个工具实现文件从 `services/tools/builtin/`
  迁入，Plugin `apply()` 通过 `ToolService.register(..., owner="core-tools",
  source="builtin")` 贡献，`ctx.effect` 可逆清理；与 skill/schedule/plan Plugin 同构。
- [x] FR2 read 描述中性化（已拍板方案 A）：删除 `create_read_tool(vision=...)`
  参数，description 改为中性文案（"图片支持取决于当前模型能力"）；运行时行为
  仍由注入的 `llm_config.vision` 判断（read.py:186 已有，不动）。
- [x] FR3 registry 私有化：`ToolService.registry` → `ToolService._registry`；
  迁移 2 处测试消费者（tests/plugins/schedule/test_plugin.py:45 改 snapshot 断言、
  tests/contracts/test_f31_service_contracts.py:115 改 view 非空断言）。
- [x] FR4 ToolService 公开面冻结：
  - 保留 `register/restrict/register_view_preparer/snapshot/schemas/prepare_view/execute`；
  - 新增 `get(name, agent_id=None)`——作用域感知的单工具查询（测试/诊断用）；
  - `execute()` 增加可选 `agent_id` 参数：传入时执行该 Agent 作用域投影解析出的
    工具（scoped shadow 覆盖同名 global；仅存在于 agent scope 的工具也可执行），
    不可见抛 `KeyError`；不传保持全局 registry 执行。`get/schemas/execute`
    共享同一投影（与 TODO 验收语义一致）。
- [x] FR5 view preparer 签名泛化：preparer 调用统一为
  `(agent_id, session_id, profile_config, llm_config)`；MCP Plugin 适配新签名
  （内部自行读取 `mcp_config` 字段）。
- [x] FR6 tool/after 审计日志消费者（已拍板最小方案）：新建
  `src/ftre/plugins/builtin/tool_audit/`，`inject=("hook_runtime",)`，
  注册 `TOOL_AFTER_SPEC` 监听（`all_agent_scopes=True`），每次工具调用输出
  一行结构化日志：`session_id/agent_id/turn_id/call_id/name/status/error`；
  不新建存储表、不新建 API；监听随 Fiber 可逆。
- [x] FR7 过滤语义统一并冻结：内置工具成为普通贡献后，`restrict(deny)` 与
  `filter_tools(allow/deny)` 作用于同一集合；冻结语义"profile allow/deny
  不豁免内置工具"（保持现状 `filter_tools` 行为）写入契约测试。
- [x] FR8 Runtime 边界收口：`prepare_view()` 删除内置工具硬编码注册；
  架构测试断言 `services/agent/runtime/` 不构造 `ToolRegistry`、不访问
  `registry`/`_registry` 属性、不 import `services.tools.builtin`。
- [x] FR9 死代码清理：删除 `build_default_tools()`（生产零调用）及
  `tests/test_plugin_tools.py` 对应测试；迁移后 `services/tools/builtin/`
  目录清空删除。

### 2.2 非功能需求

- 性能：prepare_view 每次注册 5 个贡献的工具实例为工厂产物创建，开销与现状
  硬编码路径等价，不引入每 Turn 额外查询。
- 兼容性：LLM 请求中的工具 schema 集合与顺序保持现状（内置工具仍在
  Plugin 工具之前注册，视图构建顺序不变）。
- 生命周期：core-tools / tool_audit 卸载后行为完整消失；进行中 Turn 持有的
  view 是独立实例不受卸载影响；无 core-tools 时 Gateway 可启动（Agent 无内置
  工具，由 Composition required 语义决定是否阻断——默认清单中 core-tools 必选）。

## 3. 技术方案

### 3.1 目录结构

```text
src/ftre/plugins/builtin/core_tools/
├─ __init__.py          # 导出 create_* 工厂
├─ plugin.py            # apply(): register 5 工具 + ctx.effect
├─ bash.py / read.py / write.py / edit.py / set_workspace.py   # 自 services/tools/builtin/ 迁入
└─ _io.py / _diff.py / _truncate.py                           # 私有辅助随之迁入

src/ftre/plugins/builtin/tool_audit/
├─ __init__.py
└─ plugin.py            # apply(): hook_runtime.register(TOOL_AFTER_SPEC, ...) + effect

src/ftre/services/tools/
├─ service.py           # registry 私有化、get()/execute(agent_id)、preparer 签名泛化、删除硬编码
├─ types.py / scope.py / hooks.py / plugin.py   # 不变
└─ builtin/             # 删除（文件迁出 + build_default_tools 删除）
```

### 3.2 关键决策记录

| 决策 | 选择 | 备选与否决理由 |
|---|---|---|
| read 的 vision 描述 | 方案 A：描述中性化，运行时按 `llm_config.vision` 报错 | 否决 preparer 动态重建（vision 变化需换贡献记录，preparer 语义变重）；否决工厂型贡献（ToolService 引入延迟工具概念） |
| tool/after 消费者 | 最小审计日志 Plugin | 调研证实 Core Tracer 已记录每次 tool 调用（RunType.TOOL span + session_id 在根 metadata），trace 审计落库会重复建设；guard 门禁另立后续阶段 |
| 内置工具目录 | 随 core-tools Plugin 迁入 plugins/builtin/ | 参照 F18 业务包模式（工具实现随 Owner Plugin）；保留在 services/ 会成为永久两栖层 |

### 3.3 数据流（迁移后）

```text
core-tools Plugin ──register(owner=core-tools)──┐
skill/schedule/plan Plugin ──register───────────┤
ftre-{messaging,task,team} ──register───────────┼─→ ToolService._items
MCP Plugin ──view_preparer + agent scope ───────┘

TurnExecutor._create_agent()
  → ToolService.prepare_view(agent_id, session_id, profile, llm_config)
      ① view preparers（签名: agent_id/session_id/profile_config/llm_config）
      ② 遍历 _visible(agent_id)（内置工具现在是普通贡献）
      ③ filter_tools 按 profile allow/deny 过滤（不豁免内置工具）
  → factory.create_core_agent(tool_view) → Core ReActAgent

Core tool_handler 每次调用
  → tool/after dispatch → tool_audit Plugin → 结构化日志一行
  （Tracer TOOL span 照旧，两者不重复：Tracer=可 purge 诊断库；audit=append-only 运维日志）
```

## 4. 接口定义

### 4.1 ToolService 公开面（冻结）

| 方法 | 签名 | 语义 |
|---|---|---|
| register | `(tool, owner, scope="global", source="builtin") → dispose` | 贡献工具，返回幂等清理回调 |
| restrict | `(agent_id, owner, allow=None, deny=None) → dispose` | 单 Agent 可逆限制 |
| register_view_preparer | `(preparer, *, owner) → dispose` | view 创建前准备器，签名 `(agent_id, session_id, profile_config, llm_config)` |
| snapshot | `(agent_id=None) → tuple[ToolContribution, ...]` | 可见贡献（含内置工具，带 owner/source/scope） |
| schemas | `(agent_id=None) → list[dict]` | OpenAI schema + ownership 元数据 |
| get | `(name, agent_id=None) → ToolContribution | None` | 作用域感知单工具查询（新增） |
| prepare_view | `(agent_id, session_id, profile_config=None, *, llm_config=None) → ToolRegistry` | 隔离 Core 视图（删除硬编码） |
| execute | `(name, execution_context=None, arguments=None, *, agent_id=None)` | agent_id 传入时执行该 Agent 作用域投影（scoped shadow 覆盖 global；不可见抛 KeyError）；不传为全局 registry 执行 |

### 4.2 审计日志格式

```text
logger="ftre.tool_audit" INFO
tool_call: session_id=... agent_id=... turn_id=... call_id=... name=... status=completed|failed|cancelled error=...
```

## 5. 验收标准

- [x] AC1：`python -m pytest -q tests/architecture tests/contracts tests/startup tests/lifecycle` 全部通过（232+1 修复后全绿）。
- [x] AC2：`python -m pytest -q` 全量通过（649 passed，基线 631 递增含 F34 新增 19 个测试）。
- [x] AC3：`python -m ruff check src tests packages --no-cache` 通过；`git diff --check` 通过。
- [x] AC4：`tests/` 与 `src/` 无任何对 `ToolService.registry`（公有名）的引用；
  `services/tools/builtin/` 目录不存在。
- [x] AC5：契约测试断言 `snapshot()` 含五个内置工具且 `owner == "core-tools"`；
  `get("bash")` 命中、`get("不存在")` 返回 None；`execute(agent_id)` 执行作用域投影
  （scoped shadow 覆盖 global、scoped-only 可执行）；global+scoped 同名时卸载 global
  后全局 execute 不可再执行旧工具（无残留）。
- [x] AC6：生命周期测试断言 core-tools 卸载后 `snapshot()` 不含内置工具、新 Turn
  视图无内置工具；tool_audit 卸载后 Hook 监听消失（`hook_runtime` 诊断快照为空）。
- [x] AC7：集成/契约测试断言工具调用后产生一行 tool_audit 结构化日志（含
  session_id/agent_id/name/status），Tracer TOOL span 照旧落库。
- [x] AC8：Gateway smoke：`ftre gateway --port 48771 --background` 启动，
  `GET /api/health` 返回 200 {"status":"ok"}（core-tools 为必选 Plugin，启动成功即装载成功）。
- [x] AC9：PRD/TODO/CHANGELOG 三联动完成（PRD 已验收、TODO done、CHANGELOG 已追加）。
  push/PR 合入按 AGENTS.md 约定待用户明确指示后执行。

## 6. 测试计划

- 契约测试：ToolService 公开方法签名与语义（get/execute 可见性校验/snapshot 含内置工具）。
- 架构测试：runtime/ 无 ToolRegistry 构造与 builtin import；builtin/ 目录删除；
  core-tools/tool_audit 的 manifest 与 owner 断言。
- 生命周期测试：Plugin 卸载可逆、进行中 Turn 不受影响、Hook 监听随 Fiber 消失。
- 行为测试：read 工具中性描述 + `llm_config.vision=False` 时读图报错（既有行为回归）；
  filter_tools 对内置工具的 allow/deny 不豁免语义。
- 手动验证：Gateway smoke + 一次完整对话含工具调用的日志观察。

## 7. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-26 | 初始定稿（草稿） | 基于立项前代码调研与两项用户拍板决策（read 方案 A、tool/after 最小审计日志） |
| 2026-08-26 | 定稿并转入开发中：用户确认按草稿执行；plan 工具实现随 Owner 迁至 plugins/builtin/plan/（FR1 补充，目录删除前置条件） | 用户确认开始执行；FR9 删除 builtin/ 目录要求 plan.py 一并迁入其 Owner Plugin |
| 2026-08-26 | 验收定稿：AC1-AC9 全部通过（全量 649 passed、专项 233、ruff、diff check、Gateway smoke 48771 health ok） | PROCESS.md 六步闭环验证→收尾；push/PR 待用户指示 |
| 2026-08-26 | 审查修正（round 2）：① execute(agent_id) 从仅校验可见性改为执行作用域投影（scoped shadow 覆盖 global、支持 scoped-only 工具）；② global 贡献卸载在同名 scoped shadow 存在时不再跳过 _registry 注销（消除可执行残留）；③ FR1-FR9 勾选补齐；④ TODO F34.3 标题按 PRD 非目标修正；⑤ 暂存内容 CRLF 规范化为 LF 使 git diff --cached --check 真实通过。受影响 AC5 已重跑通过 | 用户审查发现：TODO 验收"get/schemas/execute 共享作用域投影"与 FR4 原文仅校验不一致；卸载残留违反生命周期语义；PRD 勾选/TODO 标题/--cached 检查三处文档失实 |
