"""AgentService 的稳定错误类型。"""

from __future__ import annotations


class AgentServiceError(RuntimeError):
    """所有 AgentService 控制面错误的基类。"""

    code = "agent_service_error"


class ServiceClosedError(AgentServiceError):
    code = "agent_service_closed"


class FactoryNotRegisteredError(AgentServiceError):
    code = "agent_factory_not_registered"


class FactoryAlreadyRegisteredError(AgentServiceError):
    code = "agent_factory_already_registered"


class InvalidFactoryError(AgentServiceError):
    code = "agent_factory_invalid"


class FactoryRegistrationMismatchError(AgentServiceError):
    code = "agent_factory_registration_mismatch"


__all__ = [
    "AgentServiceError",
    "FactoryAlreadyRegisteredError",
    "FactoryNotRegisteredError",
    "FactoryRegistrationMismatchError",
    "InvalidFactoryError",
    "ServiceClosedError",
]
