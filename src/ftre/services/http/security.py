"""HTTP Host 使用的轻量来源校验辅助函数。"""

from __future__ import annotations

from fastapi import Request


def same_origin(request: Request) -> bool:
    """无 Origin 时放行；有 Origin 时要求其 host 与请求 Host 一致。"""
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("host", "")
    return origin.rsplit("/", 1)[-1] == host
