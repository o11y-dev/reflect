from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any

from reflect.improvements.repository import utc_now

_ARCHETYPES: dict[str, dict[str, Any]] = {
    "debugging": {
        "description": "Diagnose and repair a failure or unexpected behavior.",
        "terms": ("fail", "error", "debug", "fix", "broken", "exception", "traceback"),
    },
    "testing": {
        "description": "Add, run, or repair tests and validation.",
        "terms": ("test", "pytest", "ruff", "lint", "coverage", "validate"),
    },
    "release": {
        "description": "Prepare, validate, publish, or recover a release.",
        "terms": ("release", "changelog", "version", "publish", "deploy", "tag"),
    },
    "review": {
        "description": "Review a change, pull request, merge request, or design.",
        "terms": ("review", "pull request", "merge request", " pr ", " mr ", "comment"),
    },
    "documentation": {
        "description": "Create or update documentation and guidance.",
        "terms": ("readme", "documentation", " docs ", "guide", "spec"),
    },
    "research": {
        "description": "Explore a codebase or gather evidence before deciding.",
        "terms": ("research", "investigate", "explore", "find", "search", "understand"),
    },
    "implementation": {
        "description": "Build or modify product behavior.",
        "terms": ("implement", "build", "add", "create", "change", "refactor", "feature"),
    },
}


class TaskArchetypeService:
    """Deterministically classify sessions for comparable workflow cohorts."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def refresh(self) -> dict[str, int]:
        now = utc_now()
        for archetype_id, definition in _ARCHETYPES.items():
            self.conn.execute(
                """
                INSERT INTO task_archetypes(
                  id, name, description, matching_features_json,
                  lifecycle_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name = excluded.name,
                  description = excluded.description,
                  matching_features_json = excluded.matching_features_json,
                  updated_at = excluded.updated_at
                """,
                (
                    archetype_id,
                    archetype_id,
                    definition["description"],
                    json.dumps({"terms": definition["terms"]}, sort_keys=True),
                    now,
                    now,
                ),
            )
        rows = self.conn.execute(
            """
            SELECT s.id,
                   lower(
                     COALESCE(s.title, '') || ' ' ||
                     COALESCE(
                       (
                         SELECT GROUP_CONCAT(step_excerpt, ' ')
                         FROM (
                           SELECT substr(COALESCE(st.summary, ''), 1, 1000) AS step_excerpt
                           FROM steps st
                           WHERE st.session_id = s.id
                           ORDER BY st.seq
                           LIMIT 24
                         )
                       ),
                       ''
                     ) || ' ' ||
                     COALESCE(
                       (
                         SELECT GROUP_CONCAT(tool_name, ' ')
                         FROM (
                           SELECT tc.tool_name
                           FROM tool_calls tc
                           WHERE tc.session_id = s.id
                           ORDER BY tc.created_at
                           LIMIT 24
                         )
                       ),
                       ''
                     )
                   )
            FROM sessions s
            LEFT JOIN session_task_archetypes sta ON sta.session_id = s.id
            WHERE sta.session_id IS NULL OR sta.updated_at < s.updated_at
            """
        ).fetchall()
        classified = 0
        for session_id, text in rows:
            scores = Counter(
                {
                    archetype_id: sum(str(text).count(term) for term in definition["terms"])
                    for archetype_id, definition in _ARCHETYPES.items()
                }
            )
            archetype_id, score = scores.most_common(1)[0]
            if score <= 0:
                archetype_id = "implementation"
                confidence = 0.35
                matched_terms: list[str] = []
            else:
                matched_terms = [
                    term.strip()
                    for term in _ARCHETYPES[archetype_id]["terms"]
                    if term in str(text)
                ]
                confidence = min(0.95, 0.5 + score * 0.08)
            self.conn.execute(
                """
                INSERT INTO session_task_archetypes(
                  session_id, task_archetype_id, confidence, features_json,
                  classified_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  task_archetype_id = excluded.task_archetype_id,
                  confidence = excluded.confidence,
                  features_json = excluded.features_json,
                  classified_at = excluded.classified_at,
                  updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    archetype_id,
                    confidence,
                    json.dumps({"matched_terms": sorted(set(matched_terms))}),
                    now,
                    now,
                ),
            )
            classified += 1
        self.conn.commit()
        return {"classified": classified, "archetypes": len(_ARCHETYPES)}

    def dominant_for_observation(self, observation_id: str) -> str | None:
        row = self.conn.execute(
            """
            SELECT sta.task_archetype_id, COUNT(*) AS support
            FROM observation_evidence oe
            JOIN session_task_archetypes sta ON sta.session_id = oe.session_id
            WHERE oe.observation_id = ?
            GROUP BY sta.task_archetype_id
            ORDER BY support DESC, sta.task_archetype_id
            LIMIT 1
            """,
            (observation_id,),
        ).fetchone()
        return str(row[0]) if row else None


class WorkflowAdherenceService:
    """Track whether comparable sessions observed, invoked, or followed applied workflows."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def refresh(self) -> dict[str, int]:
        rows = self.conn.execute(
            """
            SELECT i.id, i.exposure_started_at, wc.id, wc.content_json,
                   o.repo_id, wc.task_archetype_id
            FROM interventions i
            JOIN workflow_versions wv ON wv.id = i.workflow_version_id
            JOIN workflow_candidates wc ON wc.id = wv.candidate_id
            JOIN observations o ON o.id = wc.observation_id
            WHERE i.status = 'active'
            """
        ).fetchall()
        upserted = 0
        now = utc_now()
        for intervention_id, exposed_at, candidate_id, content_json, repo_id, archetype_id in rows:
            content = json.loads(content_json)
            slug = str(content.get("slug") or "")
            sessions = self.conn.execute(
                """
                SELECT s.id,
                       EXISTS(
                         SELECT 1 FROM steps st
                         WHERE st.session_id = s.id AND lower(st.raw_attrs_json) LIKE ?
                       ) AS invoked,
                       EXISTS(
                         SELECT 1 FROM tool_calls tc
                         WHERE tc.session_id = s.id AND (
                           lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*pytest*'
                           OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*ruff*'
                           OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '* test*'
                           OR lower(COALESCE(tc.input_preview_redacted, '')) GLOB '*build*'
                         )
                       ) AS verified
                FROM sessions s
                JOIN session_task_archetypes sta ON sta.session_id = s.id
                WHERE s.started_at >= ?
                  AND COALESCE(s.repo_id, '') = COALESCE(?, '')
                  AND (? IS NULL OR sta.task_archetype_id = ?)
                ORDER BY s.started_at
                """,
                (f"%{slug.lower()}%", exposed_at, repo_id, archetype_id, archetype_id),
            ).fetchall()
            for session_id, invoked, verified in sessions:
                state = "followed" if invoked and verified else "invoked" if invoked else "ignored"
                exposure_id = f"exposure_{intervention_id}_{session_id}"
                self.conn.execute(
                    """
                    INSERT INTO workflow_exposures(
                      id, intervention_id, session_id, state, evidence_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(intervention_id, session_id) DO UPDATE SET
                      state = excluded.state,
                      evidence_json = excluded.evidence_json
                    """,
                    (
                        exposure_id,
                        intervention_id,
                        session_id,
                        state,
                        json.dumps({"candidate_id": candidate_id, "slug_observed": bool(invoked), "verification_observed": bool(verified)}),
                        now,
                    ),
                )
                upserted += 1
        self.conn.commit()
        return {"exposures": upserted}
