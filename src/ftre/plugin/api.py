"""Stable plugin-facing aliases during the Kernel-to-Cordis migration.

New plugins should import `cordis.Context`/`PluginContext` and declare
`inject`/`provide` on their entry.  The legacy `ftre.plugin.Plugin` class is
kept in `kernel` solely so existing installed plugins can be upgraded without
an atomic ecosystem migration.
"""

from cordis import Effect, Fiber, FiberState, Inject, PluginContext, Service
from ftre.platform.plugin_runtime.manifest import PluginManifest

__all__ = ["Effect", "Fiber", "FiberState", "Inject", "PluginContext", "PluginManifest", "Service"]

