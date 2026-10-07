from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from uuid import uuid4

DEFAULT_BUSY_TIMEOUT_MS = 30000
DEFAULT_WAL_AUTOCHECKPOINT = 1000
DEFAULT_BACKUP_PAGES_PER_STEP = 2048
_CONNECTION_INIT_LOCK = threading.Lock()


def busy_timeout_ms(override: int | None = None) -> int:
    value = override if override is not None else os.environ.get(
        "REFLECT_SQLITE_BUSY_TIMEOUT_MS", DEFAULT_BUSY_TIMEOUT_MS
    )
    try:
        timeout = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("REFLECT_SQLITE_BUSY_TIMEOUT_MS must be an integer in 0..2147483647") from exc
    if not 0 <= timeout <= 2147483647:
        raise ValueError("REFLECT_SQLITE_BUSY_TIMEOUT_MS must be an integer in 0..2147483647")
    return timeout


def is_sqlite_busy(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and (
        getattr(exc, "sqlite_errorcode", 0) & 0xFF
    ) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


class SnapshotNotReadyError(sqlite3.OperationalError):
    """Canonical and derived data have not finished refreshing together."""


@contextmanager
def read_transaction(conn: sqlite3.Connection):
    """Pin related reads, leaving caller-owned transactions untouched."""
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if owned:
            conn.rollback()


def snapshot_incomplete(conn: sqlite3.Connection, profile: str = "snapshot") -> bool:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'store_metadata' AND type = 'table'"
    ).fetchone():
        return False
    row = conn.execute(
        "SELECT value FROM store_metadata WHERE key = ?", (f"prepared_{profile}",)
    ).fetchone()
    return row is not None and row[0] != "ready"


def mark_snapshot(conn: sqlite3.Connection, *, ready: bool, profile: str = "snapshot") -> None:
    profiles = ("snapshot", "usage") if not ready or profile == "snapshot" else ("usage",)
    conn.executemany(
        "INSERT INTO store_metadata(key,value,updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        [(f"prepared_{item}", "ready" if ready else "incomplete", datetime.now(UTC).isoformat())
         for item in profiles],
    )
    conn.commit()


def consistent_snapshot(operation):
    """Reject a composed response if its separate SQL reads span a refresh."""
    def revision(db_path):
        conn = connect_sqlite_read_only(db_path)
        try:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'store_metadata'"
            ).fetchone():
                return None
            return conn.execute(
                "SELECT value, updated_at FROM store_metadata WHERE key = 'prepared_snapshot'"
            ).fetchone()
        finally:
            conn.close()

    @wraps(operation)
    def read(db_path, *args, **kwargs):
        before = revision(db_path)
        result = operation(db_path, *args, **kwargs)
        if revision(db_path) != before:
            raise SnapshotNotReadyError("The snapshot changed during this request. Please retry.")
        return result

    return read


def advance_snapshot_revision(conn: sqlite3.Connection) -> None:
    """Include an atomic maintenance change in the caller's transaction.

    Preserve incomplete markers: partial maintenance cannot publish a refresh.
    """
    conn.executemany(
        "INSERT INTO store_metadata(key,value,updated_at) VALUES (?, 'ready', ?) "
        "ON CONFLICT(key) DO UPDATE SET updated_at=excluded.updated_at",
        [(f"prepared_{profile}", datetime.now(UTC).isoformat()) for profile in ("snapshot", "usage")],
    )


def connect_sqlite(
    db_path: str | Path, *, strict_durability: bool = False, timeout_ms: int | None = None,
) -> sqlite3.Connection:
    """Open a SQLite connection configured for Reflect runtime defaults."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    timeout = busy_timeout_ms(timeout_ms)
    with _CONNECTION_INIT_LOCK:
        # Every implicit write transaction reserves SQLite's single writer up front.
        conn = sqlite3.connect(path, timeout=timeout / 1000, isolation_level="IMMEDIATE")
        try:
            _apply_runtime_pragmas(conn, strict_durability=strict_durability, timeout_ms=timeout)
        except Exception:
            conn.close()
            raise
    return conn


def connect_sqlite_read_only(
    db_path: str | Path, *, profile: str | None = "snapshot",
) -> sqlite3.Connection:
    """Open an existing Reflect store without creating or mutating it."""
    path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        timeout=busy_timeout_ms() / 1000,
    )
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms()};")
        conn.execute("PRAGMA query_only = ON;")
        # Pin one generation for the readiness check and all subsequent queries.
        conn.execute("BEGIN")
        if profile is not None and snapshot_incomplete(conn, profile):
            label = "report" if profile == "snapshot" else profile
            raise SnapshotNotReadyError(
                f"The {label} snapshot is incomplete. Wait for the active refresh "
                "or run `reflect refresh` to recover an interrupted refresh."
            )
    except Exception:
        conn.close()
        raise
    return conn


class SQLiteBackupService:
    """Create and atomically publish SQLite backups."""

    def __init__(
        self,
        source_factory: Callable[[str | Path], sqlite3.Connection] | None = None,
    ) -> None:
        self._source_factory = source_factory or (
            lambda path: connect_sqlite_read_only(path, profile=None)
        )

    def create(
        self,
        source_path: str | Path,
        target_path: str | Path,
        *,
        progress: Callable[[SQLiteBackupProgress], None] | None = None,
    ) -> None:
        target_path = Path(target_path)
        if target_path.exists():
            raise FileExistsError(f"Backup target already exists: {target_path}")
        partial_path = target_path.with_name(
            f".{target_path.name}.{uuid4().hex}.partial"
        )
        try:
            source = self._source_factory(source_path)
            try:
                target = sqlite3.connect(partial_path)
                try:
                    if progress is None:
                        source.backup(target)
                    else:
                        source.backup(
                            target,
                            pages=DEFAULT_BACKUP_PAGES_PER_STEP,
                            progress=lambda _status, remaining, total: progress(
                                SQLiteBackupProgress(
                                    remaining_pages=remaining,
                                    total_pages=total,
                                )
                            ),
                        )
                finally:
                    target.close()
            finally:
                source.close()
            partial_path.replace(target_path)
        except Exception:
            partial_path.unlink(missing_ok=True)
            raise


@dataclass(frozen=True)
class SQLiteBackupProgress:
    remaining_pages: int
    total_pages: int

    @property
    def completed_pages(self) -> int:
        return max(self.total_pages - self.remaining_pages, 0)

    @property
    def percent_complete(self) -> int:
        if self.total_pages <= 0:
            return 100 if self.remaining_pages <= 0 else 0
        return min(self.completed_pages * 100 // self.total_pages, 100)


def backup_sqlite(
    source_path: str | Path,
    target_path: str | Path,
    *,
    progress: Callable[[SQLiteBackupProgress], None] | None = None,
) -> None:
    """Create a consistent backup without mutating the source database."""

    SQLiteBackupService().create(source_path, target_path, progress=progress)


def _apply_runtime_pragmas(
    conn: sqlite3.Connection, *, strict_durability: bool, timeout_ms: int,
) -> None:
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(f"PRAGMA busy_timeout = {timeout_ms};")
    journal_mode = str(conn.execute("PRAGMA journal_mode;").fetchone()[0]).lower()
    if journal_mode != "wal":
        conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute(
        f"PRAGMA synchronous = {'FULL' if strict_durability else 'NORMAL'};"
    )
    conn.execute(f"PRAGMA wal_autocheckpoint = {DEFAULT_WAL_AUTOCHECKPOINT};")


def optimize(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA optimize;")
