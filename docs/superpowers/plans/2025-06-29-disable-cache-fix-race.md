# 禁用本地缓存 + 修复 HTTP/WS 竞争条件 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 禁用前端 session 消息本地缓存，每次 switchSession 都走 HTTP 拉 DB 历史 + WS attach 获取 replay/live 流，并修复两者并行导致的竞争条件。

**Architecture:** 将 `switchSession` 中的 WS attach 从 `switchTo`（同步）移到 HTTP fetch 完成后的 `.then()` 回调中（异步串行）。`switchTo` 不再调用 `subscribeOnly`。`loadSessionEvents` 的 hydrate 模式移除 `b.messages.length > 0` 保护（因为禁用缓存后 bucket 一定为空）。每次 switchSession 都清空目标 bucket。

**Tech Stack:** TypeScript, React, Zustand, vitest

## Global Constraints

- 前端仓库路径：`E:\binn\ftre-desktop`
- 暂时禁用本地缓存：每次 switchSession 都清空 bucket，走 HTTP + WS
- HTTP 必须先于 WS attach 完成，保证 `loadSessionEvents` 执行时 bucket 无 volatile 帧干扰
- `switchTo` 不再负责 WS attach，由 `switchSession` 控制时序
- 不修改后端代码

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `packages/renderer/src/stores/session.ts:263-307` | `switchSession` 时序控制 | **修改** |
| `packages/renderer/src/stores/chat.ts:1008-1015` | `switchTo` 移除 subscribeOnly | **修改** |
| `packages/renderer/src/stores/chat.ts:1037-1041` | `loadSessionEvents` 移除 hydrate 保护 | **修改** |
| `packages/renderer/src/stores/chat.ts:1032-1034` | `hasSessionCache` 始终返回 false | **修改** |

---

### Task 1: switchTo 移除 subscribeOnly，switchSession 控制时序

**Files:**
- Modify: `E:\binn\ftre-desktop\packages\renderer\src\stores\chat.ts:1008-1015`（`switchTo`）
- Modify: `E:\binn\ftre-desktop\packages\renderer\src\stores\session.ts:263-307`（`switchSession`）

**Interfaces:**
- Consumes: `wsClient.subscribeOnly`, `fetchSessionMessagesPage`, `useChat.getState().loadSessionEvents`
- Produces: `switchTo` 不再调用 `subscribeOnly`；`switchSession` 在 HTTP `.then()` 中调用 `subscribeOnly`

- [ ] **Step 1: 修改 `switchTo` — 移除 subscribeOnly 调用**

将 `chat.ts` 中 `switchTo`（约 line 1008-1015）从：

```typescript
  switchTo: (sessionId, initialMessages) => {
    const b = bucket(sessionId);
    if (initialMessages && b.messages.length === 0) b.messages = initialMessages;
    set({ sessionId, messages: b.messages, isBusy: b.isBusy, error: b.error, retryState: b.retryState, contextTokens: 0, tokenUsage: null });
    wsClient.subscribeOnly(sessionId);
    // 异步拉一次最新 token 估算（不阻塞 UI 切换）
    void get().refreshTokenUsage(sessionId);
  },
```

改为：

```typescript
  switchTo: (sessionId, initialMessages) => {
    const b = bucket(sessionId);
    if (initialMessages && b.messages.length === 0) b.messages = initialMessages;
    set({ sessionId, messages: b.messages, isBusy: b.isBusy, error: b.error, retryState: b.retryState, contextTokens: 0, tokenUsage: null });
    // WS attach 移到 switchSession 的 HTTP .then() 中，保证 HTTP 先于 WS，
    // 避免 replay 帧和 loadSessionEvents 的清空操作竞争。
    // 异步拉一次最新 token 估算（不阻塞 UI 切换）
    void get().refreshTokenUsage(sessionId);
  },
```

- [ ] **Step 2: 修改 `switchSession` — 清空 bucket + WS attach 移到 HTTP .then()**

将 `session.ts` 中 `switchSession`（约 line 263-307）从：

