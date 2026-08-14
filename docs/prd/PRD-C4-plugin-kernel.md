# PRD-C4-Cordis 风格插件内核

> 状态生命周期：草稿 → 评审 → approved（定稿）→ 开发中 → 已验收
> 本 PRD 依据 DeepSeek Harness 的 Cordis 插件框架（`cordiverse/cordis`，MIT）设计；研究分析见 `docs/design-plugin-kernel.md`。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | C4 |
| 名称 | Cordis 风格插件内核（依赖注入 + 生命周期 + 事件 + 配置树） |
| 状态 | 开发中 |
| 创建日期 | 2026-08-14 |
| 定稿日期 | 2026-08-14 |
| 关联文档 | docs/TODO.yaml 阶段 C4；AGENTS.md `<plugins>`；docs/design-plugin-kernel.md（Cordis 研究分析） |

## 1. 背景与目标

- **背景**：ftre 现有插件体系（`src/ftre/plugin/plugin.py`）采用"上帝对象 `FtrePluginApi` + 扁平扫描"模式——每个插件拿到 bus/session_manager/tool_registry 等全部内部依赖，无依赖声明、无生命周期状态机、无自动清理。`Plugin.unload()` 只调默认空 `teardown()`，插件注册的**工具/路由/hook 卸载后残留**。事件系统只有单一 filter chain（before_messages_build / before_agent_run 两个挂点），无法表达短路/并行/链式传递。
- **目标**：引入 Cordis 风格的插件内核——插件通过 `inject` 声明依赖、`provide` 提供服务，内核按依赖自动解析加载顺序；注册的能力（工具/路由/hook/channel）通过 effect 在卸载时自动倒序清理；事件支持 5 种分发模式；配置树驱动加载（group/disabled/嵌套）。一句话：**让 ftre 具备"一切皆插件、依赖声明、自动清理"的插件运行时**。
- **非目标**（本期不做，防范围蔓延）：
  - 热重载 hmr（`importlib.reload` 复杂，留二期）
  - 完整的 isolate/intercept 语义（本期仅做 group 级隔离作用域）
  - `Service.check` 的响应式降级（依赖缺失仅 → PENDING，不做 check 谓词）
  - 不改变前端协议、不改变 AgentLoop 的 SessionLane/mailbox 语义

## 2. 需求范围

### 2.1 功能需求

- [x] FR1：插件声明依赖——插件类属性 `inject: list[str]` 声明所需服务；内核保证**所有依赖就绪后才执行 setup**，依赖未就绪保持 PENDING 状态
- [x] FR2：插件提供服务——插件类属性 `provide: str | list[str]` 声明提供的服务名；setup 中通过 `ctx.provide(name, value)` 注册，使依赖它的插件可见
- [x] FR3：生命周期状态机——插件实例状态 `PENDING → LOADING → ACTIVE → FAILED → UNLOADING → DISPOSED`；状态转换发出 `internal/plugin_status` 事件可观测
- [x] FR4：effect 自动清理——插件 setup 返回的 disposer（或注册的 tool/router/hook/channel）在插件卸载时**自动倒序撤销**；重复卸载幂等（no-op）
- [x] FR5：事件 5 模式——`emit`（同步广播）/ `parallel`（并发 await）/ `serial`（串行 await 首个 bail 短路）/ `bail`（同步短路）/ `waterfall`（最后参数为 next 续延，不调 next 即否决）；`ctx.on/once` 返回 disposer，随插件卸载自动移除
- [x] FR6：配置树驱动加载——config.json `plugins` 数组支持 `{name, config, disabled, group, children}`；group 节点为子作用域，disabled 的 group 不加载子节点；配置用 pydantic schema 校验，非法 config 拒绝启动
- [x] FR7：循环依赖检测——注册时检测插件依赖图循环并报错（不启动）
- [x] FR8：现有内置插件迁移——skill/mcp/context_govern/title_gen/plan/team 迁移到新架构（`inject` 声明 + setup 返回 disposer），**行为与旧架构完全一致**（hook 注入、工具注册、路由挂载）
- [x] FR9：外部插件迁移——`~/.ftre/plugins/octo_plugin` 迁移到新 API（独立仓库单独 PR）
- [x] FR10：旧实现彻底移除——`plugin.py`（FtrePluginApi/Plugin/PluginManager）与 `hook_manager.py`（旧 filter chain HookManager）删除，无遗留引用

