# ftre-agent-runtime

ftre 平台的 Agent 具体执行实现包（PRD-F33）。

内容：

- `plugin.py` —— 唯一的 Runtime Provider Plugin（entry point `agent-runtime`），把私有 Runtime 注册为已有 `agents` Service 的 Factory
- `engine.py` —— `AgentLoop`：active Turn 运行时、Agent Hook 分发、取消与维护屏障
- `turn_executor.py` —— Turn 状态机（`BUILDING → RUNNING → FINALIZING → 终态`）
- `factory.py` —— 唯一的 Runtime `ReActAgent` 构造点
- `state.py` —— Turn 私有状态（公开结果类型是 `ftre_agent.AgentRunResult`）
- `completion.py` —— 按 `request_id` 等待的进程内完成注册表

## 依赖方向

```
ftre-agent-runtime → ftre-agent（契约）
                  → ftre-llm（Service 适配）
```

Host Service（sessions、tools、message_bus、system_prompt、agent_profiles、
llm、hook_runtime、session_events、config、workspaces）全部由 Provider Plugin
注入，只按窄公开方法调用。本包源码不 import `ftre.services.*` 实现模块，
因此可以在没有 ftre Host 的洁净环境中安装与测试。

## 装载方式

```
[project.entry-points."ftre.plugins"]
agent-runtime = "ftre_agent_runtime.plugin:apply"
```

Host Composition Root 只加载该 entry point，不手工构造 `AgentLoop`。
