"""
read 工具 - 读取文件内容或图片内容
"""
import base64
import io
import logging
import urllib.request
from pathlib import Path

from ftre_agent_core.agent.event import UserMessageEvent, user_message_event
from ftre_agent_core.tool import Tool, ToolParameter, Injected

from ._io import read_text, file_meta_header
from ._truncate import truncate_output
from ._workspace import WorkspaceAccessor

logger = logging.getLogger(__name__)

MAX_IMAGE_FILE_SIZE = 5 * 1024 * 1024
MAX_IMAGE_DIMENSION = 4096

IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}


def _resolve(path: str, cwd: str) -> Path:
    """解析路径：相对路径基于 cwd（会话工作区），绝对路径直接用；统一展开 ~ 并归一化。"""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    return p.resolve()


def _is_url(path: str) -> bool:
    return path.startswith(("http://", "https://"))


def _list_dir(p: Path) -> str:
    """列出目录下的直接条目：目录在前、文件在后，各自按名称排序。

    目录名带尾随 `/` 以便区分，文件附带字节大小，方便后续决定如何读取。
    """
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name))
    lines = ["<FTRE_SYSTEM_FACT>", f"[dir] {p}"]
    for e in entries:
        lines.append(f"  {e.name}/" if e.is_dir() else f"  {e.name}  ({e.stat().st_size} bytes)")
    lines.append("</FTRE_SYSTEM_FACT>")
    return truncate_output("\n".join(lines))


def _is_image_path(path: str) -> bool:
    # URL 一律按图片处理：本工具的 URL 场景只用于读图（远程文本应由 web 工具获取），
    # 真正的 MIME 由下载响应头校验，非图片会在 _image_to_event 中被拒绝。
    if _is_url(path):
        return True
    return Path(path).suffix.lower() in IMAGE_MIME_BY_EXT


