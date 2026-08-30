# PRD-C6 通用 Ftre Markdown 扩展协议与客户端渲染

> 本阶段把 Skill 的内嵌调用从专用字符串升级为可扩展的 Markdown-like 协议。
> 首期只实现 `skill` 类型，但协议、解析器、客户端 Renderer 和后端 Handler 必须能承载后续的 file、agent、mcp 等扩展。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | C6 |
| 名称 | 通用 Ftre Markdown 扩展协议与客户端渲染 |
| 状态 | 开发中 |
| 创建日期 | 2026-08-29 |
| 定稿日期 | 2026-08-29 |
| 验收日期 | — |
| 关联文档 | docs/TODO.yaml 的 C6；PRD-C5（file 链接兼容）；AGENTS.md |

## 1. 背景与目标

### 1.1 背景

当前 Skill 的用户入口如果只使用 `/skill-name`，它与普通命令、路径文本和未来扩展没有清晰边界。客户端只能把它当普通文本或输入补全处理，消息历史刷新后也没有可靠的结构化信息来恢复 Skill 的特殊 UI。

当前客户端的 Markdown 配置集中在：

- `E:\binn\ftre-desktop\packages\renderer\src\lib\markdown-plugins.ts`
- `E:\binn\ftre-desktop\packages\renderer\src\features\chat\AssistantMessage.tsx`
- `E:\binn\ftre-desktop\packages\renderer\src\features\inspector\renderers\MarkdownPreview.tsx`
- `E:\binn\ftre-desktop\packages\renderer\src\features\chat\ChatMessageList.tsx`

当前后端 Skill 能力集中在：

- `E:\ftre\src\ftre\plugins\builtin\skill\service.py`
- `E:\ftre\src\ftre\plugins\builtin\skill\plugin.py`
- `E:\ftre\src\ftre\plugins\builtin\skill\router.py`
- `E:\ftre\src\ftre\plugins\builtin\skill\tool.py`

本阶段不要求每种扩展都创建一个 Plugin。后端首期继续由现有 `skill` Plugin 装配 Skill Service、扩展注册表、Skill Handler 和 `agent/pre-step` Hook；通用协议放在稳定 Package 中，未来扩展只需注册新的 Handler/Renderer。

### 1.2 目标

建立一套前后端一致、可持久化、可安全降级的 Ftre Markdown 扩展协议，使消息中的扩展引用能够：

1. 被客户端 Markdown 渲染器识别并显示为专用 UI；
2. 被后端 Agent 运行时解析、校验并交给对应 Handler；
3. 以结构化元数据持久化，刷新和重连后仍能恢复相同 UI；
4. 在不改 Agent ReAct、LLM、Tool 和 Session 基础协议的前提下，继续扩展 Skill、文件引用、Agent 引用、MCP 引用等能力。
5. 让已解析的 Skill 详情携带可验证的资源来源和能力，客户端可以打开真实 `.ftre` 文件并复用现有目录预览。

### 1.3 非目标

- 不把所有 Markdown 都改成自定义语法。
- 不允许客户端执行 Skill、Tool、MCP 或任意后端逻辑。
- 不通过 `ftre://` 加载网络资源、图片或 JavaScript。
- 不把扩展内容直接替换掉用户原始消息。
- 不修改 Agent ReAct 算法、LLM 协议、ToolService 执行协议、Inbox 队列语义或 Cordis Kernel。
- 不为每个扩展类型复制一套独立解析器和 Markdown 管线。
- 不要求首期实现 file、agent、mcp 等后续 Handler；首期只实现 Skill。
- 不在客户端维护 `ftre://` 到 `.ftre` 的路径映射，也不为首个资源类型引入通用 Electron 协议或完整 FileSystemProvider。

## 2. 核心协议

### 2.1 Canonical 语法

扩展使用 Markdown 图片形态作为承载语法，但 `ftre://` 是应用内协议，不是真实图片地址：

```md
![ftre:skill](ftre://v1/skill/review-code?path=src)
```

无参数时：

```md
![ftre:skill](ftre://v1/skill/lint)
```

消息中的完整示例：

```md
请检查这个目录 ![ftre:skill](ftre://v1/skill/review-code?path=src)
```

客户端最终渲染为 Skill Card/Chip，而不是 `<img>`；用户可以展开查看类型、名称和参数。

### 2.2 语法约束

```text
![ftre:<type>](ftre://<version>/<type>/<name>?<query>)
```

约束如下：

- `version` 当前固定为 `v1`，未知版本按普通 Markdown 文本安全降级。
- `type` 使用小写 kebab-case，例如 `skill`、`file`、`agent`、`mcp`。
- `name` 使用小写 kebab-case；名称必须经过对应 Handler 的存在性和权限校验。
- query 参数使用标准 URL percent-encoding；同名参数取最后一个值或由 Handler 明确拒绝，规则必须固定。
- alt 文本只用于显示提示，不作为执行依据；执行依据来自 `ftre://` URI。
- 单个引用最大 8 KiB，单条消息最多 32 个扩展引用；超限引用按普通文本处理并记录诊断。
- URI 中禁止 `javascript:`、`data:`、`http:`、`file:` 等跳转语义；`ftre://` 只允许被 Ftre 扩展管线消费。
- 参数包含 `)`、空白或非 ASCII 字符时必须 percent-encode；客户端生成器负责编码，用户手写错误不得导致 Markdown 或解析器崩溃。

### 2.3 结构化模型

前后端都使用等价的 `ExtensionRef` 模型：

```text
ExtensionRef
├─ version: "v1"
├─ type: string
├─ name: string
├─ args: map[string, string]
├─ raw: string
└─ span: { start: integer, end: integer }
```

解析结果必须是确定性的：同一原文、同一协议版本得到相同的 `type/name/args/span`。`raw` 保留原始片段，便于持久化、调试和未知扩展降级。

### 2.4 消息元数据

用户消息继续保存原始 Markdown 文本，同时增加可选的扩展投影：

```json
{
  "source": { "kind": "user" },
  "content": "请检查这个目录 ![ftre:skill](ftre://v1/skill/review-code?path=src)",
  "extensions": [
    {
      "version": "v1",
      "type": "skill",
      "name": "review-code",
      "args": { "path": "src" },
      "raw": "![ftre:skill](ftre://v1/skill/review-code?path=src)",
      "span": { "start": 9, "end": 66 }
    }
  ]
}
```

后端注入的 Skill 内容使用现有消息机制表达，但必须带结构化来源：

```json
{
  "source": {
    "kind": "extension-invocation",
    "type": "skill",
    "name": "review-code",
    "args": { "path": "src" },
    "invocation_id": "<user_message_id>:<span_start>:v1"
  }
}
```

