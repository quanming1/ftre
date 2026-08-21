"""FastAPI application factory owned by the Gateway App Host."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

_LOCAL_DESKTOP_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"


def create_app(http_service, *, cors_origins: list[str] | None = None) -> FastAPI:
    """Materialize the frozen HttpService registry and apply desktop CORS policy."""
    app = http_service.build_app(title="ftre", version="0.2.4")

    # The desktop dev server uses an ephemeral port (for example
    # ``localhost:48651``), so allowing only the bare localhost origins makes
    # every otherwise successful API response look like a network failure in
    # the browser.  Keep custom origins exact; the regex is only the default
    # loopback policy and never permits remote hosts.
    default_origins = cors_origins is None
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["http://localhost", "http://127.0.0.1"],
        allow_origin_regex=_LOCAL_DESKTOP_ORIGIN_REGEX if default_origins else None,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    return app
