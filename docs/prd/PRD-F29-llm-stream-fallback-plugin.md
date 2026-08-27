# PRD-F29 LLM Stream Fallback Plugin

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F29 |
| 名称 | LLM Stream Fallback Plugin |
| 状态 | 已验收 |
| 创建日期 | 2026-08-25 |
| 定稿日期 | 2026-08-25 |
| 验收日期 | 2026-08-25 |
| 关联文档 | `docs/TODO.yaml` F29；`E:\\ftre-agent-core` C6；`PRD-F28-llm-recovery-plugin.md`；`AGENTS.md` |

## 1. 背景与目标

在 Core 完成前序 Retry 后，允许一个独立 Plugin 在最后一次主模型 attempt 失败且没有有效输出时，
调用备用模型并把备用模型的 `StreamChunk` 原样交回 Core。该 Package 不复制 Retry Loop，
不改变 Agent 主模型配置，也不接触 Session/Inbox。

### 非目标

- 不决定 Core 是否 Retry；该职责属于 `ftre-llm-recovery` 的 `llm/error`；
- 不处理 `overflow/context_length/too_long`，继续交给 `ftre-compaction`；
- 不在已有正文、思考或 Tool Call 后切换；
- 不递归 fallback，不在备用模型失败后再次创建备用模型；
- 不新增公共 `FallbackService`、Port 或 Coordinator。

## 2. 需求范围

- [x] **FR1：独立 Package 与入口**
  - 新增 `packages/ftre-llm-fallback/`；
  - entry point：`ftre_llm_fallback.plugin:apply`；
  - `inject = ("config", "hook_runtime")`；
  - 所有 Hook Receipt 与资源绑定 Plugin Fiber。

- [x] **FR2：最后一次 attempt 才 fallback**
  - `attempt < max_attempts` 时只调用主模型并把错误交回 Core；
  - `attempt == max_attempts` 且没有有效输出时才选择备用模型；
  - 备用模型成功时不把主模型错误 FinishChunk 交给 Core。

- [x] **FR3：错误与取消安全**
  - 只处理配置允许的错误码；
  - `overflow/context_length/too_long` 永不接管；
  - 取消信号置位、已有有效输出或 Tool Call 时不 fallback；
  - 备用模型失败后交回原始错误，不再次 fallback。

- [x] **FR4：模型解析边界**
  - 通过公开 ConfigService 的模型解析能力取得 provider/model/api_type/credentials；
  - 若 Host 尚无稳定解析方法，F29 先补 `ConfigService.resolve_llm()`，不得 import
    AgentManager、TurnExecutor 或另一个 Plugin 的私有实现；
  - 日志不得包含 API Key。
  - `ConfigService.resolve_llm(provider, model)` 返回一次性 Adapter 配置快照；找不到模型
    或凭据不完整时放弃 fallback，不在 Package 内读取文件或 AgentProfile。

- [x] **FR5：默认安装与可选启用**
  - 按 ftre 默认 Package 安装约定加入根发行依赖和 entry point；
  - Plugin 可通过配置禁用；禁用后 `llm/stream` 回到主模型直连，Core Retry 不受影响。

- [x] **FR6：流协议和资源清理**
  - 主模型错误 finish 在满足 fallback 条件时不得转发给 Core；备用成功流只转发一次；
  - 主流已产生任意协议输出、取消、非白名单错误或备用失败时不切换且不递归；
  - 主流在错误切换前显式关闭，备用 Adapter 不拥有第二个 Hook/Retry 生命周期。

## 3. 配置示例

```json
{
  "plugins": [
    {
      "id": "llm-fallback",
      "enabled": true,
      "config": {
        "provider": "OpenCode 直连",
        "model": "deepseek-v4-flash",
        "errors": ["auth_error", "bad_request", "content_filter"],
        "exclude_errors": ["overflow", "context_length", "too_long"]
      }
    }
  ]
}
```

模型只在最后一次 attempt、主模型无有效输出时调用。配置不包含独立重试次数。

## 4. 验收标准

- [x] **AC1**：前 N-1 次主模型失败均由 Core Retry 处理，fallback 调用次数为 0。
- [x] **AC2**：最后一次主模型无输出失败时，备用模型最多调用一次。
- [x] **AC3**：备用模型成功时，Core 只收到一条成功 StreamChunk 流，不产生重复文本/Tool Call。
- [x] **AC4**：主模型已输出、取消、overflow、未知错误和备用失败均不发生递归 fallback。
- [x] **AC5**：Package 未安装/禁用时 Gateway、Core Retry、Compaction 和普通 Turn 不回归。
- [x] **AC6**：unload/restart/in-flight、配置快照、日志脱敏和独立 wheel/洁净安装通过。
- [x] **AC7**：Core/ftre 全量质量门禁、Gateway smoke 和默认/禁用两种 Composition 均通过。

## 5. 测试计划

- 主模型前 N-1 次失败、最后一次 fallback 成功；
- 错误 FinishChunk 与直接异常两条路径；
- 部分正文/思考/Tool Call 后禁止切换；
- 取消、overflow、未知错误、备用失败和重复 fallback；
- 与 `ftre-llm-recovery`、`ftre-compaction` 同时启用的 Hook 顺序与生命周期；
- architecture、contracts、lifecycle、startup、全量 pytest、ruff、wheel 和 Gateway smoke。

## 6. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-25 | 初始草案：将 Stream Fallback 从 F28 拆为独立 Package，消费 `llm/stream` 的 attempt/max_attempts | Retry Policy 与模型切换属于两个不同 Owner，必须分别生命周期管理和验收 |
| 2026-08-25 | 完成 `ftre-llm-fallback`、ConfigService.resolve_llm、最后一次无输出切换、失败/取消/协议和 Fiber 生命周期回归 | fallback 不复制 Retry Loop，不读取 Agent/Session 私有状态，也不递归调用自身 |
| 2026-08-25 | 配合 Core B2 Responses 修复：Host Session 保留 `response_metadata.output_items`，下一轮仅通过 Responses 适配器筛选重放，Chat Completions 丢弃该扩展字段 | 避免跨协议泄漏传输元数据，并修复 thinking 请求携带返回态 `status` 的 400 |