`invocation_id` 用于 Hook 重试、重连和恢复时去重；原始用户消息不能被注入内容覆盖。
注入消息使用稳定的隐藏消息 ID 单独持久化，扩展 Hook 重放时按该 ID 幂等 upsert；
因此原文、扩展投影和运行时注入消息互不覆盖，也不会因重连重复出现。

### 2.5 Skill 定义、文件形态与审查规范（强制）

本节是 Skill 的唯一发现和审查规范。`SkillService`、Skill HTTP API、输入框候选、`skill` Renderer 以及
`SkillHandler` 都必须遵守本节；任何实现不得用“目录下所有 Markdown 文件”代替 Skill 发现。

#### 2.5.1 什么是 Skill

Skill 是一个可复用的、面向特定任务的指令包。它由可识别的元数据和供 Agent 在激活后读取的正文组成，
可选地携带脚本、参考资料和静态资源。Skill 不是任意 README、设计文档、示例文件或普通 Markdown 文件。

Skill 的生命周期必须分为两层：

1. **发现（catalog/discovery）**：只读取候选文件的路径和 YAML frontmatter，生成名称、描述、来源、作用域和调用策略等摘要；不得加载整篇正文，不得执行脚本或引用资料。
2. **激活（load/resolve）**：用户或模型明确调用后，按 winner 读取 `SKILL.md`/平铺文件正文，并由 `SkillHandler` 生成结构化注入消息；脚本、`references/` 和 `assets/` 只有在正文明确引用且通过 Tool/Permission 流程后才能访问。

这样可以保证 `/` 菜单和 `GET /skills` 足够快，也避免把仓库中的文档误当成可执行能力。

#### 2.5.2 两种且仅两种合法文件形态

每个 Skill root 只允许以下两种候选形态，发现深度严格为一层：

```text
<skill-root>/
├─ <skill-name>/
│  ├─ SKILL.md                 # 目录形态（唯一入口文件）
│  ├─ references/              # 可选：参考资料
│  ├─ scripts/                 # 可选：脚本，不能在发现阶段执行
│  ├─ assets/                  # 可选：静态资源
│  └─ README.md / LICENSE      # 可选说明，不是 Skill 入口
└─ <skill-name>.md             # 平铺形态（单文件 Skill）
```

必须满足以下发现规则：

- 目录形态只接受 `<skill-root>/<name>/SKILL.md`；`<skill-root>/SKILL.md`、`<skill-root>/<name>/README.md`、`<skill-root>/<name>/reference/**/SKILL.md` 均不是候选。
- 平铺形态只接受 `<skill-root>/<name>.md`，且文件必须直接位于 root；root 下的 `README.md`、`CHANGELOG.md`、`LICENSE.md`、`reference.md` 等必须忽略。
- 不递归扫描 `**/*.md` 或 `**/SKILL.md`；`references/`、`scripts/`、`assets/` 内的任何 Markdown 都不能出现在 Skill 列表。
- `SKILL.md` 大小写按规范固定为大写文件名；其他大小写变体（如 `skill.md`）不得作为目录入口。
- 候选枚举必须排序后处理（路径或规范化名称的字典序），相同输入得到相同输出；坏文件不能阻断其它候选。
- 符号链接必须解析后仍位于声明的 root 内；越界链接、特殊文件、FIFO 和目录遍历路径直接拒绝。

Ftre 首期的 root 顺序和优先级固定如下（数字越小越优先）：

| priority | scope | root |
|---:|---|---|
| 10 | workspace | `<workspace>/.ftre/skills/` |
| 20 | agent | `~/.ftre/agents/<agent_id>/skills/` |
| 30 | global | `~/.ftre/skills/`（由 `CONFIG_PATH.parent / skills` 解析） |
| 40 | Codex user | `~/.codex/skills/`（模型可见 root alias `r0`） |
| 50 | Agents user | `~/.agents/skills/`（模型可见 root alias `r1`） |
| runtime | plugin | `SkillService.register()` 显式提供，必须携带 owner、scope 和 priority |

同名 Skill 按 `(priority, owner, name)` 排序取唯一 winner；被遮蔽候选只能通过诊断/`sources()` 查看，不能在普通列表中重复展示。

#### 2.5.3 `SKILL.md` frontmatter 契约

目录形态和平铺形态都必须以 YAML frontmatter 开始，并以单独的 `---` 结束：

```yaml
---
name: review-code
description: Review source code and report correctness, security, and maintainability issues.
license: MIT                         # optional
compatibility: python 3.12           # optional, max 500 characters
metadata:                             # optional, string-to-string map
  owner: platform
  version: "1"
allowed-tools: "Read, Grep"           # optional/experimental; never grants permission by itself
whenToUse: "用户明确要求代码审查时"       # optional display hint
user-invocable: true                  # optional, default true
disable-model-invocation: false       # optional, default false
---

# Review code

具体步骤、示例、边界条件和输出格式……
```

规范化和校验要求：

- `name` 和 `description` 为必填；缺失、空值、非字符串或 YAML 解析失败时，整个候选无效并进入诊断，不得以路径名补全。
- `name` 必须是 1–64 个字符的小写 kebab-case：`^[a-z0-9]+(?:-[a-z0-9]+)*$`；不能有前导/尾随短横线或连续短横线。
- 目录形态的目录名必须与 frontmatter `name` 完全一致；平铺形态的文件 basename（去掉 `.md`）也必须与 `name` 一致，防止显示名和调用名漂移。
- `description` 必须为非空字符串，最多 1024 个字符；它用于目录摘要和选择器，不作为执行指令。
- `compatibility` 若存在最多 500 个字符；`metadata` 必须是扁平字符串键值映射；未知字段可以保留在诊断数据中，但不能改变调用权限或执行行为。
- `license`、`compatibility`、`metadata`、`allowed-tools`、`whenToUse` 是描述性/实验字段；真正的 Tool 权限仍由 ToolService/PermissionService 决定。
- `user-invocable` 和 `disable-model-invocation` 只接受 YAML boolean；为兼容现有配置，可接受大小写不敏感的 `true/false`、`yes/no`、`on/off`、`1/0` 字符串。其它字符串、数字、列表或对象视为非法 frontmatter，不能静默转换。
- `user-invocable` 缺省为 `true`；`disable-model-invocation` 缺省为 `false`。最终输出同时提供正向字段 `user_invocable` 与 `model_invocable = not disable-model-invocation`，避免消费者各自推断。
- 正文不承担元数据职责。正文建议不超过 500 行，以步骤、示例、失败处理和输出格式为主；需要的详细资料放入 `references/`，并由正文中的相对路径引用。

#### 2.5.4 发现算法（伪代码）

实现必须等价于以下算法，不能用递归 Markdown 扫描替代：

