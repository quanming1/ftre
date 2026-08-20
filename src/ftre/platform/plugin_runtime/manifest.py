"""Stable ftre metadata around a Cordis plugin entry."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


@dataclass(frozen=True)
class PluginManifest:
    id: str
    entry: str | Callable[..., Any] | Any
    source: str = "builtin"
    required: bool = False
    default_enabled: bool = True
    version: str | None = None
    description: str = ""
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not PLUGIN_ID_RE.fullmatch(self.id):
            raise ValueError(f"invalid plugin id: {self.id!r}")
        if self.source not in {"builtin", "external"} and not self.source.startswith("external:"):
            raise ValueError(f"invalid plugin source: {self.source!r}")
        if not callable(self.entry) and not isinstance(self.entry, str) and not hasattr(self.entry, "apply"):
            raise TypeError(f"plugin {self.id!r} entry is not callable")

    @property
    def entry_text(self) -> str:
        return self.entry if isinstance(self.entry, str) else f"{self.entry.__module__}:{self.entry.__name__}"

    def with_config(self, config: dict[str, Any] | None) -> "PluginManifest":
        merged = dict(self.config)
        if config:
            merged.update(config)
        return PluginManifest(
            id=self.id,
            entry=self.entry,
            source=self.source,
            required=self.required,
            default_enabled=self.default_enabled,
            version=self.version,
            description=self.description,
            config=merged,
        )

