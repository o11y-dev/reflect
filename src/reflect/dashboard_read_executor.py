from __future__ import annotations

import asyncio
import multiprocessing
import threading
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

ReadOperation = Literal["dashboard", "explore", "tools", "graph", "session", "conversation"]


def read_revision(db_path: Path, operation: ReadOperation) -> tuple[str, ...]:
    """Pin the readiness markers required by a prepared interactive read."""
    from reflect.store.sqlite import SnapshotNotReadyError, connect_sqlite_read_only

    with closing(connect_sqlite_read_only(db_path, profile="usage")) as conn:
        markers = {
            row[0]: (row[1], row[2]) for row in conn.execute(
                "SELECT key,value,updated_at FROM store_metadata "
                "WHERE key IN ('prepared_usage','prepared_snapshot')"
            )
        }
    profiles = ("prepared_usage",) if operation == "tools" else ("prepared_usage", "prepared_snapshot")
    if any(profile not in markers for profile in profiles):
        raise SnapshotNotReadyError("The publication revision is not ready.")
    return tuple(value for profile in profiles for value in markers[profile])


class DashboardReadBusyError(RuntimeError):
    """The bounded read queue is full; clients may retain data and retry."""


def _read_payload(operation: ReadOperation, db_path: Path, args: tuple, kwargs: dict):
    # Each worker opens its own existing read-only connections. No SQLite
    # connection, publication cache, preparation worker, or mutation is shared.
    from reflect.dashboard_explore import build_explore_payload
    from reflect.dashboard_queries import build_dashboard_payload
    from reflect.dashboard_sessions import build_session_payload, load_session_detail
    from reflect.store.sqlite import SnapshotNotReadyError

    expected = kwargs.pop("read_revision", None)
    if expected is not None and read_revision(db_path, operation) != expected:
        raise SnapshotNotReadyError("Publication changed before this read.")

    builders = {
        "dashboard": build_dashboard_payload,
        "explore": build_explore_payload,
        "session": build_session_payload,
        "conversation": load_session_detail,
    }
    payload = (
        build_explore_payload(db_path, operation, *args, **kwargs) if operation in {"tools", "graph"}
        else builders[operation](db_path, *args, **kwargs)
    )
    if expected is not None and read_revision(db_path, operation) != expected:
        raise SnapshotNotReadyError("Publication changed during this read.")
    if operation == "session":
        # The session builder is full-snapshot guarded, including an empty
        # result for a missing session, but predates explicit analysis metadata.
        payload.setdefault("analysis_state", "ready")
    return payload


class DashboardReadExecutor:
    """Isolate interactive read CPU from the report's background refresh GIL."""

    def __init__(self, *, max_workers: int = 2, max_outstanding: int = 4) -> None:
        if max_workers < 1 or max_outstanding < max_workers:
            raise ValueError("read capacity must cover at least one worker")
        self._max_workers = max_workers
        self._capacity = threading.BoundedSemaphore(max_outstanding)
        self._lock = threading.Lock()
        self._pool: ProcessPoolExecutor | None = None
        self._futures: set[Future] = set()
        self._closed = False

    def submit(self, operation: ReadOperation, db_path: Path, *args: Any, **kwargs: Any) -> Future:
        if operation not in {"dashboard", "explore", "tools", "graph", "session", "conversation"}:
            raise ValueError(f"Unknown dashboard read: {operation}")
        if not self._capacity.acquire(blocking=False):
            raise DashboardReadBusyError("Dashboard reads are busy. Retain current data and retry.")
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Dashboard read executor is closed")
                if self._pool is None:
                    self._pool = ProcessPoolExecutor(
                        max_workers=self._max_workers,
                        mp_context=multiprocessing.get_context("spawn"),
                    )
                future = self._pool.submit(_read_payload, operation, db_path, args, kwargs)
                self._futures.add(future)
        except BaseException:
            self._capacity.release()
            raise
        future.add_done_callback(self._finished)
        return future

    async def read(self, operation: ReadOperation, db_path: Path, *args: Any, **kwargs: Any):
        future = self.submit(operation, db_path, *args, **kwargs)
        try:
            return await asyncio.wrap_future(future)
        finally:
            # Cancellation can stop a queued read. An already running read keeps
            # its pinned snapshot, finishes safely, and releases its queue slot.
            future.cancel()

    def _finished(self, future: Future) -> None:
        with self._lock:
            self._futures.discard(future)
        self._capacity.release()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pool, self._pool = self._pool, None
            futures = tuple(self._futures)
        for future in futures:
            future.cancel()
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
