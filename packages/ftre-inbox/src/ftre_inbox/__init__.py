"""ftre 的持久消息 Inbox Plugin。

根模块只导出轻量模型，避免 ``import ftre_inbox`` 在独立 wheel 中反向加载 ftre
Gateway。运行时 Service/Plugin 通过显式子模块导入，宿主仍可使用同一个稳定包名。
"""

from .models import InboxSnapshot, QueueItem, QueueTarget
from .protocol import InboundMessage

__all__ = [
    "INBOX_BEFORE_CLAIM",
    "INBOX_BEFORE_CLAIM_SPEC",
    "INBOX_CHANGED",
    "INBOX_CHANGED_SPEC",
    "INBOX_STATUS_CHANGED",
    "INBOX_STATUS_CHANGED_SPEC",
    "BeforeClaimPayload",
    "EnterClaim",
    "InboundMessage",
    "InboxChangedPayload",
    "InboxService",
    "InboxSnapshot",
    "InboxStatusPayload",
    "QueueItem",
    "QueueTarget",
    "RejectClaim",
]


def __getattr__(name: str):
    """按需加载 Service/Hook，保持独立包的基础 import 无宿主副作用。"""
    if name == "InboxService":
        from .service import InboxService

        return InboxService
    if name in {
        "BeforeClaimPayload",
        "EnterClaim",
        "INBOX_BEFORE_CLAIM",
        "INBOX_BEFORE_CLAIM_SPEC",
        "INBOX_CHANGED",
        "INBOX_CHANGED_SPEC",
        "INBOX_STATUS_CHANGED",
        "INBOX_STATUS_CHANGED_SPEC",
        "InboxChangedPayload",
        "InboxStatusPayload",
        "RejectClaim",
    }:
        from .hooks import (
            INBOX_BEFORE_CLAIM,
            INBOX_BEFORE_CLAIM_SPEC,
            INBOX_CHANGED,
            INBOX_CHANGED_SPEC,
            INBOX_STATUS_CHANGED,
            INBOX_STATUS_CHANGED_SPEC,
            BeforeClaimPayload,
            EnterClaim,
            InboxChangedPayload,
            InboxStatusPayload,
            RejectClaim,
        )

        return {
            "BeforeClaimPayload": BeforeClaimPayload,
            "EnterClaim": EnterClaim,
            "INBOX_BEFORE_CLAIM": INBOX_BEFORE_CLAIM,
            "INBOX_BEFORE_CLAIM_SPEC": INBOX_BEFORE_CLAIM_SPEC,
            "INBOX_CHANGED": INBOX_CHANGED,
            "INBOX_CHANGED_SPEC": INBOX_CHANGED_SPEC,
            "INBOX_STATUS_CHANGED": INBOX_STATUS_CHANGED,
            "INBOX_STATUS_CHANGED_SPEC": INBOX_STATUS_CHANGED_SPEC,
            "InboxChangedPayload": InboxChangedPayload,
            "InboxStatusPayload": InboxStatusPayload,
            "RejectClaim": RejectClaim,
        }[name]
    raise AttributeError(name)
