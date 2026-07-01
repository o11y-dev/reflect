from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reflect.memory.models import (
    MemoryItem,
    MemoryProviderHealth,
    MemorySearchResult,
    MemoryValidationResult,
    utc_now,
)


def _stable_id(prefix: str, *parts: object) -> str:
    payload = ":".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha1(payload.encode('utf-8')).hexdigest()}"


def _json_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    payload["raw_attrs"] = _json_dict(payload.pop("raw_attrs_json", "{}"))
    payload["source_metadata"] = _json_dict(payload.pop("source_metadata_json", "{}"))
    return payload


def _path_scope_clause(path: str) -> tuple[str, list[str]]:
    if not path:
        return "", []
    resolved = str(Path(path).expanduser().resolve())
    like = f"{resolved.rstrip('/')}/%"
    return (
        """
        AND (
          json_extract(source_metadata_json, '$.workspace_root') = ?
          OR json_extract(source_metadata_json, '$.path') = ?
          OR json_extract(source_metadata_json, '$.path') LIKE ?
          OR json_extract(raw_attrs_json, '$.workspace_root') = ?
          OR json_extract(raw_attrs_json, '$.path') = ?
          OR json_extract(raw_attrs_json, '$.path') LIKE ?
        )
        """,
        [resolved, resolved, like, resolved, resolved, like],
    )


def _filter_clause(filters: dict | None) -> tuple[str, list[str]]:
    filters = filters or {}
    clauses: list[str] = []
    params: list[str] = []
    for column in ("type", "scope", "source", "provider", "validation_status"):
        value = filters.get(column)
        if value:
            clauses.append(f"{column} = ?")
            params.append(str(value))
    if filters.get("stale"):
        clauses.append("COALESCE(stale_reason, '') <> ''")
    if filters.get("validated"):
        clauses.append("validation_status = 'validated'")
    if filters.get("unvalidated"):
        clauses.append("validation_status <> 'validated'")
    return (" AND " + " AND ".join(clauses), params) if clauses else ("", [])


