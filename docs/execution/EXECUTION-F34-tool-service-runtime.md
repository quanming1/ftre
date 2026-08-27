# EXECUTION-F34 ToolService 最终运行时边界

> 阶段：F34 · PRD：`docs/prd/PRD-F34-tool-service-runtime.md`
> 分支：`feature/F34-tool-service-runtime`（自 `develop` @ `07545ad`）
> 安全说明：本文不含任何 API Key、凭据或运行时 session 内容。

## 1. 执行摘要

按 PRD F34 完成 ToolService 最终运行时边界：内置五工具由新建 core-tools Plugin
作为普通贡献注册（owner=core-tools），registry 私有化，`get()/execute(agent_id)`
作用域语义冻结，view preparer 契约泛化，`tool/after` 落地最小审计日志消费者
（tool-audit Plugin），`services/tools/builtin/` 目录删除，plan 工具实现随 Owner
Plugin 迁移。全量 649 passed、ruff 全绿、Gateway smoke 通过。

两项用户拍板决策：read 描述中性化（方案 A）；`tool/after` 只做最小审计日志
（guard 门禁另立后续阶段）。

## 2. 代码落点

| 变更 | 文件 |
|---|---|
| core-tools Plugin（新） | `src/ftre/plugins/builtin/core_tools/{__init__,plugin}.py` |
| 五工具 + 辅助模块迁移 | `core_tools/{bash,read,write,edit,set_workspace,_io,_diff,_truncate}.py`（自 `services/tools/builtin/` git mv） |
| plan 工具随 Owner 迁移 | `src/ftre/plugins/builtin/plan/plan.py`（plugin.py 改相对导入） |
| tool-audit Plugin（新） | `src/ftre/plugins/builtin/tool_audit/{__init__,plugin}.py` |
| 过滤逻辑归位 Service Owner | `src/ftre/services/tools/filtering.py`（filter_tools + coerce_tool_name_list） |
| ToolService 收紧 | `src/ftre/services/tools/service.py`（_registry 私有化、get()、execute(agent_id)、preparer 四参签名、删除硬编码） |
| MCP preparer 适配 | `src/ftre/plugins/builtin/mcp/plugin.py`（自行读取 mcp_config） |
| Composition 清单 | `src/ftre/app/gateway/composition.py`（core-tools 必选、tool-audit 可选） |
| 漏网 import 修复 | `src/ftre/services/agent/profile/{manager,sub_agent}.py`（coerce_tool_name_list 改新路径） |

### 2.1 测试

| 类型 | 文件 |
|---|---|
| 契约（新） | `tests/contracts/test_f34_tool_service_contracts.py`（9 个） |
| 生命周期（新） | `tests/lifecycle/test_f34_core_tools_lifecycle.py`（4 个） |
| 架构门禁（新） | `tests/architecture/test_f34_tool_boundaries.py`（6 个） |
| 既有测试迁移 | `test_plugin_tools.py`（删 build_default_tools 测试、改 import）、`test_agent_manager.py`、`test_f31_service_contracts.py`、`plugins/schedule/test_plugin.py`（registry→get）、`test_f13_plugin_first.py`/`test_f9_service_injection.py`/`test_f17_inbox_tool_owner.py`（路径更新） |

## 3. 关键实现说明

- **注册顺序不变**：core-tools 在 Composition 中紧随 tools Service 装载，其贡献先于
  skill/plan/schedule/messaging 等注册；`_visible` 按注册序合并，LLM 请求中的工具
  schema 顺序与迁移前一致（内置在前）。
- **过滤语义冻结**：内置工具成为普通贡献后，profile allow/deny 与 MCP restrict 作用于
  同一集合；契约测试固化"allow/deny 不豁免内置工具"（迁移前 filter_tools 已是此行为，
  无行为变化）。
- **read 中性描述**：`create_read_tool()` 删除 vision 参数；description 声明"需当前模型
  支持识图，不支持时返回明确报错"，运行时拦截仍是 read 函数体内的 `llm_config.vision`
  检查（未改动）。
- **tool-audit**：`all_agent_scopes=True` 全局监听 `tool/after`，waterfall 中先
  `await next_()` 再记录最终结果（日志反映真正返回给 Core 的快照）；HookRuntime 注册
  已绑定 Plugin Fiber，无第二个 Effect。
- **Gateway smoke 假阳性排除**：首次 health 检查命中了用户环境遗留的旧实例
  （site-packages 安装版，监听 48650）；新实例因端口冲突 bind 失败。改用独立端口
  48770 重新验证：启动成功、health 200、日志无错误；验证后已停止（用户旧实例未动）。

## 4. 验证证据

```text
专项（architecture/contracts/startup/lifecycle）：233 passed（含 F34 新增 19 个；round 2 后 236+）
全量：649 passed（round 2 后复验见 §7）
Ruff：All checks passed!（--no-cache）
git diff --check（未暂存区）：通过（exit 0）
Gateway smoke：--port 48771 --background → GET /api/health → {"status":"ok"}（复验通过，日志无新增错误）
```

> 更正（round 2）：本节原文声称 "git diff --check 通过"，但当时只验证了未暂存区；
> `git diff --cached --check` 实际失败（Write 工具产出的新文件为 CRLF 行尾，仓库约定 LF）。
> 已在 §7 规范化并复验通过。

AC1–AC9 已逐条通过并勾选于 PRD；三联动收尾完成（PRD 已验收、TODO done、CHANGELOG 已追加）。
push feature 分支与 PR 合入按 AGENTS.md 约定待用户明确指示后执行。

