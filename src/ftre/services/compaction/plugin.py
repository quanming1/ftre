"""Provider Plugin for the single public CompactionService owner."""

from __future__ import annotations

from cordis import Context

from .service import CompactionService

inject = ("sessions",)
provide = ("compaction",)


def apply(ctx: Context, config=None):
    """Create and publish CompactionService; hooks belong to the Feature Plugin."""
    if ctx.get("compaction", strict=False) is not None:
        return
    options = config if isinstance(config, dict) else {}
    service = CompactionService(
        session_manager=ctx.sessions,
        threshold=float(options.get("threshold", 0.8)),
    )
    ctx.provide("compaction", service)
    ctx.effect(service.close, label="compaction:close")


__all__ = ["apply", "inject", "provide"]
