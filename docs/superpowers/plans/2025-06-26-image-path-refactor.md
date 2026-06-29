# 图片路径化重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将用户上传附件和 read 工具读图的 base64 数据从事件链路中移除，改为存储到 OS temp 目录，事件中只携带绝对路径，base64 转换集中在 SessionManager 出口（`multimodal.py`）完成。

**Architecture:** 新增 `ftre/utils/image_store.py` 作为统一的图片暂存工具（save / load_as_data_url）。ws_channel 收到前端 base64 后立即落盘转路径；read 工具压缩后落盘，content 使用新的 `image_file` 类型携带路径。base64 转换有**三条出口**需要覆盖：(1) `multimodal.py` 的 `build_user_content` / `normalize_user_content` 负责历史重建和首条消息附件；(2) agent-core 的 `UserMessageEvent.to_openai_message()` 负责当前轮工具结果直接进 memory 的路径——因为 agent-core 不能反向依赖 ftre，用标准库 `base64` 直接读取文件转换。compact_handler 只提取文本，天然兼容。

**Tech Stack:** Python 3.12, tempfile, base64, pathlib, pytest, ftre-agent-core

## Global Constraints

- 临时目录：`tempfile.gettempdir() / "ftre_images"`，跨平台自动适配
- 文件名：保留原始文件名，仅保留 `[a-zA-Z0-9._-]` 字符，其余替换为 `_`
- 不主动清理 temp 文件，交给 OS
- 前端不改动（仍通过 WebSocket 发 base64）
- 新增 content 类型 `image_file`：`{"type": "image_file", "path": "<abs_path>", "mime_type": "<mime>"}`
- 事件链路中不再出现 base64 字符串
- DB 持久化只存路径，历史会话恢复后图片可能丢失（可接受）
- agent-core 不能 import ftre 模块（依赖方向：ftre → agent-core，不可反向）
- base64 转换覆盖三条出口：`to_openai_message()`（agent-core）、`build_user_content()`（ftre）、`normalize_user_content()`（ftre）

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `src/ftre/utils/image_store.py` | 统一图片暂存：save_image / load_as_data_url | **新建** |
| `src/ftre/utils/__init__.py` | 导出 image_store | **修改** |
| `src/ftre/channel/ws_channel.py` | 校验后落盘，base64→path | **修改** |
| `src/ftre/tools/read.py` | 压缩后落盘，用 image_file 类型 | **修改** |
| `src/ftre/session/multimodal.py` | build_user_content 读 path 转 base64；normalize_user_content 处理 image_file | **修改** |
| `E:/ftre-agent-core/src/ftre_agent_core/agent/event.py` | `UserMessageEvent.to_openai_message()` 处理 image_file → image_url | **修改** |
| `tests/test_plugin_tools.py` | 更新 read 工具测试断言 | **修改** |
| `tests/test_compact_algo.py` | 更新 attachment 测试用 path 替代 data | **修改** |

---

### Task 1: 新建 image_store 模块

**Files:**
- Create: `src/ftre/utils/image_store.py`
- Modify: `src/ftre/utils/__init__.py`
- Test: `tests/test_image_store.py`

**Interfaces:**
- Produces:
  - `save_image(raw: bytes, mime: str, original_name: str = "") -> str` — 将 raw bytes 存到 temp，返回绝对路径
  - `load_as_data_url(path: str, mime: str = "") -> str | None` — 从路径读取文件，返回 `data:{mime};base64,{b64}`，失败返回 None

- [ ] **Step 1: Write the failing test**

