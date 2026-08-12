# PRD-C1-插件体系

> 状态生命周期：草稿 → 评审 → approved（定稿）→ 开发中 → 已验收

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | C1 |
| 名称 | 插件体系（FtrePluginApi + PluginManager + 内置插件 skill/mcp/context_govern/title_gen） |
| 状态 | 已验收 |
| 创建日期 | 2026-08-12 |
| 定稿日期 | 2026-08-12 |
| 验收日期 | 2026-08-12 |
| 关联文档 | docs/TODO.yaml 阶段 C1；AGENTS.md |

## 1. 背景与目标

- **背景**：Gateway 需要可扩展的插件架构——工具注册、系统提示词注入、HTTP 路由、Hook 链。内置插件（skill、mcp、context_govern、title_gen）随代码发布，外部插件从 `~/.ftre/plugins/` 加载。
- **目标**：实现 FtrePluginApi 能力注册接口 + PluginManager 加载管理 + 四个内置插件全部功能可用。
- **非目标**：不实现 MCP 双层配置细节（C2）、不实现多 Agent 协作（C3）。

## 2. 需求范围

### 2.1 功能需求

- [x] FR1：FtrePluginApi 能力注册——`tool_registry`（注册全局工具）、`append_system_prompt`（注入 system prompt）、`register_router`（注册 HTTP 路由）、`register_hook`（注册 Hook 回调）
- [x] FR2：PluginManager 内置+外部加载——先加载内置插件（`src/ftre/plugin/builtin/`），再扫描外部插件目录（`~/.ftre/plugins/`）
- [x] FR3：内置插件 skill——Skill 管理（loadSkill 工具、CRUD API、system prompt 注入、per-agent 私有 skill）
- [x] FR4：内置插件 mcp——MCP 服务器管理（配置、连接池、工具注册、CRUD API）
- [x] FR5：内置插件 context_govern——上下文治理（AGENTS.md 双注入、工具事件配对/去重/悬挂清理）
- [x] FR6：内置插件 title_gen——标题生成（首条消息自动生成会话标题）

### 2.2 非功能需求

- 性能：插件加载在 Gateway 启动时完成，不影响运行时
- 安全：外部插件代码在独立进程或受限环境执行
- 兼容性：内置插件 API 向前兼容，新增能力不破坏已有插件

## 3. 技术方案

### 模块设计

| 文件 | 职责 |
|---|---|
| `src/ftre/plugin/__init__.py` | `FtrePluginApi` + `PluginManager` + `FtrePlugin` 基类 |
| `src/ftre/plugin/skill_plugin.py` | Skill 插件——loadSkill 工具 + CRUD API + prompt 注入 |
| `src/ftre/plugin/mcp_plugin.py` | MCP 插件——MCP 服务器管理 + 工具注册 |
| `src/ftre/plugin/context_govern.py` | 上下文治理——AGENTS.md 双注入 + 事件配对去重 |
| `src/ftre/plugin/title_gen.py` | 标题生成——首条消息触发 LLM 生成标题 |

### 关键数据结构

```python
class FtrePluginApi:
    tool_registry: ToolRegistry          # 注册全局工具
    def append_system_prompt(self, prompt: str) -> None: ...
    def register_router(self, router: APIRouter) -> None: ...
    def register_hook(self, hook: str, callback: Callable) -> None: ...

class PluginManager:
    def load_builtin(self) -> None: ...     # 加载 src/ftre/plugin/builtin/
    def load_external(self) -> None: ...    # 扫描 ~/.ftre/plugins/
```

## 5. 验收标准

- [x] AC1：插件可注册工具和路由——通过 FtrePluginApi 注册的工具和 HTTP 路由在 Gateway 中可用
- [x] AC2：context_govern AGENTS.md 双注入——`agent_dir/AGENTS.md` 和 `workspace/AGENTS.md` 都注入到 system prompt
- [x] AC3：title_gen 自动生成标题——首条消息后自动调用 LLM 生成会话标题
