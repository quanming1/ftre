"""Trace Service：Agent 执行轨迹的只读查询门面。

写入由 Agent Runtime/Trace exporter 负责，Service 不参与每个 stream 事件的采集；它
只把查询统一指向当前 exporter 的数据库路径，避免 API 直接依赖 SQLite schema。
"""

from __future__ import annotations

from .store import (
    SQLiteTraceExporter,
    get_trace,
    get_trace_run,
    list_trace_summaries,
)


class TraceService:
    """以稳定 key 隔离 trace 持久化，并只暴露查询方法。"""
    key = "traces"

    def __init__(self, store: SQLiteTraceExporter | None = None) -> None:
        self.store = store or SQLiteTraceExporter()

    def build_tracer(self):
        """Return the tracer bound to this Service's single exporter.

        Agent Runtime must not construct another SQLite exporter or import the
        persistence adapter.  The Trace Service owns both the query path and
        the write sink, so all runtime traces share one lifecycle boundary.
        """
        from ftre_agent.tracing import Tracer

        return Tracer([self.store])

    def list(self, **kwargs):
        """分页查询 trace 摘要。"""
        return list_trace_summaries(path=self.store.path, **kwargs)

    def get(self, trace_id: str):
        """读取一条 trace 的 runs。"""
        return get_trace(trace_id, path=self.store.path)

    def get_run(self, trace_id: str, run_id: str):
        """读取指定 trace run 的细节。"""
        return get_trace_run(trace_id, run_id, path=self.store.path)
