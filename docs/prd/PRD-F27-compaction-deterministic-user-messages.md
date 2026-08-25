# PRD-F27 Compaction 用户消息确定性生成

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | F27 |
| 名称 | Compaction 用户消息确定性生成 |
| 状态 | 已验收 |
| 创建日期 | 2026-08-25 |
| 定稿日期 | 2026-08-25 |
| 验收日期 | 2026-08-25 |
| 关联文档 | `docs/TODO.yaml` F27；`PRD-F26-compaction-token-chunks.md`；`AGENTS.md` |

## 1. 背景与目标

当前摘要提示词要求 LLM 额外输出 `all_user_messages` 节点。该节点只是对历史中真实用户消息
的机械汇总，不需要模型推理，却会增加每个 chunk 的输出负担和等待时间。本阶段由代码从同一
次压缩快照中提取真实用户消息，确定性写入摘要；LLM 只负责需要理解和归纳的节点。

同时将默认 chunk 上限从 100K 调整为 200K，仍保留配置覆盖和安全边界。

非目标：不修改 Agent Core、Inbox、Session 持久化协议、压缩 Hook、客户端协议或摘要节点的
其他语义；不新增合并 LLM。

## 2. 需求范围

### 2.1 功能需求

- [x] FR1：从压缩输入快照中按顺序提取 `role=user` 且 `name=default` 的真实用户文本，跳过
  `compact`、`compact_fast` 和隐藏系统摘要。
- [x] FR2：LLM prompt 和 XML 解析不再要求/接受 `all_user_messages`；Service 在本地合并时
  生成该节点，保留原始顺序和完整文本。
- [x] FR3：`DEFAULT_CHUNK_TOKENS` 改为 `200000`；`chunkTokens` 仍可通过配置覆盖并受
  16K–1M 边界保护。
- [x] FR4：日志继续记录块数、每块 token、模型、重试和耗时；增加确定性用户消息条数，禁止
  记录 API key 或完整 prompt。

### 2.2 非功能需求

- 正确性：用户消息节点不依赖 LLM 输出，不因模型漏节点、重复节点或格式错误而丢失。
- 性能：每个 chunk 减少一个无需推理的输出节点，默认块数降低但单次输入仍受 200K 上限约束。
- 可配置：显式 `chunkTokens` 配置优先于默认值；已有并发、超时和重试配置不变。

## 3. 技术方案

- `packages/ftre-compaction/src/ftre_compaction/service.py`：分离 LLM 节点列表与完整输出节点
  列表；新增纯函数生成用户消息节点，并在确定性合并阶段注入。
- `packages/ftre-compaction/src/ftre_compaction/config.py`：默认 chunk token 改为 200000。
- `packages/ftre-compaction/tests/`：覆盖过滤摘要、顺序保留、LLM 不再收到该节点、默认值和
  自定义覆盖。

## 4. 验收标准

- [x] AC1：包含普通用户、compact 和 compact_fast 消息的快照只把普通用户消息写入
  `all_user_messages`，顺序与原始消息一致。
- [x] AC2：LLM prompt 的 XML 节点列表不包含 `all_user_messages`；即使 LLM 输出该节点也不会
  覆盖代码生成结果。
- [x] AC3：无显式配置时 chunk 上限为 200000；显式配置和边界校验保持有效。
- [x] AC4：F27 专项测试、ftre-compaction 全量测试、ruff、Package build 和 diff check 通过。

## 5. 测试计划

- 用户消息提取：空消息、多文本块、普通消息与摘要消息混合、顺序和 Unicode。
- Prompt：节点白名单和 LLM 输入隔离。
- 配置：默认 200K、显式 100K/300K、非法值边界。
- 回归：chunk 并发、失败重试、确定性合并、compact done/failed 事件。

## 6. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-25 | 新建 F27；用户消息清单改为代码生成，默认 chunkTokens 改为 200000 | 去除机械性 LLM 输出并调整默认压缩粒度 |
| 2026-08-25 | 完成 F27；ftre 全量 544 项测试、ruff、Package wheel/sdist 和 diff check 通过 | 确定性用户消息输出与 200K 默认块完成验收 |
