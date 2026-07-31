from ftre.agent.agent_manager import AgentManager
from ftre_agent_core.permission import (
    PermissionBehavior,
    PermissionEngine,
    PermissionRequest,
)


def test_default_bash_rules_are_temporarily_disabled():
    context = AgentManager._default_agent_state().permission_context
    decision = PermissionEngine().evaluate(
        PermissionRequest(
            tool_name="bash",
            arguments={"command": "git stash pop"},
        ),
        [],
        PermissionBehavior(context["default_behavior"]),
    )
    assert context["permission_rules"] == []
    assert decision.behavior == PermissionBehavior.ALLOW
