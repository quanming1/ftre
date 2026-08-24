# AUDIT-001：以三个 DSH 插件为样本的 ftre 架构优雅度验证

> 目的：通过审计三个真实、复杂度不同的 DSH Plugin，把它们当作架构压力测试样本，验证 ftre 当前实现及 F1 目标架构的边界是否清晰、公共契约是否充分、等价能力是否会被迫侵入主程序。

> **范围声明：本文不是三个仓库的迁移计划，不建议把它们加入 ftre TODO，也不设计实际迁移排期。三个项目仅提供真实需求形状和源码证据，用于审计我们自己的架构。**

## 元信息

| 字段 | 值 |
|---|---|
| 审计日期 | 2026-08-20 |
| 状态 | 初稿 |
| 被审计架构 | ftre 当前后端；`PRD-F1-基于 cordis-py 的后端插件化重构` 草稿；ftre Desktop 当前前端 |
| 参考项目 | `E:\dsh-vision-toolkit`；`E:\dsh-context-doctor`；`E:\dsh-undo-savepoint` |
| 关联文档 | `docs/prd/访谈.md`；`docs/prd/PRD-F1-backend-plugin-refactor.md` |
| 审计原则 | 先读真实源码，再映射 Service/Plugin/Server/Client/Recovery 边界；不以 README 宣传代替代码证据 |

## 1. 执行摘要

三个仓库不是同一种 Plugin：

| 样本 | 它施加的架构压力 | ftre 当前实现表现 | F1 草稿表现 | 优雅度验证结果 |
|---|---|---|---|---|
| Context Doctor | 只读 Host 行为 Plugin + Tool + HTTP + Client Widget | 后端核心可做，但需要直接读内部模块；Client Widget 无插件槽位 | 后端大部分可自然实现，但公共可观测接口仍不足 | **后端可以；完整 UI 需要补 Client Extension** |
| Vision Toolkit | Host + Client + Skill + Agent-scoped Tool + LLM Adapter + Credential/Settings + Python Runtime 的纵向 Bundle | 只能勉强容纳十个 Tool；其余能力需要硬编码或改主程序 | 生命周期和部分 Service 可承载，但缺少六个关键 Service 与 Client 插件系统 | **未通过完整 Extension 优雅度验证** |
| Undo Savepoint | 在线 Plugin + 配置/代码快照 + Web UI + 离线 CLI/GUI + pre-boot Safe Mode | 在线 Tool/HTTP 可做；Gateway 起不来时 Plugin 无法自救 | Config/Tool/HTTP 生命周期更清晰，但 pre-boot Recovery 仍在 Plugin 边界之外 | **在线部分可以；完整灾难恢复必须增加 Recovery Extension** |

总判断：

> F1 草稿已经能描述“普通后端 Plugin”，但还没有证明能够优雅表达“纵向产品 Bundle”和“启动前恢复扩展”。如果同等复杂度的功能必须通过修改 AgentManager、WebSocketChannel、Desktop 静态组件或普通 Plugin Loader 才能实现，就说明架构边界没有通过验证。

这次审计将 ftre 的扩展类型划分为三类：

```text
Behavior/Provider Plugin
  由 Cordis Context 管理，消费/提供 Service
  示例：Context Doctor 后端、Skill、MCP

Vertical Extension Bundle
  同时包含 Backend Plugin、Desktop Client、Runtime Worker、Assets
  示例：Vision Toolkit

Recovery Extension
  一部分运行在普通 Composition 之前或之外
  示例：Undo Savepoint 的 Safe Mode、离线恢复
```

F1 不应该假装三类扩展都只是一个 `plugin.py`。

## 2. 审计样本与源码基线

| 项目 | Commit | 分支 | 版本 | License | 主要语言 |
|---|---|---|---|---|---|
| dsh-vision-toolkit | `a79d54050f346efadb3c32000e09ce0234bd953b` | `main` | `0.1.36` | MIT | TypeScript + Python Runtime/Worker |
| dsh-context-doctor | `a15e68d68f511db5ae4057c96ae1c727e21bf1b1` | `main` | `0.5.0` | BSD-3-Clause | TypeScript + React |
| dsh-undo-savepoint | `d42d93ba6c96c2c9ca8cc49497b132e3f22b9360` | `master` | `0.3.6` | MIT | JavaScript + React Bundle + PowerShell |

三个工作树在审计时均无未提交修改。

测试环境记录：

| 项目 | 结果 | 解释 |
|---|---|---|
| Vision Toolkit | `pnpm test` 在 Corepack/pnpm 取依赖时 `fetch failed` | 当前独立环境没有完成依赖准备，未进入测试逻辑 |
| Context Doctor | 包脚本在 Windows 上因 `'tests/*.test.ts'` 单引号 glob 得到 0 tests；显式文件运行 22 passed / 1 failed | 失败用例需要未安装的 `@deepseek-ai/dsh-tools` peer package |
| Undo Savepoint | 版本检查通过，随后启动前退出 | 独立 clone 无法解析 `@deepseek-ai/dsh-tools`；项目按 DSH 安装树设计 |

