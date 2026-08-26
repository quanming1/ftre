"""验证迁入后的协议由 ftre-llm 自己持有，不反向依赖待退役 Core。"""

from ftre_llm.events import FinishChunk, TextDeltaChunk


def test_stream_event_classes_are_owned_by_ftre_llm() -> None:
    assert TextDeltaChunk.__module__ == "ftre_llm.events"
    assert FinishChunk.__module__ == "ftre_llm.events"


def test_constructed_events_keep_the_migrated_shape() -> None:
    chunk = TextDeltaChunk(index=2, text="ok")
    assert chunk.type == "text-delta"
    assert chunk.index == 2
    assert chunk.text == "ok"
