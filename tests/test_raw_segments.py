from __future__ import annotations

import stat
from concurrent.futures import ThreadPoolExecutor

import orjson
import pytest

from reflect.raw_segments import (
    RawSegmentWriter,
    RawStorageGuard,
    RawStorageLimitExceeded,
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


def test_storage_guard_rejects_projected_write_without_deleting_raw(tmp_path):
    traces = tmp_path / "otel-traces.active.jsonl"
    logs = tmp_path / "otel-logs.active.jsonl"
    traces.write_bytes(b"1234")
    logs.write_bytes(b"56")
    guard = RawStorageGuard((traces, logs), max_bytes=8)

    with guard.admit(2):
        logs.write_bytes(b"5678")

    assert guard.status().raw_bytes == 8
    assert guard.status().accepting_telemetry is False
    with pytest.raises(RawStorageLimitExceeded), guard.admit(1):
        raise AssertionError("rejected admission must not yield")
    assert traces.read_bytes() == b"1234"
    assert logs.read_bytes() == b"5678"

    logs.unlink()
    assert guard.status().accepting_telemetry is True


def test_storage_guard_serializes_combined_trace_and_log_capacity(tmp_path):
    traces = tmp_path / "otel-traces.active.jsonl"
    logs = tmp_path / "otel-logs.active.jsonl"
    trace_writer = RawSegmentWriter(traces, max_bytes=1024)
    log_writer = RawSegmentWriter(logs, max_bytes=1024)
    encoded = b'{"event":1}\n'
    guard = RawStorageGuard((traces, logs), max_bytes=10 * len(encoded))

    def append(sequence: int) -> bool:
        writer = trace_writer if sequence % 2 else log_writer
        try:
            with guard.admit(len(encoded)):
                writer.append_encoded(encoded)
        except RawStorageLimitExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        accepted = list(executor.map(append, range(40)))

    assert sum(accepted) == 10
    assert traces.stat().st_size + logs.stat().st_size == 10 * len(encoded)