```typescript
  switchSession: async (sessionId) => {
    const { openTabs } = get();
    if (!openTabs.includes(sessionId)) {
      const tabs = [...openTabs, sessionId];
      set({ openTabs: tabs });
      saveTabsToStorage(tabs);
    }

    // 取消上一次切换的请求，避免浪费带宽
    _switchAbort?.abort();
    _switchAbort = new AbortController();
    const signal = _switchAbort.signal;

    // 先标记 loading，让 UI 立刻展示转圈，消除"卡住"的感觉
    const isFirstLoad = !useChat.getState().hasSessionCache(sessionId);
    set({ loadingSessionId: sessionId });

    // 切到目标 session（bucket 已有缓存则立即展示；无缓存则转圈盖住空白）
    useChat.getState().switchTo(sessionId);
    try { localStorage.setItem(sessionStorageKey(), sessionId); } catch { }

    // 缓存命中：立刻消 loading，后台静默刷新即可，不要空转圈等 HTTP
    if (useChat.getState().messages.length > 0) {
      set({ loadingSessionId: null });
    }

    // 分页拉首屏。流式期间 chat store 自己会用 mode 兜底跳过。
    fetchSessionMessagesPage(sessionId, { limit: FIRST_PAGE_EVENTS, signal } as any)
      .then((page) => {
        if (!page) return;
        useChat.getState().loadSessionEvents(
          sessionId,
          historyToEvents(page.messages),
          isFirstLoad ? "hydrate" : "refresh",
        );
        useChat.getState().prependSessionEvents(sessionId, [], page.hasMore);
      })
      .catch((err) => {
        if ((err as Error).name === "AbortError") return;
        console.error("[Session] switchSession fetch error:", err);
      })
      .finally(() => {
        if (get().loadingSessionId === sessionId) set({ loadingSessionId: null });
      });
  },
```

改为：

```typescript
  switchSession: async (sessionId) => {
    const { openTabs } = get();
    if (!openTabs.includes(sessionId)) {
      const tabs = [...openTabs, sessionId];
      set({ openTabs: tabs });
      saveTabsToStorage(tabs);
    }

    // 取消上一次切换的请求，避免浪费带宽
    _switchAbort?.abort();
    _switchAbort = new AbortController();
    const signal = _switchAbort.signal;

    // 暂时禁用本地缓存：每次切换都清空 bucket，走 HTTP + WS 全量加载
    useChat.getState().clearSessionCache(sessionId);

    set({ loadingSessionId: sessionId });

    // 切到目标 session（bucket 已清空，UI 展示 loading 转圈）
    useChat.getState().switchTo(sessionId);
    try { localStorage.setItem(sessionStorageKey(), sessionId); } catch { }

    // HTTP 先行：拉 DB 历史，loadSessionEvents 重建消息
    fetchSessionMessagesPage(sessionId, { limit: FIRST_PAGE_EVENTS, signal } as any)
      .then((page) => {
        if (!page) return;
        useChat.getState().loadSessionEvents(
          sessionId,
          historyToEvents(page.messages),
          "hydrate",
        );
        useChat.getState().prependSessionEvents(sessionId, [], page.hasMore);
        // HTTP 完成后再 WS attach：replay 只会追加到 DB 历史后面，
        // 不会和 loadSessionEvents 的清空操作竞争。
        wsClient.subscribeOnly(sessionId);
      })
      .catch((err) => {
        if ((err as Error).name === "AbortError") return;
        console.error("[Session] switchSession fetch error:", err);
      })
      .finally(() => {
        if (get().loadingSessionId === sessionId) set({ loadingSessionId: null });
      });
  },
```

- [ ] **Step 3: 添加 `clearSessionCache` 方法**

在 `chat.ts` 的 `ChatState` 接口中（约 line 856 附近 `hasSessionCache` 声明之后），添加：

```typescript
  /** 清空指定 session 的本地缓存（消息、事件、状态） */
  clearSessionCache: (sessionId: string) => void;
```

在 `chat.ts` 的 store 实现中（约 line 1032 `hasSessionCache` 实现之后），添加：

```typescript
  clearSessionCache: (sessionId) => {
    buckets.set(sessionId, emptyBucket());
    mirror(sessionId);
  },
```

同时将 `hasSessionCache` 改为始终返回 false：

```typescript
  hasSessionCache: (_sessionId) => {
    // 暂时禁用本地缓存，每次 switchSession 都走 HTTP + WS
    return false;
  },
```

- [ ] **Step 4: 修改 `loadSessionEvents` — 移除 hydrate 的 length > 0 保护**

将 `chat.ts` 中 `loadSessionEvents`（约 line 1037-1041）从：

```typescript
  loadSessionEvents: (sessionId, events, mode) => {
    const b = bucket(sessionId);
    const tail = last(b.messages);
    if (mode === "refresh" && tail?.streaming) return;
    if (mode === "hydrate" && b.messages.length > 0) return;
```

改为：

```typescript
  loadSessionEvents: (sessionId, events, mode) => {
    const b = bucket(sessionId);
    const tail = last(b.messages);
    if (mode === "refresh" && tail?.streaming) return;
    // 禁用缓存后 bucket 已在 switchSession 中清空，不需要 length > 0 保护
```