这些结果只用于确认三个样本对 DSH Service 的真实依赖；本文不尝试运行或搬运其 package，也不据此制定迁移任务。

## 3. dsh-context-doctor 审计

### 3.1 它实际做什么

Context Doctor 是三个项目中最适合当架构探针的一个。Host Plugin 注入：

```text
fs + skills + tools + sessions
          ↓
     runAudit()
       ├─ 扫描 AGENTS.md/CLAUDE.md 指令链
       ├─ 统计 Skill Catalog 和可选正文
       ├─ 统计当前 Agent 可见 Tool Schema
       ├─ 按 mcp__<server>__<tool> 汇总 MCP
       ├─ 检测重复段落、重复描述、Skill shadow
       └─ 产生报告、Developer Receipt 和裁剪建议
          ↓
  context_audit Tool + HTTP Route + Client Ring
```

主要源码：

| 文件 | 职责 |
|---|---|
| `src/index.ts` | Cordis Plugin 入口；注册 Tool；可选注入 webServer |
| `src/scan.ts` | 指令、Skill、Tool Schema 与 MCP 扫描 |
| `src/analyze.ts` | 重复、shadow 和 MCP 分组纯函数 |
| `src/audit.ts` | 组合报告、Developer Receipt 与建议 |
| `src/routes.ts` | GET 审计 API、按 cwd 缓存与 in-flight 复用 |
| `src/client/*` | 会话输入区 Context Ring 与展开面板 |

### 3.2 值得学习的设计

1. **核心分析是纯函数。** `analyze.ts` 和 token 估算不依赖 Cordis，适合作为 Service 边界是否充分的验证逻辑。
2. **Host 与 Client 分开。** Tool/HTTP 属于 Host；UI 通过稳定 JSON Report 消费，不直接访问后端对象。
3. **可选依赖。** Headless 环境没有 webServer 时只跳过 Route，Tool 继续工作。
4. **Agent-aware Tool Schema。** `tools.schemas(agent)` 获取的不是全局工具，而是当前 Agent 实际可见面。
5. **有来源与胜出信息。** Skill Summary 带 source/provider/rank，能够发现同名 shadow。
6. **有资源上限。** 单文件 256 KiB、HTTP 缓存最多 32 项、失败不缓存。
7. **诚实暴露不可见信息。** DSH 没有公开最终 Prompt assembly trace 时，报告明确写 `trimmed.status=unavailable`，没有猜测。

### 3.3 源码局限

1. Token 是启发式估算，不是模型 tokenizer 精确值。
2. Skill rank 规则硬编码在插件里，说明 DSH 的 Skill Service 没有完整暴露解析结果。
3. 它重新扫描 AGENTS.md/CLAUDE.md，而不是读取“本轮最终注入回执”；扫描结果可能与真实 Prompt assembly 不完全一致。
4. 前端预算环固定按 50k 计算，未读取当前模型 context window。
5. npm test 的单引号 glob 在本次 Windows 环境只匹配到 0 项，测试脚本的跨平台性不足。

### 3.4 映射到 ftre

| 依赖能力 | ftre 当前 | F1 草稿 | 结论 |
|---|---|---|---|
| Filesystem Service | 没有；代码直接用 Path/open 或 Read Tool | 未列入稳定 Service | **缺口** |
| Skill Catalog `list/get/source/provider/rank` | Skill Store 与 Skill Plugin 分裂；元数据不完整 | 有 SkillService，但接口未细化 | **部分支持** |
| Agent-scoped Tool Schema | 每次构造 ToolRegistry 副本；只能 snapshot/to_openai_tools | ToolService 写了 agent-scoped view，但无 provenance/schema 契约 | **部分支持** |
| Session → workspace | Session 有 workspace 字段 | SessionService 可提供 | **支持** |
| HTTP Route Contribution | 当前可注册 Router，但由 WebSocketChannel 集中挂载 | HttpService 已规划 | **支持** |
| Client Widget Slot | Desktop 没有动态插件系统；Settings/nav 静态 | F1 仅后端 PRD | **不支持** |
| 最终 Prompt assembly trace | 没有；ContextGovern 直接改 config/messages | SystemPromptService 已加入，但未定义 receipt | **缺口** |

架构验证结论：

- 若实现同等只读审计能力，后端 Tool 和 HTTP 可以通过 F1 方向承载。
- 要只依赖公共接口，仍必须补 FilesystemService、Tool Schema/Provenance、Skill Resolution 和 Prompt Assembly Receipt。
- 同等 Context Ring UI 当前只能修改 Desktop 主代码，说明项目级 Extension 架构未通过验证。

### 3.5 用于验证边界的理想分解（非迁移方案）

