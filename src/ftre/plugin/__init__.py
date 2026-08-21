"""Minimal compatibility surface for installed external plugins.

The old ``ftre.plugin.kernel`` implementation has been retired.  A small
marker base remains so already-installed ``setup(ctx, config)`` plugins can be
loaded by Cordis' ``LegacyPluginContext`` while they migrate to ``apply`` and
public Service keys.  New in-repository code must import hooks from
``ftre.services.agent.runtime.hooks`` and runtime primitives from ``cordis``.
"""

from typing import Any, ClassVar

from ftre.services.agent.runtime.hooks import (
    BEFORE_AGENT_RUN,
    BEFORE_MESSAGES_BUILD,
    AgentRunContext,
    MessagesBuildContext,
    append_to_first_system,
)


class Plugin:
    """Declarative marker for legacy external ``setup`` plugins only."""

    name: ClassVar[str] = ""
    version: ClassVar[str] = ""
    inject: ClassVar[tuple[str, ...]] = ()
    provide: ClassVar[tuple[str, ...]] = ()
    Config: ClassVar[Any] = None


__all__ = [
    "BEFORE_AGENT_RUN",
    "BEFORE_MESSAGES_BUILD",
    "AgentRunContext",
    "MessagesBuildContext",
    "Plugin",
    "append_to_first_system",
]