def _load_image(src: str, cwd: str) -> tuple[bytes, str, str]:
    """读取图片并返回 (bytes, mime_type, display_path)。"""
    if _is_url(src):
        req = urllib.request.Request(src, headers={"User-Agent": "ftre-read"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "image/png")
        mime = content_type.split(";", 1)[0].strip() or "image/png"
        return data, mime, src

    p = _resolve(src, cwd)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {p}")
    if p.is_dir():
        raise IsADirectoryError(f"是目录而非文件: {p}")
    if not p.is_file():
        raise ValueError(f"不是普通文件: {p}")

    mime = IMAGE_MIME_BY_EXT.get(p.suffix.lower(), "image/png")
    return p.read_bytes(), mime, str(p)


def _compress_image(data: bytes, mime: str) -> tuple[bytes, str]:
    """图片超过体积上限时压缩：统一转 JPEG，按尺寸缩放 + 降质，必要时二次压缩。

    返回压缩后的 (bytes, mime)。小于上限的图原样返回，不做任何转码。
    """
    if len(data) <= MAX_IMAGE_FILE_SIZE:
        return data, mime

    from PIL import Image

    img = Image.open(io.BytesIO(data))
    # JPEG 不支持透明通道，RGBA / 调色板模式直接 save 会抛错，先统一转 RGB。
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    def _encode(max_dim: int, quality: int) -> bytes:
        # 按最长边等比缩放到 max_dim 以内（不放大），再以指定质量编码为 JPEG。
        w, h = img.size
        ratio = min(max_dim / max(w, h), 1.0)
        resized = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()

    # 第一轮：限制到 4096px / quality 70；仍超限再退到 2048px / quality 60。
    compressed = _encode(MAX_IMAGE_DIMENSION, 70)
    if len(compressed) > MAX_IMAGE_FILE_SIZE:
        compressed = _encode(2048, 60)

    return compressed, "image/jpeg"


def _image_to_event(path: str, cwd: str) -> UserMessageEvent | str:
    """把图片加载为可直接喂给 LLM 的视觉输入事件；失败时返回 [error] 文本。"""
    try:
        data, mime, display_path = _load_image(path, cwd)
    except Exception as e:
        return f"[error] 图片读取失败: {e}"

    # 防御 URL 场景：响应头声称的类型可能不是图片（如错填了网页链接）。
    if not mime.startswith("image/"):
        return f"[error] 不是图片内容: {display_path} ({mime}, {len(data)} bytes)"

    try:
        data, mime = _compress_image(data, mime)
    except Exception as e:
        return f"[error] 图片压缩失败: {type(e).__name__}: {e}"

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


def create_read_tool(max_bytes: int = 256 * 1024, *, vision: bool = False) -> Tool:
    """创建 read 工具

    Args:
        max_bytes: 单文件最大读取字节数（默认 256KB）。超出必须显式 start_line/end_line
        vision: 当前模型是否支持识图。决定 description 中是否声明可读取图片
    """

    def read(
        path: str,
        start_line: int = 0,
        end_line: int = 0,
        ws: WorkspaceAccessor = Injected("workspace"),
        llm_config=Injected("llm_config"),
    ) -> str | UserMessageEvent:
        try:
            if not isinstance(ws, WorkspaceAccessor):
                return "[error] runtime_context.workspace 未注入"
            cwd = ws.get()

            # 图片分支：仅当未指定行范围且路径看起来是图片时走视觉读取。
            # 显式传了 start_line/end_line 说明调用方想按文本读，沿用文本分支。
            if start_line <= 0 and end_line <= 0 and _is_image_path(path):
                if not getattr(llm_config, "vision", False):
                    return "[error] 当前模型不支持视觉输入，无法读取图片内容。请切换到支持 vision 的模型后再读取图片。"
                return _image_to_event(path, cwd)

            p = _resolve(path, cwd)
            if not p.exists():
                return (
                    f"[error] 文件不存在: {p}\n"
                    f"提示：相对路径基于当前会话工作区 {cwd}（不是 bash 的 [cwd]）。"
                    f"用 `cd x && ...` 这类组合命令切的目录【不会】改变工作区；"
                    f"若文件在别处，请改用绝对路径读取，或先用纯 `cd <dir>` / set_workspace 切换工作区。"
                )
            if p.is_dir():
                # 目录直接列出其下条目（目录在前、文件在后，各自按名排序）。
                return _list_dir(p)
            if not p.is_file():
                return f"[error] 不是普通文件: {p}"

            # 大文件保护：未给 end_line 时拒绝整读，强制调用方分段，避免撑爆上下文。
            size = p.stat().st_size
            if size > max_bytes and end_line == 0:
                return (
                    f"[error] 文件过大 ({size} bytes > {max_bytes})。"
                    f"请使用 start_line / end_line 分段读取"
                )

            tf = read_text(p)
            lines = tf.text.splitlines()

            # 行号统一 1-indexed 左对齐展示；指定范围时按闭区间切片（end_line 含）。
            if start_line > 0 or end_line > 0:
                start = max(0, start_line - 1)
                end = end_line if end_line > 0 else len(lines)
                visible = lines[start:end]
                numbered = [f"{i + start + 1:6d}| {line}" for i, line in enumerate(visible)]
            else:
                numbered = [f"{i + 1:6d}| {line}" for i, line in enumerate(lines)]

            # 文件元信息头：绝对路径、编码、换行符、大小、行数（系统事实）。
            meta = file_meta_header(p, tf)
            note = ""
            if tf.encoding not in ("utf-8", "utf-8-sig"):
                note = f"[encoding] 内容已从 {tf.encoding} 转为 UTF-8 呈现\n"
            return truncate_output(meta + "\n" + note + "\n" + "\n".join(numbered))
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}"

    # 根据当前模型是否支持识图，动态拼接图片相关说明，避免对纯文本模型误导其可以读图。
    vision_lines = (
        (
            "- 当前模型支持识图，可读取图片：支持 png / jpg / jpeg / gif / webp / bmp / svg，"
            "本地绝对路径、相对工作区路径和 HTTP(S) URL；大图自动压缩至 5MB/4096px 以内\n"
            "- 读取图片可用于理解截图、UI 修改、浏览器操控结果、视觉回归和设计还原\n"
        )
        if vision
        else "- 当前模型不支持识图，无法读取图片，仅能读取文本文件\n"
    )

    return Tool(
        name="read",
        description=(
            "读取文件内容；文本返回带行号内容，图片返回 LLM 可识别的视觉输入。"
            "相对路径基于当前会话的工作区目录。\n"
            "- 自动识别编码（utf-8 / utf-8-sig / gbk 等），非 utf-8 文件首行会显示 [encoding]\n"
            "- 可选 start_line / end_line 限定行范围（1-indexed，闭区间）\n"
            "- 超过 256KB 的文件必须传入行范围\n"
            + vision_lines
            + "- 路径是目录时返回该目录下的文件与子目录列表（目录在前，文件附带大小）\n"
            "- 宁愿一次性读完整个文件也不要对同一文件分多次小段反复读取\n"
            "- 已经读取过的文件，内容已在上下文中，不要重复读取"
        ),
        parameters=[
            ToolParameter(
                name="path",
                type="string",
                description="文件或图片路径（绝对路径、相对当前工作区路径，图片也支持 HTTP URL）",
                required=True,
            ),
            ToolParameter(
                name="start_line",
                type="number",
                description="起始行号（1-indexed），0 表示从头",
                required=False,
            ),
            ToolParameter(
                name="end_line",
                type="number",
                description="结束行号（1-indexed，闭区间），0 表示到末尾",
                required=False,
            ),
        ],
        func=read,
    )