```text
plugins/builtin/context_doctor/
├─ plugin.py              # Inject fs/skills/tools/sessions/http
├─ audit.py               # 纯组合逻辑
├─ scan.py                # 只消费公共 Service
├─ analyze.py             # 纯函数
├─ report.py
├─ tool.py
└─ router.py

ftre-desktop extension（未来）
└─ context-doctor/
   ├─ manifest.json
   ├─ entry.tsx
   └─ ContextAuditRing.tsx
```

优雅度结论：**后端 Service 方向基本正确，但可观测契约不足；Desktop Extension 边界当前不成立。**

## 4. dsh-vision-toolkit 审计

### 4.1 它实际做什么

Vision Toolkit 不是单纯工具包，而是完整纵向扩展：

```text
Settings + Credentials
          ↓
RuntimeManager ── prepare-before-swap ── Python Runtime
          ↓
10 个 Vision Tool + vision-skills
          ↓
Agent-scoped progressive exposure
          ↓
LLM text-only model image-input variants
          ↓
Attachment/Paste bridge
          ↓
Signed Artifact HTTP delivery
          ↓
Web Settings + Tool presentation + Paste UI
```

十个主要 Tool：

`vision_glance`、`vision_ground`、`vision_detect`、`vision_crop`、`vision_trace`、`vision_pixel_diff`、`vision_long_screenshot_ocr`、`vision_extract_foreground`、`vision_dominant_colors`、`vision_html_screenshot`。

主要源码：

| 文件 | 职责 |
|---|---|
| `src/index.ts` | 总 Plugin 装配、Runtime readiness、Skill/Tool/Web 生命周期 |
| `src/config.ts` | Typed Settings、默认值、Credential Ref 和严格校验 |
| `src/runtime-manager.ts` | 新配置 prepare 完成后原子替换 active generation |
| `src/runtime.ts` | 路径策略、并发、取消、超时、Credential、工具运行和 Artifact |
| `src/tools.ts` | 十个 Tool Definition 与结构化结果 |
| `src/exposure.ts` | Agent-scoped 渐进式 Tool 暴露 |
| `src/image-input-variants.ts` | LLM Adapter、图片转视觉证据、模型变体 |
| `src/paste-images.ts` | 会话工作区粘贴图片和安全路径 |
| `src/artifact-access.ts` | HMAC capability URL、MIME/CSP、symlink/TOCTOU 防护 |
| `src/web.ts` | Settings、Credential、Health、Update、Paste、Artifact Routes |
| `src/client/*` | Settings、模型选择、粘贴、Tool 展示等 Web Client |
| `src/runtime-install.ts` / `src/upstream.ts` | 固定上游、隔离 Python、安装和完整性验证 |

### 4.2 值得学习的设计

1. **准备成功后再切换。** Runtime Generation 采用 prepare-before-swap；错误设置不会破坏正在服务的旧实例。
2. **失败时不暴露半残能力。** Runtime 未 ready 时不注册 Skill 和视觉 Tool，只保留 Settings 修复面。
3. **Tool 渐进式暴露。** 全局只暴露激活 Tool；Skill 被真实加载后，十个 Tool 才注册进具体 Agent scope，降低每轮 Schema 成本。
4. **完整取消链。** Caller Abort、Plugin dispose 和 Deadline 合并，贯穿 Semaphore、文件 IO、上游调用和子进程。
5. **会话级并发限制。** 每个 session/workspace 使用独立 Semaphore，避免一个会话占满全局。
6. **严格路径边界。** 输入只允许 workspace、平台临时目录和显式 allowedDirs；处理 symlink、realpath 和输出覆盖。
7. **Artifact 是 capability，不是裸路径。** 使用 HMAC 签名 URL、同源、CSP、MIME、文件 identity 和 no-follow 检查。
8. **Secret 不进普通配置。** Settings 只持 Credential Ref，真实 key 通过 Credential Service 解析。
9. **Host/Client 契约明确。** 前端只通过同源 HTTP 和 DSH Client Service 工作。
10. **固定上游。** Runtime 记录上游 commit、Skill patch 与哈希，避免运行时跟随 main 漂移。

### 4.3 该样本暴露出的非基础架构问题

1. 共享免费视觉端点、自动更新和运行时下载属于独立供应链与隐私问题，不能被普通 Plugin 生命周期抽象掩盖。
2. managed Python 安装、镜像回退和依赖下载会改变 ftre 的部署模型。
3. 模型变体依赖 DSH LLM Adapter Registry；ftre 当前 LLM 配置是 Agent 构造参数，不是可扩展 Service。
4. Web Client 深度使用 DSH slot/settings/tool-presentation/input-trigger；不能直接复用到 ftre Desktop。
5. `runtime.ts` 超过两千行，说明即使宿主 Service 充分，功能内部仍需要按 path/credential/process/artifact/tool operation 拆分。

### 4.4 映射到 ftre

