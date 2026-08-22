# ftre-compaction

ftre-compaction 是 ftre 的可选上下文压缩发行物。它把“什么时候压缩”和
“如何压缩”完整放在一个包内，核心 ftre 只提供稳定的 Hook、Session、Config
和 Command 契约。

## 包内职责

| 文件 | 职责 |
|---|---|
| config.py | 读取 ConfigService.snapshot()，解析本包自己的阈值、预算和摘要模型 |
| service.py | 唯一真实实现：token 水位、LLM 摘要、快速裁剪、事件、并发和取消 |
| hooks.py | 在 agent/pre-step、agent/after-turn、agent/request-error 上接入策略 |
| commands.py | 注册 /compact 与 /compress-fast，绕过 Agent Turn 直接调用 Service |
| plugin.py | Cordis 装配入口，把 Service、Hook、Command 和关闭 effect 绑定起来 |
| events.py | 复用 ftre Session 维护事件名称，不复制事件协议 |

## 一次请求的调用链

    pending.peek
      → agent/pre-step
          → 读取 CompactionConfig 快照
          → should_compact
          → compact（必要时）
          → 再次检查水位
      → Lane claim
      → Agent Turn
      → agent/after-turn
          → 使用 70% 预压线

LLM 返回上下文溢出时，agent/request-error 只在压缩 progress generation
确实前进后返回一次 RetryRequest，避免无进展重试死循环。Hook 不直接操作
Mailbox；pending 的 peek/claim/保留仍由 ftre 核心的 SessionLane 管理。

## 配置

压缩配置仍放在 ftre 的 config.json，但 Owner 属于本包：

    {
      "agents": {
        "context": {
      "precompactThreshold": 0.7,
      "compactThreshold": 0.8,
      "safetyBuffer": 1024
        },
        "compact_generation": {
          "provider": "cheap-provider",
          "model": "fast-summary-model"
        }
      }
    }

每次 Hook 或命令调用都会创建不可变配置快照。配置热更新不会改变已经开始
的压缩任务，只影响下一次边界调用。AgentConfig.llm 仍负责本轮 Agent 的
主模型上下文窗口和输出预算，避免多 Agent 场景使用错误预算。

## 安装与启用

    pip install ftre-compaction

安装后仍需在 ftre Plugin 配置中显式启用：
ftre_compaction.plugin:apply。不安装或不启用时，核心 Agent 的普通流程
继续运行，只是不会注册压缩命令和压缩 Hook。
