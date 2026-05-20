"""端到端测试：Channel → Bus → AgentLoop → Agent Core → Bus → Channel → CLI 渲染"""
import asyncio
from ftre_agent_core.agent import EventType
from ftre.bus import EventBus
from ftre.channel import TestChannel, ChannelManager
from ftre.agent.loop import AgentLoop
from ftre.config import AgentConfig, LLMConfig


async def main():
    bus = EventBus()

    # Channel + Manager
    ch = TestChannel(bus)
    mgr = ChannelManager(bus)
    mgr.register(ch)
    await mgr.start()

    # Agent Loop
    config = AgentConfig(
        llm=LLMConfig(
            model="openai/DeepSeek-V3.2",
            api_key="sk-REDACTED",
            api_base="https://llm-gateway.REDACTED.example.com/v1",
        ),
        system_prompt="你是一个简洁的助手，回答控制在一两句话。",
    )
    bus.create_session("s1")
    loop = AgentLoop(session_id="s1", bus=bus, config=config)
    loop.start()

    # 用户输入
    print("用户: 什么是 Python 的 GIL？\n")
    await ch.receive("s1", "什么是 Python 的 GIL？")

    # 等 Agent 处理完
    await asyncio.sleep(10)

    # CLI 渲染
    print("助手: ", end="")
    for event in ch.events:
        match event["type"]:
            case EventType.MESSAGE:
                print(event["data"]["content"], end="", flush=True)
            case EventType.REASONING:
                pass  # 不展示推理
            case EventType.TOOL_CALL:
                d = event["data"]
                print(f"\n  [调用 {d['name']}]", end="")
            case EventType.TOOL_RESULT:
                d = event["data"]
                print(f"\n  [结果: {d['result'][:50]}]", end="")
            case EventType.ERROR:
                print(f"\n  [错误: {event['data']['message']}]", end="")
            case EventType.DONE:
                d = event["data"]
                print(f"\n\n--- done: success={d['success']} ---")

    # 统计
    print(f"\n总事件数: {len(ch.outputs)}")
    types = [e['type'].value for e in ch.events]
    print(f"事件类型: {set(types)}")

    await loop.stop()
    await mgr.stop()


if __name__ == "__main__":
    asyncio.run(main())