| Vision 依赖 | ftre 当前 | F1 草稿 | 结论 |
|---|---|---|---|
| Cordis lifecycle | 自研 Kernel 可做部分清理 | cordis-py | **F1 支持** |
| Agent-scoped Tool 注册与 restrict | 无；构造时复制全局 Registry | 只写了 agent-scoped view，未定义动态 scope/event | **关键缺口** |
| Skill register/unregister + durable load evidence | Skill Plugin 私有实现 | SkillService 已规划但接口不足 | **部分支持** |
| Agent list/created/disposed events | AgentLoop 内部对象，没有稳定 Registry Event | AgentService 仅列 submit/cancel/wait/status | **关键缺口** |
| Settings Namespace、revision、watch、validate | Config 全局 JSON，无插件 namespace/revision | ConfigService 仍是根配置 Owner | **关键缺口** |
| Credential Ref/resolve/write | api_key 明文位于 Config | 未规划 CredentialService | **关键缺口** |
| LLM Adapter/Provider/Model Registry | 无；Agent 创建时固定 provider/model | 未规划 LlmService | **关键缺口** |
| Subprocess Service | bash 与 gateway 各自直接 subprocess | 未规划可注入 ProcessService | **关键缺口** |
| Attachment read/materialize | 有图片消息与 image_store，但不是公共 Service | AttachmentService 已规划 | **部分支持** |
| Artifact Delivery | 当前可通过文件/图片 API 暴露路径 | AttachmentService/HttpService 可承载，但安全契约未定义 | **部分支持** |
| HTTP exact/prefix routes | 当前 APIRouter 可做 | HttpService 已规划 | **支持** |
| Desktop Extension、Settings Slot、Paste Hook、Tool Renderer | 全部静态编译；ExtensionsPanel 还是占位实现 | F1 不覆盖前端 | **不支持** |

架构验证结论：

- **只实现十个 Python Tool：技术上可以。** 但如果每个 Tool 直接读取 Config、api_key、subprocess、workspace 和图片路径，代码会重新耦合成大 Plugin。
- **实现 Skill + 按 Agent 渐进暴露：F1 需要补 Agent scope 与 Tool scope 接口。**
- **实现图片粘贴、文本模型透明视觉桥、模型变体：没有 LlmService 和 Client Extension 时不能优雅实现。**
- **实现 Settings/Credential/Artifact 完整体验：当前不支持。**

### 4.5 用于验证边界的理想分解（非迁移方案）

Vision Toolkit 应被定义为 Vertical Extension Bundle，而不是一个 Python 文件：

```text
extensions/vision-toolkit/
├─ manifest.json
├─ backend/
│  ├─ plugin.py
│  ├─ service.py
│  ├─ runtime/
│  ├─ tools/
│  ├─ skill/
│  ├─ attachment_bridge.py
│  ├─ artifact.py
│  └─ router.py
├─ desktop/
│  ├─ entry.tsx
│  ├─ settings.tsx
│  ├─ paste-hook.ts
│  └─ tool-renderers.tsx
├─ worker/
│  └─ locked Python runtime
└─ assets/
   └─ vision-skills/
```

如果 ftre 仍坚持“插件只有后端 Python entry”，完整 Vision Toolkit 永远只能通过主仓库硬编码前端实现。

优雅度结论：**F1 能表达工具型后端能力，但还不能表达完整纵向 Extension；这是架构压力测试未通过，而不是迁移任务待办。**

## 5. dsh-undo-savepoint 审计

### 5.1 它实际做什么

Undo Savepoint 同时拥有四个运行面：

```text
在线 Host Plugin
  ├─ 9 个 undo_* Tool
  ├─ System Prompt Section
  ├─ fs.watch 自动快照
  └─ /api/undo/* REST

Web Client
  ├─ Header 按钮
  ├─ 快照面板
  ├─ Settings
  └─ 快捷键

Pre-boot Safe Mode
  └─ 修改 Plugin Composition，使除 undo 外的用户插件禁用

Offline Rescue
  ├─ PowerShell CLI
  └─ PowerShell GUI
```

快照对象不仅包括配置，还包括用户 Plugin 代码树；使用内容寻址 Blob 去重。恢复前创建 pre-restore 快照，支持 undo/redo；Watcher 通过恢复写入哈希抑制 echo。

主要实现：

| 文件 | 职责 |
|---|---|
| `lib/index.js` | Host Plugin、快照/恢复、Watcher、Tool、Prompt、REST；约两千行 |
| `lib/client.js` | Web UI、快捷键、Settings 与 REST Client |
| `lib/spec.json` | Node 与 PowerShell 共用的快照范围 |
| `tools/dsh-undo-savepoint-lib.ps1` | 离线恢复核心 |
| `tools/dsh-undo-savepoint.ps1` | 离线 CLI |
| `tools/dsh-undo-savepoint-gui.ps1` | DSH 起不来时的 GUI |
| `tools/dsh-plugin.ps1` | 安装插件前后自动快照与失败回退 |

