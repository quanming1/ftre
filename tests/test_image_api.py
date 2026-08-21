from fastapi import FastAPI
from fastapi.testclient import TestClient

from ftre.services.attachment.router import build_router
from ftre.services.attachment.service import AttachmentService


def _client(tmp_path) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_router(AttachmentService(tmp_path / "assets")),
        prefix="/api",
    )
    return TestClient(app)


def test_serve_image_file_from_local_path(tmp_path):
    image = tmp_path / "shot.png"
    raw = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00"
        b"\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    image.write_bytes(raw)

    response = _client(tmp_path).get("/api/image-file", params={"path": str(image)})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content == raw


def test_serve_image_file_rejects_non_image(tmp_path):
    text_file = tmp_path / "notes.txt"
    text_file.write_text("not image", encoding="utf-8")

    response = _client(tmp_path).get("/api/image-file", params={"path": str(text_file)})

    assert response.status_code == 415