## 5. 遗留与移交

- guard 门禁（`tool/before` + `ToolDeny` 策略拒绝）按决策另立后续阶段；`ToolAllow/
  ToolDeny` 契约仍备而未用，`PermissionContext` 默认 ALLOW 空转现状不变。
- `AgentService.registry`（AgentRegistry）公开属性是 F32 登记的独立债务，不在 F34
  范围；F34 架构测试刻意只约束 Tool 领域。
- `ToolService.execute()/schemas()` 生产消费者仍为零（语义已冻结，等真实需求——
  例如后续 HTTP 诊断端点——再接入）。
- 用户环境遗留旧 gateway 实例（PID 43116，site-packages 版，监听 48650）不在本仓库
  管辖，未处理；其 state 记录已被本次验证实例覆盖，如需恢复请用户自行重启。

## 6. 重构收尾审计（refactor-cleanup-audit）

按 cleanup-checklist 逐项核对后的追加修正与结论：

- **引用盘点**：`ftre.services.tools.builtin` / `build_default_tools` /公有 `tools.registry`
  / `create_read_tool(vision` 在生产代码（src、packages）零残留；剩余命中均为架构测试
  负向断言门禁与历史文档记录（F5/F17/F32 等阶段存档），合法保留。
- **调用方迁移**：`create_*_tool` 工厂的生产调用方唯一为 core-tools Plugin；测试调用方
  为行为/契约测试与门禁断言字符串。
- **目录与空壳**：`services/tools/builtin/` 已删除；零空目录；无兼容壳、别名或转发层
  （tool_audit/__init__.py 的 apply 转发在审计中识别为异构模式并已改为纯包说明，与其他
  builtin Plugin 公共导出惯例一致）。
- **注释同步修正**：`plugins/builtin/__init__.py` 的 Owner 清单补充 core_tools/tool_audit，
  并移除已退出的 team 目录残留描述。
- **生命周期/入口/数据边界**：唯一 Composition Root；bootstrap 不手工构造 ToolService
  （F13 门禁）；每个贡献/监听均绑 Fiber effect 可逆；无同名资源双注册；工具数据访问
  仍经 WorkspaceAccessor/AttachmentService 公开边界。
- **生成物清理**：全量验证后清理 `__pycache__`×68、`.ruff_cache`；`.pytest_cache` 因
  系统占用访问被拒保留（gitignored，不进入提交，不影响工作区干净判定）。
- **最终门禁（审计后复跑）**：全量 649 passed、ruff All checks passed、
  `git diff --check` exit 0、工作区无非暂存/未跟踪文件、37 文件暂存（+1614/−157）。

已知边界（非阻塞）：AGENTS.md 目录树为缩略示意（历史上 session_routes 也未列入），
未随 F34 更新；F33 TODO 条目 `prd_status: 草稿` 为规划占位，PRD 文件待 F33 立项时创建。

## 7. 审查修正（round 2，用户代码审查后）

用户审查发现两项语义缺陷与三处文档失实，均已修正：

1. **execute(agent_id) 改为执行作用域投影**（service.py）：原实现仅用 `get()` 校验
   可见性，但始终执行 `_registry` 中的全局工具——scoped shadow 能被 `get/snapshot`
   解析，实际执行的却是 global 版本，与 TODO 验收 "get/schemas/execute 共享作用域投影"
   不一致。修正后：`agent_id` 路径执行投影解析出的工具（global 走 `_registry`，
   scoped 经一次性单工具 ToolRegistry 执行，注入解析与执行契约与全局路径一致），
   scoped-only 工具也可执行。
2. **global 卸载残留修复**（service.py dispose）：原实现在同名 scoped shadow 存在时
   跳过 `_registry.unregister()`，导致 Plugin 卸载后 `snapshot()` 为空但
   `execute("x")` 仍能执行旧 global 工具。修正为仅当同名 **global** 贡献仍存在时
   才跳过注销（scoped 不持有 `_registry` 条目）。
3. **新增 3 条契约回归测试**：scoped shadow 执行、scoped-only 执行、
   global+scoped 同名卸载无残留（此前测试未覆盖该组合）。
4. **文档失实修正**：PRD FR1–FR9 勾选补齐（此前只勾了 AC）；FR4/AC5/接口表按
   投影语义更新并追加变更记录；TODO F34.3 标题去掉 guard/execution_mode
   （PRD 非目标）；本报告 §4 的 diff-check 声明更正。
5. **行尾规范化**：10 个暂存文件含 CRLF（9 个 Write 工具新建 + 1 个 git mv
   保留的历史 CRLF 文件 _truncate.py），统一规范化为 LF（仓库约定 LF，
   core.autocrlf=false、无 .gitattributes）并复验 `git diff --cached --check` 通过。

round 2 复验门禁（修正后实测）：

```text
F34 专项（契约/架构/生命周期）：22 passed（19+3 新增）
全量：652 passed in 318.78s（649 + 3 新增回归）
Ruff：All checks passed!（--no-cache）
git diff --cached --check：exit 0（行尾规范化后真实通过）
git diff --check（未暂存区）：exit 0
```

## 8. 下一步

F33（Agent Package Final Architecture）依赖的 Tool 边界已冻结，可按交接文档推荐
顺序立项 F33；或先做 F6.12（cordis-py PyPI 发行物切换）。