class LocalSQLiteMemoryProvider:
    name = "local_sqlite"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def health(self) -> MemoryProviderHealth:
        try:
            self.conn.execute("SELECT 1 FROM memories LIMIT 1").fetchone()
        except sqlite3.Error as exc:
            return MemoryProviderHealth(self.name, False, "error", str(exc))
        return MemoryProviderHealth(self.name, True, "ok", "SQLite memory store is available")

    def remember(self, item: MemoryItem) -> dict:
        item.validate_for_write()
        timestamp = utc_now()
        source = item.source_metadata
        source_payload = source.to_json_dict()
        content_hash = source.content_hash or hashlib.sha256(item.content.encode("utf-8")).hexdigest()
        memory_id = item.id or _stable_id(
            "memory",
            item.scope,
            item.type,
            content_hash,
            source.source_kind,
            source.source_ref,
        )
        raw_attrs = {
            "source_kind": source.source_kind,
            "source_ref": source.source_ref,
            **source_payload,
        }
        repo_id = self._existing_id("repos", item.repo_id or source.repo_id)
        file_id = self._existing_id("files", item.file_id or source.file_id)
        session_id = self._existing_id("sessions", item.session_id or source.session_id)
        step_id = self._existing_id("steps", item.step_id or source.step_id)
        spec_id = self._existing_id("specs", item.spec_id or source.spec_id)
        self.conn.execute(
            """
            INSERT INTO memories(
              id, scope, type, repo_id, file_id, session_id, step_id, spec_id,
              content_hash, content_preview_redacted, confidence, sensitivity, source,
              expires_at, last_seen_at, raw_attrs_json, provider, provider_memory_id,
              provider_status, validation_status, source_metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''), NULLIF(?, ''),
              ?, ?, ?, ?, ?, NULLIF(?, ''), ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              scope = excluded.scope,
              type = excluded.type,
              repo_id = excluded.repo_id,
              file_id = excluded.file_id,
              session_id = excluded.session_id,
              step_id = excluded.step_id,
              spec_id = excluded.spec_id,
              content_hash = excluded.content_hash,
              content_preview_redacted = excluded.content_preview_redacted,
              confidence = excluded.confidence,
              sensitivity = excluded.sensitivity,
              source = excluded.source,
              expires_at = excluded.expires_at,
              last_seen_at = excluded.last_seen_at,
              raw_attrs_json = excluded.raw_attrs_json,
              provider = excluded.provider,
              provider_memory_id = excluded.provider_memory_id,
              provider_status = excluded.provider_status,
              source_metadata_json = excluded.source_metadata_json,
              updated_at = excluded.updated_at
            """,
            (
                memory_id,
                item.scope,
                item.type,
                repo_id,
                file_id,
                session_id,
                step_id,
                spec_id,
                content_hash,
                item.content[:1000],
                item.confidence,
                item.sensitivity,
                source.source_kind,
                item.expires_at,
                timestamp,
                json.dumps(raw_attrs, sort_keys=True),
                item.provider or self.name,
                memory_id,
                "stored",
                "validated" if source.manual_note else "unvalidated",
                json.dumps(source_payload, sort_keys=True),
                timestamp,
                timestamp,
            ),
        )
        self._upsert_fts(
            memory_id,
            content=item.content,
            type=item.type,
            scope=item.scope,
            source=source.source_kind,
            path=source.path,
        )
        self.conn.commit()
        return self.inspect(memory_id) or {"id": memory_id}

    def list(self, *, path: str = "", filters: dict | None = None, limit: int = 100) -> list[dict]:
        previous = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row
        try:
            path_clause, path_params = _path_scope_clause(path)
            filter_clause, filter_params = _filter_clause(filters)
            rows = self.conn.execute(
                f"""
                SELECT *
                FROM memories
                WHERE 1 = 1
                  {path_clause}
                  {filter_clause}
                ORDER BY COALESCE(last_seen_at, updated_at, created_at) DESC, id ASC
                LIMIT ?
                """,
                [*path_params, *filter_params, limit],
            ).fetchall()
            return [_row_to_dict(row) for row in rows]
        finally:
            self.conn.row_factory = previous

    def search(
        self,
        query: str,
        *,
        path: str = "",
        filters: dict | None = None,
        limit: int = 20,
    ) -> list[MemorySearchResult]:
        self.rebuild_fts_if_empty()
        previous = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row
        try:
            path_clause, path_params = _path_scope_clause(path)
            filter_clause, filter_params = _filter_clause(filters)
            try:
                rows = self.conn.execute(
                    f"""
                    SELECT m.*, bm25(memory_fts) AS score
                    FROM memory_fts
                    JOIN memories m ON m.id = memory_fts.memory_id
                    WHERE memory_fts MATCH ?
                      {path_clause}
                      {filter_clause}
                    ORDER BY score ASC
                    LIMIT ?
                    """,
                    [query, *path_params, *filter_params, limit],
                ).fetchall()
            except sqlite3.OperationalError:
                like = f"%{query}%"
                rows = self.conn.execute(
                    f"""
                    SELECT m.*, 0.0 AS score
                    FROM memories m
                    WHERE (
                      content_preview_redacted LIKE ?
                      OR type LIKE ?
                      OR source LIKE ?
                      OR json_extract(raw_attrs_json, '$.path') LIKE ?
                    )
                      {path_clause}
                      {filter_clause}
                    ORDER BY COALESCE(last_seen_at, updated_at, created_at) DESC
                    LIMIT ?
                    """,
                    [like, like, like, like, *path_params, *filter_params, limit],
                ).fetchall()
            return [
                MemorySearchResult(
                    item=_row_to_dict(row),
                    score=float(row["score"] or 0),
                    provider=self.name,
                )
                for row in rows
            ]
        finally:
            self.conn.row_factory = previous

    def inspect(self, memory_id: str) -> dict | None:
        previous = self.conn.row_factory
        self.conn.row_factory = sqlite3.Row
        try:
            row = self.conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            self.conn.row_factory = previous

    def forget(self, memory_id: str) -> bool:
        self.conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
        cursor = self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self.conn.commit()
        return cursor.rowcount > 0

    def validate(self, memory_id: str) -> MemoryValidationResult:
        memory = self.inspect(memory_id)
        if not memory:
            return MemoryValidationResult(memory_id, "missing", error="Memory not found")
        source_metadata = memory.get("source_metadata") or {}
        raw_attrs = memory.get("raw_attrs") or {}
        path = str(source_metadata.get("path") or raw_attrs.get("path") or "")
        stale_reason = ""
        if path:
            source_path = Path(path).expanduser()
            if not source_path.exists():
                stale_reason = "source_path_missing"
            elif memory.get("content_hash"):
                try:
                    content_hash = hashlib.sha256(source_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
                except (OSError, UnicodeDecodeError):
                    stale_reason = "source_path_unreadable"
                else:
                    if content_hash != memory.get("content_hash"):
                        stale_reason = "source_hash_changed"
        expires_at = str(memory.get("expires_at") or "")
        if expires_at:
            try:
                expires = datetime.fromisoformat(expires_at)
            except ValueError:
                expires = None
            if expires and expires < datetime.now(tz=UTC):
                stale_reason = "expired"
        status = "stale" if stale_reason else "validated"
        timestamp = utc_now()
        self.conn.execute(
            """
            UPDATE memories
            SET validation_status = ?,
                validated_at = ?,
                validation_error = NULL,
                stale_reason = NULLIF(?, ''),
                updated_at = ?
            WHERE id = ?
            """,
            (status, timestamp, stale_reason, timestamp, memory_id),
        )
        self.conn.commit()
        return MemoryValidationResult(memory_id, status, stale_reason=stale_reason)

    def _existing_id(self, table: str, value: str) -> str:
        if not value:
            return ""
        row = self.conn.execute(f"SELECT 1 FROM {table} WHERE id = ? LIMIT 1", (value,)).fetchone()
        return value if row else ""

    def _upsert_fts(
        self,
        memory_id: str,
        *,
        content: str,
        type: str,
        scope: str,
        source: str,
        path: str,
    ) -> None:
        self.conn.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
        self.conn.execute(
            """
            INSERT INTO memory_fts(memory_id, content, type, scope, source, path)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (memory_id, content, type, scope, source, path),
        )

    def rebuild_fts_if_empty(self) -> None:
        count = self.conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
        if count:
            return
        rows = self.conn.execute(
            """
            SELECT id, content_preview_redacted, type, scope, source, raw_attrs_json, source_metadata_json
            FROM memories
            """
        ).fetchall()
        for row in rows:
            raw_attrs = _json_dict(row[5])
            source_metadata = _json_dict(row[6])
            path = str(source_metadata.get("path") or raw_attrs.get("path") or "")
            self._upsert_fts(
                str(row[0]),
                content=str(row[1] or ""),
                type=str(row[2] or ""),
                scope=str(row[3] or ""),
                source=str(row[4] or ""),
                path=path,
            )
        self.conn.commit()
