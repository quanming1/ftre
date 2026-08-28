"""跨 Package 的 Tool 声明。

声明可以携带一个执行契约，方便 Provider 以 DSH 风格构造工具；它不持有
Registry、scope、权限状态或 Host Service。实际调用仍必须经过 ToolView。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, get_type_hints


class Injected:
    """标记由 ToolService 从 ToolContext 注入的参数。"""

    def __init__(self, key: str):
        if not key or not key.strip():
            raise ValueError("Injected.key must be non-empty")
        self.key = key

    def __repr__(self) -> str:
        return f"Injected({self.key!r})"


@dataclass(frozen=True, slots=True)
class ToolParameter:
    """单个工具参数的 JSON Schema 片段。"""

    name: str
    type: str
    description: str = ""
    required: bool = True
    enum: list[Any] | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("ToolParameter.name must be non-empty")
        if self.type not in {"string", "number", "integer", "boolean", "array", "object"}:
            raise ValueError(f"unsupported ToolParameter.type: {self.type}")
        object.__setattr__(self, "enum", list(self.enum or ()))


class ToolDefinition:
    """工具名称、Schema 与执行契约。

    ``execute`` 是 Provider 提供的 callable 契约，不是 Registry 或 Service。
    ToolService 注册时会把它封装为 Host 内部 contribution，并负责注入、权限、
    审批、超时、取消和结果归一化。
    """

    parameters: ClassVar[tuple[ToolParameter, ...]] = ()

    def __init__(
        self,
        name: str,
        description: str = "",
        parameters: list[ToolParameter] | tuple[ToolParameter, ...] = (),
        execute: Callable[..., Any] | None = None,
        *,
        func: Callable[..., Any] | None = None,
        injected: tuple[str, ...] = (),
        timeout_ms: int | None = None,
        is_concurrency_safe: bool = True,
    ) -> None:
        if not name.strip():
            raise ValueError("ToolDefinition.name must be non-empty")
        if execute is not None and func is not None:
            raise ValueError("provide execute or func, not both")
        callable_value = execute or func
        self.name = name
        self.description = description
        self.parameters = tuple(parameters)
        self.execute_callable = callable_value
        self.injected = tuple(injected)
        self.timeout_ms = timeout_ms
        self.is_concurrency_safe = is_concurrency_safe

    def to_openai_dict(self) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for parameter in self.parameters:
            prop: dict[str, Any] = {
                "type": parameter.type,
                "description": parameter.description,
            }
            if parameter.enum:
                prop["enum"] = list(parameter.enum)
            properties[parameter.name] = prop
            if parameter.required:
                required.append(parameter.name)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def callable(self) -> Callable[..., Any]:
        if self.execute_callable is None:
            raise NotImplementedError(f"Tool '{self.name}' has no execute callable")
        return self.execute_callable

    def _get_callable(self) -> Callable[..., Any]:
        """Return the execution callable for Host executor integration."""
        return self.callable()

    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.callable())

    def execute(self, arguments: Mapping[str, Any] | None = None, **kwargs: Any) -> Any:
        """执行契约的直接调用形式，仅供 ToolService 内部使用。"""

        values = dict(arguments or {})
        values.update(kwargs)
        return self.callable()(**values)


def tool(
    name: str | None = None,
    description: str | None = None,
    parameters: list[ToolParameter] | None = None,
):
    """把普通函数转换为 ToolDefinition。"""

    def decorator(func: Callable[..., Any]) -> ToolDefinition:
        return ToolDefinition(
            name=name or func.__name__,
            description=(description or func.__doc__ or "").strip(),
            parameters=parameters or _infer_parameters(func),
            execute=func,
        )

    return decorator


def _infer_parameters(func: Callable[..., Any]) -> list[ToolParameter]:
    params: list[ToolParameter] = []
    try:
        hints = get_type_hints(func)
    except Exception:  # noqa: BLE001 - annotation inference is best effort
        hints = {}
    for parameter_name, parameter in inspect.signature(func).parameters.items():
        if parameter_name in {"self", "cls"} or parameter.kind in {
            parameter.VAR_POSITIONAL,
            parameter.VAR_KEYWORD,
        }:
            continue
        if isinstance(parameter.default, Injected):
            continue
        params.append(
            ToolParameter(
                name=parameter_name,
                type=_python_type_to_json_type(hints.get(parameter_name)),
                description=f"参数 {parameter_name}",
                required=parameter.default is inspect.Parameter.empty,
            )
        )
    return params


def _python_type_to_json_type(python_type: Any) -> str:
    origin = getattr(python_type, "__origin__", None)
    if origin is list:
        return "array"
    if origin is dict:
        return "object"
    return {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }.get(python_type, "string")
