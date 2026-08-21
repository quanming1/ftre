"""Provider Plugin for persisted Agent profile configuration."""

from __future__ import annotations

from cordis import PluginContext
from ftre.services.config.paths import AGENTS_DIR

from .manager import AgentManager
from .service import AgentProfileService

inject = ()
provide = ("agent_profiles",)


def apply(ctx: PluginContext, config=None):
    """Build the profile manager and publish it as ``agent_profiles``."""
    if ctx.optional("agent_profiles") is not None:
        return
    options = config if isinstance(config, dict) else {}
    manager = AgentManager(agents_dir=options.get("agents_dir", AGENTS_DIR))
    manager.ensure_default()
    ctx.provide("agent_profiles", AgentProfileService(manager))