### 5.2 值得学习的设计

1. **恢复前先保存当前状态。** 所有 restore 先生成 pre-restore，确保可以 redo。
2. **真实状态差异驱动 undo。** 跳过与当前内容相同的快照，避免“撤销成功但什么都没变”。
3. **Watcher echo 抑制。** Restore 自己产生的文件变化不会立刻制造新快照挡住 redo。
4. **配置与 Plugin 代码一起保护。** 解决“配置没变但 Plugin 代码改坏”的实际事故。
5. **内容寻址 Blob。** 跨快照去重，限制单文件和单次 Plugin 代码体积。
6. **Secret 模式。** 快照默认脱敏，真实值进入本机 vault；导出不带 vault。
7. **自保挂载。** 恢复 Composition 后重新确保 Undo Plugin 自身仍挂载。
8. **启动健康标记。** 下次启动根据上次 boot-state 判断异常，并关联 last-good snapshot。
9. **离线工具共享规范。** `spec.json` 是 Node 与 PowerShell 的单一快照范围来源。

### 5.3 源码局限

1. Host 核心集中在约两千行编译后 `lib/index.js`，缺少可读的原始 TS 源码，维护和复用成本较高。
2. 存在模块级 `ctxRef`、环境变量/argv 解析和动态 peer dependency 查找，说明其与宿主缺少正式 Recovery/Path/Profile Service。
3. 初始 baseline snapshot 使用 fire-and-forget async IIFE，不完全归属于显式 Effect。
4. 配置、快照、恢复、Watcher、Tool 和 REST 高度集中，单元边界弱于前两个项目。
5. YAML Secret 脱敏是行级策略，多行敏感值并不完整。
6. Restore、Safe Mode 和依赖同步都是高风险写操作；任何同等能力都必须进入 ftre Permission/Confirmation 流程，不能仅靠 Tool 描述提醒。

### 5.4 映射到 ftre

| Undo 依赖 | ftre 当前 | F1 草稿 | 结论 |
|---|---|---|---|
| Config 单一 Owner | 多模块直接读写 config.json | ConfigService 已规划 | **F1 改善明显** |
| Plugin Catalog/启用清单 | 扫描即装载，状态分散 | 显式 Catalog/Manager 已规划 | **F1 支持** |
| Tool/Prompt/HTTP Contribution | 当前能注册但清理和 Host 边界不完整 | Tool/SystemPrompt/HttpService 已规划 | **F1 支持** |
| File watcher 生命周期 | 各模块自行管理 | 可由 Cordis Effect 管理 | **支持** |
| Session busy/active-turn 查询 | 需要读取 AgentLoop 私有 Lane | AgentService 尚未定义 `is_busy/list_active` | **缺口** |
| 原子 Config transaction/revision | 无 | ConfigService 未定义 transaction/snapshot API | **缺口** |
| Plugin 源路径和可信 Owner | Loader 通过 sys.path 扫描目录 | Catalog 有 source，但无可快照文件清单 | **缺口** |
| Safe Mode before normal plugins | 无 | Composition Root 存在，但没有 preflight/recovery seam | **关键缺口** |
| Gateway 起不来时的离线 CLI | GatewayRuntime 只管理 start/stop/status | F1 未定义 recovery command | **关键缺口** |
| Desktop Plugin UI/快捷键注入 | Settings/nav/header 都是静态代码 | F1 不覆盖前端 | **不支持** |

架构验证结论：

- **在线 snapshot/list/diff/restore Tool 与 REST：可以实现。**
- **用 Cordis Effect 管 Watcher、Tool、Prompt 和 Route，代码会比原项目更清晰。**
- **Safe Mode 不能仅作为普通 Plugin。** 如果失败发生在 Config 解析、Cordis 创建或 Undo Plugin 之前，它没有运行机会。
- **离线恢复必须属于 `app/cli/recovery` 或独立最小可执行程序，且不能 import 全量业务 Plugin。**
- **Desktop 面板和快捷键仍需要 Client Extension 能力。**

### 5.5 用于验证边界的理想分解（非迁移方案）

```text
app/
├─ gateway/
│  ├─ recovery_preflight.py      # 普通 Composition 之前
│  └─ bootstrap.py
└─ cli/
   └─ recovery.py                # Gateway 无法启动时仍可运行

plugins/builtin/savepoint/
├─ plugin.py
├─ service.py
├─ manifest.py
├─ store.py
├─ snapshot.py
├─ restore.py
├─ redaction.py
├─ watcher.py
├─ tools.py
└─ router.py

ftre-desktop extension（未来）
└─ savepoint/
   ├─ entry.tsx
   ├─ panel.tsx
   └─ shortcuts.ts
```

`recovery_preflight.py` 不是普通业务 Plugin；它只能依赖标准库、路径解析和最小 Config Snapshot 格式。

