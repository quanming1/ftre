# PRD-F28 LLM Error Recovery Plugin

> 本阶段是 Core C5 的 ftre 配套阶段：提供可配置的 LLM 错误恢复策略 Plugin；Stream Fallback 另见 F29。

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F28 |
| 名称 | LLM Error Recovery Plugin |
| 状态 | 已验收 |
| 创建日期 | 2026-08-25 |
| 定稿日期 | 2026-08-25 |
| 验收日期 | 2026-08-25 |
| 关联文档 | `docs/TODO.yaml` F28；`docs/PROCESS.md`；`E:\\ftre-agent-core` C5；`AGENTS.md` |

## 1. 背景与目标

ftre 当前只能在 Core 内部完成默认重试，宿主 Plugin 无法在一次 LLM attempt 失败后声明“重试”或
“停止”。`agent/run-error` 发生在 Core 内部重试结束之后，`llm/stream` 只适合包裹一次调用，
两者都不适合作为通用 Retry Policy Owner。

本阶段建立独立的 `ftre-llm-recovery` Plugin/Package：它只消费 Core C5 的
`llm/error`，读取自己的配置并返回 retry/stop 决策；它不创建 Agent、不持有 Turn、
不访问 Session Repository、不复制 Core Retry Loop。

### 非目标

- 不修改客户端、WebSocket、Inbox、Session 持久化和压缩算法；
- 不让 Plugin 直接替换 Core 私有 LLM Adapter；Stream Fallback 属于 F29，不在本阶段实现；
- 不把 `ftre-compaction` 的 overflow 恢复迁移到本包；
- 不把 `send_message`、Task、Team 或 Command 纳入失败策略；
- 不增加 `RetryService`、`RetryPort`、Coordinator 或全局状态容器。

## 2. 需求范围

### 2.1 功能需求

- [x] **FR1：Host 公开重导出**
  - `ftre.services.llm.hooks` 重导出 Core C5 的同一 `LLM_ERROR_SPEC`；
  - 不定义重复 Payload、Result 或 Spec。

- [x] **FR2：独立 Package 入口**
  - 新增 `packages/ftre-llm-recovery/`，入口为 `ftre_llm_recovery.plugin:apply`；
  - Plugin 通过 Manifest 局部 `config` 参数和 `inject = ("hook_runtime",)` 获取公开依赖；
  - Hook Receipt、配置 watcher（如需要）和资源全部绑定当前 Cordis Fiber；
  - unload 后不再处理新失败，in-flight Hook 等待排空。

- [x] **FR3：策略配置**
  - 配置位于该 Plugin 自己的 `plugins[].config`；
  - 支持按 `error_code` 设置 `retry`/`stop` 和退避建议；重试次数只由 Core `max_retries` 决定；
  - 缺失、非法或冲突配置回退 `None`，让 Core 使用默认策略；
  - 不记录或回显 API Key。

- [x] **FR4：策略不突破 Core 安全边界**
  - Plugin 返回 retry 也必须受 Core `max_retries`、取消信号和 attempt 上限约束；
  - Plugin 不发送 `RetryEvent`，不修改 AgentState，不直接读取或写入 Session；
  - 一个失败 attempt 最多产生一次决策。

- [x] **FR5：与现有能力协同**
  - overflow/context_length/too_long 默认不由本包接管，继续交给 `ftre-compaction`；
  - `agent/run-error` 仍保留其 Turn 级恢复语义。

### 2.2 非功能需求

- **可选性**：未安装/未启用该 Package 时 Gateway 正常启动，Core 默认重试完全不变；
- **隔离**：Package 只能依赖公开 Core Hook、`config` 和 `hook_runtime`，不得 import AgentLoop、
  TurnExecutor、Repository 或另一个 Plugin 的私有模块；
- **可观测**：日志记录 owner、session/turn、错误码、动作和 attempt，不记录正文/API Key；
- **生命周期**：Plugin restart 不残留 listener、watcher、Task 或旧配置闭包。

## 3. 技术方案

```text
Core ReasoningExecutor
        │  dispatch
        ▼
llm/error (Core Spec)
        │
        ├─ ftre-llm-recovery Plugin
        │      └─ 读取 Plugin 配置 → 返回 None/retry/stop
        │
        └─ Core RetryExecutor
               └─ 执行真正的重试、事件、延迟和收尾
```

### 3.1 建议目录

```text
packages/ftre-llm-recovery/
├─ pyproject.toml
├─ README.md
├─ src/ftre_llm_recovery/
│  ├─ __init__.py
│  ├─ plugin.py       # apply、inject、Hook 注册与 Effect
│  ├─ config.py       # 本包配置快照和非法值降级
│  └─ policy.py       # 纯函数：Payload → None/LLMErrorDecision
└─ tests/
   ├─ test_policy.py
   ├─ test_recovery_plugin.py
   └─ tests/lifecycle/test_f14_lifecycle_matrix.py
```

