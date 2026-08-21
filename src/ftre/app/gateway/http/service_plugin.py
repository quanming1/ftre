"""Compatibility entry that keeps the HTTP Service manifest path stable."""

from ftre.services.http.plugin import apply, inject, provide

__all__ = ["apply", "inject", "provide"]
