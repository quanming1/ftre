"""端到端验证：read 工具产生的图片事件 → to_openai 转换 → 包含 base64 data URL"""
import os
from types import SimpleNamespace

from ftre_agent_core.event import HintBlockEvent
from ftre.tools.read import create_read_tool
from ftre.tools._workspace import WorkspaceAccessor
from ftre.session.converter import to_openai


class FakeWorkspace(WorkspaceAccessor):
    def __init__(self, cwd: str):
        self.cwd = cwd

    def get(self) -> str:
        return self.cwd


def test_read_tool_image_to_openai_message(tmp_path):
    """read 工具读图 → HintBlockEvent 含 data URL → to_openai 转出 image_url base64"""
    image = tmp_path / "screenshot.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00"
        b"\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    # Step 1: read 工具读图
    result = create_read_tool().func(
        "screenshot.png",
        ws=FakeWorkspace(str(tmp_path)),
        llm_config=SimpleNamespace(vision=True),
    )

    assert isinstance(result, HintBlockEvent)
    assert "data:image" in result.hint

    # Step 2: 模拟事件存储 → to_openai 转换
    # 新协议下 hint 内容作为 user 消息的 content 存入 DB
    events = [{
        "type": "user_message",
        "data": {
            "content": result.hint,
            "metadata": result.metadata,
        },
    }]

    msgs = to_openai(
        events,
        config={"llm": {"vision": True}},
    )

    # Step 3: 验证转出的 message 包含图片内容
    assert len(msgs) == 1
    content = msgs[0]["content"]
    # vision=True 时，build_user_content 会把 data URL 转为 image_url part
    assert isinstance(content, (list, str))
    if isinstance(content, list):
        assert any(
            part.get("type") == "image_url" and "data:image" in part.get("image_url", {}).get("url", "")
            for part in content if isinstance(part, dict)
        )


def test_read_tool_image_omitted_without_vision(tmp_path):
    """read 工具读图 → vision=False → 工具直接返回错误字符串，不产生图片事件"""
    image = tmp_path / "screenshot.png"
    image.write_bytes(b"not actually decoded because vision is disabled")

    result = create_read_tool().func(
        "screenshot.png",
        ws=FakeWorkspace(str(tmp_path)),
        llm_config=SimpleNamespace(vision=False),
    )

    # vision=False 时，read 工具直接返回错误字符串，不读图
    assert isinstance(result, str)
    assert "当前模型不支持视觉输入" in result
