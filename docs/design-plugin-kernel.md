# ftre 插件内核重构设计（Cordis 风格）

> 状态：设计提案（待评审）
> 日期：2026-08-14
> 背景：研究 DeepSeek Harness 的 Cordis 插件内核（E:\cordis，koishijs/cordis）后，为 ftre 设计一套等价的插件架构。

## 1. 第一性原理分析

### 1.1 问题本质
**核心问题**：ftre 需要"一切皆插件"的运行时——插件的依赖关系、生命周期、协作方式由内核管理，而不是每个插件拿到一个包含全部能力的上帝对象。

**成功标准**：新增/替换/禁用任一 Agent 能力（模型、工具、技能、hook、路由、MCP）只需写插件 + 改配置，不动内核；插件之间按声明依赖自动协作；坏插件不拖垮主流程。

### 1.2 假设挑战

| 假设 | 质疑 | 结论 |
|---|---|---|
| 插件需要拿到全部内部对象（bus/session_manager/tool_registry...） | 插件只应拿到它声明的依赖；全量注入导致隐性耦合、无法排序加载 | 放弃 → 按需注入（inject/provide） |
| 插件 setup 失败 = 回滚工具即可 | 没有生命周期状态机，无法表达"等待依赖/降级/恢复" | 放弃 → 状态机（PENDING→LOADING→ACTIVE→FAILED→UNLOADING） |
| Hook 只有"顺序 filter chain"一种形态 | 插件协作需要多形态：广播/串行短路/链式传递 | 扩展 → 事件 5 模式（emit/parallel/serial/bail/waterfall） |
| config.json 的 plugins 数组扁平即可 | 复杂 Agent 需要分组、禁用、隔离 | 扩展 → 配置树（EntryTree：group/disabled/isolate） |
| 必须照搬 Cordis 的 JS Proxy/装饰器魔法 | Python 有更朴素的等价实现（类属性声明 + 显式 get/provide） | 保留思想、换实现 |

### 1.3 基本事实（ftre 特有约束）
- Python 3.12 + asyncio，单进程长驻（`ftre gateway`），插件为进程内 Python 模块
- 现有 5 个内置插件（skill/mcp/context_govern/title_gen/plan/team 等）+ `~/.ftre/plugins/` 外部插件目录
- 能力注册点：`tool_registry` / `register_channel` / `HookManager`（before_messages_build、before_agent_run）/ `core_hook`（ON_STOP 等）/ `APIRouter` / system prompt 注入
- 配置源：`~/.ftre/config.json` 的 `plugins` 数组
- 与 Agent 协作通过 EventBus（inbound/outbound）

### 1.4 推理链
基本事实 → 内核五件套（Context / 依赖注入 / 生命周期 / 事件 / 配置树）→ 插件只需声明 name+inject+setup → 配置驱动加载 → 现有插件迁移验证。

## 2. 目标与范围

**做**：
1. 新内核 `src/ftre/plugin/kernel/`（context / registry / lifecycle / events / loader）
2. 插件声明方式升级：`inject` 依赖 + `provide` 服务 + 状态机 + effect 清理
3. 事件 5 分发模式
4. 配置树驱动加载（group/disabled）
5. 现有内置插件迁移到新架构（保留行为不变）

**不做**（本期）：
- 热重载 hmr（importlib.reload 复杂，留二期）
- isolate/intercept 完整语义（仅保留 group 级隔离）
- Service.check 响应式降级（仅依赖缺失→PENDING）

## 3. 与 Cordis 映射表

| Cordis（JS） | ftre Python 版 | 说明 |
|---|---|---|
| `Context`（Proxy + extend/isolate/intercept） | `FtreContext`（普通对象，`extend()` 子上下文） | Python 无 Proxy；用显式方法 |
| `ctx.plugin(plugin, config)` / `ctx.inject(deps, cb)` | `ctx.use(plugin_cls, config)` | 注册子插件（嵌套） |
| `Plugin.Base.inject / provide` | 插件类属性 `inject: list[str]` / `provide: str \| list[str]` | 类级声明 |
| `Fiber`（状态机 + effect 收集 + epoch 响应式） | `PluginInstance`（状态机 + `setup()` 返回 cleanup） | 响应式重载简化为"依赖缺失 PENDING、就绪自动激活" |
| `RegistryService`（`_internal: Map<callback, Runtime>`） | `PluginRegistry`（`_registry: dict[str, PluginRuntime]`） | |
| `EventsService`（emit/parallel/serial/bail/waterfall） | `EventHub`（同名 5 模式，sync/async 兼容） | |
| `Service` 基类（provide + check + config） | `BaseService`（提供服务 + 可选 `check()`） | |
| `Loader / EntryTree / EntryGroup` | `PluginLoader / EntryTree / EntryGroup` | 配置树驱动 |
| `Plugin.Config`（StandardSchema 校验） | `config: type[BaseModel] \| None`（pydantic 校验） | |

## 4. 内核 API（草案）

### 4.1 插件声明

```python
from ftre.plugin.kernel import FtreContext, Plugin, Cleanup

class SkillPlugin(Plugin):
    name = "skill"
    version = "0.1.0"
    # 依赖的服务：注入到 ctx 上（ctx.tool_registry / ctx.skill_store ...）
    inject: list[str] = ["tool_registry", "skill_store"]
    # 提供的服务
    provide: str | list[str] = "skill_manager"

    async def setup(self, ctx: FtreContext, config: dict) -> Cleanup | None:
        # 注册工具 / hook / router ...
        ctx.tool_registry.register(...)
        def cleanup():
            ctx.tool_registry.unregister(...)
        return cleanup
```

