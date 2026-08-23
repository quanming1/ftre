# 执行提示词 06：F14.6-F14.7 Host Service 与 Package 发行边界

你正在 `E:\ftre` 执行 F14.6-F14.7。目标是收紧 Host 稳定 Service，并证明 Inbox/Compaction
是真正可选的独立发行物。不要为了目录对称增加 Service，也不要把所有 Plugin 强拆成 Package。

## 一、前置检查

- 阅读强制文档、F14 PRD、执行报告、目标树和第 4.5 节 Package 门禁。
- 确认 F14.1-F14.5 已完成，旧 platform/features/agent_loop 已清零。
- 从 Composition、Plugin inject/provide 和 AST import 重新生成当前 Service 依赖图。
- 只改 ftre 仓库及其 `packages/`；不修改/发布外部仓库。

## 二、Host Service 审计

逐项审计 Config、Filesystem、HTTP、Session、Agent、LLM、Tools、System Prompt、MessageBus、
Channel registry、Workspace、Attachment：

1. 是否确有稳定 Service 价值，key 是否唯一；
2. 是否由唯一 Provider Plugin 创建、provide、start/stop；
3. 必选依赖是否在 inject 声明，是否仍有 `ctx.get()` 静默查找；
4. 是否通过 Loop、Composition、Service Bag 或全局变量间接取其他 Service；
5. Repository/Adapter/Runtime 是否只在 Owner 内部使用；
6. 是否存在单实现 Port、透传 Facade、Coordinator、重复 DTO 和 bind setter；
7. Route/Hook/Watcher/Task/Store 是否随 Fiber 可逆释放。

修复发现的问题；不要把问题仅写入报告后宣称本批完成。若某个 Service 只有一个 Owner 内部消费者，
将其降为 Owner 私有实现，而不是保留公共 key。

## 三、Inbox/Compaction Package 门禁

分别验证并修复：

- `pyproject.toml` 的完整 build-system、依赖和 `ftre.plugins` entry point；
- Host 不硬依赖、不 concrete import、不创建 no-op fallback；
- `ftre` extras 能表达 inbox/compaction/full 安装组合；
- 未安装 Package 时 Host 可 import、最小 Composition 可启停、直接 Agent Turn 可运行；
- 安装后 Plugin discovery、load/unload/restart、配置和数据目录正确；
- wheel 不夹带缓存、测试数据、临时数据库或 Host 私有源码；
- Package 只依赖稳定公共 Service/Hook 契约；
- 各 Package 自身 tests、README、中文边界注释和版本元数据准确。

使用新建的、路径已验证的临时虚拟环境做洁净安装。临时目录必须位于明确的 repo 外临时位置，
完成后可安全清理；不得对工作区根或用户目录执行递归删除。

## 四、包化候选审计

对 MCP、Skill、Schedule、Team 分别按 PRD 七条 Package 门禁给出 `ready/not-ready` 和证据。
本批不因“看起来可以”就移动到 `packages/`，也不创建空 package 壳。未满足项成为后续独立阶段，
不阻塞 F14，只要它们已经是边界完整的 Builtin Plugin。

## 五、注释和卫生

- Host Service 注释解释公开边界，私有实现注释解释存储/并发/清理原因。
- Package 入口用中文说明 inject/provide、缺失能力和卸载语义。
- 删除无效 Port/Facade 时同步删除描述旧层级的注释、测试和文档。
- 扫描死代码、未使用 import、动态 getattr 兼容、缓存、build/dist、egg-info、临时 venv 和空目录。

## 六、验证与停止条件

执行 Service/architecture/lifecycle/startup 专项、两个 Package 独立测试、wheel build、洁净安装、
无包启动、全量 pytest、ruff、diff check。

完成后：

- Host Service 表与实际 key/provider/inject 完全一致；
- 无 Locator、bind setter、跨 Owner private import 和无价值中间层；
- Inbox/Compaction 通过全部独立发行门禁，Host 无包可运行；
- 包化候选报告有证据但未擅自扩 scope；
- 更新 PRD/执行报告/TODO F14.6-F14.7，按职责提交、不 push、工作树干净；
- 汇报依赖图变化、删除项、wheel/洁净安装命令结果、提交和最终批输入。
