"""压缩事件名称与 ftre Session 事件契约的窄桥接。

事件的协议 Owner 在 ftre 的 SessionMaintenanceEvent；本包只把三个稳定名称
重新导出，避免复制字符串常量，也避免核心反向 import 可选压缩包。
"""

from ftre.services.session.events import SessionMaintenanceEvent


class CompactEventName:
    """供 Service 内部使用的可读别名，不创建第二套事件协议。"""

    START = SessionMaintenanceEvent.COMPACTION_START
    DONE = SessionMaintenanceEvent.COMPACTION_DONE
    FAILED = SessionMaintenanceEvent.COMPACTION_FAILED


__all__ = ["CompactEventName"]