### 2.2 非功能需求

- **性能**：插件加载为启动一次性完成，无运行时热路径开销；事件分发性能不低于现有 filter chain
- **安全**：服务访问严格校验——插件未声明 `inject` 不能通过 ctx 访问该服务（防上帝对象隐性耦合）
- **兼容性**：config.json 旧 `plugins` 格式（仅 name+config）平滑解析；`before_messages_build` / `before_agent_run` 两个挂点的语义保留（`append_to_first_system` 辅助函数照旧可用）
- **可观测**：插件加载/卸载/失败记录日志（logger），状态事件可订阅

## 3. 技术方案

### 3.1 核心概念总览（五件套）

| 概念 | 一句话 | 对应 Cordis |
|---|---|---|
| **Context（ctx）** | 插件拿到的"作用域容器"，所有服务/事件/注册 API 都走它 | `Context` |
| **inject / provide** | 插件声明"我要什么服务"（inject）和"我提供什么服务"（provide），内核据此排序与注入 | `inject` / `ctx.provide` |
| **生命周期状态机** | 插件实例从注册到卸载的状态流转，setup 失败/依赖缺失有明确状态 | `Fiber` |
| **effect（自动清理）** | 插件注册的能力（工具/路由/hook）自动收集清理函数，卸载时倒序撤销 | `ctx.effect` / disposer |
| **事件分发** | 插件之间不直接调用，靠事件协作（5 种分发模式） | `ctx.emit/waterfall/...` |

下面逐一详解配置树、生命周期、插件编写。

### 3.2 配置树（是什么 + 示例）

**是什么**：`config.json` 里 `plugins` 数组不再是扁平列表，而是一棵**嵌套树**——每个节点是一条"插件配置"（Entry），节点分两种：**叶子节点**（加载为一个插件实例）和**分组节点**（group，不加载代码，只创建一个子作用域，其 children 都在该作用域下加载）。

**为什么要树**：扁平数组无法表达三件事——① 一组插件一起启用/禁用；② 子作用域隔离（同名服务在组内不污染全局）；③ 嵌套组织（一个平台的多个插件归一组）。

**示例**（config.json）：
```jsonc
{
  "plugins": [
    // ── 叶子节点：单个插件 ──────────────────────────
    { "name": "skill", "config": { "skills_dir": "~/.ftre/skills" } },
    { "name": "mcp",   "config": { "servers": {} } },

    // ── 分组节点：一组插件共用一个子作用域 ──────────
    {
      "id": "octo",             // 节点唯一 id（默认=name，可显式指定）
      "group": true,            // 标记为分组（不加载代码）
      "disabled": false,
      "children": [
        { "name": "octo_channel",    "config": { "api_url": "http://..." } },
        { "name": "octo_management", "disabled": true }   // 子节点可单独禁用
      ]
    },

    // ── 整组禁用：children 全部不加载 ──────────────
    {
      "id": "experimental",
      "group": true,
      "disabled": true,          // ← 整棵子树跳过
      "children": [ { "name": "title_gen" }, { "name": "plan" } ]
    }
  ]
}
```

**加载规则**：
1. **叶子节点** → `import` 对应插件模块 → 校验 config（pydantic schema）→ 创建 PluginInstance
2. **group 节点** → 不 import，创建子 Context 作用域；其 children 递归处理
3. **disabled=true**（叶子或 group）→ 整棵子树跳过，不 import 不加载
4. 每个节点有唯一 `id`（日志/定位/热更新用）

**对应 Cordis**：`EntryTree`（整棵树）/ `EntryGroup`（分组节点）/ `Entry`（单个节点）。

### 3.3 插件生命周期流程（状态机 + 示例）

