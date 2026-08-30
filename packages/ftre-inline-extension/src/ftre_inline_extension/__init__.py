"""Stable protocol types for inline ftre message extensions."""

from .errors import ExtensionParseError
from .parser import ExtensionParser
from .registry import (
    ExtensionDiagnostic,
    InlineExtensionHandler,
    InlineExtensionRegistry,
)
from .types import ExtensionContext, ExtensionRef, ExtensionResolution, ExtensionSpan

__all__ = [
    "ExtensionContext",
    "ExtensionDiagnostic",
    "ExtensionParseError",
    "ExtensionParser",
    "ExtensionRef",
    "ExtensionResolution",
    "ExtensionSpan",
    "InlineExtensionHandler",
    "InlineExtensionRegistry",
]
