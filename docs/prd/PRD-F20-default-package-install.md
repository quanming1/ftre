# PRD-F20 ftre 默认 Package 安装与装配

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F20 |
| 名称 | ftre 默认安装全部仓内 Package |
| 状态 | 已验收 |
| 创建日期 | 2026-08-24 |
| 定稿日期 | 2026-08-24 |
| 验收日期 | 2026-08-24 |
| 关联文档 | `docs/TODO.yaml` F20；`AGENTS.md`；`docs/PROCESS.md` |

## 1. 背景与目标

当前 `ftre` 的根发行物只安装 Host 依赖，`packages/` 下的 Inbox、Compaction、Messaging、
Task、Team 还需要用户分别选择 extras 或手工安装。这会造成“源码存在但 Plugin 没有被发现”、
客户端命令缺失和默认运行行为不一致。

本阶段把仓库内的五个 Package 定义为 ftre 默认发行组合：安装 `ftre` 即安装所有 Package，
默认 Composition 也加载这些 Package 的 Plugin。能力仍可通过配置禁用或卸载；Package 的
职责和独立 entry point 不合并、不复制。

非目标：本阶段不修改客户端、不改变消息协议、不把 Package 源码复制进 Host、不发布 PyPI，
不改变各 Package 的 Service/Hook/Tool Owner。

## 2. 需求范围

### 2.1 功能需求

- [x] FR1：根项目的默认发行依赖包含 `ftre-inbox`、`ftre-compaction`、`ftre-messaging`、
  `ftre-task`、`ftre-team`，`pip install ftre` 后五个发行物均可被 metadata 发现。
- [x] FR2：根项目保留按能力选择的 extras 兼容入口，但 extras 不再是默认安装的唯一方式。
- [x] FR3：默认 Composition 对五个 Package 声明唯一 Plugin Manifest，并在 Package 已安装时
  默认加载；配置 `enabled: false` 仍可禁用非必选业务 Package。
- [x] FR4：`ftre-compaction` 默认加载后注册 `/compact`、`/compress-fast`，命令来源为
  `ftre-compaction`；未请求的 `/compress` 不凭空添加。
- [x] FR5：安装契约、默认装配、禁用语义和 entry point 均有自动化测试；测试使用本地源码或
  已安装 editable Package，不依赖真实 LLM。

### 2.2 非功能需求

- Package 仍是独立发行边界，Host 只通过 Plugin entry point 和公开 Service/Hook 协作。
- 默认安装不引入第二个 Owner、全局 setter、Service Locator 或兼容壳。
- 缺少可选 Package 时仍可生成诊断并保持基础 Host 可启动；正式发行的默认安装必须包含全部五包。

## 3. 技术方案

1. 在根 `pyproject.toml` 的 `[project].dependencies` 声明五个 Package 的版本范围；保留
   `inbox`、`compaction`、`messaging`、`task`、`team`、`full` extras 作为显式组合兼容入口。
2. 在 `src/ftre/app/gateway/composition.py` 增加 `compaction` Manifest；其余四包已有 Manifest
   继续由 Composition 统一声明，entry 使用各 Package 的 `module:attribute`。
3. 默认 Manifest 使用 `default_enabled=True`、`required=False`（Inbox 仍是当前 Gateway 的
   必选数据面）；业务 Package 可由配置禁用，不影响核心 Agent。
4. 测试验证默认依赖集合、entry point、Composition 状态和命令注册；同时保留 Package 独立
   wheel 的发行边界测试。

## 4. 接口定义

默认安装后的稳定命令列表包含：

```text
/compact       → ftre-compaction CompactionService（LLM 摘要压缩）
/compress-fast → ftre-compaction CompactionService（无 LLM 快速裁剪）
```

`/compress` 不是本阶段协议；如需该别名，另立命令兼容变更。

## 5. 验收标准

- [x] AC1：解析根 `pyproject.toml` 时，默认 dependencies 包含五个 `packages/` 发行物。
- [x] AC2：`PluginDiscovery` 能发现 `inbox`、`compaction`、`messaging`、`task`、`team` 五个 entry point。
- [x] AC3：默认 Composition 中五个 Package 的 Manifest 被选中；禁用一个业务 Package 后只
  移除该 Package 的行为，不影响 Agent 和 Inbox。
- [x] AC4：默认 Composition 的 CommandService 返回 `/compact` 与 `/compress-fast`，来源均为
  `ftre-compaction`。
- [x] AC5：`python -m pytest -q tests/architecture tests/lifecycle tests/startup packages/*/tests`
  通过，`ruff` 和 `git diff --check` 通过。
- [x] AC6：Gateway `/api/commands` 返回压缩命令，客户端刷新后能从现有动态命令接口读取它们。

## 6. 测试计划

- 静态测试：根依赖、Package 元数据、entry point 和 Composition Manifest。
- 生命周期测试：默认加载、业务 Package 禁用、Composition close/unload。
- 行为测试：命令列表包含两个压缩命令，压缩逻辑继续由 Package 自有测试覆盖。
- 手动验证：启动 Gateway，GET `/api/commands`，确认命令来源和名称。

## 7. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-24 | 初始定稿：默认安装并装配 `packages/` 下五个 Package；保留单能力 extras | 避免安装成功但 Plugin 未发现，保证默认运行能力完整 |
| 2026-08-24 | 完成根依赖、Composition、测试和 Gateway smoke；FR1-FR5、AC1-AC6 全部通过 | 默认安装约定落地并验证压缩命令可见 |
