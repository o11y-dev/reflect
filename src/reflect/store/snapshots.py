"""Immutable, leased publications of a completed working SQLite database.

Publishing requires the caller's PreparationLock. Readers take only the short
publication lock, never the preparation lock. This module does not redirect
working-store connections: callers must explicitly acquire a read generation.
"""
from __future__ import annotations

import ctypes
import fcntl
import os
import shutil
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from reflect.store.sqlite import SQLiteBackupService, connect_sqlite_read_only
from reflect.utils import _json_dumps, _json_loads


class SnapshotCapacityError(RuntimeError):
    """Publication must wait while existing data remains available."""


@dataclass(frozen=True)
class PublishedSnapshot:
    generation: str
    path: Path
    completed_at: str
    source_revision: str


class PublishedSnapshotStore:
    """Own publication admission, atomic visibility and cross-process leases."""

    def __init__(self, db_path: Path, *, reserve_bytes: int = 2 * 1024**3):
        self.db_path = Path(db_path).expanduser().resolve()
        self.directory = self.db_path.with_name(self.db_path.name + '.snapshots')
        self.manifest = self.directory / 'current.json'
        self.reserve_bytes = reserve_bytes

    @contextmanager
    def _lock(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.directory / 'publication.lock').open('a+b') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def current(self) -> PublishedSnapshot | None:
        try:
            data = _json_loads(self.manifest.read_text())
        except FileNotFoundError:
            return None
        generation = str(data['generation'])
        if len(generation) != 32 or any(c not in '0123456789abcdef' for c in generation):
            raise ValueError('Invalid published snapshot generation')
        return PublishedSnapshot(generation, self.directory / f'{generation}.db',
                                 str(data['completed_at']), str(data['source_revision']))

    @contextmanager
    def acquire(self, snapshot: PublishedSnapshot | None = None) -> Iterator[PublishedSnapshot]:
        # Resolve and lease under the same lock used by garbage collection.
        with self._lock():
            snapshot = snapshot or self.current()
            if snapshot is not None and snapshot.path != self.directory / f'{snapshot.generation}.db':
                raise ValueError('Snapshot does not belong to this publication store')
            if snapshot is None:
                raise sqlite3.OperationalError('No completed snapshot has been published')
            handle = (self.directory / f'{snapshot.generation}.lease').open('a+b')
            try:
                fcntl.flock(handle, fcntl.LOCK_SH)
                if not snapshot.path.is_file():
                    raise sqlite3.OperationalError('Published snapshot is missing')
            except BaseException:
                handle.close()
                raise
        try:
            yield snapshot
        finally:
            handle.close()

    def _check_space(self, additional: int = 0) -> None:
        if shutil.disk_usage(self.directory).free < self.reserve_bytes + additional:
            raise SnapshotCapacityError('Snapshot publication would consume the disk reserve')

    def _remove_unleased(self, path: Path) -> bool:
        lease = path.with_suffix('.lease')
        with lease.open('a+b') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            path.unlink(missing_ok=True)
            lease.unlink(missing_ok=True)
        return True

    def _collect(self, current: PublishedSnapshot | None, *, admission: bool) -> None:
        paths = sorted(self.directory.glob('*.db'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        retained = {current.path} if current else set()
        if not admission:
            previous = next((p for p in paths if p not in retained), None)
            if previous:
                retained.add(previous)
        for path in paths:
            if path not in retained:
                self._remove_unleased(path)
        if admission and len(list(self.directory.glob('*.db'))) >= 3:
            raise SnapshotCapacityError('Older snapshot generations are still leased')

    def _clone(self, target: Path) -> bool:
        if sys.platform != 'darwin':
            return False
        # The preparation owner excludes writers. A busy checkpoint means a
        # working-store reader still needs the WAL; use SQLite backup instead.
        with closing(sqlite3.connect(self.db_path)) as conn:
            busy, frames, checkpointed = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if busy or frames != checkpointed:
            return False
        library = ctypes.CDLL(None, use_errno=True)
        clonefile = library.clonefile
        clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        clonefile.restype = ctypes.c_int
        return clonefile(os.fsencode(self.db_path), os.fsencode(target), 0) == 0

    def _sync_directory(self) -> None:
        directory_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def publish(self, *, evidence_revision: str) -> PublishedSnapshot:
        """Publish completed evidence while the caller owns PreparationLock."""
        if not evidence_revision:
            raise ValueError('A material evidence revision is required')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        source = connect_sqlite_read_only(self.db_path)
        try:
            revision = source.execute("SELECT value,updated_at FROM store_metadata WHERE key='prepared_snapshot'").fetchone()
            if revision is None or revision[0] != 'ready':
                raise sqlite3.OperationalError('Working snapshot is not complete')
        finally:
            source.close()
        current = self.current()
        if current and current.source_revision == evidence_revision:
            return current
        with self._lock():
            # PreparationLock excludes every other publisher. Only interrupted
            # builds can remain here, including SQLiteBackupService partials.
            for abandoned in self.directory.glob('*.partial'):
                abandoned.unlink()
            self._collect(current, admission=True)
        self._check_space()
        generation = uuid4().hex
        partial = self.directory / f'{generation}.partial'
        target = self.directory / f'{generation}.db'
        manifest_partial = self.directory / 'current.partial'
        try:
            if not self._clone(partial):
                partial.unlink(missing_ok=True)
                self._check_space(self.db_path.stat().st_size)
                SQLiteBackupService().create(self.db_path, partial, progress=lambda _: self._check_space())
            self._check_space()
            with partial.open('rb') as handle:
                os.fsync(handle.fileno())
            partial.replace(target)
            self._sync_directory()
            snapshot = PublishedSnapshot(generation, target, datetime.now(UTC).isoformat(), evidence_revision)
            data = asdict(snapshot)
            data.pop('path')
            with manifest_partial.open('w') as handle:
                handle.write(_json_dumps(data))
                handle.flush()
                os.fsync(handle.fileno())
            manifest_partial.replace(self.manifest)
            self._sync_directory()
        except BaseException:
            partial.unlink(missing_ok=True)
            manifest_partial.unlink(missing_ok=True)
            # A failure after manifest replacement must not remove its DB.
            published = self.current()
            if published is None or published.generation != generation:
                target.unlink(missing_ok=True)
            raise
        with self._lock():
            self._collect(snapshot, admission=False)
        return snapshot


_ACTIVE_READ_CONTEXT: ContextVar[SnapshotReadContext | None] = ContextVar('reflect_snapshot_read', default=None)


def active_review_state(connection=None):
    context = _ACTIVE_READ_CONTEXT.get()
    if context is None or (connection is not None and connection is not context.connection):
        return None
    return context.review_state


def active_read_connection(path: Path):
    context = _ACTIVE_READ_CONTEXT.get()
    if context is not None and path in {context.db_path, context.snapshot.path}:
        return context.connection
    return None


class SnapshotReadContext:
    """Pin evidence and current human state for an entire composed response."""

    def __init__(self, db_path: Path, *, snapshot: PublishedSnapshot | None = None):
        self.db_path = Path(db_path).expanduser().resolve()
        self._requested_snapshot = snapshot
        self._stack = ExitStack()
        self.review_state = None
        self.state_path = None

    def __enter__(self):
        from reflect.store.user_state import UserStateStore

        active = _ACTIVE_READ_CONTEXT.get()
        self._borrowed_context = active if active and active.db_path == self.db_path else None
        if self._borrowed_context:
            if self._requested_snapshot is not None and self._requested_snapshot != active.snapshot:
                raise ValueError('Nested reads must use the same published generation')
            return self._borrowed_context
        try:
            self.snapshot = self._stack.enter_context(PublishedSnapshotStore(self.db_path).acquire(self._requested_snapshot))
            self.connection = connect_sqlite_read_only(self.snapshot.path, immutable=True)
            self.connection.shared = True
            row = self.connection.execute("SELECT value FROM main.store_metadata WHERE key='user_state_cutover_v1'").fetchone()
            if row:
                self.state_path = Path(row[0])
                self.review_state = UserStateStore(self.state_path).snapshot(connection=self.connection, schema='user_state')
            self._token = _ACTIVE_READ_CONTEXT.set(self)
            return self
        except BaseException:
            if hasattr(self, 'connection'):
                self.connection.shared = False
                self.connection.close()
            self._stack.close()
            raise

    def __exit__(self, *exception):
        if self._borrowed_context:
            return
        _ACTIVE_READ_CONTEXT.reset(self._token)
        self.connection.shared = False
        self.connection.close()
        self._stack.close()