### 3.2 配置示例

```json
{
  "plugins": [
    {
      "id": "llm-recovery",
      "enabled": true,
      "config": {
        "rules": {
          "rate_limit": {"action": "retry", "delay": 2.0},
          "timeout": {"action": "retry"},
          "bad_request": {"action": "stop"}
        },
        "exclude_codes": ["overflow", "context_length", "too_long"]
      }
    }
  ]
}
```

配置不再声明独立的 `max_attempts`；重试次数只由 Core 的 `max_retries` 决定。`delay` 是建议值，
由 Core 负责非负化；没有匹配规则必须返回 `None`，不能把未知错误静默改成 retry。

## 4. 接口定义

### 4.1 Plugin Manifest

```toml
[project.entry-points."ftre.plugins"]
llm-recovery = "ftre_llm_recovery.plugin:apply"
```

`provide` 为空；该 Plugin 是无状态行为贡献，不为单一策略增加公共 Service key。

### 4.2 失败策略行为

| 条件 | Package 行为 |
|---|---|
| 未启用 | 不注册 Hook，Core 默认策略 |
| 未匹配错误 | 返回 `None`，Core 默认策略 |
| 匹配 retry | 返回 retry 建议，Core 执行并受自己的 `max_retries` 硬上限约束 |
| 匹配 stop | 返回 stop，Core 结束当前 Reasoning |
| overflow/context_length | 默认放行给 compaction/默认策略 |
| Hook 本身异常 | 诊断 + fail-open，不能让 Gateway 崩溃 |

## 5. 验收标准

- [x] **AC1**：Core C5 的 Spec 在 ftre 中是同一对象，不能出现重复 Owner。
- [x] **AC2**：Package 未安装/禁用时，Gateway、普通 Agent Turn 和现有重试测试通过。
- [x] **AC3**：匹配 retry 的错误最多按 Core 上限执行，RetryEvent 数量和 attempt 一致。
- [x] **AC4**：匹配 stop 的错误不再执行下一次 attempt，原始错误码保留。
- [x] **AC5**：未知错误、非法配置、Hook 异常均 fail-open，不静默扩大重试。
- [x] **AC6**：overflow/context_length 不被本包吞掉，压缩 Package 的现有恢复测试不回归。
- [x] **AC7**：Plugin unload/restart、in-flight drain 和配置快照隔离测试通过。
- [x] **AC8**：Package wheel/sdist 不包含缓存、测试数据或 Host 私有源码；洁净安装后 entry point 可发现。
- [x] **AC9**：ftre architecture/lifecycle/startup、Core pytest/ruff、Package tests 和 Gateway smoke 全部通过。

## 6. 测试计划

- Core C5 合同测试：Payload 校验、None/retry/stop、Hook 异常和硬上限；
- ftre Package 单测：规则匹配、未知错误、排除码、非法配置和日志脱敏；
- 生命周期：启用、禁用、unload、restart、in-flight 调用；
- 跨包：与 `ftre-compaction` 同时启用时 overflow 归属不冲突；
- 独立发行：wheel、洁净虚拟环境安装、最小 Composition 启停和普通 Turn；
- 回归：`tests/test_execute_reasoning.py`、`tests/test_turn_lifecycle.py`、
  `tests/contracts/test_f7_hook_pipeline.py` 及全量质量门禁。

## 7. 评审结论

当前 ftre 已有可用的 Core Dispatcher、Agent scope 和 Fiber Effect；阻断点只有 Core 尚未发布
`llm/error` 契约。直接把 Retry Loop 迁入本包会制造第二个状态机，违反轻内核原则；
本 PRD 采用“策略 Plugin + Core 执行器”的最小边界，不增加 `RetryService` 或 `Port`。

## 8. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-25 | 历史草案曾消费 `llm/attempt-failed`（已废弃），现改为 Core `llm/error`；只贡献 retry/stop 策略 | 当前 `llm/stream` 无生产消费者，`agent/run-error` 触发过晚；需要可选、可卸载且不复制 Core 状态机的 Retry Owner |
| 2026-08-25 | Hook 名称改为 `llm/error`；移除 fallback 时序、`LLMStreamPayload` 和 Plugin 独立 `max_attempts`，将 Stream Fallback 拆到 F29 | F28 只拥有 retry/stop 策略，避免两个恢复 Owner 混在一个 Package |
| 2026-08-25 | 实现并完成 Core/ftre 跨仓测试；配置通过 Manifest 局部快照传入，HookRuntime 作为唯一注入能力 | 保持 Package 无状态，避免策略配置与 Host ConfigService 形成双重 Owner |
