from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_WAL_AUTOCHECKPOINT = 1000


def connect_sqlite(db_path: str | Path, *, strict_durability: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection configured for Reflect runtime defaults."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    _apply_runtime_pragmas(conn, strict_durability=strict_durability)
    return conn


def _apply_runtime_pragmas(conn: sqlite3.Connection, *, strict_durability: bool) -> None:
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute(
        f"PRAGMA synchronous = {'FULL' if strict_durability else 'NORMAL'};"
    )
    conn.execute(f"PRAGMA wal_autocheckpoint = {DEFAULT_WAL_AUTOCHECKPOINT};")
    conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS};")


def optimize(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA optimize;")
