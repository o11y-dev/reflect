"""Read OpenCode's native SQLite session store into typed source records."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reflect.utils import _json_loads

_SUCCESS_TOOL_STATES = {"completed", "complete", "success", "succeeded"}
_FAILED_TOOL_STATES = {"error", "failed", "failure", "cancelled", "canceled", "aborted"}


def opencode_tool_success(value: object) -> bool | None:
    status = str(value or "").lower()
    if status in _SUCCESS_TOOL_STATES:
        return True
    if status in _FAILED_TOOL_STATES:
        return False
    return None


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    try:
        payload = _json_loads(value)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True, slots=True)
class OpenCodePartRecord:
    id: str
    created_ms: int
    data: dict[str, Any]

    @property
    def type(self) -> str:
        return str(self.data.get("type") or "")


@dataclass(frozen=True, slots=True)
class OpenCodeMessageRecord:
    id: str
    created_ms: int
    data: dict[str, Any]
    parts: tuple[OpenCodePartRecord, ...]

    @property
    def role(self) -> str:
        return str(self.data.get("role") or "")

    @property
    def model(self) -> str:
        return str(self.data.get("modelID") or "")

    @property
    def provider(self) -> str:
        return str(self.data.get("providerID") or "")


@dataclass(frozen=True, slots=True)
class OpenCodeSessionRecord:
    id: str
    parent_id: str
    directory: str
    title: str
    created_ms: int
    updated_ms: int
    messages: tuple[OpenCodeMessageRecord, ...]


class OpenCodeSessionStore:
    """Expose provider records without leaking SQLite details into consumers."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def load(
        self,
        session_id: str | None = None,
        *,
        after_updated_ms: int = 0,
        after_session_id: str = "",
    ) -> tuple[OpenCodeSessionRecord, ...]:
        conn = sqlite3.connect(
            f"{self.path.as_uri()}?mode=ro",
            uri=True,
            timeout=5,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only = ON")
            params: tuple[object, ...] = ()
            where = ""
            if session_id:
                where = "WHERE s.id = ?"
                params = (session_id,)
            elif after_updated_ms or after_session_id:
                where = """
                WHERE s.time_updated > ?
                   OR (s.time_updated = ? AND s.id > ?)
                """
                params = (after_updated_ms, after_updated_ms, after_session_id)
            rows = conn.execute(
                f"""
                SELECT
                  s.id AS session_id,
                  COALESCE(s.parent_id, '') AS parent_id,
                  COALESCE(s.directory, '') AS directory,
                  COALESCE(s.title, '') AS title,
                  s.time_created AS session_created_ms,
                  s.time_updated AS session_updated_ms,
                  m.id AS message_id,
                  m.time_created AS message_created_ms,
                  m.data AS message_data,
                  p.id AS part_id,
                  p.time_created AS part_created_ms,
                  p.data AS part_data
                FROM session s
                LEFT JOIN message m ON m.session_id = s.id
                LEFT JOIN part p ON p.message_id = m.id
                {where}
                ORDER BY
                  s.time_updated, s.id,
                  m.time_created, m.id,
                  p.time_created, p.id
                """,
                params,
            )
            return self._records(rows)
        finally:
            conn.close()

    @staticmethod
    def _records(rows) -> tuple[OpenCodeSessionRecord, ...]:
        sessions: dict[str, dict[str, Any]] = {}
        for row in rows:
            session_id = str(row["session_id"])
            session = sessions.setdefault(
                session_id,
                {
                    "parent_id": str(row["parent_id"] or ""),
                    "directory": str(row["directory"] or ""),
                    "title": str(row["title"] or ""),
                    "created_ms": int(row["session_created_ms"] or 0),
                    "updated_ms": int(row["session_updated_ms"] or 0),
                    "messages": {},
                },
            )
            message_id = str(row["message_id"] or "")
            if not message_id:
                continue
            message = session["messages"].setdefault(
                message_id,
                {
                    "created_ms": int(row["message_created_ms"] or 0),
                    "data": _json_object(row["message_data"]),
                    "parts": [],
                },
            )
            part_id = str(row["part_id"] or "")
            if part_id:
                message["parts"].append(
                    OpenCodePartRecord(
                        id=part_id,
                        created_ms=int(row["part_created_ms"] or 0),
                        data=_json_object(row["part_data"]),
                    )
                )

        records: list[OpenCodeSessionRecord] = []
        for session_id, session in sessions.items():
            messages = tuple(
                OpenCodeMessageRecord(
                    id=message_id,
                    created_ms=int(message["created_ms"]),
                    data=message["data"],
                    parts=tuple(message["parts"]),
                )
                for message_id, message in session["messages"].items()
            )
            records.append(
                OpenCodeSessionRecord(
                    id=session_id,
                    parent_id=str(session["parent_id"]),
                    directory=str(session["directory"]),
                    title=str(session["title"]),
                    created_ms=int(session["created_ms"]),
                    updated_ms=int(session["updated_ms"]),
                    messages=messages,
                )
            )
        return tuple(records)


__all__ = [
    "OpenCodeMessageRecord",
    "OpenCodePartRecord",
    "OpenCodeSessionRecord",
    "OpenCodeSessionStore",
    "opencode_tool_success",
]
