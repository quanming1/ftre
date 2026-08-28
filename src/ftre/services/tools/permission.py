"""ToolService 的纯权限决策引擎。"""

from __future__ import annotations

import re

from ftre_agent.tool.permission import (
    PermissionBehavior,
    PermissionDecision,
    PermissionRequest,
    PermissionRule,
)


class PermissionEngine:
    """按显式规则求值，不持有 Agent 或 ToolService 状态。"""

    def evaluate(
        self,
        request: PermissionRequest,
        rules: list[PermissionRule],
        default_behavior: PermissionBehavior = PermissionBehavior.ASK,
    ) -> PermissionDecision:
        def matches(rule: PermissionRule) -> bool:
            if not rule.enabled or rule.tool_name not in ("*", request.tool_name):
                return False
            for name, pattern in rule.argument_regex.items():
                if name not in request.arguments:
                    return False
                try:
                    if re.fullmatch(pattern, str(request.arguments[name])) is None:
                        return False
                except re.error:
                    return False
            return True

        matched = [rule for rule in rules if matches(rule)]
        if not matched:
            return PermissionDecision(
                behavior=default_behavior,
                reason="No permission rule matched",
            )
        highest = max(rule.priority for rule in matched)
        candidates = [rule for rule in matched if rule.priority == highest]
        behaviors = {rule.behavior for rule in candidates}
        if len(behaviors) > 1:
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                reason="Conflicting permission rules at the same priority",
            )
        rule = candidates[0]
        return PermissionDecision(
            behavior=rule.behavior,
            reason=f"Matched permission rule: {rule.id}",
            rule_id=rule.id,
        )


__all__ = ["PermissionEngine"]
