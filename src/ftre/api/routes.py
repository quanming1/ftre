"""
API 路由
"""
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ftre_agent_core.agent import EventType
from ..agent import create_agent

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    system_prompt: str = ""


@router.post("/chat")
async def chat(req: ChatRequest):
    """流式对话接口"""
    agent = create_agent()
    if req.system_prompt:
        agent.system_prompt = req.system_prompt

    def event_stream():
        import json
        for event in agent.run(req.message):
            data = {
                "type": event["type"].value,
                "data": event.get("data", {}),
            }
            yield f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/health")
async def health():
    return {"status": "ok"}
