# Issue #207 / #212 排查报告（2026-06-30）

## 复现条件
- 正式环境 `https://im.mlamp.cn/`
- 触发路径：进入 FT-A2 Team 主群（channelId=`ee9ff8320f804e0bb1b86bcfbbd6de2e`, channelType=2）→ 头部"更多"→"查找聊天内容"→ 搜"设计稿" → 点"王宜林 06/17 聊天记录"的"定位到聊天"

## 已确认的根因（代码层）

### 根因 1：`locateMessageWindow` 用单页替换 InfiniteQuery cache
文件 `E:\octo-web-2\src\features\chat\lib\locate-reply-message.ts:62-66`

```ts
const opts = messagesInfiniteQueryOptions(channel);
qc.setQueryData<InfiniteData<Message[], number>>(opts.queryKey, {
  pages: [page],
  pageParams: [locateWindowStart(messageSeq)],
});
```

问题：
1. `pages: [page]` —— 用 30 条定位窗口**替换**原有 pages，丢失最新页
2. `pageParams: [messageSeq - 5]` —— `getNextPageParam`（`messages.query.ts:43-54`）会从 `oldest = page 中最小 messageSeq` 计算下一页参数，结果是**继续拉更老的历史**，永远不能拉最新消息

### 根因 2：消息列表没有"加载最新"机制
- `messagesInfiniteQueryOptions`（`E:\octo-web-2\src\features\chat\queries\messages.query.ts`）只定义了 `getNextPageParam`，无 `getPreviousPageParam`
- 全 chat 模块 `grep fetchPreviousPage` 命中 0 次（`src/features/chat/components|hooks|queries|lib`）
- 所以**滚动到底部只会触发 `fetchNextPage`（拉更老历史），不会拉新消息**

### 根因 3：WebSocket 实时消息 append 到定位窗口
文件 `E:\octo-web-2\src\features\chat\hooks\use-messages-sync.hook.ts:194-198`

```ts
return {
  ...prev,
  pages: [[...firstPage, ...newOnes], ...prev.pages.slice(1)],
};
```

`firstPage` 是定位窗口（不是最新消息），WebSocket 收到的新消息被 append 到 `firstPage` 末尾。视觉上 `pages[0]` 仍是定位窗口，新消息**视觉上看不到**（在视口之外）。

### 根因 4：定位后 `pendingLocateForChannel=true` 期间三个 hook skip
文件 `E:\octo-web-2\src\features\chat\components\message-list.tsx:481-490`

```ts
useInitialScrollToBottom(scrollRef, firstReadyKey, pendingLocateForChannel);
useFollowBottomOnNewMessages(scrollRef, followKey, pendingLocateForChannel);
usePulldownToLoadHistory(scrollRef, pageCount, hasNextPage, isFetchingNextPage, fetchNextPage, pendingLocateForChannel);
```

`pendingLocateForChannel` 在 `chatLocateMessageActions.clear(requestId)` 后才变 false。在此期间：
- 不会自动滚到底部
- 新消息不会触发 auto-follow
- "上拉加载更早历史"被禁用

### 根因 5（#212 维度）：浮动按钮状态在定位后错乱
文件 `E:\octo-web-2\src\features\chat\hooks\use-scroll-to-bottom-button.hook.ts:18-23`

- `atBottom = distanceFromBottom < 200`
- 定位后用户视觉在"中间"（定位消息附近），`distanceFromBottom` 远大于 200 → `atBottom=false` → 浮动按钮**应该**显示
- 但 `useFollowBottomOnNewMessages` skip（根因 4）→ 即使按钮 visible，点下去 `scrollToBottom({behavior:"smooth"})` 后，新消息因 cache 没更新（根因 1+2）而看不到

## 网络证据（复现过程中真实抓取）

### 定位前（FT-A2 Team 主群，cache 24 条 374-402）
- `message/channel/sync`: `{"channel_id":"ee9ff832...","channel_type":2,"start_message_seq":0,"pull_mode":0}` → 24 条最新

### 点击"王宜林 06/17"定位后
- `message/channel/sync` （请求 #441）: `{"channel_id":"ee9ff832...","channel_type":2,"start_message_seq":0,"pull_mode":0}` —— **注意 `start_message_seq=0`**，这是初始 pageParam，**不是定位窗口用的 startSeq=260（=265-5）**！

Wait, **这有问题**。定位窗口应该用 `startMessageSeq=260, pullMode=Up`，但 #441 是 `start_message_seq=0, pull_mode=0(Down)`。所以 #441 不是 `locateMessageWindow` 的 fetch，**它是 useInfiniteQuery 在 channel/sync 的初始挂载**。

意味着 `locateMessageWindow` 的 `fetchLocateWindow`（用 pullMode=Up）的网络请求**可能在我之前的复现中被覆盖了**，或者**SDK 内部把 pullMode=Up 改写成 pullMode=0**。需要进一步 debug 抓取。

## 关键代码位置汇总

