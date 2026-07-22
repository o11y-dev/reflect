from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from reflect.improvements.repository import utc_now


class NudgeService:
    """Opt-in, bounded nudge queue intended for an OpenTelemetry hook bridge."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def configure(
        self,
        rule_id: str,
        rule_version: int,
        *,
        enabled: bool,
        cooldown_seconds: int = 900,
        max_per_session: int = 1,
    ) -> str:
        if cooldown_seconds < 60:
            raise ValueError("Nudge cooldown must be at least 60 seconds")
        if not 1 <= max_per_session <= 5:
            raise ValueError("Nudge max_per_session must be between 1 and 5")
        exists = self.conn.execute(
            "SELECT 1 FROM rule_definitions WHERE id = ? AND version = ?",
            (rule_id, rule_version),
        ).fetchone()
        if not exists:
            raise KeyError(f"Rule not found: {rule_id} v{rule_version}")
        policy_id = f"nudge_policy_{rule_id}_{rule_version}"
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO nudge_policies(
              id, rule_id, rule_version, enabled, cooldown_seconds,
              max_per_session, config_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              enabled = excluded.enabled,
              cooldown_seconds = excluded.cooldown_seconds,
              max_per_session = excluded.max_per_session,
              updated_at = excluded.updated_at
            """,
            (
                policy_id,
                rule_id,
                rule_version,
                int(enabled),
                cooldown_seconds,
                max_per_session,
                now,
                now,
            ),
        )
        self.conn.commit()
        return policy_id

    def evaluate_session(self, session_id: str) -> list[str]:
        """Queue eligible nudges; disabled policies make this a no-op."""
        rows = self.conn.execute(
            """
            SELECT DISTINCT np.id, np.cooldown_seconds, np.max_per_session,
                   o.id, o.title, o.summary
            FROM nudge_policies np
            JOIN observations o ON o.rule_id = np.rule_id AND o.rule_version = np.rule_version
            JOIN observation_evidence oe ON oe.observation_id = o.id
            WHERE np.enabled = 1
              AND oe.session_id = ?
              AND o.status NOT IN ('dismissed', 'resolved', 'rejected', 'rolled_back')
            ORDER BY o.impact_score DESC
            """,
            (session_id,),
        ).fetchall()
        queued: list[str] = []
        now_dt = datetime.now(tz=UTC)
        now = now_dt.isoformat()
        for policy_id, cooldown_seconds, max_per_session, observation_id, title, summary in rows:
            prior = self.conn.execute(
                """
                SELECT COUNT(*), MAX(created_at) FROM nudges
                WHERE policy_id = ? AND session_id = ?
                """,
                (policy_id, session_id),
            ).fetchone()
            if int(prior[0] or 0) >= int(max_per_session):
                continue
            if prior[1]:
                last = datetime.fromisoformat(str(prior[1]).replace("Z", "+00:00"))
                if now_dt - last < timedelta(seconds=int(cooldown_seconds)):
                    continue
            nudge_id = f"nudge_{uuid.uuid4().hex}"
            message = f"Reflect signal: {title}. {summary} Review the linked evidence before changing course."
            self.conn.execute(
                """
                INSERT INTO nudges(
                  id, policy_id, session_id, observation_id, message_redacted, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?)
                """,
                (nudge_id, policy_id, session_id, observation_id, message, now),
            )
            queued.append(nudge_id)
        self.conn.commit()
        return queued

    def poll_for_hook(self, session_id: str, *, limit: int = 3) -> list[dict[str, Any]]:
        """Claim queued messages for the local opentelemetry-hooks integration."""
        rows = self.conn.execute(
            """
            SELECT id, observation_id, message_redacted, created_at
            FROM nudges
            WHERE session_id = ? AND status = 'queued'
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (session_id, max(1, min(limit, 5))),
        ).fetchall()
        if not rows:
            return []
        now = utc_now()
        self.conn.executemany(
            "UPDATE nudges SET status = 'delivered', delivered_at = ? WHERE id = ?",
            [(now, row[0]) for row in rows],
        )
        self.conn.commit()
        return [
            {
                "id": row[0],
                "observation_id": row[1],
                "message": row[2],
                "created_at": row[3],
                "transport": "opentelemetry_hooks_local_poll",
            }
            for row in rows
        ]


class HookNudgeBridge:
    """Narrow adapter exposed to opentelemetry-hooks; no policy logic lives in hooks."""

    def __init__(self, service: NudgeService):
        self.service = service

    def poll(self, session_id: str) -> str:
        return json.dumps(
            {"schema_version": 1, "session_id": session_id, "nudges": self.service.poll_for_hook(session_id)},
            sort_keys=True,
        )