优雅度结论：**F1 可让在线 Plugin 部分变得优雅，但完整灾难恢复必须新增 pre-boot/offline 边界。**

## 6. 跨项目 Service 能力矩阵

图例：

- ✅ 已有可用公共能力
- ◐ 有内部实现或 F1 只有方向，契约不足
- ❌ 当前没有

| 能力 | Context Doctor | Vision Toolkit | Undo Savepoint | ftre 当前 | F1 草稿 |
|---|---:|---:|---:|---:|---:|
| Cordis Fiber/Effect/Inject | 必需 | 必需 | 必需 | ◐ 自研 | ✅ cordis-py |
| Tool reversible register | 必需 | 必需 | 必需 | ◐ | ✅ 规划 |
| Agent-scoped Tool scope/restrict | 审计实际可见面 | 核心能力 | — | ❌ | ◐ |
| Tool schema + source/provider provenance | 核心能力 | 有益 | — | ❌ | ◐ |
| Skill list/get/register + resolution provenance | 核心能力 | 核心能力 | — | ◐ | ◐ |
| System Prompt section + assembly receipt | 审计对象 | 有益 | 必需 | ❌ | ◐ |
| Filesystem Service + path policy | 核心能力 | 核心能力 | 快照范围 | ❌ | ❌ |
| Session workspace/public runtime state | 必需 | 必需 | busy guard | ◐ | ◐ |
| Settings namespace/revision/watch | — | 核心能力 | 配置面板 | ❌ | ❌ |
| Credential reference/resolve/write | — | 核心能力 | vault 可选 | ❌ | ❌ |
| LLM adapter/model registry | — | 核心能力 | — | ❌ | ❌ |
| Cancellable Subprocess Service | — | 核心能力 | deps sync | ❌ | ❌ |
| Attachment read/materialize | — | 核心能力 | — | ◐ | ◐ |
| Signed Artifact delivery | — | 核心能力 | 导出文件 | ❌ | ◐ |
| HTTP exact/prefix route contribution | UI 数据 | 多条 Route | 多条 Route | ◐ | ✅ |
| Client Extension manifest/slot/lifecycle | Ring | 核心能力 | Panel/Shortcut | ❌ | ❌ |
| Config transaction/snapshot/revision | — | Settings save | 核心能力 | ❌ | ◐ |
| Plugin source inventory/file ownership | — | self-update | 核心能力 | ❌ | ◐ |
| Pre-boot recovery/safe mode | — | — | 核心能力 | ❌ | ❌ |
| Offline recovery CLI | — | — | 核心能力 | ❌ | ❌ |

## 7. 对 F1 架构的自审结论

### 7.1 已经正确的部分

1. **选择 cordis-py 是正确的。** 三个项目都大量依赖可撤销注册、可选依赖和依赖就绪后激活；手工 shutdown 无法优雅覆盖。
2. **Service/Plugin/Server 分开是正确的。** Vision 的 Web Routes 和 Undo 的 Server 依赖证明 Server 是资源，Plugin 才是生命周期 Owner。
3. **HttpService 必须独立。** 三个项目都有 Tool 之外的 HTTP 数据面。
4. **SystemPromptService 必须存在。** Context Doctor 需要审计它，Undo 需要贡献 Section，Vision 的 Skill/Tool 需要与 Prompt 协调。
5. **显式启用外部 Plugin 是正确的。** 未启用的 Vision/Undo 模块可能有依赖解析、文件或网络副作用，不能在 discovery 阶段 import。
6. **AgentLoop 私有化是正确的。** Plugin 应使用 AgentService，不应读取 `_lanes` 等内部字段。

### 7.2 F1 仍然过于乐观的部分

#### A. ToolService 只有名字，没有足够契约

F1 必须明确 ToolService 至少支持：

```text
register(definition) -> disposer
register(scope=agent_id, definition) -> disposer
restrict(scope=agent_id, allow/deny) -> disposer
schemas(agent_id) -> schema + source + provider
snapshot(agent_id) -> 当前真实可见面
execution context -> session/workspace/cancel signal
```

否则 Context Doctor 无法审计真实工具面，Vision 无法渐进暴露。

#### B. SkillService 缺少解析和来源信息

仅有 CRUD 不够。应公开：

```text
list(cwd, agent_id)
get(name, cwd, agent_id)
register(skill) -> disposer
source/provider/rank
winner + shadowed entries
durable load evidence/event
```

#### C. SystemPromptService 需要 Assembly Receipt

只拼接字符串仍然不可审计。每个 Section 应记录：

```text
name
source plugin
scope
priority/order
bytes/tokens estimate
included/trimmed
```

Context Doctor 应读取本轮真实 Receipt，而不是重新猜测。

#### D. F1 缺少通用 Filesystem/Workspace Service