```python
for entry in sorted(skill_root.iterdir(), key=lambda p: p.name):
    if entry.is_dir() and entry.name != ".system":
        candidate = entry / "SKILL.md"
        if candidate.is_file() and candidate.name == "SKILL.md":
            inspect(candidate, kind="dir", declared_name=entry.name)
    elif entry.is_file() and entry.suffix == ".md" and entry.name != "SKILL.md":
        inspect(entry, kind="file", declared_name=entry.stem)
```

`inspect()` 的顺序固定为：路径 containment 检查 → UTF-8 读取 → 完整 YAML 解析 → 必填字段和名称校验 → 调用策略解析 → 生成摘要。
任何一步失败都返回结构化诊断（root、path、reason、line/column），并继续处理下一个 entry。发现阶段不得读取候选目录中的 README、references、scripts 或 assets。

#### 2.5.5 SkillService 的公开语义

`SkillService` 是 Skill 文件系统和作用域解析的唯一 owner，至少提供以下语义：

| API | 返回 | 约束 |
|---|---|---|
| `list(agent_id, workspace)` | 去重后的 winner 摘要数组 | 不返回正文；只包含有效 Skill；确定性排序 |
| `get(name, agent_id, workspace)` | winner 的完整 `SkillRecord` | 按需读取正文；再次校验文件未越界且 metadata 有效 |
| `sources(name, ...)` | winner 与 shadowed 候选 | 仅诊断使用，不改变 winner |
| `diagnostics` / `GET /skills/diagnostics` | 最近一次发现失败的结构化诊断 | 只读；包含 root、scope、path、reason，YAML 错误附 line/column |
| `scan_roots(agent_id, workspace)` | 当前请求的有序文件系统 root 快照 | 返回稳定 alias、scope、绝对 path、priority 和 exists；只描述发现范围，不读取正文；runtime Skill 不伪造文件 root |
| `register(skill, owner, scope, priority)` | 幂等 disposer | runtime 贡献必须显式声明 owner/scope/priority |
| `create/update/delete` | 全局 root 的 CRUD 结果 | 只能生成两种合法文件形态；原子写入并保留 frontmatter |

`list()`、HTTP `GET /skills` 和输入框候选只能暴露 `name/description/scope/kind/updated_at/user_invocable/model_invocable/disabled` 等摘要字段。
只有明确的 `get()`/详情请求才允许读取正文；不能因 hover、补全或刷新 Session 自动把所有 Skill 正文注入 Agent。

模型可见的 Skill 使用说明由 Skill Plugin 注册到 `SystemPromptService`，并按当前 Agent/工作区动态生成两部分：

- `### 技能根`：列出 `workspace`、`agent`、`global`、`r0`、`r1` 等稳定 alias 对应的绝对路径、scope 和优先级；没有工作区时不生成 workspace root。`r0` 固定映射用户 `~/.codex/skills`，`r1` 固定映射用户 `~/.agents/skills`。
- `### 扫描范围`：明确每个 root 只扫描一层，只接受 `<root>/<skill-name>/SKILL.md` 或 `<root>/<skill-name>.md`；root 级 `SKILL.md`、README、LICENSE、reference、references、scripts、assets 不属于 Skill 入口。

这两个章节只是发现协议和定位提示，不授予模型直接读取文件或执行脚本的权限。正文仍必须通过 `loadSkill` 或宿主注入的 canonical `ftre://v1/skill/...` 内容激活。

#### 2.5.6 调用策略和各层边界

```text
客户端 / 输入框
  └─ 只展示 user_invocable=true 且未 disabled 的摘要，生成 canonical token
后端 pre-step / SkillHandler
  └─ 只处理 source.kind=user 的 canonical token
  └─ SkillService.get() → 重新校验存在性、scope、调用策略和权限
  └─ 生成 extension-invocation UserMessage
Agent / ToolService
  └─ 读取已解析正文；脚本执行必须走 ToolService + PermissionService
```

- `user-invocable=false` 的 Skill 不出现在用户选择器，但仍可由 `get()` 供受信任的内部流程读取。
- `disable-model-invocation=true` 的 Skill 不注入模型可见目录，也不能由模型自动选择；用户显式调用仍需通过 `user-invocable` 和权限检查。
- `disabled`、不存在、frontmatter 无效、scope 不匹配或权限拒绝时，canonical token 保留为普通文本并返回 `accepted=false`；不能猜测相近 Skill，也不能执行降级脚本。
- 客户端渲染只使用持久化的 `ExtensionRef/display`；不读取本地 Skill 目录、不执行脚本、不根据 alt 文本调用后端。

#### 2.5.7 资源、安全和变更规则

- 发现和 `list()` 是只读操作，不执行 `scripts/`，不解析 references 的指令，不发起网络请求。
- 激活时所有资源路径必须相对 Skill 目录并通过 realpath containment；禁止 `..`、绝对路径、符号链接越界和隐式工作区切换。
- 资源变更（README、references、scripts、assets）不改变目录摘要；`SKILL.md` 或平铺文件 frontmatter/body 变化才触发该 Skill 的版本/`updated_at` 变化。
- 历史消息保存 raw、display 和 invocation_id；Skill 文件后续修改不回写历史消息，恢复时不得用当前正文替换历史正文。
- 日志记录 `root/scope/name/path/reason` 等诊断信息，不记录完整 Skill 正文、密钥或脚本输出；非法候选不能让 HTTP 列表返回 500。

#### 2.5.8 审查清单（每次新增或修改 Skill 必须逐项确认）

1. **形态**：是否为 `<name>/SKILL.md` 或 root 直下 `<name>.md`，没有把 README/reference 当入口？
2. **命名**：目录名、文件 basename 与 frontmatter `name` 是否一致且符合 kebab-case/长度限制？
3. **元数据**：是否有非空 `description`，YAML 是否可解析，布尔字段是否为合法类型？
4. **正文**：是否说明何时使用、步骤、输入输出、失败处理和边界条件，且没有把权限声明伪装成正文指令？
5. **资源**：references/scripts/assets 是否按相对路径组织，是否存在越界链接或发现阶段执行代码？
6. **调用策略**：`user-invocable`、`disable-model-invocation`、disabled 状态是否与预期一致？
7. **作用域**：放置的 root 是否正确，重名时 winner/shadowed 是否符合优先级？
8. **安全**：Skill 来源是否可信，脚本是否通过 ToolService/PermissionService，是否避免网络、密钥和破坏性操作？
9. **可恢复性**：删除/修改 Skill 后，历史消息和 `invocation_id` 是否保持不变？
10. **测试**：是否覆盖合法、非法、嵌套、重名、策略隐藏、CRUD 原子写入和刷新/重连场景？

本规范参考 Agent Skills 的官方文件格式和渐进式加载原则（`SKILL.md` + 可选资源），并与 DSH 的
`packages/skill/skill-filesystem` 一层发现、frontmatter 校验和 provider registry 语义对齐：

