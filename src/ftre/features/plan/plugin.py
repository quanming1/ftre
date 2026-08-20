from __future__ import annotations

from cordis import PluginContext

inject = ("tools",)
provide = ()


def apply(ctx: PluginContext, config=None):
    # Existing plan tool registration remains owned by the compatibility
    # adapter; this plugin deliberately has no hidden global state.
    return None

