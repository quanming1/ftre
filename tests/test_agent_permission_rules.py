from ftre_agent_core.permission import PermissionBehavior

from ftre.services.agent.profile.manager import AgentManager


def test_default_bash_rules_are_temporarily_disabled():
    context = AgentManager._default_agent_state().permission_context
    assert context.permission_rules == []
    assert context.default_behavior == PermissionBehavior.ALLOW
