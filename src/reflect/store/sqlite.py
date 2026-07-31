from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

DEFAULT_BUSY_TIMEOUT_MS = 30000
DEFAULT_WAL_AUTOCHECKPOINT = 1000
DEFAULT_BACKUP_PAGES_PER_STEP = 2048
_CONNECTION_INIT_LOCK = threading.Lock()


def connect_sqlite(db_path: str | Path, *, strict_durability: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection configured for Reflect runtime defaults."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _CONNECTION_INIT_LOCK:
        conn = sqlite3.connect(path, timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000)
        try:
            _apply_runtime_pragmas(conn, strict_durability=strict_durability)
        except Exception:
            conn.close()
            raise
    return conn


def connect_sqlite_read_only(db_path: str | Path) -> sqlite3.Connection:
    """Open an existing Reflect store without creating or mutating it."""
    path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000,
    )
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS};")
        conn.execute("PRAGMA query_only = ON;")
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
        self._source_factory = source_factory or connect_sqlite_read_only

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


def _apply_runtime_pragmas(conn: sqlite3.Connection, *, strict_durability: bool) -> None:
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS};")
    journal_mode = str(conn.execute("PRAGMA journal_mode;").fetchone()[0]).lower()
    if journal_mode != "wal":
        conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute(
        f"PRAGMA synchronous = {'FULL' if strict_durability else 'NORMAL'};"
    )
    conn.execute(f"PRAGMA wal_autocheckpoint = {DEFAULT_WAL_AUTOCHECKPOINT};")


def optimize(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA optimize;")
