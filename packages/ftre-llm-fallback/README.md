# ftre-llm-fallback

可选的最后一次 LLM 流 fallback Plugin。

它只消费 Runtime 的 `llm/stream`：前面的 attempt 原样交回 Runtime Retry；只有最后一次
attempt、主模型尚未产生任何流式输出且错误码命中配置时，才创建一次备用模型流。
备用模型不再经过 Hook，因此不会递归 fallback；模型配置由公开 `ConfigService`
解析，API key 不写入日志。

```json
{
  "id": "llm-fallback",
  "enabled": true,
  "config": {
    "provider": "OpenCode 直连",
    "model": "deepseek-v4-flash",
    "errors": ["rate_limit", "timeout", "bad_request"],
    "exclude_errors": ["overflow", "context_length", "too_long"]
  }
}
```

缺少 provider/model、错误码未命中、取消、已有输出或备用调用失败时，Plugin 都把原始
主模型错误交回 Runtime。卸载后主模型直连，Runtime Retry 行为不变。
