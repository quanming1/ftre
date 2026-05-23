"""
文件 IO 辅助 — 编码与换行保真

read/write/edit 共用，保证：
- 读取：自动尝试 utf-8 / utf-8-sig / gbk / cp936 / latin-1
- 编辑：写回时保留原文件的 encoding 和换行风格（CRLF/LF/CR）
- 新建：utf-8 + LF
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# 候选编码顺序：先严格再宽松
_ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "gbk", "cp936", "shift-jis")


@dataclass
class TextFile:
    """读出来的纯文本文件，记录原始 encoding 和换行风格"""
    text: str         # 已统一为 \n 的内容
    encoding: str     # 实际成功的编码
    newline: str      # "\r\n" / "\r" / "\n"
    had_bom: bool     # 是否带 utf-8 BOM


def _detect_newline(raw: str) -> str:
    """根据原始（未替换）字符串判断主要换行风格"""
    if "\r\n" in raw:
        return "\r\n"
    if "\r" in raw:
        return "\r"
    return "\n"


def read_text(path: Path) -> TextFile:
    """
    读取文本文件，自动尝试多种编码。
    返回的 text 已统一为 \\n 换行；原换行风格保存在 newline 字段。
    """
    data = path.read_bytes()

    # 显式判断 UTF-8 BOM
    had_bom = data.startswith(b"\xef\xbb\xbf")

    # 候选编码顺序：先严格再宽松
    # 注意：把 utf-8-sig 放第一只用于"剥离 BOM"，是否真带 BOM 由 had_bom 决定
    last_error: Exception | None = None
    raw: str | None = None
    enc_used: str = ""
    for enc in _ENCODING_CANDIDATES:
        try:
            raw = data.decode(enc)
            enc_used = enc
            break
        except UnicodeDecodeError as e:
            last_error = e
            continue

    if raw is None:
        raw = data.decode("latin-1")
        enc_used = "latin-1"

    # 校正：utf-8-sig 解码不带 BOM 的 utf-8 文件也会成功，
    # 但写回时会加 BOM。所以无 BOM 时统一为 utf-8。
    if enc_used == "utf-8-sig" and not had_bom:
        enc_used = "utf-8"

    newline = _detect_newline(raw)
    text = raw.replace("\r\n", "\n").replace("\r", "\n")

    return TextFile(text=text, encoding=enc_used, newline=newline, had_bom=had_bom)


def write_text_preserving(path: Path, text: str, original: TextFile) -> int:
    """
    写回文件时保留原 encoding 和换行风格。
    传入的 text 应该使用 \\n 作为换行（也即 read_text 返回的格式）。
    返回写入的字节数。
    """
    # 还原换行风格
    if original.newline != "\n":
        out = text.replace("\n", original.newline)
    else:
        out = text

    # encoding：utf-8-sig 会自动加 BOM
    encoding = original.encoding
    # latin-1 不一定能编码所有字符，回退 utf-8
    if encoding == "latin-1":
        try:
            out.encode("latin-1")
        except UnicodeEncodeError:
            encoding = "utf-8"

    data = out.encode(encoding)
    path.write_bytes(data)
    return len(data)


def write_text_new(path: Path, text: str, encoding: str = "utf-8", newline: str = "\n") -> int:
    """
    创建新文件：默认 utf-8 + LF。
    传入的 text 应该使用 \\n 作为换行；如需 CRLF 显式传入。
    返回写入的字节数。
    """
    if newline != "\n":
        text = text.replace("\n", newline)
    data = text.encode(encoding)
    path.write_bytes(data)
    return len(data)
