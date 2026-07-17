"""端到端验证：read 工具产生的 image_file 事件 → to_openai_messages 转换 → 包含 base64 data URL"""
import os
from types import SimpleNamespace

from ftre_agent_core.agent.event import UserMessageEvent
from ftre.tools.read import create_read_tool
from ftre.tools._workspace import WorkspaceAccessor
from ftre.session.converter import to_openai


class FakeWorkspace(WorkspaceAccessor):
    def __init__(self, cwd: str):
        self.cwd = cwd

    def get(self) -> str:
        return self.cwd


def test_read_tool_image_to_openai_message(tmp_path):
    """read 工具读图 → 事件用 image_file 类型 → to_openai_messages 转出 image_url base64"""
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

    assert isinstance(result, UserMessageEvent)
    assert result.content[0]["type"] == "image_file"
    assert os.path.exists(result.content[0]["path"])

    # Step 2: 模拟事件存储 → to_openai_messages 转换
    events = [{
        "type": "user_message",
        "data": {
            "content": result.content,
            "metadata": result.metadata,
        },
    }]

    msgs = to_openai(
        events,
        config={"llm": {"vision": True}},
    )

    # Step 3: 验证转出的 message 包含 image_url base64
    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_read_tool_image_omitted_without_vision(tmp_path):
    """read 工具读图 → vision=False → to_openai_messages 省略图片"""
    image = tmp_path / "screenshot.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00"
        b"\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    result = create_read_tool().func(
        "screenshot.png",
        ws=FakeWorkspace(str(tmp_path)),
        llm_config=SimpleNamespace(vision=True),
    )

    assert isinstance(result, UserMessageEvent)

    events = [{
        "type": "user_message",
        "data": {
            "content": result.content,
            "metadata": result.metadata,
        },
    }]

    msgs = to_openai(
        events,
        config={"llm": {"vision": False}},
    )

    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert isinstance(content, str)
    assert "当前模型不支持视觉输入" in content
