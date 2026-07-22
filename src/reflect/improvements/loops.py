from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from typing import Any

from reflect.improvements.models import (
    LoopDetail,
    LoopKind,
    LoopOccurrenceRecord,
    LoopRecord,
    LoopStatus,
)
from reflect.improvements.repository import utc_now
from reflect.store.migrate import migrate


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _fingerprint(*parts: object) -> str:
    raw = "\x1f".join(str(part or "").strip().lower() for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class LoopService:
    """Detect and retain repeated workflow cycles independently of skill proposals."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        migrate(conn)

    def refresh(self, *, commit: bool = True) -> dict[str, int]:
        """Refresh stalled retries and confirmed productive routines from canonical evidence."""
        now = utc_now()
        seen: set[str] = set()
        stalled = self._refresh_stalled(now=now, seen=seen)
        productive = self._refresh_productive(now=now, seen=seen)
        if seen:
            placeholders = ",".join("?" for _ in seen)
            resolved = self.conn.execute(
                f"""
                UPDATE loop_patterns
                SET status = 'resolved', updated_at = ?
                WHERE id NOT IN ({placeholders})
                  AND status = 'detected'
                """,
                (now, *sorted(seen)),
            ).rowcount
        else:
            resolved = self.conn.execute(
                "UPDATE loop_patterns SET status = 'resolved', updated_at = ? WHERE status = 'detected'",
                (now,),
            ).rowcount
        if commit:
            self.conn.commit()
        return {
            "detected": stalled + productive,
            "stalled": stalled,
            "productive": productive,
            "resolved": int(resolved or 0),
        }

    def _refresh_stalled(self, *, now: str, seen: set[str]) -> int:
        cursor = self.conn.execute(
            """
            SELECT s.repo_id, tc.session_id, lower(trim(tc.tool_name)) AS tool_key,
                   tc.tool_name, tc.input_hash,
                   CASE WHEN lower(COALESCE(tc.status, '')) IN ('error', 'failed', 'failure')
                        THEN 1 ELSE 0 END AS is_error
            FROM steps st
            JOIN tool_calls tc ON tc.step_id = st.id
            JOIN sessions s ON s.id = tc.session_id
            WHERE NULLIF(tc.input_hash, '') IS NOT NULL
              AND NULLIF(trim(tc.tool_name), '') IS NOT NULL
              AND lower(trim(tc.tool_name)) NOT IN ('write_stdin', 'wait')
              AND lower(COALESCE(tc.input_preview_redacted, '')) NOT LIKE '%"decision":"approved"%'
              AND lower(trim(COALESCE(tc.input_preview_redacted, ''))) NOT IN (
                '[redacted]', 'redacted', '{}', 'null'
              )
            ORDER BY tc.session_id, st.seq, tc.id
            """
        )
        repeated: dict[tuple[str | None, str, str, str], list[Any]] = {}
        current_key: tuple[str | None, str, str, str] | None = None
        current_name = ""
        current_count = 0
        current_errors = 0

        def flush_run() -> None:
            nonlocal current_key, current_name, current_count, current_errors
            if current_key is None or current_count < 3:
                return
            aggregate = repeated.setdefault(
                current_key,
                [current_key[0], current_key[1], current_key[2], current_name, current_key[3], 0, 0, 0],
            )
            aggregate[5] += current_count
            aggregate[6] += current_errors
            aggregate[7] += 1

        for repo_id, session_id, tool_key, tool_name, input_hash, is_error in cursor:
            key = (repo_id, str(session_id), str(tool_key), str(input_hash))
            if key != current_key:
                flush_run()
                current_key = key
                current_name = str(tool_name)
                current_count = 0
                current_errors = 0
            current_count += 1
            current_errors += int(is_error or 0)
        flush_run()
        rows = list(repeated.values())
        grouped: dict[tuple[str | None, str], list[sqlite3.Row | tuple]] = defaultdict(list)
        names: dict[tuple[str | None, str], str] = {}
        for row in rows:
            key = (row[0], str(row[2]))
            grouped[key].append(row)
            names.setdefault(key, str(row[3]))
        qualified = {
            key: occurrences
            for key, occurrences in grouped.items()
            if len({str(row[1]) for row in occurrences}) >= 2
            or sum(int(row[6] or 0) for row in occurrences) > 0
        }
        for (repo_id, tool_key), occurrences in qualified.items():
            fingerprint = _fingerprint("stalled", repo_id or "local", tool_key)
            loop_id = f"loop_{fingerprint}"
            seen.add(loop_id)
            session_count = len({str(row[1]) for row in occurrences})
            repeat_count = sum(int(row[5]) for row in occurrences)
            error_count = sum(int(row[6] or 0) for row in occurrences)
            run_count = sum(int(row[7]) for row in occurrences)
            confidence = min(0.96, 0.58 + session_count * 0.04 + run_count * 0.015)
            tool_name = names[(repo_id, tool_key)]
            self._upsert_pattern(
                loop_id=loop_id,
                fingerprint=fingerprint,
                kind=LoopKind.STALLED,
                title=f"Repeated {tool_name} calls without state change",
                summary=(
                    f"{repeat_count} consecutive same-input {tool_name} calls formed "
                    f"{run_count} retry run(s) across {session_count} session(s); "
                    f"{error_count} call(s) recorded failure."
                ),
                scope_type="repository" if repo_id else "user",
                scope_id=str(repo_id or "local"),
                repo_id=repo_id,
                tool_name=tool_name,
                occurrence_count=repeat_count,
                affected_session_count=session_count,
                state_change_count=0,
                confidence=confidence,
                evidence={
                    "input_fingerprint_count": len({str(row[4]) for row in occurrences}),
                    "retry_run_count": run_count,
                    "error_call_count": error_count,
                    "classification": "consecutive_identical_input_retry",
                },
                now=now,
            )
            self.conn.execute("DELETE FROM loop_occurrences WHERE loop_id = ?", (loop_id,))
            for row in occurrences[:200]:
                self._insert_occurrence(
                    loop_id=loop_id,
                    session_id=str(row[1]),
                    tool_name=tool_name,
                    input_hash=str(row[4]),
                    repeat_count=int(row[5]),
                    error_count=int(row[6] or 0),
                    state_changed=False,
                    outcome=self._session_outcome(str(row[1])),
                    evidence={
                        "classification": "consecutive_identical_input_retry",
                        "retry_run_count": int(row[7]),
                    },
                    now=now,
                )
        return len(qualified)

    def _refresh_productive(self, *, now: str, seen: set[str]) -> int:
        rows = self.conn.execute(
            """
            SELECT o.id, o.fingerprint, o.title, o.summary, o.scope_type, o.scope_id,
                   o.repo_id, o.occurrence_count, o.affected_session_count,
                   o.confidence, o.baseline_query_json
            FROM observations o
            WHERE o.rule_id = 'high_performing_repeated_workflow'
              AND o.status NOT IN ('resolved', 'dismissed', 'rejected')
            ORDER BY o.confidence DESC, o.affected_session_count DESC
            """
        ).fetchall()
        for row in rows:
            fingerprint = _fingerprint("productive", row[1])
            loop_id = f"loop_{fingerprint}"
            seen.add(loop_id)
            baseline = _loads(row[10], {})
            signature = str(baseline.get("tool_signature") or "")
            signature_tools = [item.strip() for item in signature.split(">") if item.strip()]
            self._upsert_pattern(
                loop_id=loop_id,
                fingerprint=fingerprint,
                kind=LoopKind.PRODUCTIVE,
                title=str(row[2]),
                summary=str(row[3]),
                scope_type=str(row[4]),
                scope_id=str(row[5]),
                repo_id=row[6],
                tool_name=None,
                occurrence_count=int(row[7]),
                affected_session_count=int(row[8]),
                state_change_count=max(0, len(signature_tools) - 1),
                confidence=float(row[9]),
                evidence={
                    "observation_id": str(row[0]),
                    "task_archetype_id": baseline.get("task_archetype_id"),
                    "tool_signature": signature,
                    "classification": "confirmed_failure_free_routine",
                },
                now=now,
            )
            self.conn.execute("DELETE FROM loop_occurrences WHERE loop_id = ?", (loop_id,))
            evidence_rows = self.conn.execute(
                """
                SELECT DISTINCT session_id
                FROM observation_evidence
                WHERE observation_id = ? AND session_id IS NOT NULL
                ORDER BY session_id
                LIMIT 200
                """,
                (row[0],),
            ).fetchall()
            signature_hash = _fingerprint(signature) if signature else None
            for evidence_row in evidence_rows:
                session_id = str(evidence_row[0])
                self._insert_occurrence(
                    loop_id=loop_id,
                    session_id=session_id,
                    tool_name="workflow_sequence",
                    input_hash=signature_hash,
                    repeat_count=max(1, len(signature_tools)),
                    error_count=0,
                    state_changed=True,
                    outcome=self._session_outcome(session_id) or "completed_without_tool_failure",
                    evidence={"tool_signature": signature},
                    now=now,
                )
        return len(rows)

    def _upsert_pattern(
        self,
        *,
        loop_id: str,
        fingerprint: str,
        kind: LoopKind,
        title: str,
        summary: str,
        scope_type: str,
        scope_id: str,
        repo_id: str | None,
        tool_name: str | None,
        occurrence_count: int,
        affected_session_count: int,
        state_change_count: int,
        confidence: float,
        evidence: dict[str, Any],
        now: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO loop_patterns(
              id, fingerprint, kind, title, summary, scope_type, scope_id, repo_id,
              tool_name, occurrence_count, affected_session_count, state_change_count,
              confidence, status, evidence_json, first_seen_at, last_seen_at,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'detected', ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              kind = excluded.kind,
              title = excluded.title,
              summary = excluded.summary,
              occurrence_count = excluded.occurrence_count,
              affected_session_count = excluded.affected_session_count,
              state_change_count = excluded.state_change_count,
              confidence = excluded.confidence,
              status = CASE WHEN loop_patterns.status = 'resolved' THEN 'detected' ELSE loop_patterns.status END,
              evidence_json = excluded.evidence_json,
              last_seen_at = excluded.last_seen_at,
              updated_at = excluded.updated_at
            """,
            (
                loop_id,
                fingerprint,
                kind.value,
                title,
                summary,
                scope_type,
                scope_id,
                repo_id,
                tool_name,
                occurrence_count,
                affected_session_count,
                state_change_count,
                confidence,
                _json(evidence),
                now,
                now,
                now,
                now,
            ),
        )

    def _insert_occurrence(
        self,
        *,
        loop_id: str,
        session_id: str,
        tool_name: str,
        input_hash: str | None,
        repeat_count: int,
        error_count: int,
        state_changed: bool,
        outcome: str | None,
        evidence: dict[str, Any],
        now: str,
    ) -> None:
        occurrence_id = "loop_occurrence_" + _fingerprint(loop_id, session_id, input_hash or "none")
        self.conn.execute(
            """
            INSERT INTO loop_occurrences(
              id, loop_id, session_id, tool_name, input_hash, repeat_count,
              error_count, state_changed, outcome, evidence_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                occurrence_id,
                loop_id,
                session_id,
                tool_name,
                input_hash,
                repeat_count,
                error_count,
                int(state_changed),
                outcome,
                _json(evidence),
                now,
                now,
            ),
        )

    def _session_outcome(self, session_id: str) -> str | None:
        row = self.conn.execute(
            """
            SELECT outcome FROM session_outcomes
            WHERE session_id = ?
            ORDER BY CASE source WHEN 'operator_feedback' THEN 0 ELSE 1 END, updated_at DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        return str(row[0]) if row else None

    def list(
        self,
        *,
        kind: LoopKind | None = None,
        status: LoopStatus | None = None,
        limit: int = 100,
    ) -> list[LoopRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind.value)
        if status:
            clauses.append("status = ?")
            params.append(status.value)
        else:
            clauses.append("status IN ('detected', 'acknowledged', 'promoted')")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT id, fingerprint, kind, title, summary, scope_type, scope_id,
                   repo_id, tool_name, occurrence_count, affected_session_count,
                   state_change_count, confidence, status, evidence_json,
                   first_seen_at, last_seen_at, updated_at
            FROM loop_patterns
            {where}
            ORDER BY CASE status WHEN 'detected' THEN 0 WHEN 'promoted' THEN 1 ELSE 2 END,
                     confidence DESC, affected_session_count DESC, updated_at DESC
            LIMIT ?
            """,
            (*params, max(1, min(limit, 500))),
        ).fetchall()
        return [self._record(row) for row in rows]

    def show(self, loop_id: str, *, limit: int = 100) -> LoopDetail:
        row = self.conn.execute(
            """
            SELECT id, fingerprint, kind, title, summary, scope_type, scope_id,
                   repo_id, tool_name, occurrence_count, affected_session_count,
                   state_change_count, confidence, status, evidence_json,
                   first_seen_at, last_seen_at, updated_at
            FROM loop_patterns WHERE id = ?
            """,
            (loop_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Loop not found: {loop_id}")
        occurrence_rows = self.conn.execute(
            """
            SELECT id, loop_id, session_id, tool_name, input_hash, repeat_count,
                   error_count, state_changed, outcome, evidence_json
            FROM loop_occurrences
            WHERE loop_id = ?
            ORDER BY repeat_count DESC, error_count DESC, session_id
            LIMIT ?
            """,
            (loop_id, max(1, min(limit, 200))),
        ).fetchall()
        return LoopDetail(
            loop=self._record(row),
            occurrences=[
                LoopOccurrenceRecord(
                    id=str(item[0]),
                    loop_id=str(item[1]),
                    session_id=str(item[2]),
                    tool_name=str(item[3]),
                    input_hash=item[4],
                    repeat_count=int(item[5]),
                    error_count=int(item[6]),
                    state_changed=bool(item[7]),
                    outcome=item[8],
                    evidence=_loads(item[9], {}),
                )
                for item in occurrence_rows
            ],
        )

    def evidence_bundle(self, loop_id: str) -> dict[str, Any]:
        detail = self.show(loop_id, limit=50)
        return {
            "schema_version": 1,
            "loop": detail.loop.model_dump(mode="json"),
            "occurrences": [item.model_dump(mode="json") for item in detail.occurrences],
            "selection_policy": {
                "maximum_occurrences": 50,
                "source": "canonical_loop_ledger",
                "input_content": "privacy_safe_hashes_only",
            },
        }

    def mark_promoted(self, loop_id: str, skill_id: str) -> None:
        detail = self.show(loop_id, limit=1)
        evidence = dict(detail.loop.evidence)
        evidence["promoted_skill_id"] = skill_id
        now = utc_now()
        self.conn.execute(
            """
            UPDATE loop_patterns
            SET status = 'promoted', evidence_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (_json(evidence), now, loop_id),
        )
        self.conn.commit()

    @staticmethod
    def _record(row: sqlite3.Row | tuple) -> LoopRecord:
        return LoopRecord(
            id=str(row[0]),
            fingerprint=str(row[1]),
            kind=LoopKind(str(row[2])),
            title=str(row[3]),
            summary=str(row[4]),
            scope_type=str(row[5]),
            scope_id=str(row[6]),
            repo_id=row[7],
            tool_name=row[8],
            occurrence_count=int(row[9]),
            affected_session_count=int(row[10]),
            state_change_count=int(row[11]),
            confidence=float(row[12]),
            status=LoopStatus(str(row[13])),
            evidence=_loads(row[14], {}),
            first_seen_at=str(row[15]),
            last_seen_at=str(row[16]),
            updated_at=str(row[17]),
        )
