"""Host ToolService：注册、作用域投影和受控执行的唯一 Owner。

Agent 公共包只定义 ToolDefinition 与 ToolView；本模块持有 contribution、权限
配置、Injected 解析和 callable 调度。Runtime 只能拿到一次性的 ToolView。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping
from typing import Any

from ftre_agent.hooks import (
    TOOL_AFTER_SPEC,
    TOOL_BEFORE_SPEC,
    ToolAfterPayload,
    ToolArguments,
    ToolBeforePayload,
    ToolCallIdentity,
    ToolDeny,
)
from ftre_agent.hooks import (
    ToolExecutionResult as HookToolExecutionResult,
)
from ftre_agent.tool import (
    Injected,
    ToolContext,
    ToolDefinition,
    ToolExecutionResult,
    ToolSchema,
)
from ftre_agent.tool.permission import PermissionRequest

from .approval import ApprovalOutcome, ApprovalRequest
from .filtering import coerce_tool_name_list
from .scope import ToolRestriction
from .types import ToolContribution


class _ToolView:
    """一次 Agent Run 的不可变可见工具快照。"""

    def __init__(self, contributions: tuple[ToolContribution, ...], service: ToolService):
        self._contributions = contributions
        self._service = service

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.definition.name for item in self._contributions)

    def __len__(self) -> int:
        return len(self._contributions)

    def schemas(self) -> tuple[ToolSchema, ...]:
        return tuple(
            ToolSchema(
                name=item.definition.name,
                description=item.definition.description,
                parameters=item.definition.to_openai_dict()["function"]["parameters"],
                extra={"owner": item.owner, "source": item.source, "scope": item.scope},
            )
            for item in self._contributions
        )

    def get(self, name: str) -> ToolDefinition | None:
        item = next((item for item in self._contributions if item.definition.name == name), None)
        return item.definition if item is not None else None

    def to_openai_tools(self) -> list[dict[str, Any]]:
        return [item.definition.to_openai_dict() for item in self._contributions]

    def execute(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        context: ToolContext | None = None,
        **kwargs: Any,
    ):
        """调用 ToolView。

        Runtime 传入 ``arguments`` + ``context`` 并 await 返回协程；Host 负责
        权限、审批、注入、执行和结果归一化。
        """
        if context is not None:
            async def run() -> ToolExecutionResult:
                item = next(
                    (item for item in self._contributions if item.definition.name == name),
                    None,
                )
                if item is None:
                    return ToolExecutionResult(
                        status="failed", error=f"tool {name!r} is not visible"
                    )
                return await self._service._execute_contribution(
                    item, arguments or {}, context
                )

            return run()
        runtime_context = kwargs.pop("runtime_context", {})
        values = dict(arguments or {})
        values.update(kwargs)
        return self._service.execute(name, runtime_context, values)

    def _resolve_injections(
        self,
        name: str,
        arguments: Mapping[str, Any],
        runtime_context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        item = next((item for item in self._contributions if item.definition.name == name), None)
        if item is None:
            return dict(arguments)
        return self._service._resolve_injections(
            item.definition, arguments, runtime_context or {}
        )

class ToolService:
    """拥有工具注册、scoped view 和受控执行。"""

    key = "tools"

    def __init__(
        self,
        *,
        hook_runtime: Any | None = None,
        permission_engine: Any | None = None,
        approval_service: Any | None = None,
    ) -> None:
        self._items: list[ToolContribution] = []
        self._restrictions: list[ToolRestriction] = []
        self._view_preparers: list[tuple[str, Callable[..., Any]]] = []
        self._hook_runtime = hook_runtime
        self._permission_engine = permission_engine
        self._approval_service = approval_service

    def register(
        self,
        definition: ToolDefinition,
        owner: str,
        scope: str = "global",
        source: str = "builtin",
    ) -> Callable[[], bool]:
        if not isinstance(definition, ToolDefinition):
            raise TypeError("ToolService.register expects ToolDefinition")
        name = definition.name
        if any(item.definition.name == name and item.scope == scope for item in self._items):
            raise ValueError(f"tool {name!r} already registered in {scope}")
        contribution = ToolContribution(name, owner, source, scope, definition)
        self._items.append(contribution)
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            try:
                self._items.remove(contribution)
            except ValueError:
                return False
            return True

        return dispose

    def restrict(
        self,
        agent_id: str,
        owner: str,
        allow: set[str] | None = None,
        deny: set[str] | None = None,
    ) -> Callable[[], bool]:
        restriction = ToolRestriction(
            agent_id,
            owner,
            frozenset(allow or ()),
            frozenset(deny or ()),
        )
        self._restrictions.append(restriction)
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            try:
                self._restrictions.remove(restriction)
            except ValueError:
                return False
            return True

        return dispose

    def register_view_preparer(
        self, preparer: Callable[..., Any], *, owner: str
    ) -> Callable[[], bool]:
        if not callable(preparer):
            raise TypeError("preparer must be callable")
        entry = (owner, preparer)
        self._view_preparers.append(entry)
        disposed = False

        def dispose() -> bool:
            nonlocal disposed
            if disposed:
                return False
            disposed = True
            try:
                self._view_preparers.remove(entry)
            except ValueError:
                return False
            return True

        return dispose

    def snapshot(self, agent_id: str | None = None) -> tuple[ToolContribution, ...]:
        return tuple(self._visible(agent_id))

    def get(self, name: str, agent_id: str | None = None) -> ToolContribution | None:
        return next((item for item in self._visible(agent_id) if item.name == name), None)

    def schemas(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                **item.definition.to_openai_dict(),
                "owner": item.owner,
                "source": item.source,
                "scope": item.scope,
            }
            for item in self._visible(agent_id)
        ]

    async def prepare_view(
        self,
        agent_id: str,
        session_id: str,
        profile_config: Any | None = None,
        *,
        llm_config: Any | None = None,
    ) -> _ToolView:
        for _owner, preparer in tuple(self._view_preparers):
            result = preparer(agent_id, session_id, profile_config, llm_config)
            if inspect.isawaitable(result):
                await result
        visible = list(self._visible(agent_id))
        tools_config = _profile_value(profile_config, "tools_config")
        if tools_config:
            allow = set(coerce_tool_name_list(tools_config.get("allow"), "allow"))
            deny = set(coerce_tool_name_list(tools_config.get("deny"), "deny"))
            visible = [
                item for item in visible
                if item.name not in deny and (not allow or item.name in allow)
            ]
        return _ToolView(tuple(visible), self)

    def execute(
        self,
        name: str,
        execution_context: Mapping[str, Any] | None = None,
        arguments: Mapping[str, Any] | None = None,
        *,
        agent_id: str | None = None,
    ) -> Any:
        item = self.get(name, agent_id)
        if item is None:
            if agent_id is None:
                raise ValueError(f"Tool {name!r} not found")
            raise KeyError(f"tool {name!r} is not visible to agent {agent_id!r}")
        values = self._resolve_injections(item.definition, arguments or {}, execution_context or {})
        result = item.definition.execute(values)
        if inspect.isawaitable(result):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(result)
            raise RuntimeError("async ToolDefinition requires await ToolView.execute()")
        return result

    async def _execute_contribution(
        self,
        item: ToolContribution,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolExecutionResult:
        call = ToolCallIdentity(
            call_id=context.call_id,
            name=item.definition.name,
            session_id=str(context.metadata.get("session_id", "")),
            turn_id=str(context.metadata.get("turn_id", "")),
            agent_id=str(context.metadata.get("agent_id", "")),
            iteration=int(context.metadata.get("iteration", 0) or 0),
        )
        pre = await self._dispatch_hook(
            TOOL_BEFORE_SPEC,
            ToolBeforePayload(call, arguments, context.cancellation or asyncio.Event()),
            context,
        )
        if isinstance(pre, ToolDeny):
            return ToolExecutionResult(status="failed", error=pre.reason or "Tool denied")
        if isinstance(pre, ToolArguments):
            arguments = dict(pre.arguments)
        permission_result = self._permission_decision(item, arguments, context)
        if permission_result is not None:
            behavior = getattr(permission_result.behavior, "value", permission_result.behavior)
            if behavior == "deny":
                return ToolExecutionResult(status="failed", error=permission_result.reason)
            if behavior == "ask":
                if self._approval_service is None:
                    return ToolExecutionResult(
                        status="failed",
                        error="approval unavailable",
                        metadata={
                            "approval_required": True,
                            "reason": permission_result.reason,
                            "rule_id": permission_result.rule_id,
                        },
                    )
                approval = await self._approval_service.request(
                    ApprovalRequest(
                        call_id=context.call_id,
                        tool_name=item.definition.name,
                        arguments=dict(arguments),
                        metadata=dict(context.metadata),
                    )
                )
                if approval.outcome is not ApprovalOutcome.ALLOWED:
                    return ToolExecutionResult(
                        status="failed",
                        error=approval.reason or approval.outcome.value,
                        metadata={"approval_required": True, "reason": approval.reason},
                    )
        try:
            values = self._resolve_injections(item.definition, arguments, context.metadata)
            if context.cancellation is not None and context.cancellation.is_set():
                result = ToolExecutionResult(status="cancelled", error="cancelled")
                return await self._dispatch_after(call, arguments, result, context)
            result = item.definition.execute(values)
            if inspect.isawaitable(result):
                result = await result
            normalized = _normalize_result(result)
        except asyncio.CancelledError:
            normalized = ToolExecutionResult(status="cancelled", error="cancelled")
        except Exception as exc:  # noqa: BLE001 - tool failure is a result boundary
            normalized = ToolExecutionResult(status="failed", error=str(exc))
        return await self._dispatch_after(call, arguments, normalized, context)

    async def _dispatch_hook(self, spec, payload, context: ToolContext):
        if self._hook_runtime is None:
            result = spec.default(payload) if spec.default is not None else None
            return await result if inspect.isawaitable(result) else result
        return await self._hook_runtime.dispatch(spec, payload, context=context.metadata.get("hook_context"))

    def _permission_decision(self, item, arguments, context: ToolContext):
        if self._permission_engine is None:
            return None
        permission_context = context.metadata.get("permission_context")
        if permission_context is None:
            return None
        return self._permission_engine.evaluate(
            request=PermissionRequest(tool_name=item.definition.name, arguments=dict(arguments)),
            rules=list(getattr(permission_context, "permission_rules", ())),
            default_behavior=getattr(permission_context, "default_behavior", "allow"),
        )

    async def _dispatch_after(
        self,
        call: ToolCallIdentity,
        arguments: Mapping[str, Any],
        result: ToolExecutionResult,
        context: ToolContext,
    ) -> ToolExecutionResult:
        post = await self._dispatch_hook(
            TOOL_AFTER_SPEC,
            ToolAfterPayload(
                call,
                arguments,
                result,
                context.cancellation or asyncio.Event(),
            ),
            context,
        )
        if isinstance(post, ToolExecutionResult):
            return post
        if isinstance(post, HookToolExecutionResult):
            return ToolExecutionResult(
                output=post.output,
                status=post.status,
                error=post.error,
                metadata=dict(post.metadata),
                value=post.value,
            )
        return result

    @staticmethod
    def _resolve_injections(
        definition: ToolDefinition,
        arguments: Mapping[str, Any],
        runtime_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        values = dict(arguments)
        fn = definition.execute_callable
        if fn is None:
            return values
        try:
            for name, parameter in inspect.signature(fn).parameters.items():
                if isinstance(parameter.default, Injected) and name not in values:
                    values[name] = runtime_context.get(parameter.default.key)
        except (TypeError, ValueError):
            pass
        return values

    def _visible(self, agent_id: str | None) -> list[ToolContribution]:
        candidates = [
            item for item in self._items
            if item.scope == "global" or item.scope == f"agent:{agent_id}"
        ]
        by_name: dict[str, ToolContribution] = {}
        for item in candidates:
            by_name[item.name] = item
        items = list(by_name.values())
        if agent_id is None:
            return [item for item in items if item.scope == "global"]
        for restriction in reversed(self._restrictions):
            if restriction.agent_id != agent_id:
                continue
            if restriction.allow:
                items = [item for item in items if item.name in restriction.allow]
            items = [item for item in items if item.name not in restriction.deny]
        return items


def _normalize_result(value: Any) -> ToolExecutionResult:
    if isinstance(value, ToolExecutionResult):
        return value
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], Mapping):
        return ToolExecutionResult(output=str(value[0]), metadata=dict(value[1]), value=value)
    return ToolExecutionResult(output=value if isinstance(value, str) else str(value), value=value)


def _profile_value(profile_config: Any, field: str) -> Any:
    if profile_config is None:
        return None
    if isinstance(profile_config, Mapping):
        return profile_config.get(field)
    return getattr(profile_config, field, None)
