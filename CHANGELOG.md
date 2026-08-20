# Changelog

## [未发布]

### 修复

- B2：`context_compact_start` 事件明确携带实际使用的摘要模型，客户端不再将普通对话模型误显示在压缩横幅。

### 性能

- B2：`/compress-fast` 改用批量原子消息更新，避免多条消息逐条重写完整 session state；24 MB 生产会话副本实测耗时由 979ms 降至 454ms。