Context Doctor、Vision 和 Undo 都需要受控路径、取消和来源边界。让每个 Plugin 自己使用 `Path/open/realpath` 会重复安全逻辑并破坏可测试性。

#### E. ConfigService 还不是 SettingsService

根配置单一所有权解决了“谁写文件”，但 Vision 还需要：

- Plugin namespace。
- Typed schema/defaults。
- revision/compare-and-swap。
- prepare-before-commit。
- watch 与 rejected generation。
- Secret 引用而不是明文。

这些不应全部塞进 ConfigService；建议拆成 SettingsService + CredentialService。

#### F. AgentService 不能只提供 submit/cancel/wait/status

还需要稳定的：

- Agent list/get。
- created/disposed 事件。
- session → active Agent。
- per-Agent child Context 或 Tool Scope。
- active turn/busy 查询。

#### G. Backend Plugin 不是完整 Extension

ftre Desktop 当前：

- Settings 导航静态数组。
- Chat Header/Input 没有 Slot Registry。
- Tool Card 映射静态。
- ExtensionsPanel 仍有 `TODO` 占位。
- Inspector 只有进程内静态 Tab Registry，没有第三方 manifest、隔离装载和卸载。

因此任何带 UI 的第三方 Extension 都必须修改前端主仓库，说明当前只有后端 Plugin 架构，没有完整 Extension 架构。

#### H. Recovery 必须在 Plugin Loader 之外

Undo 样本证明至少需要：

```text
ftre CLI
   ↓
minimal recovery preflight
   ↓
load config safely
   ↓
create cordis Context
   ↓
normal Composition
```

如果 Config、Plugin entry 或普通 Composition 已损坏，Recovery 仍必须可执行。

## 8. 架构改进建议

### 8.1 建议纳入 F1 定稿的 P0 契约

这些是让“普通后端 Plugin”真正可复用的基础，不要求实现三个样本本身：

1. 补全 ToolService：agent scope、restrict、schema/provenance、取消上下文。
2. 补全 SkillService：source/provider/rank、winner/shadow、register disposer。
3. 补全 SystemPromptService：Section Registry 与 Assembly Receipt。
4. 新增最小 FilesystemService/WorkspaceService：resolve、stat、read、受控路径与 cancellation。
5. 补全 AgentService：list/get、created/disposed、busy 和 Agent Tool Scope。
6. ConfigService 提供原子写、revision 和 watch；Plugin 不能自行覆盖 config.json。
7. HttpService 同时支持 exact/prefix route、body limit、same-origin policy helper 和 freeze/restart_required。
8. Composition Root 预留 `recovery_preflight`，即使完整 Savepoint 功能进入后续 TODO。

### 8.2 建议进入后续 TODO 的 P1 平台能力

1. SettingsService：Plugin namespace、schema、revision、prepare/activate generation。
2. CredentialService：Secret Ref、resolve、write、永不回显。
3. LlmService：Provider/Adapter/Model Registry、模型元数据和可撤销 adapter。
4. SubprocessService：进程树、取消、超时、工作区和环境白名单。
5. ArtifactService：受控产物描述、预览/download capability、MIME/CSP/symlink 防护。
6. Desktop Client Extension：manifest、允许的 Slot、Settings Page、Tool Renderer、Input Hook、生命周期和权限。
7. Recovery CLI/Safe Mode：不依赖普通 Plugin Composition。

### 8.3 暂不进入基础架构的 P2

1. Plugin 自更新。
2. 远程 Marketplace。
3. 自动下载托管 Python。
4. 多 Distribution Bundle 安装。
5. 运行时 Router HMR。

这些能力安全和供应链成本高，应在真实需求出现后单独立 PRD。

## 9. 三个架构验证用例

本节定义纸面/契约验证，不要求实现或迁移三个项目。

### 验证用例 A：只读上下文审计

假设一个第三方 Plugin 需要统计当前 Agent 实际可见的 Prompt Section、Skill 和 Tool Schema：

- 如果它只能通过公共 Service 获得带 source/provider/scope 的真实结果，则通过。
- 如果它必须扫描内部目录、读取 AgentManager 私有字段或复制 Skill 优先级算法，则不通过。

当前结果：**未完全通过**。F1 有 Service 名称，但缺少 Assembly Receipt、Provenance 和 Resolution 契约。

### 验证用例 B：带 Runtime 和 Client 的纵向扩展

假设一个第三方 Extension 同时需要 Agent-scoped Tool、Settings、Credential、LLM Adapter、Worker、Artifact Route 和 Desktop UI：

- 如果 Backend、Client、Worker 可以通过各自 manifest 和稳定 Service 独立装卸，则通过。
- 如果必须在 AgentManager、WebSocketChannel、SettingsPanel、ChatInput 和 Tool Card 中加入专用分支，则不通过。

当前结果：**不通过**。F1 只覆盖后端 Plugin，项目还没有 Vertical Extension Bundle 边界。