```python
# tests/test_image_store.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_image_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ftre.utils.image_store'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/ftre/utils/image_store.py
"""
统一图片暂存工具。

用户上传附件和 read 工具读图都通过本模块将图片数据落盘到 OS temp 目录，
事件链路中只携带文件路径；base64 转换在 SessionManager 出口完成。
"""
from __future__ import annotations

import base64
import logging
import os
import re
import tempfile
import uuid

logger = logging.getLogger(__name__)

_IMG_DIR = os.path.join(tempfile.gettempdir(), "ftre_images")

_MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

_EXT_TO_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _sanitize_filename(name: str) -> str:
    """保留原始文件名，仅保留 [a-zA-Z0-9._-] 字符，其余替换为 _。"""
    name = os.path.basename(name)
    name = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    if not name or name in (".", ".."):
        name = f"image_{uuid.uuid4().hex[:8]}"
    return name


def save_image(raw: bytes, mime: str, original_name: str = "") -> str:
    """将 raw bytes 存到 temp 目录，返回绝对路径。

    Args:
        raw: 图片二进制数据
        mime: MIME 类型，如 "image/png"
        original_name: 原始文件名，用于命名（会做安全过滤）

    Returns:
        存储文件的绝对路径
    """
    os.makedirs(_IMG_DIR, exist_ok=True)

    ext = _MIME_TO_EXT.get(mime, ".png")

    if original_name:
        name = _sanitize_filename(original_name)
        # 确保扩展名正确
        if not name.lower().endswith(ext):
            name = f"{name}{ext}"
    else:
        name = f"image_{uuid.uuid4().hex[:8]}{ext}"

    path = os.path.join(_IMG_DIR, name)

    # 文件名冲突时追加 _1, _2, ...
    if os.path.exists(path):
        base_part, ext_part = os.path.splitext(name)
        counter = 1
        while os.path.exists(path):
            path = os.path.join(_IMG_DIR, f"{base_part}_{counter}{ext_part}")
            counter += 1

    with open(path, "wb") as f:
        f.write(raw)

    logger.debug(f"[image_store] saved {len(raw)} bytes -> {path}")
    return path


def load_as_data_url(path: str, mime: str = "") -> str | None:
    """从路径读取文件，返回 data URL。失败返回 None。

    Args:
        path: 文件绝对路径
        mime: MIME 类型。为空时从文件扩展名推断。

    Returns:
        形如 "data:image/png;base64,xxxx" 的 data URL，或 None
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()

        if not mime:
            ext = os.path.splitext(path)[1].lower()
            mime = _EXT_TO_MIME.get(ext, "image/png")

        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        logger.warning(f"[image_store] failed to load {path}: {e}")
        return None
```