**状态机**：
```
                    依赖就绪（所有 inject 的服务都已 provide）
  PENDING ──────────────────────────────────────────────▶ LOADING
     ▲                                                       │ 执行 setup()
     │ 依赖的服务被移除                                       │
     │ （回到等待，不执行 setup）                              ▼
     │                                                   ACTIVE（正常运行）
     │                                                       │ effect 已注册，能力可用
     │                                                       │
     │                                                       │ 卸载触发
     │                                                       │ （dispose / config 变更 / 依赖服务消失）
     │                                                       ▼
     │                                                  UNLOADING
     │                                                       │ effect 倒序清理中
     │                                                       │
     └───────────────────────────────────────────────────────┘ 清理完成
                                                             │
                                        setup() 抛错 ───────▶ FAILED（记录错误，不拖垮其他插件）
```

**状态含义**：

| 状态 | 含义 |
|---|---|
| `PENDING` | 已注册但依赖未就绪，**不执行 setup**，等待依赖 |
| `LOADING` | 依赖就绪，正在执行 setup() |
| `ACTIVE` | setup 成功，能力已注册可用 |
| `FAILED` | setup 抛错，记录日志；不影响其他插件 |
| `UNLOADING` | 正在倒序执行 effect 清理 |
| `DISPOSED` | 已完全卸载，资源释放 |

**Example 走一遍**：
```python
# 插件 A 依赖 tool_registry（启动时已 provide）
class SkillPlugin(Plugin):
    inject = ["tool_registry"]
# → 注册时 tool_registry 已就绪 → 直接 LOADING → setup → ACTIVE

# 插件 B 依赖 session_manager（稍后才 provide）
class TeamPlugin(Plugin):
    inject = ["session_manager"]
# → 注册时 session_manager 未就绪 → PENDING（不 setup）
# → 稍后 ctx.provide("session_manager", sm) → 内核通知 TeamPlugin
# → 自动 LOADING → setup → ACTIVE

# 卸载 SkillPlugin
ctx.loader.unload("skill")
# → UNLOADING → 倒序执行 effect（先撤 hook → 再撤路由 → 最后撤工具）
# → DISPOSED；此时 loadSkill 工具、/skills 路由、before_run hook 全部消失
```

### 3.4 插件编写完整示例（迁移前后对比）

**旧（现状 skill_plugin.py）**——上帝对象 + 无清理：
```python
class SkillPlugin(Plugin):
    name = "skill"
    def setup(self):
        cfg = self.api.config or {}                       # 裸 dict 配置
        self.api.tool_registry.register(create_load_skill_tool(...))   # 注册工具
        self.api.register_router(self._build_router())                 # 注册路由
        self.api.register_hook(BEFORE_AGENT_RUN, self._inject_prompt)  # 注册 hook
        # ⚠️ 没有 teardown —— 卸载后工具/路由/hook 全部残留
```

**新（升级后）**——依赖声明 + effect 自动清理：
```python
from ftre.plugin.kernel import Plugin, FtreContext, Cleanup

class SkillPlugin(Plugin):
    name = "skill"
    version = "1.0.0"
    inject = ["tool_registry", "command_manager"]   # ① 声明依赖：就绪才 setup
    provide = "skill_manager"                        # ② 声明提供的服务
    Config = SkillConfig                             # ③ pydantic schema 校验 config

    async def setup(self, ctx: FtreContext, config: SkillConfig) -> Cleanup | None:
        # 注册工具 —— 框架自动跟踪，卸载时自动撤销（无需手写 unregister）
        tool = create_load_skill_tool(config.skills_dir)
        ctx.tool_registry.register(tool)

        # 注册路由 —— 同样自动跟踪
        ctx.register_router(self._build_router())

        # 注册 hook（事件监听）—— ctx.on 返回 disposer，卸载时自动移除
        ctx.on("agent/before_run", self._inject_prompt)

        # 提供服务给其他插件
        ctx.provide("skill_manager", SkillManager(config))

        # 可选：额外清理逻辑（上面的注册框架已自动清理，这里只写额外的）
        def cleanup():
            logger.info("skill 插件已卸载")
        return cleanup
```