### 验证用例 C：普通 Composition 失败后的恢复

假设 Config 或 Plugin entry 已损坏，Gateway 无法完成普通启动：

- 如果最小 recovery preflight/offline CLI 不加载业务 Plugin 也能检查和恢复配置，则通过。
- 如果恢复逻辑本身必须等普通 Plugin Loader 成功后才可用，则不通过。

当前结果：**不通过**。Composition Root 提供了合适落点，但 F1 尚未定义 pre-boot Recovery seam。

## 10. 最终架构判断

### 当前实现

**不够优雅。** 等价复杂度的能力会直接依赖 AgentManager、ToolRegistry、Config、WebSocketChannel 和 Desktop 静态组件内部细节，并产生专用 if/else。

### F1 草稿

**骨架方向优雅，但公共契约尚未达到优雅。** Cordis、唯一 Composition Root、Service/Plugin/Server 分离和显式启用是正确设计；Tool/Skill/SystemPrompt/Filesystem/Agent 可观测面仍不足。

### 边界判断

- F1 应只对“后端 Behavior/Provider Plugin”负责，并把这条边界做扎实。
- Client Extension 和 Recovery Extension 可以是后续架构，不必为了声称万能而塞进 F1。
- 但 F1 必须明确声明自己不覆盖这两类扩展，不能把“Backend Plugin 架构”表述成“整个项目的 Extension 架构”。

三个样本的作用到此为止：它们是反例和压力测试，不是待迁移功能。

> Plugin 架构不等于 Extension 架构，更不等于 Recovery 架构。

## 11. 主要源码证据

### Vision Toolkit

- `E:\dsh-vision-toolkit\src\index.ts`
- `E:\dsh-vision-toolkit\src\exposure.ts`
- `E:\dsh-vision-toolkit\src\runtime-manager.ts`
- `E:\dsh-vision-toolkit\src\runtime.ts`
- `E:\dsh-vision-toolkit\src\tools.ts`
- `E:\dsh-vision-toolkit\src\image-input-variants.ts`
- `E:\dsh-vision-toolkit\src\paste-images.ts`
- `E:\dsh-vision-toolkit\src\artifact-access.ts`
- `E:\dsh-vision-toolkit\src\web.ts`
- `E:\dsh-vision-toolkit\src\client\index.tsx`

### Context Doctor

- `E:\dsh-context-doctor\src\index.ts`
- `E:\dsh-context-doctor\src\scan.ts`
- `E:\dsh-context-doctor\src\analyze.ts`
- `E:\dsh-context-doctor\src\audit.ts`
- `E:\dsh-context-doctor\src\routes.ts`
- `E:\dsh-context-doctor\src\client\ContextAuditRing.tsx`

### Undo Savepoint

- `E:\dsh-undo-savepoint\lib\index.js`
- `E:\dsh-undo-savepoint\lib\client.js`
- `E:\dsh-undo-savepoint\lib\spec.json`
- `E:\dsh-undo-savepoint\tools\dsh-undo-savepoint-lib.ps1`
- `E:\dsh-undo-savepoint\tools\dsh-undo-savepoint.ps1`
- `E:\dsh-undo-savepoint\tools\dsh-undo-savepoint-gui.ps1`

### ftre 对照

- `E:\ftre\src\ftre\main.py`
- `E:\ftre\src\ftre\plugin\kernel`
- `E:\ftre\src\ftre\tools\__init__.py`
- `E:\ftre\src\ftre\agent\agent_manager.py`
- `E:\ftre\src\ftre\agent\loop.py`
- `E:\ftre\src\ftre\plugin\builtin\skill_plugin.py`
- `E:\ftre\src\ftre\channel\ws_channel.py`
- `E:\binn\ftre-desktop\packages\renderer\src\features\settings\SettingsPanel.tsx`
- `E:\binn\ftre-desktop\packages\renderer\src\features\extensions\ExtensionsPanel.tsx`
- `E:\binn\ftre-desktop\packages\renderer\src\features\inspector\tabRegistry.ts`

## 12. 后续审计动作

1. 逐项评审 `8.1` 的 P0 契约，决定哪些写回 F1 PRD。
2. 为 ToolService、SkillService、SystemPromptService 和 AgentService 单独写接口草案。
3. 设计最小 Client Extension Manifest，不立即实现远程插件市场。
4. 设计 `recovery_preflight` 的边界，保证它不依赖普通 Plugin。
5. F1 基座落地后，优先用 Context Doctor 后端作为第三方 Plugin 验证样本。

## 13. 审计记录

| 日期 | 内容 |
|---|---|
| 2026-08-20 | 建立首篇外部 Plugin 架构自审；以三个仓库为压力测试样本，验证 ftre 的 Service、Host、Client 和 Recovery 边界 |
| 2026-08-20 | 修正审计范围：删除迁移计划表述，明确三个仓库只用于验证架构优雅度，不进入迁移排期 |
