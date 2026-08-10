from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import orjson

DEFAULT_SEGMENT_BYTES = 256 * 1024 * 1024
DEFAULT_RAW_STORAGE_BYTES = 4 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class RawSegmentInventory:
    active_exists: bool
    active_bytes: int
    closed_count: int
    closed_bytes: int

    @property
    def total_bytes(self) -> int:
        return self.active_bytes + self.closed_bytes


@dataclass(frozen=True)
class RawStorageStatus:
    raw_bytes: int
    raw_limit_bytes: int

    @property
    def accepting_telemetry(self) -> bool:
        return self.raw_bytes < self.raw_limit_bytes

    @property
    def usage_ratio(self) -> float:
        return self.raw_bytes / self.raw_limit_bytes if self.raw_limit_bytes else 1.0


class RawStorageLimitExceeded(RuntimeError):
    def __init__(self, status: RawStorageStatus, *, requested_bytes: int) -> None:
        super().__init__(
            "Reflect raw OTLP storage rejected a "
            f"{requested_bytes:,}-byte telemetry batch because it would exceed "
            f"the {status.raw_limit_bytes:,}-byte limit "
            f"({status.raw_bytes:,} bytes currently stored). Run `reflect refresh`."
        )
        self.status = status
        self.requested_bytes = requested_bytes


class RawStorageGuard:
    """Serialize admission and writes across all managed raw OTLP signals."""

    def __init__(
        self,
        active_paths: tuple[Path, ...],
        *,
        max_bytes: int = DEFAULT_RAW_STORAGE_BYTES,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("raw storage max_bytes must be positive")
        resolved = tuple(dict.fromkeys(path.expanduser().resolve() for path in active_paths))
        if not resolved:
            raise ValueError("raw storage requires at least one active path")
        self.active_paths = resolved
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def status(self) -> RawStorageStatus:
        with self._lock:
            return self._status_unlocked()

    @contextmanager
    def admit(self, requested_bytes: int) -> Iterator[RawStorageStatus]:
        if requested_bytes < 0:
            raise ValueError("requested_bytes must not be negative")
        with self._lock:
            status = self._status_unlocked()
            if status.raw_bytes + requested_bytes > status.raw_limit_bytes:
                raise RawStorageLimitExceeded(status, requested_bytes=requested_bytes)
            yield status

    def _status_unlocked(self) -> RawStorageStatus:
        return RawStorageStatus(
            raw_bytes=sum(segment_inventory(path).total_bytes for path in self.active_paths),
            raw_limit_bytes=self.max_bytes,
        )


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
    try:
        active_bytes = active_path.stat().st_size
    except FileNotFoundError:
        active_bytes = 0

    def existing_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    return RawSegmentInventory(
        active_exists=active_bytes > 0 or active_path.is_file(),
        active_bytes=active_bytes,
        closed_count=len(closed),
        closed_bytes=sum(existing_size(path) for path in closed),
    )


class RawSegmentWriter:
    """Append and rotate OTLP JSONL for one owning gateway process."""

    def __init__(self, active_path: Path, *, max_bytes: int = DEFAULT_SEGMENT_BYTES):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.active_path = active_path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def append(self, payload: dict) -> None:
        self.append_encoded(orjson.dumps(payload) + b"\n")

    def append_encoded(self, raw: bytes) -> None:
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
    "DEFAULT_RAW_STORAGE_BYTES",
    "DEFAULT_SEGMENT_BYTES",
    "RawSegmentInventory",
    "RawSegmentWriter",
    "RawStorageGuard",
    "RawStorageLimitExceeded",
    "RawStorageStatus",
    "closed_segment_paths",
    "readable_segment_paths",
    "segment_inventory",
    "segment_prefix",
]