- Agent Skills Specification：<https://github.com/agentskills/agentskills/blob/main/docs/specification.mdx>
- DSH filesystem provider：`E:\deepseek-harness\packages\skill\skill-filesystem\src\index.ts`
- DSH filesystem provider README：`E:\deepseek-harness\packages\skill\skill-filesystem\README.zh.md`
- DSH registry README：`E:\deepseek-harness\packages\skill\skill\README.zh.md`

### 2.6 资源 URI、来源与能力契约（C6.5）

`ftre://` 只表示资源身份，不表示本地路径。Skill 的 canonical URI 固定为：

```text
ftre://v1/skill/<name>
```

URI 不携带绝对路径；`agent_id`、`workspace` 等解析上下文由 HTTP 请求或当前 Session 提供。后端按
2.5.2 的优先级选出唯一 winner 后，详情接口返回来源和能力：

```json
{
  "uri": "ftre://v1/skill/cron-watch",
  "media_type": "text/markdown",
  "revision": "mtime:1724930000.123456",
  "source": {
    "kind": "filesystem",
    "path": "C:/Users/user/.ftre/skills/cron-watch/SKILL.md"
  },
  "capabilities": {
    "read": true,
    "browse": true,
    "write": false
  }
}
```

运行时注册、没有可验证文件来源的 Skill 使用 content source：

```json
{
  "source": { "kind": "content" },
  "capabilities": { "read": true, "browse": false, "write": false }
}
```

约束：

- `source.kind=filesystem` 时，`path` 必须是后端 realpath 校验后的现有文件，并位于声明的 Skill root 内；
- `source.kind=content` 只能提供内容快照，不能伪造路径；
- `capabilities` 是 UI 能力声明，不授予写权限；写操作仍由 ToolService/PermissionService 负责；
- `revision` 用于内容快照缓存和刷新判断，不要求客户端访问 Skill 目录；
- 客户端只能消费后端返回的 `source`，禁止字符串拼接、`Path.cwd()` 回退或把 `ftre://` 交给 fs IPC；
- 当前只有 Skill 一种资源，不新增通用 `ResourceResolver`、`ResourceTab`、Electron custom scheme 或完整
  FileSystemProvider；第二种资源出现时再抽取公共 Resolver/Provider 契约。

## 3. 架构与边界

### 3.1 总体关系

```text
用户输入 / 历史消息
        │
        ├─ 客户端：Markdown AST → FtreExtensionRegistry → Renderer
        │                                      └─ SkillCard / FileCard / ...
        │
        └─ 后端：agent/pre-step → FtreExtensionParser → HandlerRegistry
                                               └─ SkillHandler → SkillService
                                                               → 注入 UserMessage
```

### 3.2 Package、Service、Plugin、Hook 归属

| 层 | 责任 | 首期归属 |
|---|---|---|
| `ftre-inline-extension` Package | 协议模型、语法解析、规范化序列化、错误类型 | `E:\ftre\packages\ftre-inline-extension\` |
| `InlineExtensionService` | Handler 注册、按 type 查找、生命周期 disposer、解析调用入口 | 由现有 `skill` Plugin 提供，稳定 key 为 `inline_extensions` |
| Skill Handler | 校验 Skill、读取 SkillService、生成 Agent 注入消息 | `E:\ftre\src\ftre\plugins\builtin\skill\` |
| `agent/pre-step` Hook | 在用户消息进入本轮 Agent 后触发扩展处理 | `skill` Plugin 注册；只处理 `source.kind=user` |
| 客户端 Renderer Registry | 按 type 选择 React Renderer，未知类型安全降级 | `E:\binn\ftre-desktop\packages\renderer\src\lib\` |
| Session/Event Projection | 保存原文、扩展投影和隐藏注入快照，保证历史恢复 | 现有 Session Message 投影；注入快照使用 `extension_<invocation_id>` 幂等 upsert，不新增第二存储 |

首期可以由一个 `skill` Plugin 完成装配，但不能把协议模型命名为 Skill 专用类型。未来非 Skill Plugin 通过 `inline_extensions.register()` 贡献 Handler，不得直接 import Skill 私有实现。

### 3.3 后端生命周期

```text
Channel 接收 user/message
  ↓
Session 持久化原始内容
  ↓
AgentService / agent/pre-step
  ↓
InlineExtensionService.parse(content)
  ↓
逐个按 invocation_id 去重并查找 Handler
  ↓
SkillHandler.resolve(ref, context)
  ↓
生成 extension-invocation UserMessage
  ↓
以 `extension_<invocation_id>` 幂等 upsert 到 Session（`hide=true`）
  ↓
Agent 继续正常 reasoning
```

以下内容不能触发后端执行：

- Assistant 消息中的扩展文本；
- ToolResult、系统提示词、压缩摘要中的扩展文本；
- 前端仅用于预览的未提交草稿；
- 未注册、被禁用、无权限或版本不支持的扩展。

## 4. 后端接口定义

### 4.1 `ExtensionParser`

```python
class ExtensionParser(Protocol):
    def parse(self, text: str) -> tuple[ExtensionRef, ...]: ...
    def serialize(self, ref: ExtensionRef) -> str: ...
```

要求：

- `parse` 只做语法识别和 URL 解码，不读取文件、不调用 SkillService、不执行 Tool。
- 解析失败返回空结果或诊断，不抛出会中断 Agent 的异常。
- `serialize(parse(text)[0])` 输出 canonical 形式；参数顺序稳定。

### 4.2 `InlineExtensionService`

```python
class InlineExtensionService(Protocol):
    def register(
        self,
        handler: "InlineExtensionHandler",
        *,
        owner: str,
        priority: int = 100,
    ) -> Callable[[], bool]: ...

    def parse(self, text: str) -> tuple[ExtensionRef, ...]: ...

    async def resolve(
        self,
        ref: ExtensionRef,
        *,
        context: ExtensionContext,
    ) -> "ExtensionResolution": ...
```

`register` 返回幂等 disposer，必须绑定 Plugin Effect。相同 `type/name` 的 Handler 按 priority 和 owner 产生唯一 winner，冲突需要记录诊断。

### 4.3 `InlineExtensionHandler`

```python
class InlineExtensionHandler(Protocol):
    type: str

    async def resolve(
        self,
        ref: ExtensionRef,
        *,
        context: ExtensionContext,
    ) -> "ExtensionResolution": ...
```

```text
ExtensionContext
├─ session_id
├─ agent_id
├─ workspace
├─ user_message_id
└─ signal