- [ ] **Step 5: 验证编译通过**

Run: `cd E:\binn\ftre-desktop\packages\renderer && node_modules\.bin\tsc.CMD --noEmit --project tsconfig.json 2>&1 | findstr "chat.ts session.ts"`
Expected: 无新增错误（已有的无关错误不受影响）

- [ ] **Step 6: 验证现有测试通过**

Run: `cd E:\binn\ftre-desktop\packages\renderer && node_modules\.bin\vitest.CMD run 2>&1`
Expected: 已有测试 PASS

- [ ] **Step 7: Commit**

```bash
cd E:\binn\ftre-desktop
git add packages/renderer/src/stores/chat.ts packages/renderer/src/stores/session.ts
git commit -m "fix: 禁用本地缓存，HTTP 先于 WS attach 消除竞争条件

- switchTo 移除 subscribeOnly，时序由 switchSession 控制
- switchSession 清空 bucket 后 HTTP 先行，.then() 中再 WS attach
- loadSessionEvents 移除 hydrate 的 length>0 保护
- hasSessionCache 始终返回 false，clearSessionCache 清空 bucket"
```

---

### Task 2: 测试 — 验证 switchSession 时序正确性

**Files:**
- Test: `E:\binn\ftre-desktop\packages\renderer\src\stores\session.test.ts`（如已存在则追加，否则新建）

- [ ] **Step 1: 检查现有测试文件**

Run: `dir E:\binn\ftre-desktop\packages\renderer\src\stores\session.test.ts 2>&1`

如果不存在，在 `chat.reducer.test.ts` 中追加测试（因为它已经测试了 reducer 行为）。

- [ ] **Step 2: 在 `chat.reducer.test.ts` 末尾追加 clearSessionCache 测试**

```typescript
describe("clearSessionCache", () => {
  it("clears bucket messages and state", () => {
    const { useChat } = await import("@/stores/chat");
    const sid = "ws::test_clear";

    // 先填充一些消息
    useChat.getState().loadSessionEvents(sid, [
      { type: "user_message", data: { metadata: { hide: false }, content: "hello" }, ts: 1000 },
      { type: "assistant_message_complete", data: { content: "hi" }, ts: 2000 },
    ], "hydrate");

    expect(useChat.getState().hasSessionCache(sid)).toBe(false); // 暂时禁用缓存

    // clearSessionCache 应该清空 bucket
    useChat.getState().clearSessionCache(sid);

    // 重新 hydrate 应该能正常工作（不被旧数据阻挡）
    useChat.getState().loadSessionEvents(sid, [
      { type: "user_message", data: { metadata: { hide: false }, content: "world" }, ts: 3000 },
    ], "hydrate");

    // 验证只有新消息
    const msgs = useChat.getState().messages;
    expect(msgs.length).toBe(1);
    expect(msgs[0].content).toBe("world");
  });
});
```

- [ ] **Step 3: Run test to verify it passes**

Run: `cd E:\binn\ftre-desktop\packages\renderer && node_modules\.bin\vitest.CMD run src/stores/chat.reducer.test.ts 2>&1`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
cd E:\binn\ftre-desktop
git add packages/renderer/src/stores/chat.reducer.test.ts
git commit -m "test: clearSessionCache 清空 bucket 后可重新 hydrate"
```

---

## Self-Review

**1. Spec coverage:**
- ✅ 禁用本地缓存：`hasSessionCache` 始终返回 false（Task 1: Step 3）
- ✅ 每次 switchSession 都走 HTTP + WS：`clearSessionCache` 清空 bucket（Task 1: Step 3）
- ✅ HTTP 先于 WS attach：`subscribeOnly` 移到 `.then()` 中（Task 1: Step 2）
- ✅ `switchTo` 不再负责 WS attach（Task 1: Step 1）
- ✅ `loadSessionEvents` 移除 hydrate 保护（Task 1: Step 4）
- ✅ 测试覆盖（Task 2）

**2. Placeholder scan:** 无 TBD / TODO。所有代码块完整。

**3. Type consistency:**
- `clearSessionCache(sessionId: string) => void` — Task 1 Step 3 定义，Step 2 消费 — 一致
- `hasSessionCache` 返回 `false` — Task 1 Step 3 定义，`session.ts:277` 消费 — 一致
- `loadSessionEvents` mode 参数从 `"hydrate" | "refresh"` 简化为始终 `"hydrate"` — Task 1 Step 2 传入 `"hydrate"` — 一致
