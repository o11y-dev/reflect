from __future__ import annotations

import asyncio
import multiprocessing
import threading
from concurrent.futures import Future, ProcessPoolExecutor
from pathlib import Path
from typing import Any, Literal

ReadOperation = Literal["dashboard", "explore", "tools", "graph", "session", "conversation"]


def read_revision(db_path: Path, operation: ReadOperation) -> tuple[str, ...]:
    from reflect.store.snapshots import SnapshotReadContext
    with SnapshotReadContext(db_path) as context:
        revision = context.review_state.revision if context.review_state else 0
        return (context.snapshot.generation, str(revision))


class DashboardReadBusyError(RuntimeError):
    """The bounded read queue is full; clients may retain data and retry."""


def _read_payload(operation: ReadOperation, db_path: Path, args: tuple, kwargs: dict):
    # Each worker opens its own existing read-only connections. No SQLite
    # connection, publication cache, preparation worker, or mutation is shared.

    kwargs.pop("read_revision", None)
    publication = kwargs.pop("publication")
    from reflect.store.snapshots import SnapshotReadContext
    with SnapshotReadContext(db_path, snapshot=publication):
        return _build_payload(operation, db_path, args, kwargs)


def _build_payload(operation, db_path, args, kwargs):
    from reflect.dashboard_explore import build_explore_payload
    from reflect.dashboard_queries import build_dashboard_payload
    from reflect.dashboard_sessions import build_session_payload, load_session_detail

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
                from reflect.store.snapshots import PublishedSnapshotStore
                lease = PublishedSnapshotStore(db_path).acquire()
                publication = lease.__enter__()
                kwargs["publication"] = publication
                try:
                    future = self._pool.submit(_read_payload, operation, db_path, args, kwargs)
                except BaseException:
                    lease.__exit__(None, None, None)
                    raise
                future.add_done_callback(lambda _: lease.__exit__(None, None, None))
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
