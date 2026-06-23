from ftre.agent.loop import AGENT_EXECUTION_POLICY, _build_agent_system_prompt


def test_execution_policy_is_appended_to_custom_system_prompt():
    prompt = _build_agent_system_prompt("自定义提示词")

    assert prompt.startswith("自定义提示词\n\n")
    assert AGENT_EXECUTION_POLICY in prompt
    assert "必须立即实际调用工具" in prompt
    assert "必须实际运行相关命令验证改动" in prompt


def test_mcp_hint_is_appended_after_execution_policy():
    prompt = _build_agent_system_prompt("基础提示词", "<mcp>工具说明</mcp>")

    assert prompt.index(AGENT_EXECUTION_POLICY) < prompt.index("<mcp>工具说明</mcp>")
