from ftre_agent_core.permission import PermissionBehavior
from ftre_agent_runtime.factory import default_agent_state


def test_default_bash_rules_are_temporarily_disabled():
    context = default_agent_state().permission_context
    assert context.permission_rules == []
    assert context.default_behavior == PermissionBehavior.ALLOW
