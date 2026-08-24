import base64
import os

from ftre.plugins.builtin.channels.websocket.channel import (
    _persist_attachments,
)
from ftre.services.attachment import AttachmentService


def test_persist_attachments_replaces_data_with_path(tmp_path):
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    b64 = base64.b64encode(raw).decode("ascii")

    attachments = [
        {"type": "image", "mime_type": "image/png", "data": b64, "name": "test.png"},
    ]

    _persist_attachments(attachments, AttachmentService(tmp_path))

    assert "data" not in attachments[0]
    assert "path" in attachments[0]
    assert os.path.exists(attachments[0]["path"])
    assert attachments[0]["name"] == "test.png"
    assert attachments[0]["mime_type"] == "image/png"


def test_persist_attachments_handles_no_name(tmp_path):
    raw = b"\x00" * 10
    b64 = base64.b64encode(raw).decode("ascii")

    attachments = [
        {"type": "image", "mime_type": "image/jpeg", "data": b64},
    ]

    _persist_attachments(attachments, AttachmentService(tmp_path))

    assert "data" not in attachments[0]
    assert "path" in attachments[0]
    assert attachments[0]["path"].endswith(".jpg")


def test_persist_attachments_noop_on_empty(tmp_path):
    attachments = []
    _persist_attachments(attachments, AttachmentService(tmp_path))
    assert attachments == []


def test_persist_attachments_noop_on_none(tmp_path):
    _persist_attachments(None, AttachmentService(tmp_path))
