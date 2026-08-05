"""Session 包：Entity / Storage / Message 分层 + SessionManager 业务门面。

对外只暴露 SessionManager 门面，保持历史 import 路径不变。
"""
from .manager import SessionManager

__all__ = ["SessionManager"]
