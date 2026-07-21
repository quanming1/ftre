"""
文件变更 diff 生成 — 供 edit/write 工具的 metadata 使用。

用 Python 标准库 difflib 在内存中对比修改前后的文件内容，
生成 unified diff 文本及增删行数统计，不依赖 git。
"""
from __future__ import annotations

import difflib
from pathlib import Path


def build_diff_metadata(
    file_path: str | Path,
    content_old: str,
    content_new: str,
) -> dict:
    """生成 tool_result.metadata 中的 diff 信息。

    Args:
        file_path: 文件绝对路径
        content_old: 修改前内容（已统一为 \\n 换行）
        content_new: 修改后内容（已统一为 \\n 换行）

    Returns:
        {"file": ..., "before": ..., "after": ..., "diff": ..., "additions": N, "deletions": M}
        如果新旧内容完全一致，返回空 dict（不产生 metadata）。
    """
    if content_old == content_new:
        return {}

    display_path = str(file_path).replace("\\", "/")

    diff_lines = list(difflib.unified_diff(
        content_old.splitlines(keepends=True),
        content_new.splitlines(keepends=True),
        fromfile=display_path,
        tofile=display_path,
    ))
    diff_text = "".join(diff_lines)

    additions = sum(
        1 for line in diff_lines
        if line.startswith("+") and not line.startswith("+++")
    )
    deletions = sum(
        1 for line in diff_lines
        if line.startswith("-") and not line.startswith("---")
    )

    return {
        "file": display_path,
        "before": content_old,
        "after": content_new,
        "diff": diff_text,
        "additions": additions,
        "deletions": deletions,
    }
