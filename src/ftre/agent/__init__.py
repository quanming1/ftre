"""Legacy Agent package surface with lazy exports to avoid runtime cycles."""

__all__ = ["AgentLoop"]


def __getattr__(name: str):
    if name == "AgentLoop":
        from ftre.services.agent.runtime.loop.engine import AgentLoop

        return AgentLoop
    raise AttributeError(name)
