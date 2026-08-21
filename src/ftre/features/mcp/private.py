"""Helpers for constructing per-Agent MCP scopes."""

from __future__ import annotations


def private_scope(agent_id: str) -> str:
    """Return the stable scope label used to isolate one Agent's servers."""
    return f"agent:{agent_id}"
