# PRD-E1-session-search

## 元信息

| 字段 | 值 |
|---|---|
| 阶段 | E1 |
| 名称 | 会话内容搜索（后端内存检索与接口） |
| 状态 | approved |
| 创建日期 | 2026-08-17 |
| 定稿日期 | 2026-08-17 |
| 关联文档 | docs/TODO.yaml 阶段 E1；配套前端 ftre-desktop D1 |

## 1. 背景与目标

- **背景**：会话历史落在 `~/.ftre/sessions/<sid>/state.json`，单文件可达数十 MB。用户需要按关键字搜历史会话（标题或消息正文）。关键事实：网关启动时 `JsonStateStore.load_all()` 已把全部 state 加载进内存，运行期读写均在内存——"查询卡死"的唯一来源是查询时读盘解析 JSON，而这没有必要。
- **目标**：在内存态上做子串检索（`asyncio.to_thread` 不阻塞事件循环），提供 `GET /sessions/search`，返回标题命中优先、更新时间倒序的结果集，每会话带最多 3 条命中摘要。
- **非目标**：不引入全文索引库等第二套存储（已评估并否决 FTS5 旁路索引：单用户量级内存扫描即百毫秒级，索引带来的同步/一致性复杂度不成正比）；不做正则/高级语法；不做分页游标。

## 2. 需求范围

### 2.1 功能需求

- [ ] FR1：`GET /sessions/search?q=&limit=&workspace=`——按关键字检索会话标题与 user/assistant 消息正文；标题命中排前，组内按更新时间倒序；空 `q` 返回空集。
- [ ] FR2：中文任意长度子串可检索（1 字起），大小写不敏感（对 ASCII）。
- [ ] FR3：`workspace` 参数可选精确过滤；`limit` 截断结果，`total` 为过滤后会话总数。
- [ ] FR4：每会话最多 3 条命中摘要（按消息序倒序取最近），摘要为命中位置前后各约 80 字符，不返回整条超长消息。
- [ ] FR5：检索在工作线程执行，不阻塞事件循环；查询即时反映最新内存态（新消息/改标题/删会话无需任何同步机制）。

### 2.2 非功能需求

- **性能**：真实个人数据量级（~50MB 文本）单次检索 < 600ms；极端压力量级（~220MB，多年积累上限）< 2.5s（CPython C 层子串扫描吞吐上限）。检索全部在 `asyncio.to_thread` 工作线程执行，任何量级都不阻塞事件循环/UI。纯中文查询跳过大小写折叠分配（省 ~40% 耗时）。
- **健壮性**：消息内容形态（多 text part / 空 / 超长）全部容错；摘要截断，不复制放大。
- **兼容性**：纯标准库，无新依赖、无新存储。

## 3. 技术方案

- `src/ftre/session/search.py`：纯函数 `search_sessions(states, q, limit, workspace) -> dict`。输入内存快照 `list[(sid, AgentStateFile)]`，输出与接口一致的 dict。无状态、无 I/O，单测直接构造数据即可。
- `src/ftre/session/manager.py`：`SessionManager.search_sessions(q, limit, workspace)`——取 `repository.all_states()` 快照 + `asyncio.to_thread(search_sessions, ...)`。
- `src/ftre/api/routes.py`：`GET /sessions/search` 调 manager；manager 未注入时 503（与现有路由一致风格）。
- **main.py 零改动**：无新组件、无生命周期。

### 性能细节

- 标题先判（命中即置 `title_matched`，正文仍扫）；正文逐条 `q_lower in text_lower`，命中才做摘要；
- 小写化仅 ASCII（`str.lower()`），避免中文全角误折叠；
- 不预建任何缓存——数据已在内存，扫描成本与收益不匹配时再议。

## 4. 接口定义

`GET /sessions/search?q=关键字&limit=30&workspace=E:/x` →

```json
{
  "query": "关键字", "total": 2,
  "results": [
    {
      "session_id": "ws_sess_x", "title": "...", "workspace": "E:/x",
      "channel": "ws", "updated_at": "2026-08-17T01:00:00+08:00",
      "title_matched": true,
      "hits": [{"mid": "msg_1", "role": "user", "snippet": "...关键字..."}]
    }
  ]
}
```

## 5. 验收标准

- [ ] AC1：中文 1 字/2 字/≥3 字子串、标题关键字均可命中对应会话（单测断言）。
- [ ] AC2：`workspace` 过滤与 `limit`/`total` 行为正确（单测断言）。
- [ ] AC3：摘要含命中位置且长度受限；多模态消息文本可被检索（单测断言）。
- [ ] AC4：性能基准：真实规模（~55MB）< 600ms；极端规模（~220MB，最坏全扫）< 2.5s（单测内计时断言，防止回归为逐字节 Python 循环）。
- [ ] AC5：pytest 全量回归不劣化。

## 6. 测试计划

- 单测 `tests/test_session_search.py`：内存构造 sessions 覆盖 FR1~FR4 + AC4 性能基准。
- 手动：`curl "http://127.0.0.1:48650/api/sessions/search?q=..."` 实测延迟。

## 7. 变更记录

| 日期 | 变更内容 | 理由 |
|---|---|---|
| 2026-08-17 | 初始定稿 | — |
| 2026-08-17 | 技术路线由 FTS5 旁路索引改为内存态直接检索 | state.json 启动即全量在内存；单用户量级内存扫描即达百毫秒级，第二套存储与增量同步复杂度不成正比（用户决策） |
