from __future__ import annotations

import json

import pytest

from ftre.services.session.persistence.chunk_rows import (
    decode_storage_record,
    pack_chunk_runs,
)
from ftre.services.session.persistence.jsonl import (
    WriteBehindCoordinator,
    read_event_log,
    write_event_log_atomic,
)


def _chunk(seq: int, text: str, *, message_id: str = "m1", kind: str = "text") -> dict:
    return {
        "type": "assistant/chunk",
        "seq": seq,
        "time": 1000 + seq * 10,
        "message_id": message_id,
        "data": {
            "kind": kind,
            "delta": text,
            "block_id": "b1",
            "tool_call_id": None,
        },
    }


def test_pack_chunk_runs_is_lossless_and_reduces_rows():
    events = [_chunk(i, str(i)) for i in range(1000)]
    records = pack_chunk_runs(events)

    assert len(records) == 1
    assert decode_storage_record(records[0]) == events


def test_pack_does_not_cross_message_or_block_boundary():
    first = [_chunk(i, str(i)) for i in range(3)]
    second = [_chunk(i + 3, str(i), message_id="m2") for i in range(3)]

    records = pack_chunk_runs([*first, *second])

    assert len(records) == 2
    assert decode_storage_record(records[0]) == first
    assert decode_storage_record(records[1]) == second


def test_pack_preserves_short_runs_and_unknown_shapes():
    short = [_chunk(i, str(i)) for i in range(2)]
    unknown = {
        "type": "assistant/chunk",
        "seq": 2,
        "time": 1020,
        "message_id": "m1",
        "data": {"kind": "future", "delta": "x"},
    }
    records = pack_chunk_runs([*short, unknown])

    assert records == [*short, unknown]


def test_read_event_log_decodes_packed_rows(tmp_path):
    events = [_chunk(i, str(i)) for i in range(8)]
    session_dir = tmp_path / "ws_sess_chunks"
    write_event_log_atomic(session_dir, events)

    physical_lines = (session_dir / "session.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(physical_lines) == 2
    assert json.loads(physical_lines[1])["type"] == "text-chunks"
    assert read_event_log(session_dir) == events


@pytest.mark.asyncio
async def test_write_behind_packs_typed_chunks(tmp_path):
    session_dir = tmp_path / "ws_sess_chunks"
    writer = WriteBehindCoordinator(lambda _sid: session_dir)
    from ftre_agent.session import SessionLog

    log = SessionLog("ws_sess_chunks")
    writer.attach("ws_sess_chunks", log)
    for i in range(8):
        log.append(
            "assistant/chunk",
            {
                "kind": "text",
                "delta": str(i),
                "block_id": "b1",
                "tool_call_id": None,
            },
            message_id="m1",
        )

    await writer.flush("ws_sess_chunks")
    await writer.close()

    physical_lines = (session_dir / "session.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(physical_lines) == 2
    assert read_event_log(session_dir) == list(log.events)


def test_malformed_chunk_row_fails_loud(tmp_path):
    session_dir = tmp_path / "ws_sess_chunks"
    session_dir.mkdir()
    (session_dir / "session.jsonl").write_text(
        json.dumps({"v": 1, "format": "ftre-session-log"})
        + "\n"
        + json.dumps({"type": "text-chunks", "seq0": 0})
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="chunk storage row 损坏"):
        read_event_log(session_dir)
