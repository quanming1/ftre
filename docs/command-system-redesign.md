# ftre 指令系统重构设计

> 借鉴 Qwen Code 的指令架构，结合 ftre 的 Python/async 后端特性做适配。

## 1. 现状

| 维度 | 当前 |
|---|---|
| 指令数 | 3 条（/cancel /compact /compress-fast） |
| 注册方式 | `loop.py` 硬编码 `register()` |
| 返回值 | 无，handler 写 `ctx.meta` |
| 子命令 | 不支持 |
| 自定义指令 | 不支持 |
| Skill 作指令 | 不支持 |

## 2. 目标

- handler 有**类型化返回值**，解耦 handler 和 pipeline
- 支持**文件指令**（用户用 `.md` 文件定义，不改代码）
- **Skill 自动注册**为 slash command
- 支持**子命令**（`/memory add`）
- 不破坏现有 3 条指令和两级调度

## 3. 不做的事

| 不做 | 原因 |
|---|---|
| 多 Loader 架构（6 个 Loader） | 3 条指令不需要，file_loader 和 skill_plugin 直接注册 |
| 9 种返回值 | ftre 是后端，dialog/confirm 等前端逻辑用 SendMessage/Handled 覆盖 |
| `modelInvocable` | Skill 通过 hook 注入 system prompt，不需要 LLM 主动调 slash |
| MCP prompt 作指令 | MCP 工具已注册到 tool_registry |
| 改两级调度 | system/普通两级已很好 |

## 4. 架构

### 4.1 整体流程（改动部分加粗）

```
用户输入 /xxx
  ↓
_dispatch（不改）
  ├── try_dispatch_system → /cancel（锁外）
  └── pipeline.run → _step_command（锁内）
       ├── command_manager.match() → 递归匹配含子命令
       ├── 命中 → 持久化 user_message → **执行 handler → 得到 CommandResult**
       │     ├── **SubmitPrompt** → 替换 inbound content, return True（继续→LLM）
       │     ├── **SendMessage** → 推消息给前端, return False（短路）
       │     ├── **Handled** → return False（短路）
       │     └── **Passthrough** → return True（继续→LLM，安全默认）
       └── 未命中 → return True（继续→LLM）
```

### 4.2 文件结构

```
src/ftre/command/
  __init__.py          # 导出
  types.py             # 新建：CommandResult 联合类型 + 扩展 CommandDef
  manager.py           # 改造：Handler 签名 + 子命令匹配 + dispatch 返回 CommandResult
  builtin.py           # 新建：内置指令注册（从 loop.py 抽出）
  file_loader.py       # 新建：扫描 .md 文件指令
```

Skill 作指令不新建文件，直接在 `skill_plugin.py` 里注册。

## 5. 核心类型设计

### 5.1 CommandResult（types.py）

```python
from dataclasses import dataclass, field
from typing import Union

@dataclass
class SubmitPrompt:
    """把 prompt 提交给 LLM"""
    content: str | list[dict]
    model_override: str | None = None

@dataclass
class SendMessage:
    """给用户显示一条消息"""
    content: str
    level: str = "info"  # info / warning / error

@dataclass
class Handled:
    """已处理，短路 pipeline"""
    pass

@dataclass
class Passthrough:
    """不是指令，交给 LLM"""
    pass

CommandResult = Union[SubmitPrompt, SendMessage, Handled, Passthrough]
```

**设计决策：不设 OpenDialog**

Qwen Code 有 `OpenDialogActionReturn`（27 种 dialog），因为它的指令直接操作 Ink UI。ftre 是后端，对话框由前端管理，后端只需推 WebSocket 事件。`SendMessage` 或 `Handled` 已覆盖所有场景。

### 5.2 CommandDef 扩展

```python
@dataclass
class CommandDef:
    command: str                        # "/memory"
    description: str = ""
    args_hint: str = ""
    system: bool = False
    sub_commands: list["CommandDef"] = field(default_factory=list)
    source: str = "builtin"             # builtin / file / skill
```

### 5.3 CommandContext（不变）

```python
@dataclass
class CommandContext:
    raw: str                # 原始输入 "/model gpt-5"
    command: str            # 命中的指令 "/model"
    args: str | None        # 指令后的文本 "gpt-5"
    meta: dict[str, Any]    # pipeline data，handler 可修改
```

### 5.4 Handler 签名变化

```python
# 旧
Handler = Callable[[CommandContext], None | Awaitable[None]]

# 新
Handler = Callable[[CommandContext], CommandResult | Awaitable[CommandResult]]
```

## 6. 子命令匹配

`/memory add key=value` 匹配流程：

1. 在 `self._entries` 中匹配 `/memory` → 有 `sub_commands`
2. 在 `sub_commands` 中匹配 `add` → args = `key=value`
3. 执行 `add` 的 handler，返回 CommandResult

```python
@staticmethod
def _match_entry(
    entries: list[tuple[CommandDef, Handler]],
    raw: str,
) -> tuple[CommandDef, Handler, str | None] | None:
    cmd = raw.strip()
    if not cmd:
        return None
    for d, handler in entries:
        if cmd == d.command or cmd.startswith(d.command + " "):
            args = cmd[len(d.command):].strip() or None
            # 有子命令 → 递归匹配
            if d.sub_commands and args:
                sub = cls._match_entry(
                    [(sc, sc._handler) for sc in d.sub_commands],
                    d.command + " " + args,
                )
                if sub:
                    return sub
            return (d, handler, args)
    return None
```

