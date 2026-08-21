"""Compatibility alias for AgentManager construction.

This module is intentionally not a Service or Plugin entry point. The public
``agents`` Service is provided by ``services.agent.service``; this alias keeps
older imports working while the Agent runtime is migrated incrementally.
"""

from ftre.agent.agent_manager import AgentManager

__all__ = ["AgentManager"]
