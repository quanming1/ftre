"""Host Session/Provider 边界的 Responses Output Item 重放回归。"""

from ftre_agent.event import ModelCallEndEvent
from ftre_agent.message import AssistantMsg, ThinkingBlock

from ftre.services.session.message.converter import to_openai


def test_persisted_output_items_are_carried_to_next_responses_request():
    message = AssistantMsg(
        id="assistant-1",
        content=[ThinkingBlock(thinking="先检查配置")],
    )
    message.append_event(
        ModelCallEndEvent(
            reply_id="assistant-1",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            response_metadata={
                "output_items": [
                    {
                        "type": "reasoning",
                        "id": "rs-1",
                        "content": [{"type": "reasoning_text", "text": "先检查配置"}],
                        "status": "completed",
                    }
                ]
            },
        )
    )

    provider_message = to_openai([message])[0]

    assert provider_message["responses_output_items"][0]["id"] == "rs-1"
    assert provider_message["responses_output_items"][0]["status"] == "completed"