```python
# src/ftre/utils/__init__.py  — 在末尾追加
from .image_store import save_image, load_as_data_url

__all__ = ["Pipeline", "Handler", "save_image", "load_as_data_url"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_image_store.py -v`
Expected: PASS (all 6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/ftre/utils/image_store.py src/ftre/utils/__init__.py tests/test_image_store.py
git commit -m "feat: add image_store utility for temp-based image storage"
```

---

### Task 2: ws_channel 附件落盘——base64 替换为路径

**Files:**
- Modify: `src/ftre/channel/ws_channel.py:48-88`（`_validate_attachments` 函数）
- Modify: `src/ftre/channel/ws_channel.py:307-312`（`_on_message` 中校验后的落盘逻辑）

**Interfaces:**
- Consumes: `save_image` from `ftre.utils.image_store`
- Produces: attachment dict 结构从 `{"type":"image","mime_type":"...","data":"<base64>","name":"..."}` 变为 `{"type":"image","mime_type":"...","path":"<abs_path>","name":"..."}`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ws_attachment_persist.py
import base64
import os
from unittest.mock import patch

from ftre.channel.ws_channel import _validate_attachments, _persist_attachments


def test_persist_attachments_replaces_data_with_path():
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    b64 = base64.b64encode(raw).decode("ascii")

    attachments = [
        {"type": "image", "mime_type": "image/png", "data": b64, "name": "test.png"},
    ]

    _persist_attachments(attachments)

    assert "data" not in attachments[0]
    assert "path" in attachments[0]
    assert os.path.exists(attachments[0]["path"])
    assert attachments[0]["name"] == "test.png"
    assert attachments[0]["mime_type"] == "image/png"


def test_persist_attachments_handles_no_name():
    raw = b"\x00" * 10
    b64 = base64.b64encode(raw).decode("ascii")

    attachments = [
        {"type": "image", "mime_type": "image/jpeg", "data": b64},
    ]

    _persist_attachments(attachments)

    assert "data" not in attachments[0]
    assert "path" in attachments[0]
    assert attachments[0]["path"].endswith(".jpg")


def test_persist_attachments_noop_on_empty():
    attachments = []
    _persist_attachments(attachments)
    assert attachments == []


def test_persist_attachments_noop_on_none():
    _persist_attachments(None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ws_attachment_persist.py -v`
Expected: FAIL with `ImportError: cannot import name '_persist_attachments'`

- [ ] **Step 3: Write minimal implementation**

在 `ws_channel.py` 中，`_validate_attachments` 函数**之后**新增 `_persist_attachments` 函数：

```python
# src/ftre/channel/ws_channel.py — 在 _validate_attachments 之后（约 line 88 后）新增

def _persist_attachments(attachments: list | None) -> None:
    """将 attachments 中的 base64 data 落盘，替换为 path。

    在 _validate_attachments 校验通过后调用。原地修改 attachments 列表。
    """
    if not attachments:
        return

    from ftre.utils.image_store import save_image

    for att in attachments:
        if not isinstance(att, dict):
            continue
        if att.get("type") != "image":
            continue

        b64 = att.get("data", "")
        mime = att.get("mime_type", "image/png")
        name = att.get("name", "")

        try:
            raw = base64.b64decode(b64)
        except Exception:
            logger.warning(f"[ws-channel] 附件落盘失败，跳过: {name}")
            continue

        path = save_image(raw, mime, original_name=name)
        del att["data"]
        att["path"] = path
```

然后在 `_on_message` 方法中，校验通过后、进入 Bus 之前，调用 `_persist_attachments`：

找到 `_on_message` 中这段代码（约 line 307-312）：
```python
        # user_message 附件校验：违规直接拒绝，不进 Bus
        ok, err = _validate_attachments(data.get("attachments"))
        if not ok:
            logger.warning(f"[ws-channel] user_message 附件非法: {err}")
            await self._reject(ws, frame.get("id", ""), session_id, err)
            return
```

在其**之后**（`self._attach(session_id, ws)` 之前）插入：
```python
        # 附件落盘：base64 → temp 文件路径，事件链路不再携带 base64
        _persist_attachments(data.get("attachments"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ws_attachment_persist.py -v`
Expected: PASS (all 4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/ftre/channel/ws_channel.py tests/test_ws_attachment_persist.py
git commit -m "feat: ws_channel persists image attachments to temp, replaces base64 with path"
```

---

### Task 3: read 工具——压缩后落盘，使用 image_file 类型

**Files:**
- Modify: `src/ftre/tools/read.py:1-10`（移除 `import base64`，新增 image_store import）
- Modify: `src/ftre/tools/read.py:136-147`（`_image_to_event` 返回值）
- Test: `tests/test_plugin_tools.py:61-88`（`test_read_tool_reads_relative_image_path`）

**Interfaces:**
- Consumes: `save_image` from `ftre.utils.image_store`
- Produces: UserMessageEvent content 从 `[{"type":"image_url","image_url":{"url":"data:..."}}]` 变为 `[{"type":"image_file","path":"<abs_path>","mime_type":"<mime>"}]`

- [ ] **Step 1: Write the failing test（更新现有测试）**

将 `tests/test_plugin_tools.py` 中的 `test_read_tool_reads_relative_image_path` 更新为：

```python
def test_read_tool_reads_relative_image_path(tmp_path):
    import os

    class FakeWorkspace(WorkspaceAccessor):
        def __init__(self, cwd: str):
            self.cwd = cwd

        def get(self) -> str:
            return self.cwd

    image = tmp_path / "screen.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00"
        b"\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    result = create_read_tool().func(
        "screen.png",
        ws=FakeWorkspace(str(tmp_path)),
        llm_config=SimpleNamespace(vision=True),
    )

    assert isinstance(result, UserMessageEvent)
    assert result.metadata["hide"] is True
    assert result.metadata["path"] == str(image.resolve())
    assert result.content[0]["type"] == "image_file"
    assert "path" in result.content[0]
    assert os.path.exists(result.content[0]["path"])
    assert result.content[0]["mime_type"] == "image/png"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_plugin_tools.py::test_read_tool_reads_relative_image_path -v`
Expected: FAIL with `AssertionError: assert 'image_url' == 'image_file'`

- [ ] **Step 3: Write minimal implementation**

在 `src/ftre/tools/read.py` 中：

1. 移除 `import base64`（line 4），改为：

```python
import io
import logging
import urllib.request
from pathlib import Path
```

2. 在 import 区添加（`from ftre_agent_core...` 之后）：

```python
from ftre.utils.image_store import save_image
```

3. 将 `_image_to_event` 函数末尾（line 136-147）从：

```python
    # 以 data URI 内联，避免 LLM 侧再发起网络请求；base64 用 ascii 解码即可。
    data_uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    logger.info(f"[read] image {display_path} -> {mime}, {len(data)} bytes")

    # hide=True：图片本体只作为模型视觉输入，不在 UI 会话流里重复展示。
    return user_message_event(
        content=[{
            "type": "image_url",
            "image_url": {"url": data_uri},
        }],
        metadata={"hide": True, "path": display_path, "mime": mime, "size": len(data)},
    )
```

改为：

```python
    # 落盘到 temp 目录，事件链路只携带路径；base64 转换在 SessionManager 出口完成。
    original_name = Path(display_path).name if display_path else ""
    stored_path = save_image(data, mime, original_name=original_name)
    logger.info(f"[read] image {display_path} -> {stored_path} ({mime}, {len(data)} bytes)")

    # hide=True：图片本体只作为模型视觉输入，不在 UI 会话流里重复展示。
    return user_message_event(
        content=[{
            "type": "image_file",
            "path": stored_path,
            "mime_type": mime,
        }],
        metadata={"hide": True, "path": display_path, "mime": mime, "size": len(data)},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_plugin_tools.py::test_read_tool_reads_relative_image_path -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/ftre/tools/read.py tests/test_plugin_tools.py
git commit -m "feat: read tool stores image to temp, uses image_file type with path"
```

---

### Task 4: agent-core event.py——to_openai_message 处理 image_file

read 工具返回 `UserMessageEvent` 后，`react_runner.py:685` 调用 `ev.to_openai_message()` 将结果直接写入 agent memory。这条路径**绕过 ftre 的 multimodal.py**，必须在 agent-core 层完成 `image_file` → `image_url` 的转换。

agent-core 不能 import ftre 模块（依赖方向：ftre → agent-core，不可反向），因此用标准库 `base64` 直接读文件转换。

**Files:**
- Modify: `E:/ftre-agent-core/src/ftre_agent_core/agent/event.py:287-289`（`UserMessageEvent.to_openai_message`）
- Test: `E:/ftre-agent-core/tests/test_user_message_event_image_file.py`

**Interfaces:**
- Consumes: Task 3 产出的 `UserMessageEvent(content=[{"type":"image_file","path":"...","mime_type":"..."}])`
- Produces: `to_openai_message()` 返回 `{"role":"user","content":[{"type":"image_url","image_url":{"url":"data:..."}}]}`，可直接进 agent memory

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_message_event_image_file.py
import base64
import os
import tempfile

from ftre_agent_core.agent.event import UserMessageEvent


def _make_temp_image(raw: bytes, suffix: str = ".png") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return path


def test_to_openai_message_converts_image_file_to_image_url():
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    path = _make_temp_image(raw, ".png")

    ev = UserMessageEvent(
        content=[{"type": "image_file", "path": path, "mime_type": "image/png"}],
    )

    msg = ev.to_openai_message()

    assert msg["role"] == "user"
    content = msg["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")

    # 验证 base64 内容正确
    b64_part = content[0]["image_url"]["url"].split(",", 1)[1]
    assert base64.b64decode(b64_part) == raw


def test_to_openai_message_handles_missing_file():
    ev = UserMessageEvent(
        content=[{"type": "image_file", "path": "/nonexistent/image.png", "mime_type": "image/png"}],
    )

    msg = ev.to_openai_message()

    assert msg["role"] == "user"
    content = msg["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "图片加载失败" in content[0]["text"]


def test_to_openai_message_preserves_image_url_type():
    """已有的 image_url 类型不受影响。"""
    ev = UserMessageEvent(
        content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}],
    )

    msg = ev.to_openai_message()

    assert msg["content"][0] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}


def test_to_openai_message_preserves_text_type():
    ev = UserMessageEvent(content="hello world")

    msg = ev.to_openai_message()

    assert msg == {"role": "user", "content": "hello world"}


def test_to_openai_message_mixed_content():
    raw = b"\x00" * 50
    path = _make_temp_image(raw, ".png")

    ev = UserMessageEvent(
        content=[
            {"type": "text", "text": "看这张图"},
            {"type": "image_file", "path": path, "mime_type": "image/png"},
        ],
    )

    msg = ev.to_openai_message()

    content = msg["content"]
    assert content[0] == {"type": "text", "text": "看这张图"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_user_message_event_image_file.py -v`
Expected: FAIL with `AssertionError: assert 'image_file' == 'image_url'`（当前 `to_openai_message` 原样返回 content）

- [ ] **Step 3: Write minimal implementation**

在 `E:/ftre-agent-core/src/ftre_agent_core/agent/event.py` 中，找到 `UserMessageEvent.to_openai_message`（约 line 287-289）：

```python
    def to_openai_message(self) -> dict:
        """转为 OpenAI 格式 user message，可直接追加到 memory。"""
        return {"role": "user", "content": self.content}
```

替换为：

```python
    def to_openai_message(self) -> dict:
        """转为 OpenAI 格式 user message，可直接追加到 memory。

        content 中的 image_file part 会被转换为 image_url（读文件转 base64），
        以兼容 OpenAI 多模态格式。其他 part 类型原样保留。
        """
        content = self.content
        if isinstance(content, list):
            content = [_convert_image_file_part(p) for p in content]
        return {"role": "user", "content": content}
```

然后在 `UserMessageEvent` 类**之后**、`_from_type` 工厂函数**之前**（约 line 294 处）新增模块级辅助函数：

```python
def _convert_image_file_part(part: dict) -> dict:
    """将 image_file part 转换为 image_url（读文件转 base64 data URL）。

    agent-core 不能依赖 ftre 的 image_store，因此用标准库 base64 直接读取。
    文件不存在或读取失败时降级为文本提示，不抛异常。
    """
    if not isinstance(part, dict):
        return part
    if part.get("type") != "image_file":
        return part

    path = part.get("path", "")
    mime = part.get("mime_type", "image/png")
    if not path:
        return {"type": "text", "text": "[图片加载失败: 无文件路径]"}

    try:
        import base64 as _b64
        with open(path, "rb") as f:
            raw = f.read()
        b64 = _b64.b64encode(raw).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        }
    except Exception as e:
        return {"type": "text", "text": f"[图片加载失败: {path} ({e})]"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_user_message_event_image_file.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Commit**

```bash
cd E:/ftre-agent-core
git add src/ftre_agent_core/agent/event.py tests/test_user_message_event_image_file.py
git commit -m "feat: UserMessageEvent.to_openai_message converts image_file to image_url"
```

---

### Task 5: multimodal.py——路径转 base64 的集中转换点

**Files:**
- Modify: `src/ftre/session/multimodal.py:83-150`（`normalize_user_content` 增加 `image_file` 处理）
- Modify: `src/ftre/session/multimodal.py:153-192`（`build_user_content` 改为读 `path` 而非 `data`）

**Interfaces:**
- Consumes: `load_as_data_url` from `ftre.utils.image_store`
- Produces: `normalize_user_content` 能处理 `image_file` 类型 part → 转为 `image_url`（读文件转 base64）；`build_user_content` 能从 attachment 的 `path` 字段读取文件转 base64

- [ ] **Step 1: Write the failing test**

```python
# tests/test_multimodal_path.py
import os
import base64

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

    # 缺失文件时跳过图片，不崩溃
    assert isinstance(result, list)
    assert result[0] == {"type": "text", "text": "看图"}
    assert len(result) == 1  # image_file 被跳过


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

    # 缺失文件时只返回文本
    assert isinstance(result, str)
    assert result == "描述图片"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_multimodal_path.py -v`
Expected: FAIL（`normalize_user_content` 不认识 `image_file` 类型；`build_user_content` 找不到 `data` 字段）

- [ ] **Step 3: Write minimal implementation**

在 `src/ftre/session/multimodal.py` 中：

1. 在文件顶部 import 区添加（`from xml.sax.saxutils import escape` 之后）：

```python
from ftre.utils.image_store import load_as_data_url
```

2. 在 `normalize_user_content` 函数中，找到 `elif ptype == "image":` 分支（约 line 129-130）：

```python
        elif ptype == "image":
            omitted_image = True
```

在其**之后**新增 `image_file` 处理分支：

```python
        elif ptype == "image_file":
            if include_images:
                file_path = part.get("path", "")
                file_mime = part.get("mime_type", "")
                if file_path:
                    data_url = load_as_data_url(file_path, mime=file_mime)
                    if data_url:
                        normalized.append({
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        })
                    else:
                        omitted_image = True
                else:
                    omitted_image = True
            else:
                omitted_image = True
```

3. 将 `build_user_content` 函数中的 attachment 处理循环（约 line 176-188）从：

```python
    for att in attachments:
        if att.get("type") != "image":
            continue
        mime = att.get("mime_type", "")
        b64 = att.get("data", "")
        if not mime or not b64:
            continue
        parts_multi.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            }
        )
