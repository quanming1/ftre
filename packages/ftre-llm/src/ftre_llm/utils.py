"""
LLM 模块工具函数
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class _ThreadPool:
    """LLM 原始日志的本地后台写入器，不依赖全局线程池。"""

    def __init__(self) -> None:
        self.io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ftre-llm-log")


thread_pool = _ThreadPool()

_LOG_DIR = Path("data/logs/llm")
_logger = logging.getLogger("ftre_llm.raw")


def _ensure_log_dir() -> Path:
    """确保日志目录存在，返回当天目录"""
    now = datetime.now(UTC)
    day_dir = _LOG_DIR / now.strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir


class LLMLogger:
    """
    记录单次 LLM 调用的输入和原始输出到 .log 文件。

    用法：
        logger = LLMLogger(model)
        logger.log_input(messages, tools)
        for chunk in response:
            logger.log_chunk(chunk)
        logger.flush()
    """

    def __init__(self, model: str):
        self.model = model
        self._lines: list[str] = []
        self._id = uuid4().hex[:8]
        self._start_time = datetime.now(UTC)

    def log_input(self, messages: list[dict], tools: list[dict] | None) -> None:
        """记录请求输入"""
        self._lines.append(f"=== LLM CALL {self._start_time.isoformat()} ===")
        self._lines.append(f"Model: {self.model}")
        self._lines.append(f"Messages: {json.dumps(messages, ensure_ascii=False, default=str)}")
        if tools:
            self._lines.append(f"Tools: {json.dumps(tools, ensure_ascii=False, default=str)}")
        self._lines.append("--- STREAM OUTPUT ---")

    def log_chunk(self, chunk) -> None:
        """记录一个原始 chunk"""
        try:
            if hasattr(chunk, "model_dump"):
                data = chunk.model_dump()
            elif hasattr(chunk, "__dict__"):
                data = str(chunk)
            else:
                data = str(chunk)

            if isinstance(data, dict):
                self._lines.append(json.dumps(data, ensure_ascii=False, default=str))
            else:
                self._lines.append(str(data))
        except Exception:  # noqa: BLE001 - logging must not affect streaming
            self._lines.append(f"[LOG_ERROR] {type(chunk)}")

    def flush(self) -> None:
        """异步写入文件"""
        self._lines.append("=== END ===\n")
        content = "\n".join(self._lines)
        thread_pool.io.submit(self._write, content)

    def _write(self, content: str) -> None:
        try:
            day_dir = _ensure_log_dir()
            filename = f"{self._start_time.strftime('%H%M%S_%f')}_{self._id}.log"
            filepath = day_dir / filename
            filepath.write_text(content, encoding="utf-8")
        except Exception as e:  # noqa: BLE001 - logging must not affect streaming
            _logger.debug(f"写入 LLM 日志失败: {e}")

