import os

from ftre.utils.image_store import save_image
from ftre.session.multimodal import normalize_user_content, build_user_content


def test_normalize_user_content_image_file_with_vision():
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    path = save_image(raw, "image/png", "test.png")

    content = [
        {"type": "text", "text": "看图"},
        {"type": "image_file", "path": path, "mime_type": "image/png"},
    ]

    result = normalize_user_content(content, include_images=True)

    assert isinstance(result, list)
    assert result[0] == {"type": "text", "text": "看图"}
    assert result[1]["type"] == "image_url"
    assert result[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_normalize_user_content_image_file_without_vision():
    raw = b"\x00" * 10
    path = save_image(raw, "image/png", "test.png")

    content = [
        {"type": "text", "text": "看图"},
        {"type": "image_file", "path": path, "mime_type": "image/png"},
    ]

    result = normalize_user_content(content, include_images=False)

    assert isinstance(result, str)
    assert "当前模型不支持视觉输入" in result
    assert "看图" in result


def test_normalize_user_content_image_file_missing_file():
    content = [
        {"type": "text", "text": "看图"},
        {"type": "image_file", "path": "/nonexistent/image.png", "mime_type": "image/png"},
    ]

    result = normalize_user_content(content, include_images=True)

    # 缺失文件时跳过图片，不崩溃，只返回文本
    assert isinstance(result, str)
    assert "看图" in result


def test_build_user_content_with_path_attachment():
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    path = save_image(raw, "image/png", "upload.png")

    attachments = [
        {"type": "image", "mime_type": "image/png", "path": path, "name": "upload.png"},
    ]

    result = build_user_content("描述图片", attachments, include_images=True)

    assert isinstance(result, list)
    assert result[0] == {"type": "text", "text": "描述图片"}
    assert result[1]["type"] == "image_url"
    assert result[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_build_user_content_with_path_attachment_without_vision():
    raw = b"\x00" * 10
    path = save_image(raw, "image/png", "upload.png")

    attachments = [
        {"type": "image", "mime_type": "image/png", "path": path, "name": "upload.png"},
    ]

    result = build_user_content("描述图片", attachments, include_images=False)

    assert isinstance(result, str)
    assert "当前模型不支持视觉输入" in result


def test_build_user_content_with_missing_path_file():
    attachments = [
        {"type": "image", "mime_type": "image/png", "path": "/nonexistent/img.png", "name": "img.png"},
    ]

    result = build_user_content("描述图片", attachments, include_images=True)

    # 缺失文件时图片被跳过，只返回文本部分
    assert isinstance(result, list)
    assert result == [{"type": "text", "text": "描述图片"}]
