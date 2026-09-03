"""Provide the package-level ProcessService to Host consumers."""

from __future__ import annotations

from cordis import Context
from ftre_process import ProcessService

inject = ()
provide = ("process",)


def apply(ctx: Context, config=None) -> None:
    service = ProcessService()
    ctx.provide("process", service)
    ctx.effect(lambda: service.close, label="process:close")
