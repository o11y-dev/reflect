from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

DEFAULT_BUSY_TIMEOUT_MS = 30000
DEFAULT_WAL_AUTOCHECKPOINT = 1000
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


def backup_sqlite(source_path: str | Path, target_path: str | Path) -> None:
    """Create a consistent backup without mutating the source database."""

    source = connect_sqlite_read_only(source_path)
    try:
        target = sqlite3.connect(Path(target_path))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


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
