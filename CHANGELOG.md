# Changelog

## [未发布]

### 性能

- B2：`/compress-fast` 改用批量原子消息更新，避免多条消息逐条重写完整 session state；24 MB 生产会话副本实测耗时由 979ms 降至 454ms。
