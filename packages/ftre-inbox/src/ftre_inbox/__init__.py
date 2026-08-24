"""ftre 的可选持久消息 Inbox。

根模块只导出轻量模型，避免 ``import ftre_inbox`` 在独立 wheel 中反向加载 ftre
Gateway。运行时 Service/Plugin 通过显式子模块导入，宿主仍可使用同一个稳定包名。
"""

from .models import InboxSnapshot, QueueItem, QueueTarget
from .protocol import InboundMessage

__all__ = [
    "INBOX_BEFORE_CLAIM",
    "INBOX_BEFORE_CLAIM_SPEC",
    "INBOX_CLAIMED",
    "INBOX_CLAIMED_SPEC",
    "INBOX_DISCARDED",
    "INBOX_DISCARDED_SPEC",
    "INBOX_INSERTED",
    "INBOX_INSERTED_SPEC",
    "BeforeClaimPayload",
    "EnterClaim",
    "InboundMessage",
    "InboxMutationPayload",
    "InboxService",
    "InboxSnapshot",
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
        "InboxMutationPayload",
        "INBOX_CLAIMED",
        "INBOX_CLAIMED_SPEC",
        "INBOX_BEFORE_CLAIM",
        "INBOX_BEFORE_CLAIM_SPEC",
        "INBOX_DISCARDED",
        "INBOX_DISCARDED_SPEC",
        "INBOX_INSERTED",
        "INBOX_INSERTED_SPEC",
        "RejectClaim",
    }:
        from .hooks import (
            INBOX_BEFORE_CLAIM,
            INBOX_BEFORE_CLAIM_SPEC,
            INBOX_CLAIMED,
            INBOX_CLAIMED_SPEC,
            INBOX_DISCARDED,
            INBOX_DISCARDED_SPEC,
            INBOX_INSERTED,
            INBOX_INSERTED_SPEC,
            BeforeClaimPayload,
            EnterClaim,
            InboxMutationPayload,
            RejectClaim,
        )

        return {
            "BeforeClaimPayload": BeforeClaimPayload,
            "EnterClaim": EnterClaim,
            "InboxMutationPayload": InboxMutationPayload,
            "INBOX_CLAIMED": INBOX_CLAIMED,
            "INBOX_CLAIMED_SPEC": INBOX_CLAIMED_SPEC,
            "INBOX_BEFORE_CLAIM": INBOX_BEFORE_CLAIM,
            "INBOX_BEFORE_CLAIM_SPEC": INBOX_BEFORE_CLAIM_SPEC,
            "INBOX_DISCARDED": INBOX_DISCARDED,
            "INBOX_DISCARDED_SPEC": INBOX_DISCARDED_SPEC,
            "INBOX_INSERTED": INBOX_INSERTED,
            "INBOX_INSERTED_SPEC": INBOX_INSERTED_SPEC,
            "RejectClaim": RejectClaim,
        }[name]
    raise AttributeError(name)