| 现象 | 代码位置 | 行号 |
|---|---|---|
| `setQueryData` 替换 cache | `E:\octo-web-2\src\features\chat\lib\locate-reply-message.ts` | 62-66 |
| `getNextPageParam` 只算更老历史 | `E:\octo-web-2\src\features\chat\queries\messages.query.ts` | 43-54 |
| WebSocket 消息 append 到 pages[0] | `E:\octo-web-2\src\features\chat\hooks\use-messages-sync.hook.ts` | 194-198 |
| 三个 hook 在 `pendingLocateForChannel` 时 skip | `E:\octo-web-2\src\features\chat\components\message-list.tsx` | 481-490 |
| `useLocateRequestedMessage` 检查 channel 匹配 | `E:\octo-web-2\src\features\chat\components\message-list.tsx` | 285 |
| 浮动按钮状态逻辑 | `E:\octo-web-2\src\features\chat\hooks\use-scroll-to-bottom-button.hook.ts` | 18-23, 81-103 |
| "定位到聊天"按钮 | `E:\octo-web-2\src\features\chat\components\channel-search-panel.tsx` | 818-826 |

## 修复方向（待用户决定）

### 方案 A：定位窗口 fetch 后写回"完整 cache"
`locateMessageWindow` 拉窗口后，先 `qc.setQueryData` 设窗口，再 `qc.invalidateQueries` 触发完整刷新；或直接 `qc.removeQueries` 后用 `useInfiniteQuery` 重 mount。

### 方案 B：补 `getPreviousPageParam` + 加 `fetchPreviousPage` 按钮
定位后 `pendingLocateForChannel` 清除时，对 `pages[0]` 执行 `fetchPreviousPage(0)` 把最新消息拉回来；或在 message-list 底部加"加载最新消息"按钮。

### 方案 C：定位前先 sync 完整 pages，再 merge 定位窗口
`useLocateRequestedMessage` 触发 `locateMessageWindow` 前，先确保 InfiniteQuery 有最新 pages。merge 而不是 replace。

## 最小可复现（BotFather 私聊）

1. 切到 BotFather（私聊，最简单，不跨群） → cache 28 条，seq 63-92
2. 头部"更多"→"查找聊天内容"→搜"test"→1 个结果"蒋全明 04/15 10:52 large-test.json"
3. 点"定位到聊天"
4. **cache 变成 42 条 seq 23-78**（不再是 63-92）
5. 滚动到底，等 5 秒
6. **cache 不变**，lastSeq 仍 78，新消息 91/92 完全看不到
7. 左侧 sidebar BotFather 仍显示 17 条未读

**截图证据**：
- `E:\ftre\bug207-step1-before-locate.png` — FT-A2 Team 定位前（h2=FT-A2 Team，cache 24 条 374-402，搜索结果王宜林 06/17）
- `E:\ftre\bug207-step2-after-locate.png` — BotFather 定位后（cache 28→42 条，seq 63-92→23-78）
- `E:\ftre\bug207-step3-final.png` — 5 秒等待后（cache 不变，最新 91/92 永远看不到，浮动按钮 visible）

## 已存在的 Issue
- **#207**: `[Bug] 历史记录定位到较旧消息后，滚动到底部无法加载最新消息` (许建文)
- **#212**: `[Bug] 历史记录定位到较旧消息后，浮动滚动到底部按钮的显示逻辑和交互逻辑不对` (许建文)
- 两者关联，已由 @猴大宝 在"问题记录"群创建

## 复现命令（如果用户要在 PR 中重跑）

```js
// 浏览器 console 中:
// 1. 切到 BotFather 或任何有足够历史消息的频道
// 2. 点击"更多" → "查找聊天内容"
// 3. 搜索"test"（或任何有历史匹配的关键字）
// 4. 点击"定位到聊天"
// 5. 观察 DevTools Elements 中 [data-msg-seq] 范围（被定位窗口替换）
// 6. 在 React Query devtools 中观察 ["chat","messages",channelType,channelId] 的 pages 数组（只剩 1 页）
// 7. 等 5+ 秒，看是否有 [POST] /api/message/channel/sync 新请求（应该没有）
```

## 建议修复方向

**核心修复**：`locateMessageWindow`（`src/features/chat/lib/locate-reply-message.ts:62-66`）目前是**replace 整个 cache**，应该改为：

1. **方案 A**（最小改动）：定位完成后调用 `qc.invalidateQueries({ queryKey: opts.queryKey })`，让 useInfiniteQuery 重新拉首屏（即 `pageParam=0, pullMode=Down`），这样最新 30 条会覆盖定位窗口
2. **方案 B**（更彻底）：在 `messagesInfiniteQueryOptions` 加 `getPreviousPageParam: () => 0` 和 `fetchPreviousPage` 机制；`message-list` 底部加"加载最新消息"按钮
3. **方案 C**（推荐）：定位窗口 fetchLocateWindow 后**merge**而不是 replace —— fetch 完成时，先用最新 30 条 + 定位窗口合并：
   - 用一个临时 queryKey（`["chat","messages",channelType,channelId,"__locate__"]`）存定位窗口
   - 用户视觉上看到定位窗口（与现在一致）
   - 同时后台 `invalidateQueries` 重新拉原始 cache
   - 定位 clear 后 message-list 自动切回原 cache

**额外修复**（#212）：`useScrollToBottomButton` 应该在 cache 变更后重新计算 `tailKey` 和 `baselineTailKey`，避免重复"未读 17"提示（也即 `tailKeyRef.current` 重新指向真实最新消息 seq）。