**关键差异**：

| 维度 | 旧 | 新 |
|---|---|---|
| 依赖获取 | `self.api.*` 全量（上帝对象） | `inject` 声明，按需注入 |
| 配置 | 裸 `self.api.config` dict | `Config` pydantic schema 校验 |
| 清理 | 手写 teardown（默认空，残留） | effect 自动倒序清理 |
| 依赖排序 | 无（扫描顺序） | 内核按依赖图自动排序 |
| 服务暴露 | 无 | `provide` 给其他插件 |

### 3.5 模块设计（目录树）

```
src/ftre/plugin/
├── __init__.py                    # 导出新 API：FtreContext/PluginLoader/Plugin/事件常量
├── kernel/                        # 新增：Cordis 风格内核
│   ├── __init__.py                #   汇总导出
│   ├── context.py                 #   FtreContext：服务容器 + use/嵌套子上下文 + 事件混入
│   ├── registry.py                #   PluginRegistry + PluginRuntime + Plugin 基类（inject/provide/Config）
│   ├── lifecycle.py               #   PluginInstance 状态机 + effect 收集与倒序清理
│   ├── events.py                  #   EventHub：emit/parallel/serial/bail/waterfall + on/once
│   ├── loader.py                  #   PluginLoader + EntryTree/EntryGroup/Entry（配置树）
│   └── services.py                #   BaseService + 现有能力适配（bus/tool_registry/channel_manager/... 注册为 service）
└── builtin/                       # 6 插件改造（保留文件名，重写 setup）
    ├── skill_plugin.py
    ├── mcp_plugin.py
    ├── context_govern.py
    ├── title_gen.py
    ├── plan_plugin.py
    └── team_plugin.py
```

**删除**：`plugin.py`、`hook_manager.py`（被 kernel 取代）。

### 3.6 关键数据结构

- `FtreContext`：服务容器（`provide/get/set/has`）+ 子上下文嵌套（`use`）+ 事件方法混入（`on/emit/...`）+ 能力访问器（`tool_registry/bus/...` 由 services.py 适配）
- `Plugin`（新基类）：`name / version / inject: list[str] / provide: str|list[str] / Config: type[BaseModel]|None` + `setup(ctx, config) -> Cleanup | None`
- `PluginInstance`：状态机 + `_disposables`（倒序清理列表）+ 依赖快照
- `EventHub`：`_hooks: dict[str, list[Handler]]`，Handler 绑定所属 PluginInstance（卸载时移除）
- `EntryTree / EntryGroup / Entry`：配置树，Entry 含 `id/name/config/disabled/group/children`

### 3.7 依赖选型

- 配置校验：pydantic（ftre 已用）
- 不引入第三方插件框架——内核自研（对齐 Cordis 思想，Python 化实现）

## 4. 接口定义

```python
from ftre.plugin.kernel import FtreContext, Plugin, Cleanup

class SkillPlugin(Plugin):
    name = "skill"
    version = "1.0.0"
    inject: list[str] = ["tool_registry", "command_manager"]   # 依赖声明
    provide: str = "skill_manager"                              # 提供的服务

    async def setup(self, ctx: FtreContext, config: dict) -> Cleanup | None:
        ctx.tool_registry.register(...)          # 注册工具（自动跟踪）
        ctx.provide("skill_manager", ...)
        def cleanup():
            ctx.tool_registry.unregister(...)
        return cleanup                            # 卸载时倒序执行

# 启动组装（main.py）
ctx = FtreContext()
ctx.provide("tool_registry", tool_registry)
ctx.provide("bus", bus)
loader = PluginLoader(ctx, config_data)
await loader.load()          # 配置树驱动，自动按依赖排序
```

事件挂点语义（对应现有 hook）：
- `agent/before_messages_build` ← `BEFORE_MESSAGES_BUILD`（waterfall，可裁剪 messages/config）
- `agent/before_run` ← `BEFORE_AGENT_RUN`（waterfall，可注入上下文/系统提示词）

