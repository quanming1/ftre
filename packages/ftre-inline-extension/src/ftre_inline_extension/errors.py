"""Errors raised by the inline extension protocol boundary."""

from __future__ import annotations


class ExtensionParseError(ValueError):
    """Raised by ``parse_one`` when a token is malformed or unsupported."""


__all__ = ["ExtensionParseError"]