### 4.2 FtreContext

```python
class FtreContext:
    parent: FtreContext | None        # 子插件作用域
    config: dict                      # 当前作用域配置
    # 服务
    def provide(self, name: str, value: Any, check: Callable[[], bool] | None = None) -> None
    def get(self, name: str, strict: bool = True) -> Any   # 严格模式缺依赖抛错
    def has(self, name: str) -> bool
    # 子插件
    def use(self, plugin: type[Plugin], config: dict | None = None) -> PluginInstance
    # 事件
    def on(self, event: str, handler: Callable, mode: str = "emit", prepend: bool = False) -> Cleanup
    def emit(self, event: str, *args) -> None
    async def parallel(self, event: str, *args) -> None
    async def serial(self, event: str, *args) -> Any
    def bail(self, event: str, *args) -> Any
    def waterfall(self, event: str, *args) -> Any
    # 能力注册（对齐现有 FtrePluginApi 的注册点，迁移到 ctx 上）
    @property
    def tool_registry(self) -> ToolRegistry
    @property
    def bus(self) -> EventBus
    # ... channel_manager / session_manager / hook_manager / routers ...
```

### 4.3 生命周期状态机

```
PENDING ──依赖就绪──▶ LOADING ──setup() 完成──▶ ACTIVE
   ▲                     │                        │
   │                     │ setup() 抛错           │ 卸载
   │                     ▼                        ▼
   └────依赖回归────  FAILED ←───────          UNLOADING ──cleanup 完成──▶ DISPOSED
```

- 插件 `inject` 声明的服务未全部提供 → 保持 PENDING（不执行 setup）
- 依赖服务被卸载 → 依赖它的插件先 UNLOADING（逆依赖序），cleanup 后回到 PENDING 或 DISPOSED
- setup 抛错 → FAILED，记录错误，不拖垮其他插件
- 循环依赖 → 注册时检测并报错

### 4.4 事件 5 模式

| 模式 | 语义 | 触发方法 |
|---|---|---|
| `emit` | 同步广播，逐个调用 | `ctx.emit(name, *args)` |
| `parallel` | 并行 await，聚合错误 | `await ctx.parallel(name, *args)` |
| `serial` | 串行 await，首个非空结果短路 | `await ctx.serial(name, *args)` |
| `bail` | 同步短路，首个非空结果返回 | `ctx.bail(name, *args)` |
| `waterfall` | 链式传递 next，可修改参数 | `ctx.waterfall(name, *args)` |

事件 handler 与插件生命周期绑定：插件卸载时其注册的 handler 自动移除（由 PluginInstance 收集）。

### 4.5 配置树驱动（EntryTree）

```jsonc
// ~/.ftre/config.json
{
  "plugins": [
    { "name": "skill", "config": { "dir": "~/.ftre/skills" } },
    { "name": "mcp", "config": { "servers": {} } },
    {
      "id": "octo", "group": true,
      "children": [
        { "name": "octo.channel", "config": { "server": "ws://..." } },
        { "name": "octo.management", "disabled": true }
      ]
    }
  ]
}
```

- 配置树每个节点 = Entry（id/name/config/group/disabled）
- group 节点 → 子 Context 作用域（隔离）；disabled → 不加载（含子节点）
- 配置变更 → 对应 Entry 热更新（config diff → 重启该插件实例）

## 5. 兼容与迁移策略

1. **新内核独立成包** `src/ftre/plugin/kernel/`，与现有 `plugin.py` 并存一个过渡期
2. **现有内置插件逐个迁移**：Plugin 子类改造为声明 `inject` + `setup` 返回 cleanup；`FtrePluginApi` 的能力以 `BaseService` 形式提供给 ctx（tool_registry/bus/channel_manager/... 各自成为 service）
3. **FtrePluginApi 兼容层**：迁移完成后删除（禁止尾巴），外部插件同步迁移
4. **配置格式**：`plugins` 数组格式扩展（加 `disabled`/`group`/`children`），旧格式（仅 name+config）兼容解析

## 6. 文件布局

```
src/ftre/plugin/
  kernel/
    __init__.py
    context.py      # FtreContext
    registry.py     # PluginRegistry + PluginRuntime
    lifecycle.py    # PluginInstance（状态机）
    events.py       # EventHub（5 模式）
    loader.py       # PluginLoader + EntryTree + EntryGroup + Entry
    services.py     # BaseService + 现有能力适配（tool_registry/bus/channel/...）
  builtin/          # 迁移后的内置插件
    skill_plugin.py
    mcp_plugin.py
    context_govern.py
    title_gen.py
    ...
  plugin.py         # 迁移后删除（旧实现）
```

## 7. 验收标准

- AC1：插件声明 `inject` 依赖后，依赖未就绪保持 PENDING，就绪自动激活（测试验证时序）
- AC2：循环依赖注册时报错；依赖被卸载时依赖方按逆序先清理
- AC3：事件 5 模式行为正确（emit 同步广播 / parallel 聚合错误 / serial 短路 / bail 短路 / waterfall 链式）
- AC4：配置树支持 group/disabled；disabled 组不加载子节点
- AC5：现有内置插件全量迁移后行为不变（pytest 全绿、ruff 通过、gateway 启动正常）
- AC6：旧 FtrePluginApi / plugin.py 删除，无遗留引用（grep 验证）