## 5. 验收标准

- [x] AC1：依赖时序——插件 A `inject=['b']`，b 未就绪时 A 保持 PENDING；provide b 后 A 自动 LOADING→ACTIVE（pytest 断言状态序列）
- [x] AC2：循环依赖——A↔B 互相 inject，注册时报错且不启动（pytest 断言抛错）
- [x] AC3：effect 清理——插件卸载后其注册的工具/路由/hook/channel 全部撤销（pytest 断言对应 registry/hook 表为空）；重复卸载 no-op
- [x] AC4：事件 5 模式——单测覆盖 emit/parallel/serial/bail/waterfall 各自语义（含 waterfall 短路否决）
- [x] AC5：配置树——disabled 的 group 不加载子节点；group 嵌套子作用域生效（pytest 断言）
- [x] AC6：配置校验——非法 config（schema 不匹配）拒绝启动并报错（pytest 断言抛 ValidationError）
- [x] AC7：内置插件迁移后行为不变——skill/mcp/context_govern/title_gen/plan/team 的 hook 注入、工具注册、路由挂载与旧架构一致（pytest + 手动：启动后对话能命中 skill 提示词注入、MCP 工具、标题生成）
- [x] AC8：外部 octo 插件迁移后正常加载（octo 仓库独立 PR 验证）
- [x] AC9：旧实现彻底移除——`plugin.py`/`hook_manager.py` 删除；`grep FtrePluginApi\|PluginManager\|HookManager src/ftre` 无残留引用
- [ ] AC10：端到端——`ftre gateway` 启动加载插件树正常（日志列出全部插件 ACTIVE）；`pytest` 全绿；`ruff check` 通过

> AC10 进度（2026-08-14）：使用当前真实 `~/.ftre/config.json` 完成网关启动/取消/资源释放冒烟，Octo 两个 Bot 均完成认证；`pytest -q` 314 passed，C4 新增与修改文件 `ruff check` 通过。全仓 `ruff check .` 仍有 292 条既有告警，不属于 C4 改动，因此本阶段尚不标记“已验收”。

## 6. 测试计划

- **单元测试**（`tests/plugin/` 新增）：
  - `test_kernel_registry.py`：依赖解析顺序、循环依赖报错、provide/get
  - `test_kernel_lifecycle.py`：状态机转换、effect 倒序清理、卸载幂等
  - `test_kernel_events.py`：5 种分发模式 + on/once 自动移除
  - `test_kernel_loader.py`：配置树解析、group/disabled、schema 校验
- **集成测试**：现有插件测试套件（若有）全部通过；新增"skill 提示词注入端到端"验证
- **手动验证**：
  1. `ftre gateway` 启动，日志确认 skill/mcp/context_govern/title_gen/plan/team 全部 ACTIVE
  2. 对话触发 skill 匹配 → loadSkill 加载 → 验证 skill 提示词注入
  3. 对话触发 MCP 工具调用 → 验证私有 MCP 工具注册
  4. 首条消息 → 验证标题生成 hook
  5. 验证 octo 插件（若启用）Channel 注册 + before_run 注入

## 7. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-14 | 初始定稿（评审中）：Cordis 风格插件内核设计，含 FR1-FR10、AC1-AC10 | 基于 DeepSeek Harness Cordis 框架研究（design-plugin-kernel.md），升级 ftre 插件体系以支持依赖声明、生命周期、自动清理、事件 5 模式、配置树 |
| 2026-08-14 | 补充技术方案概念详解：配置树（3.2）、生命周期状态机（3.3）、插件编写迁移前后对比示例（3.4） | 评审反馈：原文档概念讲解不足，需用 Example 说清配置树、生命周期流程 |
| 2026-08-14 | FR1-FR10 实现完成，AC1-AC9 通过；后端 314 项测试、Octo 40 项回归及真实配置网关生命周期冒烟通过 | 接管中断开发后完成内核、内置插件、Octo 迁移与验证；补齐遗留 `module` 配置兼容；AC10 仅剩全仓历史 Ruff 基线待处理 |
