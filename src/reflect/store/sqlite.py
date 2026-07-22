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
