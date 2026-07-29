import base64

from ftre.session.converter import to_openai
from ftre_agent_core.message import Base64Source, DataBlock, MsgName, UserMsg


def _image_message():
    return UserMsg(
        name=MsgName.DEFAULT,
        content=[
            DataBlock(
                source=Base64Source(
                    data=base64.b64encode(b"png").decode("ascii"),
                    media_type="image/png",
                )
            )
        ],
    )


def test_persisted_image_msg_to_openai_with_vision():
    messages = to_openai([_image_message()], config={"llm": {"vision": True}})
    assert messages[0]["content"][0]["type"] == "image_url"
    assert messages[0]["content"][0]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


def test_persisted_image_msg_is_omitted_without_vision():
    messages = to_openai([_image_message()], config={"llm": {"vision": False}})
    assert "当前模型不支持视觉输入" in messages[0]["content"][0]["text"]
