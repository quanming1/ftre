# 执行提示词 02：F14.2 Kernel 命名与业务零知识迁移

你正在 `E:\ftre` 执行 F14.2。完整实现 `platform → kernel`，并把业务 HookSpec 交还各自
Owner。不要只增加新目录后保留旧 re-export。本批完成后旧生产路径必须消失。

## 一、前置动作

1. 完整阅读仓库强制文档、F14 PRD、提示词目录 README 和 F14 执行报告。
2. 检查上一批 F14.1 已完成、专项门禁为绿、当前分支正确、工作树没有未知修改。
3. 先用 `rg --files src/ftre/platform`、import 扫描和 HookSpec 清单重新确认实际范围；不要只按
   文件名机械移动。
4. 只改 ftre 后端仓库；本批授权 commit，不授权 push/merge/release。

## 二、实施要求

### 1. 迁移 Kernel 路径

- 建立 `src/ftre/kernel/hooks` 与 `src/ftre/kernel/plugins` 目标结构；
- 迁移 Hook Runtime 的通用机制：spec 基类、dispatch、scope、receipt、取消、诊断；
- 迁移 Plugin Runtime：manifest、discovery、catalog、loader、manager、diagnostics；
- 同步修改所有生产、测试、文档和 Composition imports；
- 删除 `src/ftre/platform`，禁止 `sys.modules`、try-import、re-export 或 alias 兼容。

### 2. 业务 HookSpec 归属

逐个处理原 Kernel 中的业务 Hook 名称：

- Agent Hook → Agent Owner；
- Tool Hook → Tool Owner；
- LLM Hook → LLM Owner；
- Session Hook → Session Owner；
- System Prompt Hook → System Prompt Owner；
- Messaging Hook → Messaging Owner；
- Inbox/Compaction Hook → 对应 Package Owner。

Kernel 可以知道 HookSpec 如何执行，但不能知道 `agent/*`、`tool/*`、`session/*` 等具体名称。
不要建立新的中央 `hook_names.py` 或 `contracts/` 垃圾桶。

### 3. Composition 边界

- Composition 只创建根 Context、唯一 HookRuntime、PluginManager 和 Manifest 清单；
- Bootstrap 不得接管本次移动产生的业务装配；
- 保持 Context/Fiber/Effect 使用官方 `cordis-py`，不增加本地 fallback。

### 4. 架构门禁

更新 F14.1 测试，使其直接验证目标路径：

- `src/ftre/platform` 不存在；
- `ftre.kernel` 对业务 Owner 无 import；
- Kernel 源码不定义业务 HookSpec 常量；
- 全仓生产 import 不引用 `ftre.platform`；
- Plugin Runtime 的加载、失败、pending、unload/restart 契约仍成立。

## 三、注释规范

- 新 `kernel` 包级文档必须用中文说明“机制层”和禁止边界。
- 对 in-flight drain、逆序 dispose、scope carrier、依赖 pending 等非显然语义补充原因注释。
- 搬迁注释时同步更新路径和 Owner；删除已经描述旧平台分层的陈旧注释。
- 不用大量分隔线和逐行翻译增加视觉噪音；公共类型和生命周期入口必须有准确 docstring。

## 四、清理与验证

重点扫描：

```powershell
rg -n "ftre\.platform|src/ftre/platform" src tests packages docs
rg -n "AGENT_|SESSION_|TOOL_|PROMPT_|INBOX_|COMPACTION_" src/ftre/kernel
python -m pytest -q tests/architecture tests/hooks tests/lifecycle tests/startup
python -m pytest -q
python -m ruff check src tests packages
git diff --check
```

删除迁移产生的空目录、缓存、重复 `__all__`、陈旧测试替身和未使用 import。不要删除用户运行数据
或仓库外缓存。全量测试失败必须修复或如实阻塞，禁止跳过。

## 五、收尾与停止条件

- 更新 F14 PRD 变更记录、执行报告和真实路径文档；只有证据齐全才标 `F14.2 done`。
- 按职责分批 commit，commit 前重读规范；不得 push。
- 停止前确认旧目录和旧 import 为零、专项与全量门禁通过、工作树无本批未提交文件。
- 汇报提交、移动/删除清单、HookSpec 新 Owner、测试结果和第 03 批输入。
