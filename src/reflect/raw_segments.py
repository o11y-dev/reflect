from __future__ import annotations

import fcntl
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import orjson

DEFAULT_SEGMENT_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class RawSegmentInventory:
    active_exists: bool
    active_bytes: int
    closed_count: int
    closed_bytes: int

    @property
    def total_bytes(self) -> int:
        return self.active_bytes + self.closed_bytes


def segment_prefix(active_path: Path) -> str:
    name = active_path.name
    if name.endswith(".active.jsonl"):
        return name.removesuffix(".active.jsonl")
    return active_path.stem


def closed_segment_paths(active_path: Path) -> tuple[Path, ...]:
    """Return immutable gateway segments in deterministic oldest-first order."""

    prefix = segment_prefix(active_path)
    return tuple(
        path
        for path in sorted(active_path.parent.glob(f"{prefix}.*.jsonl"))
        if path != active_path and path.is_file()
    )


def readable_segment_paths(active_path: Path) -> tuple[Path, ...]:
    closed = closed_segment_paths(active_path)
    return (*closed, active_path) if active_path.is_file() else closed


def segment_inventory(active_path: Path) -> RawSegmentInventory:
    closed = closed_segment_paths(active_path)
    return RawSegmentInventory(
        active_exists=active_path.is_file(),
        active_bytes=active_path.stat().st_size if active_path.is_file() else 0,
        closed_count=len(closed),
        closed_bytes=sum(path.stat().st_size for path in closed),
    )


class RawSegmentWriter:
    """Append OTLP envelopes and atomically close bounded JSONL segments."""

    def __init__(self, active_path: Path, *, max_bytes: int = DEFAULT_SEGMENT_BYTES):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.active_path = active_path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def append(self, payload: dict) -> None:
        raw = orjson.dumps(payload) + b"\n"
        self.active_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if (
                self.active_path.is_file()
                and self.active_path.stat().st_size
                and self.active_path.stat().st_size + len(raw) > self.max_bytes
            ):
                os.replace(self.active_path, self._next_closed_path())
            fd = os.open(
                str(self.active_path),
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                os.write(fd, raw)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _next_closed_path(self) -> Path:
        prefix = segment_prefix(self.active_path)
        candidate = self.active_path.with_name(f"{prefix}.{time.time_ns():020d}.jsonl")
        while candidate.exists():
            candidate = self.active_path.with_name(
                f"{prefix}.{time.time_ns():020d}.jsonl"
            )
        return candidate


__all__ = [
    "DEFAULT_SEGMENT_BYTES",
    "RawSegmentInventory",
    "RawSegmentWriter",
    "closed_segment_paths",
    "readable_segment_paths",
    "segment_inventory",
    "segment_prefix",
]
