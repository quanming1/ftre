import os

from ftre.utils.image_store import save_image, load_as_data_url


def test_save_image_returns_absolute_path():
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    path = save_image(raw, "image/png", "screenshot.png")

    assert os.path.isabs(path)
    assert os.path.exists(path)
    with open(path, "rb") as f:
        assert f.read() == raw


def test_save_image_sanitizes_filename():
    raw = b"\x00" * 10
    path = save_image(raw, "image/png", "scréen!!!shot.png")

    basename = os.path.basename(path)
    assert "é" not in basename
    assert "!" not in basename
    assert basename.endswith(".png")


def test_save_image_handles_collision():
    raw = b"\x00" * 10
    path1 = save_image(raw, "image/png", "dup.png")
    path2 = save_image(raw, "image/png", "dup.png")

    assert path1 != path2
    assert os.path.exists(path1)
    assert os.path.exists(path2)


def test_save_image_no_original_name():
    raw = b"\x00" * 10
    path = save_image(raw, "image/jpeg")

    assert os.path.exists(path)
    assert path.endswith(".jpg")


def test_load_as_data_url_returns_valid_data_uri():
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    path = save_image(raw, "image/png", "test.png")

    data_url = load_as_data_url(path, "image/png")

    assert data_url is not None
    assert data_url.startswith("data:image/png;base64,")
    # base64 payload non-empty
    assert len(data_url.split(",", 1)[1]) > 0


def test_load_as_data_url_returns_none_for_missing_file():
    result = load_as_data_url("/nonexistent/path/image.png", "image/png")

    assert result is None