## 7. 文件指令

### 7.1 扫描目录

- 用户级：`~/.ftre/commands/`
- 项目级：`<workspace>/.ftre/commands/`

### 7.2 .md 文件格式

```markdown
---
description: 审查当前 Git diff
argument-hint: [file-path]
---
请审查以下代码变更，重点关注：
1. 潜在 bug
2. 安全问题
3. 性能问题

{{args}}
```

| 部分 | 用途 |
|---|---|
| frontmatter `description` | 指令描述，显示在命令面板 |
| frontmatter `argument-hint` | 参数提示 |
| 正文 | prompt 模板，提交给 LLM |
| `{{args}}` | 占位符，替换为用户输入参数 |

### 7.3 文件名 → 指令名

`~/.ftre/commands/review.md` → `/review`
`<workspace>/.ftre/commands/deploy-check.md` → `/deploy-check`

子目录 → 子命令：
`~/.ftre/commands/memory/add.md` → `/memory add`（通过子命令匹配）

### 7.4 handler

文件指令的 handler 固定逻辑：

```python
async def _file_command_handler(ctx: CommandContext, template: str) -> CommandResult:
    content = template.replace("{{args}}", ctx.args or "")
    return SubmitPrompt(content=content)
```

## 8. Skill 作指令

### 8.1 注册

在 `skill_plugin.py` 的 Skill 加载回调中：

```python
def on_skill_loaded(self, skill: Skill):
    self.api.command_manager.register(
        f"/{skill.name}",
        lambda ctx: SubmitPrompt(content=skill.body),
        description=skill.description,
        source="skill",
    )
```

### 8.2 handler

Skill 的 handler 固定返回 `SubmitPrompt(content=skill.body)`——Skill 的 body 就是 prompt 内容，交给 LLM 处理。

### 8.3 卸载

Skill 删除时注销对应指令：

```python
def on_skill_unloaded(self, skill_name: str):
    self.api.command_manager.unregister(f"/{skill_name}")
```

## 9. _step_command 改造

```python
async def _step_command(self, data: dict) -> bool:
    if not self.command_manager:
        return True

    # 1. 先判断是否命中（不执行 handler）
    cmd_def = self.command_manager.match(data)
    if cmd_def is None:
        return True  # 未命中，继续 pipeline → LLM

    # 2. 命中 → 先持久化用户输入
    inbound = data["inbound"]
    session_id = inbound.from_session or inbound.data.get("session_id", "")
    content = inbound.data.get("content", "")
    if session_id and content:
        await self.session_manager.save_message(
            session_id, "user_message",
            {"content": normalize_stored_user_content(content),
             "metadata": {"hide": False}},
        )

    # 3. 执行 handler，得到 CommandResult
    result = await self.command_manager.dispatch(data)

    # 4. 根据返回值决定下一步
    match result:
        case SubmitPrompt(content=prompt_content, model_override=mo):
            data["inbound"].data["content"] = prompt_content
            return True   # 继续 pipeline → LLM
        case SendMessage(content=msg, level=level):
            await self._send_user_message(session_id, inbound.from_channel, msg, level)
            return False  # 短路
        case Handled():
            return False  # 短路
        case Passthrough() | None:
            return True   # 继续 pipeline → LLM
        case _:
            return True   # 安全默认
```

## 10. 迁移现有指令

| 指令 | 当前 handler | 改造后返回值 |
|---|---|---|
| /cancel | 写 ctx.meta，cancel task | `Handled()` |
| /compact | 调 compact_manager | `Handled()` |
| /compress-fast | 调 compact_manager | `Handled()` |

三条指令都不需要提交 prompt 给 LLM，都是后端处理完毕后短路 pipeline。

## 11. 实现步骤

| # | 文件 | 内容 |
|---|---|---|
| 1 | `command/types.py` | 新建：CommandResult 联合类型 + 扩展 CommandDef |
| 2 | `command/manager.py` | 改造：Handler 签名 + 子命令递归匹配 + dispatch 返回 CommandResult |
| 3 | `agent/loop.py` | 改造 `_step_command`：match-case 分发 + 现有指令返回 Handled() |
| 4 | `command/builtin.py` | 新建：抽出 3 条内置指令注册 |
| 5 | `command/file_loader.py` | 新建：扫描 .md + frontmatter 解析 + {{args}} 替换 |
| 6 | `agent/loop.py` | 启动时调用 file_loader 加载文件指令 |
| 7 | `plugin/builtin/skill_plugin.py` | Skill 加载时自动注册 slash command |
| 8 | 全部文件 | 通读 + 语法检查 |
| 9 | 端到端验证 | 现有指令行为不变 + 文件指令加载 + Skill 注册 |

## 12. 对比 Qwen Code

| 维度 | Qwen Code | ftre | 取舍理由 |
|---|---|---|---|
| 返回值类型 | 9 种 | 4 种 | 后端不需要 dialog/stream/confirm |
| Loader | 6 个并行 | 直接注册 | 3 条指令不需要多 Loader |
| modelInvocable | 有 | 不要 | Skill 通过 hook 注入 |
| ExecutionMode | 3 种 | 2 种（system/普通） | 已有两级调度够用 |
| 文件指令 | .toml + .md | .md only | Markdown 已够，少一种格式 |
| 冲突解决 | 扩展重命名 | 后注册覆盖 | 指令数少，不需要复杂冲突解决 |
