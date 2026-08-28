# ftre-llm-recovery

可选的 LLM 失败策略 Plugin。

它只监听 Runtime 的 `llm/error`，按本包配置返回 `retry` 或 `stop`。真正的
RetryEvent、退避、消息重读、取消和流式收尾仍由 Agent Runtime 负责；本包不创建
Agent、不持有 Session，也不实现第二套重试循环。

```json
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
```

没有匹配规则时返回 `None`，交回 Runtime 默认策略。Plugin 卸载后，Runtime 仍可独立运行。
