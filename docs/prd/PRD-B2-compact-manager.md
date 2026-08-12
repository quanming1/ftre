# PRD-B2-CompactManager

> 状态生命周期：草稿 → 评审 → approved（定稿）→ 开发中 → 已验收

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | B2 |
| 名称 | CompactManager 上下文压缩（should_compact + do_compact + compress-fast + idle 策略） |
| 状态 | 已验收 |
| 创建日期 | 2026-08-12 |
| 定稿日期 | 2026-08-12 |
| 验收日期 | 2026-08-12 |
| 关联文档 | docs/TODO.yaml 阶段 B2；AGENTS.md |

## 1. 背景与目标

- **背景**：随着对话轮次增加，上下文窗口会溢出（token 数超过模型限制）。需要 LLM 压缩摘要机制，将历史消息折叠为摘要，释放上下文空间。同时需要零 LLM 的快速裁剪方案处理 ToolResultBlock 等大体积内容。
- **目标**：实现 CompactManager——水位判断触发压缩、LLM 摘要压缩、手动 `/compact` 和 `/compress-fast` 命令、共享 Task 去重防止重复压缩。
- **非目标**：不实现 TurnExecutor 状态机（B3）、不实现压缩结果的前端展示。

## 2. 需求范围

### 2.1 功能需求

- [x] FR1：should_compact 水位判断——检查当前上下文 token 数是否超过阈值（threshold），超过则触发压缩
- [x] FR2：do_compact LLM 摘要——调用 LLM 将历史消息压缩为摘要，替换原始消息，保留最近若干轮
- [x] FR3：`/compact` 手动压缩——用户发送 `/compact` 命令手动触发 LLM 压缩
- [x] FR4：`/compress-fast` 零 LLM 裁剪——不调用 LLM，直接裁剪 ToolResultBlock 等大体积内容，快速释放空间
- [x] FR5：共享 Task 去重——同一 session 的压缩请求复用同一个 asyncio.Task，不重复执行
- [x] FR6：cancel_compact——取消正在进行的压缩任务

### 2.2 非功能需求

- 性能：compress-fast 耗时 < 100ms（无 LLM 调用）
- 安全：压缩不丢失 UserMessage 和最近 N 轮 AssistantMessage
- 兼容性：压缩后的摘要消息标记 `compressed: true`，可被识别

## 3. 技术方案

### 模块设计

| 文件 | 职责 |
|---|---|
| `src/ftre/agent/compact_manager.py` | `CompactManager`，should_compact + do_compact + compress-fast + Task 去重 |
| `src/ftre/agent/compact_events.py` | 压缩相关事件定义（CompactStartEvent / CompactEndEvent） |

### 关键数据结构

```python
class CompactManager:
    threshold: int               # token 水位阈值
    _tasks: dict[str, asyncio.Task]  # session_id → 压缩 Task（去重）

    async def should_compact(self, session_id: str) -> bool: ...
    async def do_compact(self, session_id: str) -> None: ...
    async def compress_fast(self, session_id: str) -> None: ...
    async def cancel_compact(self, session_id: str) -> None: ...
```

## 5. 验收标准

- [x] AC1：水位 ≥ threshold 触发压缩——上下文 token 数超过阈值时自动触发 do_compact
- [x] AC2：共享 Task 不重复——同一 session 并发触发两次压缩，只执行一次 LLM 调用
- [x] AC3：compress-fast 裁剪 ToolResultBlock——执行后 ToolResultBlock 被替换为占位摘要，不调用 LLM
