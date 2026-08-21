from __future__ import annotations

from fastapi import Request


def same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("host", "")
    return origin.rsplit("/", 1)[-1] == host