ExtensionResolution
├─ accepted: bool
├─ display: { label, description, icon? }
├─ message: UserMessage | None
├─ reason: string | None
└─ invocation_id: string
```

Handler 边界：

- 只负责当前扩展类型的业务校验和解析，不负责修改原始用户消息。
- 拒绝时返回 `accepted=false`，原文继续作为普通文本，不让单个未知 Skill 破坏整轮 Agent。
- 可产生一条结构化 `extension-invocation` UserMessage；不能直接调用 LLM、Tool 或 Channel。
- 必须使用 `context.signal` 支持取消；不可在后台遗留任务。

### 4.4 Skill Handler

首期 Handler 从 `SkillService` 读取 Skill：

```text
type = "skill"
name = ref.name
args = ref.args
```

校验顺序：

1. 名称和参数格式校验；
2. `SkillService.get()` 查找当前 Agent/工作区 winner；
3. 检查 `user_invocable`、来源和权限；
4. 生成带 `invocation_id` 的 Skill 注入 UserMessage；
5. 对同一用户消息和 span 重复调用直接返回已有结果。

现有 `/skill-name` 可以在迁移期继续作为输入补全手势，但不再作为后端执行协议。只有 canonical `![ftre:skill](ftre://...)` 才进入扩展 Handler。

### 4.5 HTTP/API

现有 `GET /skills` 和 `GET /skills/{name}` 继续保留，用于客户端 Skill 列表和展开详情；不新增按任意 URL 执行扩展的 API。

`GET /skills/{name}` 接收 `agent_id`、`workspace` 查询参数，返回 2.6 的 `uri/source/capabilities/revision`
字段以及完整正文。列表接口仍只返回摘要，不暴露绝对路径和正文。

如客户端需要通用展示元数据，增加只读接口：

```http
POST /extensions/resolve
```

请求只允许传入已解析的 `ExtensionRef` 和当前 `session_id`，返回 `ExtensionResolution.display`，绝不执行 Agent、Tool 或 Skill 注入。该接口不是首期必需项，首期优先使用消息中持久化的 display 元数据。

### 4.6 Skill 详情解析契约

```python
class SkillService:
    def get(self, name: str, agent_id: str = "default", workspace: str | None = None) -> SkillRecord | None: ...
    def serialize(self, item: SkillRecord, agent_id: str = "default") -> dict[str, Any]: ...
```

`serialize()` 是唯一的 HTTP 详情投影入口，必须生成稳定 `ftre://v1/skill/<name>`、`text/markdown`、
内容 revision、`source` 和 `capabilities`。Handler 仍只调用 `get()`，不读取文件；HTTP 路由不自行拼接路径。

## 5. 客户端 Markdown 集成

### 5.1 统一渲染入口

现有 `remarkPlugins`、`rehypePlugins` 和 `urlTransform` 是共用入口。首期新增：

```text
remarkFtreExtensions
FtreExtensionRegistry
FtreExtensionComponents
```

以下位置必须使用同一套配置，避免聊天、摘要、文件预览出现语义漂移：

- `AssistantMessage.tsx`
- `MarkdownPreview.tsx`
- `ChatMessageList.tsx` 的摘要渲染
- 未来新增的 Markdown 消息视图

### 5.2 Renderer Registry

```ts
export interface FtreExtensionRef {
  version: "v1";
  type: string;
  name: string;
  args: Record<string, string>;
  raw: string;
}

export interface FtreExtensionRenderer {
  type: string;
  render(ref: FtreExtensionRef, context: { compact: boolean }): React.ReactNode;
}

export interface FtreExtensionRegistry {
  register(renderer: FtreExtensionRenderer): () => boolean;
  get(type: string): FtreExtensionRenderer | undefined;
}
```

首期 `skill` Renderer：

- 默认显示单行 Skill Chip；
- 点击展开名称、参数、来源和描述；
- 未找到描述时仍显示名称，不阻塞消息渲染；
- `compact=true`（侧边栏/摘要）只显示短标签；
- 不触发后端执行，不发起任意 URL 请求。

### 5.3 Markdown AST 处理

扩展解析优先在 Markdown AST 层完成，不对完整消息反复执行全量正则：

1. 识别 `image` 节点的 `src` 是否为 `ftre://v1/...`；
2. 解析并校验 type/name/query；
3. 交给 `FtreExtensionRegistry`；
4. 已注册类型渲染专用组件；
5. 未注册或解析失败时渲染安全的原始文本/普通 Markdown，不渲染为图片。

`urlTransform` 只放行 `ftre` 给内部组件，内部组件必须再次验证版本和路径；不能把 `ftre://` 直接交给浏览器导航。

### 5.4 输入框行为

- Skill 选择器生成 canonical token，而不是插入 `/skill-name`。
- Slate/输入编辑器把 token 当作可删除的 inline atom 或受保护文本区间；复制和提交时还原为原始 Markdown。
- 用户手写合法 token 时，输入框可即时显示 Chip；未闭合或非法 token 保持普通文本。
- 发送内容不由客户端改写；后端收到的文本必须与用户提交内容一致。

### 5.5 流式和性能

- 复用现有 `splitBlocks` 和 `React.memo`，只重新解析变更中的 Markdown block。
- 扩展解析复杂度为 O(n)，不得为每个 Renderer 再扫描整条消息。
- 单 block 最多渲染 32 个扩展引用；超限使用普通文本降级。
- Renderer 必须是纯展示组件；展开状态保存在客户端，不写入 Session。
- Assistant 流式输出中出现未闭合 token 时不提前渲染；token 完整后再替换为组件。

### 5.6 文件预览映射

Skill Renderer 点击详情后复用现有 `openFilePreview()`/`FileRenderer`，不新建 Skill 专用 Tab：

```text
source.kind=filesystem
  → filePath = source.path
  → content = detail.content（快照）
  → 允许 PreviewHeader 的目录面包屑和 FilePathPopover

source.kind=content
  → 使用 ftre://v1/skill/<name>/SKILL.md 作为只读显示标识
  → 只使用 content 快照，不调用 fs:readFile/stat/listDirectory
```

`FileRenderer` 的文件系统操作由资源能力控制，而不是按 Skill 类型全局禁用。真实文件来源可展开其
`.ftre` 父目录；内容来源没有目录语义。两种来源均不得回退到当前工作目录。

## 6. 持久化、恢复与安全

### 6.1 持久化规则

- 原始 user/message 永远保留；`extensions` 是可重建投影，不是替代正文。
- Skill 注入消息作为独立、隐藏的 UserMessage 快照持久化，source.kind 为 `extension-invocation`，消息 ID 为 `extension_<invocation_id>`。
- Session 刷新时优先使用已持久化的 `extensions` 元数据；旧消息没有元数据时可重新解析，但结果必须与 v1 parser 一致。
- `invocation_id` 在恢复、WS 重放和 Hook 重试中保持稳定；重复事件只能命中同一隐藏消息的幂等 upsert，不得重复注入。
- Skill 文件变化只影响下一次解析，不修改历史消息中的 raw 和 display。

### 6.2 安全边界

- 只有 `source.kind=user` 的消息允许触发 Handler；Assistant 输出中的相同文本只做展示。
- 前端不根据 alt 文本执行任何能力。
- 后端 Handler 必须重新做存在性、用户可调用性、工作区和权限检查，不能信任客户端传入的 display。
- 禁止路径穿越、协议跳转、任意 HTTP 请求和 HTML 注入；所有 query 先 URL 解码，再按 Handler schema 校验。
- 日志记录 `session_id、message_id、invocation_id、type、name、accepted/reason`，不得记录敏感 Skill 正文或密钥。

### 6.3 来源和 IPC 安全

- SkillService 在返回 filesystem source 前执行 realpath containment，拒绝越界符号链接和不存在文件；
- Renderer 不把 URI 当作操作系统路径，Electron fs IPC 只接收已解析的真实 path；
- `source.path` 只用于本地受控文件预览，目录浏览仍由现有 `FilePathPopover`/`fs:listDirectory` 完成；
- content source 的预览必须跳过 `fs:readFile`、`fs:stat`、`fs:listDirectory` 和 git 查询；
- 不使用 `file://` 或新的自定义协议加载 Skill 内容，避免把任意文件权限扩大到浏览器层。

## 7. 分阶段实施计划

### 阶段 C6.1：协议与黄金测试

范围：

- 创建 `packages/ftre-inline-extension/`，实现 `ExtensionRef`、parser、serializer 和错误类型；
- 固定 v1 语法、编码、长度限制、未知版本降级和 invocation_id 规则；
- 建立前后端共享的 JSON fixtures/黄金用例；
- 不接入 Agent、不改客户端 UI。

验证：

- 合法/非法/未知版本/参数编码/超长 token 的解析结果符合协议；
- canonical serialize 具有确定性；
- Python 单元测试和 TypeScript fixture 测试通过；
- `python -m ruff check src tests packages`、客户端 TypeScript 检查通过。

提交门槛：

- 只提交协议 Package、fixture 和 PRD 变更；
- 评审通过后才能进入 C6.2。

### 阶段 C6.2：客户端通用 Markdown Renderer

范围：

- 在 `packages/renderer/src/lib/` 增加 Ftre Markdown 扩展 registry、AST 适配和安全 URL 处理；
- Skill Renderer 首期只展示静态 Chip/Card；
- 接入 `AssistantMessage`、`MarkdownPreview`、`ChatMessageList` 和输入框 token 插入；
- 复用 streaming block/memo 机制，不增加全量扫描。

验证：

- `![ftre:skill](ftre://v1/skill/review-code?path=src)` 渲染为 Skill Card，不创建 img 请求；
- 普通图片、file 链接、HTTP 链接行为不回归；
- 未知 type、未知 version、非法 query 安全降级；
- 流式未闭合 token 不破坏布局，消息大文本渲染无明显性能回归；
- `pnpm --filter @ftre/renderer test`、`pnpm --filter @ftre/renderer build` 通过。

提交门槛：

- 客户端测试和手动截图验收通过；
- 不修改后端 Agent 行为。

### 阶段 C6.3：后端 Registry、Skill Handler 与 pre-step

范围：

- 由现有 `src/ftre/plugins/builtin/skill/plugin.py` 装配 `InlineExtensionService`；
- 注册通用 parser/handler registry 和可逆 disposer；
- 增加 Skill Handler，在 `agent/pre-step` 解析 canonical token、校验 Skill 并注入独立 UserMessage；
- 将 `SkillService` 的发现器收口为 2.5 节规定的一层算法，移除递归 Markdown 兜底；
- 用完整 YAML frontmatter 校验替换“路径名 + 简单字符串读取”的宽松兜底，暴露结构化坏文件诊断；
- 继续提供现有 Skill Tool、Skill HTTP 列表和详情接口；
- `/skill-name` 只保留为迁移期输入补全，不再作为后端执行协议。

验证：

- 用户消息包含合法 Skill token 时只注入一次 Skill 内容；
- Assistant、ToolResult、系统提示词和压缩文本中的 token 不会执行；
- 未知/禁用/无权限 Skill 保持普通文本；
- 同一 message_id/span 的 Hook 重试不重复注入；
- Skill Handler 只通过 SkillService 获取内容，不直接读取目录或操作 Agent 私有状态；
- `GET /skills`、输入框候选不再出现 README、LICENSE、reference、scripts、assets 或嵌套 `SKILL.md`；
- 缺失/非法 frontmatter 的候选只进入诊断，不会让列表接口失败或被路径名伪装成 Skill；
- 后端 pytest、Ruff、架构扫描全部通过。

提交门槛：

- 增加 pre-step、幂等、权限和取消回归测试；
- 通过 Agent/Session 集成验收后进入 C6.4。

### 阶段 C6.4：持久化投影、恢复与跨端收尾

范围：

- 在 UserMessage 投影中保存 `extensions`；
- 持久化 `extension-invocation` source 和 invocation_id；
- WS 实时事件、HTTP 历史加载、刷新和重连使用同一结构化字段；
- 清理旧的 Skill 专用 UI 分支和重复解析逻辑；
- 增加未来 `file` 或测试扩展的最小注册样例，证明协议不是 Skill 硬编码。

验证：

- 发送消息 → Skill 注入 → 强制刷新/重连后，Skill Card 和展开信息不消失；
- 重放历史不会重复产生 UserMessage 或再次执行 Handler；
- 同一消息在聊天正文、摘要、Inspector 预览中显示一致；
- 删除/卸载 Skill Plugin 后，基础 Markdown 仍可渲染，Skill token 安全降级；
- 全量后端/客户端测试、构建、架构扫描和手动验收通过。

提交门槛：

- PRD 验收项逐条留痕；
- 更新 `docs/TODO.yaml` 为 done、PRD 状态为已验收、`CHANGELOG.md` 追加未发布记录。

### 阶段 C6.5：Skill 资源来源解析与文件预览收口

范围：

- `SkillService.serialize()` 增加 canonical `uri`、`media_type`、`revision`、`source`、`capabilities`；
- `GET /skills/{name}` 继续使用 `agent_id/workspace` 解析 winner，不新增通用资源路由；
- 客户端 `fetchSkill()` 传递当前 Agent/工作区上下文并映射详情契约；
- Skill Renderer 对 filesystem/content source 选择真实文件预览或只读内容快照；
- 删除 Skill Renderer 合成 filesystem 假路径的逻辑，保留仅用于 content source 的虚拟显示标识；
- 保留现有 FileRenderer，不新增 Skill Tab、通用 Resolver 或 Electron custom scheme；
- 增加跨 root、真实目录展开、content source 不触发 fs IPC 的回归测试。

验证：

- workspace/agent/global 同名 Skill 的详情返回实际 winner 的绝对 `SKILL.md` 路径；
- 点击 workspace 或 global Skill 后，PreviewHeader 能展开其真实父目录，且不再出现 `ENOENT ftre://...`；
- runtime Skill 没有文件路径时仍可打开正文，但不会调用任何文件系统 IPC；
- 刷新/重连后同一 URI 仍解析为当前上下文的 winner，revision 变化只影响下一次详情请求；
- 旧后端缺少新字段时客户端安全降级为 content source，不阻塞消息渲染；
- 后端契约测试、客户端 renderer/FileRenderer 测试、类型检查和构建全部通过。

提交门槛：

- C6.5 代码、测试和本节变更记录一并提交；
- 逐条记录 AC17-AC21 的自动化结果；
- 不在本阶段引入第二种资源或通用资源基础设施。

## 8. 测试计划

### 8.1 协议单元测试

- 基本 Skill token、无参数 token、多参数 token；
- percent-encoding、中文参数、括号和空格；
- 普通图片和普通链接不误判；
- 未知 type/version、空 name、非法 kebab-case；
- 长度/数量上限、重复 query、canonical 序列化。

### 8.2 后端测试

- Registry 注册、冲突优先级和 disposer 幂等；
- Skill Handler winner、user-invocable、工作区和权限校验；
- Skill discovery 只识别 `<name>/SKILL.md` 和 root 直下 `<name>.md`，不会把 README、LICENSE、reference、scripts、assets 中的 Markdown 列入目录；
- 目录名/文件 basename 与 frontmatter `name` 不一致、缺少 `description`、YAML 损坏或布尔字段类型非法时，候选被拒绝且其它 Skill 仍可正常发现；
- `.ftre/skills`、agent、global 和 runtime scope 的优先级、winner/shadowed 诊断与确定性排序；
- `list()` 只返回摘要，`get()` 才读取正文；资源变更不污染目录摘要；
- Skill 详情返回 `uri/source/capabilities/revision`，filesystem source 能打开真实目录，content source 不触发 fs IPC；
- pre-step 只处理用户消息；
- invocation_id 去重、重试、WS 重放和恢复；
- 取消、Handler 异常、未知扩展不会阻断 Agent；
- 持久化 raw、extensions 和 extension-invocation source。

### 8.3 客户端测试

- AST 识别和 Renderer 注册/卸载；
- Skill Card 展开、紧凑摘要和无描述降级；
- `ftre://` 不触发浏览器网络请求；
- 输入框插入、删除、复制、提交 token；
- Assistant、用户消息、摘要和 MarkdownPreview 共用同一配置；
- Skill filesystem/content 两种详情来源的预览行为、上下文 winner 和旧后端降级；
- 流式未闭合 token、超长消息和多 token 性能回归。

### 8.4 手动验收

1. 在输入框选择 Skill，确认插入 canonical token 而非 `/skill-name`。
2. 发送后确认用户消息显示 Skill Card，点击可展开参数。
3. 确认 Agent 收到一次结构化 Skill 注入并继续执行。
4. 刷新 Session，确认 Card、名称和参数仍在，且没有重复注入。
5. 在 Assistant 文本中输出同样 token，确认只展示、不执行。
6. 卸载 Skill Plugin 或输入未知 type，确认消息仍可正常显示。

### 8.5 Skill 文件审查验收

使用临时 Skill root 构造以下目录，`GET /skills` 和输入框候选必须只出现两个合法 Skill：

```text
skills/
├─ review-code/SKILL.md             # 合法目录形态
│  ├─ README.md                     # 忽略
│  ├─ references/guide.md           # 忽略
│  └─ scripts/check.py              # 忽略
├─ lint.md                          # 合法平铺形态
├─ README.md                        # 忽略
├─ reference/SKILL.md               # 忽略（嵌套目录）
├─ nested/example/SKILL.md          # 忽略（超过一层）
└─ malformed/SKILL.md               # 缺失/非法 frontmatter，忽略并记录诊断
```

验收还必须覆盖：

1. 目录名和 `name` 不一致时拒绝，不使用路径名兜底；
2. `user-invocable: false` 不出现在输入框，但 `get()` 仍能返回受信任的详情；
3. `disable-model-invocation: true` 不进入模型目录；
4. 相同名称在 workspace/agent/global/runtime 同时存在时只显示 priority 最小的 winner；
5. 修改 README 或 references 不改变 `updated_at`/目录摘要，修改 `SKILL.md` 才更新候选；
6. 删除或重连后历史消息不重新读取当前 Skill 正文，不重复生成 invocation；
7. 任意坏文件、符号链接越界或资源路径穿越都不会导致列表接口 500。

## 9. 验收标准

- [ ] AC1：v1 canonical 语法、编码规则和 `ExtensionRef` 前后端一致。
- [ ] AC2：客户端 Markdown 在聊天、摘要、Inspector 中统一识别 `ftre://` 并渲染专用组件。
- [ ] AC3：`ftre://` 永不触发真实图片、网络或浏览器导航。
- [ ] AC4：Skill 是首个 Handler，但协议不包含 Skill 专用字段以外的硬编码分支。
- [ ] AC5：后端只在用户消息的 `agent/pre-step` 阶段处理扩展，未知扩展安全降级。
- [ ] AC6：原始用户消息、扩展投影和隐藏 `extension-invocation` UserMessage 独立保存。
- [ ] AC7：`invocation_id` 保证重试、重连、刷新和历史重放幂等。
- [ ] AC8：卸载 Skill Plugin 后，基础 Markdown 和普通 Agent 流程不受影响。
- [ ] AC9：后端 pytest/Ruff/架构扫描和客户端 test/build 全部通过。
- [ ] AC10：C6.1-C6.5 每阶段完成独立验证和提交，未完成阶段不得提前标记 done。
- [ ] AC11：Skill 发现严格遵守一层规则，只接受 `<name>/SKILL.md` 或 root 直下 `<name>.md`；README、LICENSE、references、scripts、assets 和嵌套 `SKILL.md` 永不出现在目录。
- [ ] AC12：每个候选都通过完整 frontmatter、name/description、目录名一致性和调用策略校验；坏候选只产生结构化诊断，不阻断其它候选。
- [ ] AC13：`list()`/HTTP/输入框只返回摘要，`get()`/激活才读取正文；发现阶段不执行脚本、不读取资源、不发起网络请求。
- [ ] AC14：workspace、agent、global、runtime 的 winner/shadowed 结果确定且可解释，重名 Skill 不重复展示。
- [ ] AC15：Skill 的调用权限与 Tool/Permission Service 分离；`allowed-tools` 等 frontmatter 描述字段不能自行授予权限。
- [ ] AC16：Skill 审查清单和 8.5 的目录、策略、资源、安全、刷新/重连用例均有自动化回归测试及验收记录。
- [x] AC17：Skill 详情返回稳定 `ftre://v1/skill/<name>`、`media_type`、`revision`、`source` 和 `capabilities`；客户端不自行映射 URI 到路径。
- [x] AC18：workspace、agent、global winner 的详情返回经过 containment 校验的真实 `SKILL.md` 路径，点击预览可展开真实父目录。
- [x] AC19：无 filesystem source 的 runtime Skill 使用 content source，只读预览不调用 fs IPC、git 查询或当前工作目录回退。
- [x] AC20：旧后端详情缺少新资源字段时，客户端安全降级为 content source；新后端异常不会阻塞 Markdown 消息渲染。
- [x] AC21：本阶段不新增通用 ResourceResolver、ResourceTab、Electron custom scheme 或完整 FileSystemProvider；现有 FileRenderer 仍是唯一文件预览 Owner。

## 10. 目标文件结构

```text
E:\ftre\
├─ packages\ftre-inline-extension\
│  ├─ pyproject.toml
│  └─ src\ftre_inline_extension\
│     ├─ __init__.py
│     ├─ types.py              # ExtensionRef / ExtensionContext / Resolution
│     ├─ parser.py             # v1 语法解析和 canonical 序列化
│     ├─ registry.py           # Handler 注册、winner 和 disposer
│     └─ errors.py
├─ src\ftre\plugins\builtin\skill\
│  ├─ plugin.py                # 装配 inline_extensions、SkillService、Hook、Tool、Router
│  ├─ extension_handler.py     # skill 类型 Handler
│  └─ service.py               # Skill 目录、元数据和 CRUD
├─ src\ftre\services\session\message\
│  └─ ...                       # extensions/source 投影，不新增独立存储
└─ tests\
   ├─ contracts\test_inline_extension_protocol.py
   ├─ integration\test_skill_inline_extension.py
   └─ lifecycle\test_inline_extension_disposal.py

E:\binn\ftre-desktop\
└─ packages\renderer\src\
   ├─ lib\
   │  ├─ markdown-plugins.ts    # 统一接入 remark/rehype 和安全协议
   │  ├─ ftre-extensions.tsx   # ExtensionRef、解析和 Skill Renderer
   │  └─ ftre-extensions.test.tsx
   ├─ features\chat\
   │  ├─ ChatInput.tsx          # token 插入/编辑
   │  └─ ...                    # Assistant/User/摘要复用统一 Markdown 配置
   └─ features\chat\slate\
      └─ ChatInputEditor.ts      # token 节点序列化/恢复
```

资源解析不新增目录：Skill 的 filesystem/content source 由现有 `SkillService` 和 `FileRenderer` 承担；只有出现
第二种真实资源类型后，才评估抽取公共 Provider。

## 11. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-29 | 创建 C6 草稿；确定通用 `![ftre:type](ftre://v1/...)` 协议、客户端 Renderer Registry、后端 Handler Registry、Skill 首期实现和四阶段验收计划 | 将 Skill 内嵌调用从专用命令升级为可扩展、可持久化、可安全降级的消息协议 |
| 2026-08-29 | 进入开发；补充 `ftre-inline-extension` Package、Skill HTTP CRUD、客户端 HTTP Skill 候选加载、输入框 Skill token 和消息 Markdown Renderer 的实现边界 | C6 实施开始，现有工作区实现与 PRD 目标对齐 |
| 2026-08-29 | 完成前后端实现与回归验证；扩展投影、隐藏注入快照和 `invocation_id` 幂等 upsert 收口 | 明确刷新/重连恢复语义；待按 C6.1-C6.4 提交后标记已验收 |
| 2026-08-29 | 补充 Skill 文件定义、两种合法文件形态、一层发现算法、frontmatter/调用策略、资源安全、优先级和审查清单；增加 8.5 与 AC11-AC16 验收项 | 修复 README/reference 被误列为 Skill 的根因，建立可执行且可回归的发现规范 |
| 2026-08-29 | 按新增规范收口 `SkillService`：严格一层发现、完整 YAML frontmatter、name/description/策略校验、结构化诊断、HTTP diagnostics、CRUD 约束和 metadata 保留；补充后端与输入框/Renderer 回归验证 | Skill 列表不再把 README/reference 等资源误识别为 Skill；当前验证：后端 759 passed、架构 176 passed、renderer 专项 13 passed、Ruff 通过 |
| 2026-08-29 | 新增 C6.5 资源来源解析契约：`ftre://v1/skill/<name>` 只作语义 URI，详情返回 filesystem/content source、revision 和 capabilities；客户端使用后端真实路径打开现有 FileRenderer，内容来源跳过 fs IPC；补充不过度设计边界和 AC17-AC21 | 修复 Skill 预览把语义 URI 当文件路径导致目录展开 `ENOENT` 的问题；采用 Provider/Resolver 的最小落地方式，暂不引入通用资源基础设施 |
| 2026-08-29 | 完成 C6.5：后端 Skill detail、客户端上下文查询与 source-aware preview 已实现；后端全量 760 passed、Ruff 通过，客户端全量 58 文件/546 tests、tsc 和 Vite build 通过；AC17-AC21 全部核验 | 确认真实 filesystem Skill 可展开 `.ftre` 父目录，content Skill 不触发 fs IPC，旧详情字段缺失时安全降级 |
| 2026-08-29 | 收尾审计修复来源与预览边界：只有目录发现器确认的 `filesystem` Owner 才能暴露真实路径；运行时 Skill 即使携带 path 也保持 content source；虚拟 URI 缺少内容快照时直接显示错误，不再回退 `fs.readFile/stat`；新增两条回归测试 | 防止运行时路径越权和虚拟资源触发本地文件系统 IPC，落实 AC17-AC19 的来源安全约束 |
| 2026-08-30 | 借鉴 Codex GPT-5.6 的 Skill 使用规范：由 `skill` Plugin 注册动态 Prompt section，按当前 Agent/工作区只注入可由模型调用的 Skill 名称与描述；明确显式 `ftre://` 引用、`loadSkill` 激活、最小匹配集合、资源相对路径和安全回退规则 | 让模型了解可用 Skill 与激活边界，同时保持正文按需加载，不把 Skill 规则污染全局 system prompt 或把 README 等资源误当作 Skill |
| 2026-08-30 | 增加 `scan_roots(agent_id, workspace)` 和模型可见的 `### 技能根`、`### 扫描范围`，公开当前请求的 workspace/agent/global 根、优先级及一层发现规则 | 对齐 Codex 的技能根协议，同时保持 FTRE 的作用域和 SkillService 唯一文件系统边界 |
