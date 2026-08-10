from __future__ import annotations

import stat
from concurrent.futures import ThreadPoolExecutor

import orjson

from reflect.raw_segments import (
    RawSegmentWriter,
    closed_segment_paths,
    readable_segment_paths,
    segment_inventory,
    segment_prefix,
)


def test_segment_paths_and_inventory_are_deterministic(tmp_path):
    active = tmp_path / "otel-traces.active.jsonl"
    older = tmp_path / "otel-traces.00000000000000000001.jsonl"
    newer = tmp_path / "otel-traces.00000000000000000002.jsonl"
    unrelated = tmp_path / "otel-logs.00000000000000000001.jsonl"
    older.write_bytes(b"old\n")
    newer.write_bytes(b"newer\n")
    unrelated.write_bytes(b"ignored\n")
    active.write_bytes(b"active\n")

    assert segment_prefix(active) == "otel-traces"
    assert closed_segment_paths(active) == (older, newer)
    assert readable_segment_paths(active) == (older, newer, active)

    inventory = segment_inventory(active)
    assert inventory.active_exists is True
    assert inventory.active_bytes == len(b"active\n")
    assert inventory.closed_count == 2
    assert inventory.closed_bytes == len(b"old\nnewer\n")
    assert inventory.total_bytes == len(b"old\nnewer\nactive\n")


def test_writer_rotates_before_crossing_the_segment_bound(tmp_path):
    active = tmp_path / "otel-traces.active.jsonl"
    first = {"sequence": 1}
    second = {"sequence": 2}
    first_raw = orjson.dumps(first) + b"\n"
    second_raw = orjson.dumps(second) + b"\n"
    writer = RawSegmentWriter(active, max_bytes=len(first_raw) + len(second_raw) - 1)

    writer.append(first)
    writer.append(second)

    closed = closed_segment_paths(active)
    assert len(closed) == 1
    assert closed[0].read_bytes() == first_raw
    assert active.read_bytes() == second_raw
    assert stat.S_IMODE(active.stat().st_mode) == 0o600


def test_writer_serializes_concurrent_thread_appends(tmp_path):
    active = tmp_path / "otel-logs.active.jsonl"
    writer = RawSegmentWriter(active, max_bytes=1024 * 1024)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda sequence: writer.append({"sequence": sequence}), range(100)))

    records = [orjson.loads(line) for line in active.read_bytes().splitlines()]
    assert len(records) == 100
    assert {record["sequence"] for record in records} == set(range(100))
