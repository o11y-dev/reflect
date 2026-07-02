from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from reflect.memory.models import MemoryItem, MemorySourceMetadata, utc_now
from reflect.memory.registry import MemoryProviderRegistry
from reflect.memory.sqlite_provider import LocalSQLiteMemoryProvider
from reflect.store.instruction_memory import _classify_instruction, discover_instruction_files


class MemoryService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.local = LocalSQLiteMemoryProvider(conn)
        self.registry = MemoryProviderRegistry(self.local)

    def provider_health(self) -> list[dict[str, Any]]:
        return [health.__dict__ for health in self.registry.health()]

    def remember(
        self,
        item: MemoryItem,
        *,
        semantic_domain: str = "reflect_operational",
        provider: str | None = None,
    ) -> dict:
        item.validate_for_write()
        target = provider or self._route_provider(semantic_domain, item)
        if target == "local_sqlite":
            return self.local.remember(item)
        remote = self.registry.get(target)
        try:
            health = remote.health()
            if not health.available:
                raise RuntimeError(health.detail or health.status)
            remote_result = remote.remember(item)
            mirrored = self.local.remember(item)
            self.conn.execute(
                """
                UPDATE memories
                SET provider = ?,
                    provider_memory_id = COALESCE(NULLIF(?, ''), provider_memory_id),
                    provider_status = 'mirrored',
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    target,
                    str(remote_result.get("id") or remote_result.get("memory_id") or ""),
                    utc_now(),
                    mirrored["id"],
                ),
            )
            self.conn.commit()
            return self.local.inspect(str(mirrored["id"])) or mirrored
        except Exception as exc:  # noqa: BLE001 - remote adapters must not block local memory
            mirrored = self.local.remember(item)
            self.conn.execute(
                """
                UPDATE memories
                SET provider = ?,
                    provider_status = 'local_fallback',
                    validation_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (target, str(exc), utc_now(), mirrored["id"]),
            )
            self.conn.commit()
            return self.local.inspect(str(mirrored["id"])) or mirrored

    def list_memories(
        self,
        *,
        path: Path | None,
        all_memories: bool = False,
        filters: dict | None = None,
        limit: int = 100,
    ) -> list[dict]:
        scoped_path = "" if all_memories else str((path or Path.cwd()).expanduser().resolve())
        return self.local.list(path=scoped_path, filters=filters, limit=limit)

    def search(
        self,
        query: str,
        *,
        path: Path | None,
        filters: dict | None = None,
        provider: str = "local_sqlite",
        limit: int = 20,
    ) -> list[dict]:
        scoped_path = str((path or Path.cwd()).expanduser().resolve()) if path is not None else ""
        results = self.registry.get(provider).search(query, path=scoped_path, filters=filters, limit=limit)
        return [{"score": result.score, "provider": result.provider, **result.item} for result in results]

    def sync_path(self, path: Path, *, home_root: Path | None = None) -> dict[str, int]:
        workspace_root = path.expanduser().resolve()
        files = discover_instruction_files(workspace_root, home_root=home_root or Path.home())
        inserted = 0
        updated = 0
        for source_path in files:
            try:
                text = source_path.read_text(encoding="utf-8")
                stat = source_path.stat()
            except (OSError, UnicodeDecodeError):
                continue
            kind, scope = _classify_instruction(source_path, workspace_root, home_root=home_root or Path.home())
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            memory_id = f"instruction_{hashlib.sha1(str(source_path).encode('utf-8')).hexdigest()}"
            existed = self.local.inspect(memory_id) is not None
            attrs = {
                "path": str(source_path),
                "name": source_path.name,
                "kind": kind,
                "scope": scope,
                "workspace_root": str(workspace_root),
                "size": stat.st_size,
            }
            source = MemorySourceMetadata.from_path(
                source_path,
                workspace_root=workspace_root,
                source_kind="filesystem_instruction_scan",
                content_hash=content_hash,
                attrs=attrs,
            )
            self.remember(
                MemoryItem(
                    id=memory_id,
                    content=_redacted_preview(source_path, text),
                    type=kind,
                    scope=scope,
                    source_metadata=source,
                    confidence=1.0,
                    sensitivity="private" if scope == "user" else "unknown",
                ),
                semantic_domain="reflect_operational",
                provider="local_sqlite",
            )
            if existed:
                updated += 1
            else:
                inserted += 1
        return {"discovered": len(files), "inserted": inserted, "updated": updated}

    def inspect(self, memory_id: str) -> dict | None:
        return self.local.inspect(memory_id)

    def forget(self, memory_id: str) -> bool:
        return self.local.forget(memory_id)

    def validate(self, memory_id: str) -> dict:
        return self.local.validate(memory_id).__dict__

    def candidates(self, *, path: Path | None = None, session_id: str = "", limit: int = 50) -> list[dict]:
        self._generate_candidates(path=path, session_id=session_id, limit=limit)
        rows = self.conn.execute(
            """
            SELECT id, scope, type, content_preview_redacted, confidence,
                   source_metadata_json, evidence_json, status, promoted_memory_id
            FROM memory_candidates
            WHERE status = 'candidate'
            ORDER BY confidence DESC, updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        candidates: list[dict] = []
        for row in rows:
            candidates.append(
                {
                    "id": row[0],
                    "scope": row[1],
                    "type": row[2],
                    "content": row[3],
                    "confidence": row[4],
                    "source_metadata": _loads(row[5]),
                    "evidence": _loads(row[6]),
                    "status": row[7],
                    "promoted_memory_id": row[8] or "",
                }
            )
        return candidates

    def promote_candidate(self, candidate_id: str) -> dict:
        row = self.conn.execute(
            """
            SELECT id, scope, type, content_preview_redacted, confidence,
                   source_metadata_json, evidence_json
            FROM memory_candidates
            WHERE id = ?
            """,
            (candidate_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"Memory candidate not found: {candidate_id}")
        source_payload = _loads(row[5])
        source = MemorySourceMetadata(
            source_kind=str(source_payload.get("source_kind") or "graph_candidate"),
            source_ref=str(source_payload.get("source_ref") or candidate_id),
            path=str(source_payload.get("path") or ""),
            workspace_root=str(source_payload.get("workspace_root") or ""),
            session_id=str(source_payload.get("session_id") or ""),
            attrs=source_payload,
        )
        remembered = self.remember(
            MemoryItem(
                content=str(row[3]),
                type=str(row[2]),
                scope=str(row[1]),
                confidence=float(row[4] or 0.5),
                source_metadata=source,
            ),
            semantic_domain="reflect_operational",
            provider="local_sqlite",
        )
        self.conn.execute(
            """
            UPDATE memory_candidates
            SET status = 'promoted', promoted_memory_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (remembered["id"], utc_now(), candidate_id),
        )
        self.conn.commit()
        return remembered

    def _route_provider(self, semantic_domain: str, item: MemoryItem) -> str:
        if semantic_domain == "generic_agent_session" and os.environ.get("AGENTMEMORY_URL"):
            return "agentmemory"
        if semantic_domain == "generic_agent_session" and (
            os.environ.get("LITELLM_MEMORY_URL") or os.environ.get("REFLECT_LITELLM_MEMORY_URL")
        ):
            return "litellm"
        if semantic_domain == "generic_agent_session" and os.environ.get("MEMORYPALACE_URL"):
            return "memorypalace"
        return "local_sqlite"

    def _generate_candidates(self, *, path: Path | None, session_id: str, limit: int) -> None:
        timestamp = utc_now()
        session_clause = "AND ge.session_id = ?" if session_id else ""
        params: list[object] = [session_id] if session_id else []
        rows = self.conn.execute(
            f"""
            SELECT
              ge.kind,
              tn.kind,
              tn.label,
              COUNT(*) AS occurrences,
              COUNT(DISTINCT ge.session_id) AS session_support,
              GROUP_CONCAT(DISTINCT ge.session_id) AS session_ids
            FROM graph_edges ge
            JOIN graph_nodes tn ON tn.id = ge.target_node_id
            WHERE ge.kind IN ('used_tool', 'used_skill', 'spawned_subagent', 'achieved_outcome', 'touched_folder', 'addressed_spec')
              AND ge.session_id IS NOT NULL
              {session_clause}
            GROUP BY ge.kind, tn.kind, tn.label
            HAVING COUNT(DISTINCT ge.session_id) >= CASE WHEN ? <> '' THEN 1 ELSE 2 END
            ORDER BY session_support DESC, occurrences DESC
            LIMIT ?
            """,
            [*params, session_id, limit],
        ).fetchall()
        workspace = str((path or Path.cwd()).expanduser().resolve())
        for row in rows:
            edge_kind, node_kind, label, occurrences, support, session_ids_csv = row
            content = (
                f"Recurring Reflect graph pattern: {edge_kind} -> {node_kind} {label} "
                f"across {support} session(s)."
            )
            candidate_id = f"candidate_{hashlib.sha1(content.encode('utf-8')).hexdigest()}"
            source_metadata = {
                "source_kind": "graph_candidate",
                "source_ref": candidate_id,
                "workspace_root": workspace,
            }
            evidence = {
                "edge_kind": edge_kind,
                "target_kind": node_kind,
                "target_label": label,
                "occurrences": int(occurrences or 0),
                "session_support": int(support or 0),
                "session_ids": sorted(sid for sid in str(session_ids_csv or "").split(",") if sid),
            }
            self.conn.execute(
                """
                INSERT INTO memory_candidates(
                  id, scope, type, content_preview_redacted, confidence,
                  source_metadata_json, evidence_json, status, created_at, updated_at
                ) VALUES (?, 'project', 'graph_pattern', ?, ?, ?, ?, 'candidate', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  confidence = excluded.confidence,
                  evidence_json = excluded.evidence_json,
                  updated_at = excluded.updated_at
                """,
                (
                    candidate_id,
                    content,
                    min(0.95, 0.5 + (float(support or 0) * 0.1)),
                    json.dumps(source_metadata, sort_keys=True),
                    json.dumps(evidence, sort_keys=True),
                    timestamp,
                    timestamp,
                ),
            )
        self.conn.commit()


def _redacted_preview(path: Path, text: str, *, max_chars: int = 360) -> str:
    try:
        path.relative_to(Path.home())
        return f"[user config: {path.name}]"
    except ValueError:
        cleaned = " ".join(line.strip() for line in text.splitlines() if line.strip())
        return cleaned[:max_chars]


def _loads(value: object) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