```

改为：

```python
    for att in attachments:
        if att.get("type") != "image":
            continue
        mime = att.get("mime_type", "")
        file_path = att.get("path", "")
        if not file_path:
            continue
        data_url = load_as_data_url(file_path, mime=mime)
        if not data_url:
            continue
        parts_multi.append(
            {
                "type": "image_url",
                "image_url": {"url": data_url},
            }
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_multimodal_path.py -v`
Expected: PASS (all 6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/ftre/session/multimodal.py tests/test_multimodal_path.py
git commit -m "feat: multimodal.py converts image_file/path to base64 at SessionManager boundary"
```

---

### Task 6: 更新 compact_algo 测试——attachment 用 path 替代 data

**Files:**
- Modify: `tests/test_compact_algo.py:340-411`（6 个 image 相关测试）

**Interfaces:**
- Consumes: Task 5 的 `build_user_content` 支持 path-based attachments

- [ ] **Step 1: Write the failing test（更新现有测试）**

将 `tests/test_compact_algo.py` 中 6 个 image 相关测试的 attachment 从 `{"data": "abc"}` 改为使用真实 temp 文件。在文件顶部添加 import：

```python
import base64
import os
from ftre.utils.image_store import save_image
```

将以下 3 个测试（`test_to_openai_messages_omits_images_by_default`、`test_to_openai_messages_omits_images_when_vision_disabled`、`test_to_openai_messages_keeps_images_when_vision_enabled`）中的 attachment 部分：

```python
            "attachments": [{
                "type": "image",
                "mime_type": "image/png",
                "data": "abc",
            }],
```

改为：

```python
            "attachments": [{
                "type": "image",
                "mime_type": "image/png",
                "path": _make_test_image(),
            }],
```

并在文件顶部（import 之后）添加辅助函数：

```python
def _make_test_image() -> str:
    """创建一个真实的 temp 图片文件，返回路径。"""
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    return save_image(raw, "image/png", "compact_test.png")
```

对于 `test_to_openai_messages_keeps_images_when_vision_enabled`，断言部分需要更新——不再断言固定的 base64 值，而是断言 data URL 前缀：

```python
    assert msgs[0]["content"][0] == {"type": "text", "text": "看图"}
    assert msgs[0]["content"][1]["type"] == "image_url"
    assert msgs[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
```

对于 `test_to_openai_messages_omits_user_message_images_without_vision` 和 `test_to_openai_messages_keeps_user_message_images_with_vision`——这两个测试使用 `image_url` 类型（来自历史 UserMessageEvent），不是 attachment，不需要改动。

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_compact_algo.py -k "image" -v`
Expected: FAIL（`build_user_content` 不再识别 `data` 字段，只认 `path`）

- [ ] **Step 3: 验证测试通过（无需额外实现代码，Task 5 已完成实现）**

Run: `python -m pytest tests/test_compact_algo.py -k "image" -v`
Expected: PASS

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_compact_algo.py
git commit -m "test: update compact_algo tests to use path-based image attachments"
```

---

### Task 7: 端到端验证——read 工具 + ws_channel 完整链路

**Files:**
- Test: `tests/test_image_e2e.py`

**Interfaces:**
- Consumes: Task 1-6 的全部实现

- [ ] **Step 1: Write the test**

```python
# tests/test_image_e2e.py
"""端到端验证：read 工具产生的 image_file 事件 → to_openai_messages 转换 → 包含 base64 data URL"""
import os
from types import SimpleNamespace

from ftre_agent_core.agent.event import UserMessageEvent
from ftre.tools.read import create_read_tool
from ftre.tools._workspace import WorkspaceAccessor
from ftre.session.manager import SessionManager


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

    msgs = SessionManager.to_openai_messages(
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

    msgs = SessionManager.to_openai_messages(
        events,
        config={"llm": {"vision": False}},
    )

    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert isinstance(content, str)
    assert "当前模型不支持视觉输入" in content
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python -m pytest tests/test_image_e2e.py -v`
Expected: PASS (all 2 tests)

- [ ] **Step 3: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_image_e2e.py
git commit -m "test: end-to-end verification of image_file path-based flow"
```

---

## Self-Review

**1. Spec coverage:**
- ✅ 用户上传附件 base64 → 后端落盘转路径（Task 2: ws_channel `_persist_attachments`）
- ✅ read 工具压缩后落盘，使用 image_file 类型（Task 3）
- ✅ 当前轮工具结果进 memory 的 base64 转换（Task 4: `UserMessageEvent.to_openai_message()` in agent-core）
- ✅ 历史重建和首条消息附件的 base64 转换（Task 5: `build_user_content` + `normalize_user_content` in multimodal.py）
- ✅ 统一 temp 存储，原始文件名去特殊符号（Task 1: `image_store.py`）
- ✅ 不主动清理（Task 1: 无清理逻辑）
- ✅ 前端不改动（Task 2: ws_channel 仍接收 base64，后端落盘）

**2. Placeholder scan:** 无 TBD / TODO / "implement later"。所有代码块完整。

**3. Type consistency:**
- `save_image(raw: bytes, mime: str, original_name: str = "") -> str` — Task 1 定义，Task 2/3 消费，签名一致
- `load_as_data_url(path: str, mime: str = "") -> str | None` — Task 1 定义，Task 5 消费，签名一致
- attachment dict: Task 2 产出 `{"type":"image","mime_type":"...","path":"...","name":"..."}`，Task 5 消费 `att.get("path")` — 一致
- `image_file` part: Task 3 产出 `{"type":"image_file","path":"...","mime_type":"..."}`，Task 4（agent-core `_convert_image_file_part`）和 Task 5（ftre `normalize_user_content`）消费 `part.get("path")` + `part.get("mime_type")` — 一致
