from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from reflect.improvements.models import (
    SkillDetail,
    SkillInstallationRecord,
    SkillLifecycleState,
    SkillMeasurementRecord,
    SkillOrigin,
    SkillRecord,
    SkillUsageSessionRecord,
    SkillVersionRecord,
    SkillVersionStatus,
)
from reflect.improvements.repository import ImprovementRepository, utc_now
from reflect.improvements.workflows import WorkflowService
from reflect.store.migrate import migrate

_SAFE_SKILL = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _skill_id(slug: str) -> str:
    return f"skill_{_hash(slug)[:24]}"


def _normalize_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")[:63]
    return slug if _SAFE_SKILL.fullmatch(slug) else ""


class SkillRegistryService:
    """Durable skill identity, version, evidence, installation, and usage registry."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        migrate(conn)
        self.repository = ImprovementRepository(conn)
        self.workflows = WorkflowService(conn)

    def refresh(
        self,
        *,
        scan_paths: Iterable[Path] = (),
        commit: bool = True,
    ) -> dict[str, int]:
        workflow_result = self.sync_workflow_candidates()
        filesystem_result = self.sync_paths(scan_paths)
        usage = self.sync_usage()
        measurements = self.sync_measurements()
        if commit:
            self.conn.commit()
        return {
            "workflow_skills": workflow_result["skills"],
            "filesystem_skills": filesystem_result["skills"],
            "installations": filesystem_result["installations"],
            "missing_installations": filesystem_result["missing"],
            "usage": usage,
            "measurements": measurements,
        }

    def sync_workflow_candidates(
        self,
        candidate_ids: Iterable[str] | None = None,
    ) -> dict[str, int]:
        wanted = {str(item) for item in candidate_ids or []}
        if wanted:
            candidates = self.repository.list_candidates(limit=500)
            candidates = [item for item in candidates if item.id in wanted]
        else:
            # The workflow service groups evidence-specific candidate rows by
            # reusable slug. Registry versions represent behavior changes,
            # while the grouped ledger retains all supporting observations.
            candidates = self.workflows.list(limit=500)
        tracked = 0
        versions = 0
        for candidate in candidates:
            source = candidate.content.get("source") or {}
            source_kind = str(
                source.get("kind") or candidate.provenance.get("source") or "rule_blueprint"
            )
            origin = self._origin_for(source_kind)
            rendered = self.workflows._render_skill(candidate)
            version_status, lifecycle = self._candidate_status(candidate.status.value)
            skill_id, version_id, created = self._track_version(
                slug=str(candidate.content.get("slug") or candidate.id),
                name=str(candidate.content.get("slug") or candidate.title),
                description=str(candidate.content.get("description") or candidate.hypothesis),
                origin=origin,
                content=rendered,
                workflow=candidate.content,
                source_kind=source_kind,
                source_agent=source.get("agent"),
                source_loop_id=source.get("loop_id"),
                source_workflow_id=source.get("workflow_id"),
                workflow_candidate_id=candidate.id,
                version_status=version_status,
                lifecycle=lifecycle,
                now=candidate.updated_at,
            )
            tracked += 1
            versions += int(created)
            ledger = self.repository.workflow_session_ledger(candidate.id, limit=200)
            for observation_id in ledger.observation_ids or [candidate.observation_id]:
                self._link_evidence(version_id, "observation", observation_id)
            if source.get("loop_id"):
                self._link_evidence(version_id, "loop", str(source["loop_id"]))
            if source.get("workflow_id"):
                self._link_evidence(version_id, "workflow", str(source["workflow_id"]))
            for source_session in ledger.source_sessions:
                self._link_evidence(version_id, "session", source_session.session_id)
            installation_rows = self.conn.execute(
                """
                SELECT i.target_path, i.applied_hash, i.status, i.created_at, i.updated_at
                FROM interventions i
                JOIN workflow_versions wv ON wv.id = i.workflow_version_id
                WHERE wv.candidate_id = ?
                ORDER BY i.created_at
                """,
                (candidate.id,),
            ).fetchall()
            for install in installation_rows:
                self._upsert_installation(
                    skill_id=skill_id,
                    version_id=version_id,
                    path=Path(str(install[0])),
                    installed_hash=install[1],
                    status=str(install[2]),
                    now=str(install[4] or install[3]),
                )
        return {"skills": tracked, "versions_created": versions}

    def sync_paths(self, paths: Iterable[Path]) -> dict[str, int]:
        roots = [Path(item).expanduser().resolve() for item in paths]
        files: set[Path] = set()
        for root in roots:
            if root.is_file() and root.name == "SKILL.md":
                files.add(root)
            elif (root / "SKILL.md").is_file():
                files.add(root / "SKILL.md")
            elif root.is_dir():
                files.update(path for path in root.glob("*/SKILL.md") if path.is_file())
        now = utc_now()
        seen_paths: set[str] = set()
        skills = 0
        installations = 0
        for path in sorted(files):
            parsed = self._parse_skill_file(path)
            if parsed is None:
                continue
            slug, description, content = parsed
            skill_id, version_id, _created = self._track_version(
                slug=slug,
                name=slug,
                description=description,
                origin=SkillOrigin.IMPORTED,
                content=content,
                workflow={},
                source_kind="filesystem",
                source_agent=None,
                source_loop_id=None,
                source_workflow_id=None,
                workflow_candidate_id=None,
                version_status=SkillVersionStatus.ACTIVE,
                lifecycle=SkillLifecycleState.ACTIVE,
                now=now,
            )
            resolved = str(path.resolve())
            seen_paths.add(resolved)
            self._upsert_installation(
                skill_id=skill_id,
                version_id=version_id,
                path=path,
                installed_hash=_hash(content),
                status="active",
                now=now,
            )
            skills += 1
            installations += 1
        missing = 0
        for row in self.conn.execute(
            "SELECT id, path FROM skill_installations WHERE status = 'active'"
        ).fetchall():
            installation_path = Path(str(row[1])).expanduser().resolve()
            in_scan_scope = any(
                installation_path == root or installation_path.is_relative_to(root)
                for root in roots
            )
            if in_scan_scope and str(installation_path) not in seen_paths:
                self.conn.execute(
                    "UPDATE skill_installations SET status = 'missing', updated_at = ? WHERE id = ?",
                    (now, row[0]),
                )
                missing += 1
        self.conn.execute(
            """
            UPDATE skills
            SET lifecycle_state = 'stale', updated_at = ?
            WHERE origin = 'imported' AND lifecycle_state = 'active'
              AND EXISTS (
                SELECT 1 FROM skill_installations WHERE skill_id = skills.id
              )
              AND NOT EXISTS (
                SELECT 1 FROM skill_installations
                WHERE skill_id = skills.id AND status = 'active'
              )
            """,
            (now,),
        )
        return {"skills": skills, "installations": installations, "missing": missing}

    def sync_usage(self) -> int:
        rows = self.conn.execute(
            """
            SELECT gn.label, ge.session_id, MAX(COALESCE(ge.last_seen_at, ge.updated_at)),
                   MAX(ge.weight)
            FROM graph_edges ge
            JOIN graph_nodes gn ON gn.id = ge.target_node_id
            WHERE ge.kind = 'used_skill'
              AND gn.kind = 'Skill'
              AND ge.session_id IS NOT NULL
            GROUP BY gn.label, ge.session_id
            """
        ).fetchall()
        now = utc_now()
        upserted = 0
        for label, session_id, observed_at, weight in rows:
            slug = _normalize_slug(str(label))
            if not slug:
                continue
            skill_id = _skill_id(slug)
            self._upsert_skill(
                skill_id=skill_id,
                slug=slug,
                name=str(label),
                description="Observed in local agent-session telemetry.",
                origin=SkillOrigin.IMPORTED,
                lifecycle=SkillLifecycleState.STALE,
                current_version_id=None,
                now=str(observed_at or now),
            )
            version_row = self.conn.execute(
                "SELECT current_version_id FROM skills WHERE id = ?",
                (skill_id,),
            ).fetchone()
            version_id = str(version_row[0]) if version_row and version_row[0] else None
            usage_id = f"skill_usage_{_hash(f'{skill_id}:{session_id}')[:24]}"
            timestamp = str(observed_at or now)
            self.conn.execute(
                """
                INSERT INTO skill_usage(
                  id, skill_id, skill_version_id, session_id, state, confidence,
                  evidence_json, observed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'observed', ?, ?, ?, ?, ?)
                ON CONFLICT(skill_id, session_id) DO UPDATE SET
                  skill_version_id = excluded.skill_version_id,
                  confidence = MAX(skill_usage.confidence, excluded.confidence),
                  evidence_json = excluded.evidence_json,
                  observed_at = excluded.observed_at,
                  updated_at = excluded.updated_at
                """,
                (
                    usage_id,
                    skill_id,
                    version_id,
                    str(session_id),
                    min(0.95, 0.55 + float(weight or 1) * 0.05),
                    _json({"source": "behavioral_memory_graph", "edge_kind": "used_skill"}),
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            upserted += 1
        return upserted

    def sync_measurements(self) -> int:
        rows = self.conn.execute(
            """
            SELECT m.id, sv.skill_id, sv.id, m.metric_name, m.before_value,
                   m.after_value, m.verdict, m.confidence, m.measured_at,
                   m.cohort_json, m.before_count, m.after_count, m.delta
            FROM measurements m
            JOIN interventions i ON i.id = m.intervention_id
            JOIN workflow_versions wv ON wv.id = i.workflow_version_id
            JOIN skill_versions sv ON sv.id = (
              SELECT latest.id FROM skill_versions latest
              WHERE latest.workflow_candidate_id = wv.candidate_id
              ORDER BY latest.version DESC LIMIT 1
            )
            ORDER BY m.measured_at
            """
        ).fetchall()
        synced = 0
        for row in rows:
            measurement_id = f"skill_measurement_{_hash(str(row[0]))[:24]}"
            details = {
                "source_measurement_id": str(row[0]),
                "cohort": _loads(row[9], {}),
                "before_count": int(row[10]),
                "after_count": int(row[11]),
                "delta": row[12],
            }
            self.conn.execute(
                """
                INSERT INTO skill_measurements(
                  id, skill_id, skill_version_id, metric_name, before_value,
                  after_value, verdict, confidence, measured_at, details_json,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  skill_id = excluded.skill_id,
                  skill_version_id = excluded.skill_version_id,
                  before_value = excluded.before_value,
                  after_value = excluded.after_value,
                  verdict = excluded.verdict,
                  confidence = excluded.confidence,
                  measured_at = excluded.measured_at,
                  details_json = excluded.details_json,
                  updated_at = excluded.updated_at
                """,
                (
                    measurement_id,
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                    row[4],
                    row[5],
                    str(row[6]),
                    float(row[7]),
                    str(row[8]),
                    _json(details),
                    str(row[8]),
                    utc_now(),
                ),
            )
            synced += 1
        return synced

    def list(
        self,
        *,
        lifecycle: SkillLifecycleState | None = None,
        include_stale: bool = True,
        limit: int = 100,
    ) -> list[SkillRecord]:
        if lifecycle:
            where = "WHERE s.lifecycle_state = ?"
            params: tuple[Any, ...] = (lifecycle.value,)
        elif not include_stale:
            where = "WHERE s.lifecycle_state <> ?"
            params = (SkillLifecycleState.STALE.value,)
        else:
            where = ""
            params = ()
        rows = self.conn.execute(
            f"""
            SELECT s.id, s.slug, s.name, s.description, s.origin, s.lifecycle_state,
                   s.current_version_id,
                   (SELECT COUNT(DISTINCT CASE WHEN cv.workflow_json <> '{{}}' THEN cv.workflow_json ELSE cv.content_hash END) FROM skill_versions cv WHERE cv.skill_id = s.id),
                   sv.status, sv.source_agent,
                   (SELECT COUNT(DISTINCT CASE WHEN v.workflow_json <> '{{}}' THEN v.workflow_json ELSE v.content_hash END) FROM skill_versions v WHERE v.skill_id = s.id),
                   (SELECT COUNT(DISTINCT e.entity_type || ':' || e.entity_id || ':' || e.relationship) FROM skill_evidence e JOIN skill_versions ev ON ev.id = e.skill_version_id WHERE ev.skill_id = s.id),
                   (SELECT COUNT(*) FROM skill_installations i WHERE i.skill_id = s.id AND i.status = 'active'),
                   (SELECT COUNT(*) FROM skill_usage u WHERE u.skill_id = s.id),
                   (SELECT COUNT(*) FROM skill_measurements m WHERE m.skill_id = s.id),
                   s.first_seen_at, s.last_seen_at, s.updated_at
            FROM skills s
            LEFT JOIN skill_versions sv ON sv.id = s.current_version_id
            {where}
            ORDER BY CASE s.lifecycle_state WHEN 'active' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                     s.updated_at DESC, s.slug
            LIMIT ?
            """,
            (*params, max(1, min(limit, 500))),
        ).fetchall()
        records = [self._skill_record(row) for row in rows]
        if not records:
            return records
        placeholders = ",".join("?" for _ in records)
        version_rows = self.conn.execute(
            f"""
            SELECT id, skill_id, content_hash, workflow_json
            FROM skill_versions
            WHERE skill_id IN ({placeholders})
            ORDER BY skill_id, version DESC
            """,
            tuple(item.id for item in records),
        ).fetchall()
        versions_by_skill: dict[str, list[tuple[str, str, str]]] = {}
        for version_id, skill_id, content_hash, workflow_json in version_rows:
            versions_by_skill.setdefault(str(skill_id), []).append(
                (str(version_id), str(content_hash), str(workflow_json))
            )
        installation_rows = self.conn.execute(
            f"""
            SELECT skill_id, target_kind
            FROM skill_installations
            WHERE status = 'active' AND skill_id IN ({placeholders})
            ORDER BY skill_id, target_kind
            """,
            tuple(item.id for item in records),
        ).fetchall()
        targets_by_skill: dict[str, list[str]] = {}
        for skill_id, target_kind in installation_rows:
            targets_by_skill.setdefault(str(skill_id), []).append(str(target_kind))
        return [
            self._with_installation_targets(
                self._with_semantic_version_summary(
                    item,
                    versions_by_skill.get(item.id, []),
                ),
                targets_by_skill.get(item.id, []),
            )
            for item in records
        ]

    def counts_by_lifecycle(self) -> dict[str, int]:
        return {
            str(state): int(count)
            for state, count in self.conn.execute(
                "SELECT lifecycle_state, COUNT(*) FROM skills GROUP BY lifecycle_state"
            ).fetchall()
        }

    def show(self, skill_id_or_slug: str) -> SkillDetail:
        row = self.conn.execute(
            """
            SELECT s.id, s.slug, s.name, s.description, s.origin, s.lifecycle_state,
                   s.current_version_id,
                   (SELECT COUNT(DISTINCT CASE WHEN cv.workflow_json <> '{}' THEN cv.workflow_json ELSE cv.content_hash END) FROM skill_versions cv WHERE cv.skill_id = s.id),
                   sv.status, sv.source_agent,
                   (SELECT COUNT(DISTINCT CASE WHEN v.workflow_json <> '{}' THEN v.workflow_json ELSE v.content_hash END) FROM skill_versions v WHERE v.skill_id = s.id),
                   (SELECT COUNT(DISTINCT e.entity_type || ':' || e.entity_id || ':' || e.relationship) FROM skill_evidence e JOIN skill_versions ev ON ev.id = e.skill_version_id WHERE ev.skill_id = s.id),
                   (SELECT COUNT(*) FROM skill_installations i WHERE i.skill_id = s.id AND i.status = 'active'),
                   (SELECT COUNT(*) FROM skill_usage u WHERE u.skill_id = s.id),
                   (SELECT COUNT(*) FROM skill_measurements m WHERE m.skill_id = s.id),
                   s.first_seen_at, s.last_seen_at, s.updated_at
            FROM skills s
            LEFT JOIN skill_versions sv ON sv.id = s.current_version_id
            WHERE s.id = ? OR s.slug = ?
            """,
            (skill_id_or_slug, skill_id_or_slug),
        ).fetchone()
        if row is None:
            raise KeyError(f"Skill not found: {skill_id_or_slug}")
        skill = self._skill_record(row)
        version_rows = self.conn.execute(
            """
            SELECT id, skill_id, version, content_markdown, content_hash,
                   workflow_json, source_kind, source_agent, source_loop_id,
                   source_workflow_id, workflow_candidate_id, status, created_at, updated_at
            FROM skill_versions WHERE skill_id = ? ORDER BY version DESC
            """,
            (skill.id,),
        ).fetchall()
        installation_rows = self.conn.execute(
            """
            SELECT id, skill_id, skill_version_id, target_kind, target_ref, path,
                   installed_hash, status, first_seen_at, last_seen_at
            FROM skill_installations WHERE skill_id = ? ORDER BY last_seen_at DESC
            """,
            (skill.id,),
        ).fetchall()
        evidence_rows = self.conn.execute(
            """
            SELECT e.skill_version_id, e.entity_type, e.entity_id, e.relationship, e.confidence
            FROM skill_evidence e JOIN skill_versions sv ON sv.id = e.skill_version_id
            WHERE sv.skill_id = ?
            ORDER BY sv.version DESC, e.entity_type, e.entity_id
            LIMIT 500
            """,
            (skill.id,),
        ).fetchall()
        usage_rows = self.conn.execute(
            """
            SELECT u.session_id, u.skill_version_id, u.state, u.outcome,
                   u.confidence, u.evidence_json, u.observed_at,
                   s.title, a.name, s.started_at, s.status, w.root_path
            FROM skill_usage u
            JOIN sessions s ON s.id = u.session_id
            LEFT JOIN agents a ON a.id = s.agent_id
            LEFT JOIN workspaces w ON w.id = s.workspace_id
            WHERE u.skill_id = ?
            ORDER BY u.observed_at DESC, u.session_id
            LIMIT 200
            """,
            (skill.id,),
        ).fetchall()
        measurement_rows = self.conn.execute(
            """
            SELECT id, skill_id, skill_version_id, metric_name, before_value,
                   after_value, verdict, confidence, measured_at, details_json
            FROM skill_measurements WHERE skill_id = ? ORDER BY measured_at DESC
            LIMIT 500
            """,
            (skill.id,),
        ).fetchall()
        semantic_versions = self._semantic_versions(version_rows)
        skill = self._with_semantic_version_summary(
            skill,
            [(str(item[0]), str(item[4]), str(item[5])) for item in version_rows],
        )
        skill = self._with_installation_targets(
            skill,
            [str(item[3]) for item in installation_rows if str(item[7]) == "active"],
        )
        return SkillDetail(
            skill=skill,
            versions=semantic_versions,
            installations=[
                SkillInstallationRecord(
                    id=str(item[0]),
                    skill_id=str(item[1]),
                    skill_version_id=item[2],
                    target_kind=str(item[3]),
                    target_ref=str(item[4]),
                    path=str(item[5]),
                    installed_hash=item[6],
                    status=str(item[7]),
                    first_seen_at=str(item[8]),
                    last_seen_at=str(item[9]),
                )
                for item in installation_rows
            ],
            evidence=[
                {
                    "skill_version_id": str(item[0]),
                    "entity_type": str(item[1]),
                    "entity_id": str(item[2]),
                    "relationship": str(item[3]),
                    "confidence": float(item[4]),
                }
                for item in evidence_rows
            ],
            usage_sessions=[
                SkillUsageSessionRecord(
                    session_id=str(item[0]),
                    skill_version_id=item[1],
                    state=str(item[2]),
                    outcome=item[3],
                    confidence=float(item[4]),
                    evidence=_loads(item[5], {}),
                    observed_at=str(item[6]),
                    title=item[7],
                    agent=item[8],
                    started_at=str(item[9]),
                    status=str(item[10]),
                    workspace=item[11],
                )
                for item in usage_rows
            ],
            measurements=[
                SkillMeasurementRecord(
                    id=str(item[0]),
                    skill_id=str(item[1]),
                    skill_version_id=item[2],
                    metric_name=str(item[3]),
                    before_value=item[4],
                    after_value=item[5],
                    verdict=str(item[6]),
                    confidence=float(item[7]),
                    measured_at=str(item[8]),
                    details=_loads(item[9], {}),
                )
                for item in measurement_rows
            ],
        )

    def workflow_candidate_for(self, skill_id_or_slug: str) -> str:
        detail = self.show(skill_id_or_slug)
        candidate = next(
            (item.workflow_candidate_id for item in detail.versions if item.workflow_candidate_id),
            None,
        )
        if not candidate:
            raise RuntimeError(
                f"Skill {detail.skill.slug} is tracked from the filesystem or telemetry and has no pending Reflect workflow to apply"
            )
        return candidate

    def skill_for_candidate(self, candidate_id: str) -> SkillRecord:
        row = self.conn.execute(
            """
            SELECT s.slug FROM skills s JOIN skill_versions sv ON sv.skill_id = s.id
            WHERE sv.workflow_candidate_id = ?
            ORDER BY sv.version DESC LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"No registered skill for workflow candidate: {candidate_id}")
        return self.show(str(row[0])).skill

    def _track_version(
        self,
        *,
        slug: str,
        name: str,
        description: str,
        origin: SkillOrigin,
        content: str,
        workflow: dict[str, Any],
        source_kind: str,
        source_agent: str | None,
        source_loop_id: str | None,
        source_workflow_id: str | None,
        workflow_candidate_id: str | None,
        version_status: SkillVersionStatus,
        lifecycle: SkillLifecycleState,
        now: str,
    ) -> tuple[str, str, bool]:
        normalized_slug = _normalize_slug(slug)
        if not normalized_slug:
            raise ValueError(f"Unsafe skill slug: {slug!r}")
        skill_id = _skill_id(normalized_slug)
        content_hash = _hash(content)
        # Establish the stable skill identity before inserting a version. The
        # current version remains unset until the child row exists so both
        # sides of the foreign-key relationship are valid at every step.
        self._upsert_skill(
            skill_id=skill_id,
            slug=normalized_slug,
            name=name,
            description=description,
            origin=origin,
            lifecycle=lifecycle,
            current_version_id=None,
            now=now,
        )
        existing = self.conn.execute(
            "SELECT id FROM skill_versions WHERE skill_id = ? AND content_hash = ?",
            (skill_id, content_hash),
        ).fetchone()
        if existing:
            version_id = str(existing[0])
            self.conn.execute(
                """
                UPDATE skill_versions
                SET workflow_json = CASE WHEN ? <> '{}' THEN ? ELSE workflow_json END,
                    source_kind = CASE
                      WHEN source_kind = 'filesystem' OR ? <> 'filesystem' THEN ?
                      ELSE source_kind
                    END,
                    source_agent = COALESCE(?, source_agent),
                    source_loop_id = COALESCE(?, source_loop_id),
                    source_workflow_id = COALESCE(?, source_workflow_id),
                    workflow_candidate_id = COALESCE(?, workflow_candidate_id),
                    status = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    _json(workflow),
                    _json(workflow),
                    source_kind,
                    source_kind,
                    source_agent,
                    source_loop_id,
                    source_workflow_id,
                    workflow_candidate_id,
                    version_status.value,
                    now,
                    version_id,
                ),
            )
            created = False
        else:
            version = int(
                self.conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM skill_versions WHERE skill_id = ?",
                    (skill_id,),
                ).fetchone()[0]
            )
            version_id = f"skill_version_{_hash(f'{skill_id}:{content_hash}')[:24]}"
            self.conn.execute(
                """
                INSERT INTO skill_versions(
                  id, skill_id, version, content_markdown, content_hash, workflow_json,
                  source_kind, source_agent, source_loop_id, source_workflow_id,
                  workflow_candidate_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    skill_id,
                    version,
                    content,
                    content_hash,
                    _json(workflow),
                    source_kind,
                    source_agent,
                    source_loop_id,
                    source_workflow_id,
                    workflow_candidate_id,
                    version_status.value,
                    now,
                    now,
                ),
            )
            created = True
        current_row = self.conn.execute(
            """
            SELECT s.current_version_id, sv.status
            FROM skills s LEFT JOIN skill_versions sv ON sv.id = s.current_version_id
            WHERE s.id = ?
            """,
            (skill_id,),
        ).fetchone()
        status_priority = {
            "active": 0,
            "pending": 1,
            "stale": 2,
            "rejected": 3,
            "rolled_back": 4,
            "superseded": 5,
        }
        current_version_id = version_id
        if (
            current_row
            and current_row[0]
            and status_priority.get(str(current_row[1]), 9)
            < status_priority.get(version_status.value, 9)
        ):
            current_version_id = None
        self._upsert_skill(
            skill_id=skill_id,
            slug=normalized_slug,
            name=name,
            description=description,
            origin=origin,
            lifecycle=lifecycle,
            current_version_id=current_version_id,
            now=now,
        )
        return skill_id, version_id, created

    def _upsert_skill(
        self,
        *,
        skill_id: str,
        slug: str,
        name: str,
        description: str,
        origin: SkillOrigin,
        lifecycle: SkillLifecycleState,
        current_version_id: str | None,
        now: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO skills(
              id, slug, name, description, origin, lifecycle_state,
              current_version_id, first_seen_at, last_seen_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name = CASE WHEN excluded.description <> '' THEN excluded.name ELSE skills.name END,
              description = CASE WHEN excluded.description <> '' THEN excluded.description ELSE skills.description END,
              origin = CASE
                WHEN skills.origin = 'imported' AND excluded.origin <> 'imported' THEN excluded.origin
                ELSE skills.origin
              END,
              lifecycle_state = CASE
                WHEN excluded.lifecycle_state = 'active' THEN 'active'
                WHEN skills.lifecycle_state = 'active' THEN skills.lifecycle_state
                ELSE excluded.lifecycle_state
              END,
              current_version_id = COALESCE(excluded.current_version_id, skills.current_version_id),
              last_seen_at = excluded.last_seen_at,
              updated_at = excluded.updated_at
            """,
            (
                skill_id,
                slug,
                name,
                description,
                origin.value,
                lifecycle.value,
                current_version_id,
                now,
                now,
                now,
                now,
            ),
        )

    def _link_evidence(
        self,
        version_id: str,
        entity_type: str,
        entity_id: str,
        *,
        relationship: str = "supporting",
        confidence: float = 1.0,
    ) -> None:
        evidence_id = (
            f"skill_evidence_{_hash(f'{version_id}:{entity_type}:{entity_id}:{relationship}')[:24]}"
        )
        self.conn.execute(
            """
            INSERT INTO skill_evidence(
              id, skill_version_id, entity_type, entity_id, relationship, confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(skill_version_id, entity_type, entity_id, relationship) DO UPDATE SET
              confidence = MAX(skill_evidence.confidence, excluded.confidence)
            """,
            (evidence_id, version_id, entity_type, entity_id, relationship, confidence, utc_now()),
        )

    def _upsert_installation(
        self,
        *,
        skill_id: str,
        version_id: str | None,
        path: Path,
        installed_hash: str | None,
        status: str,
        now: str,
    ) -> None:
        resolved = path.expanduser().resolve()
        target_kind, target_ref = self._installation_target(resolved)
        installation_id = f"skill_installation_{_hash(str(resolved))[:24]}"
        self.conn.execute(
            """
            INSERT INTO skill_installations(
              id, skill_id, skill_version_id, target_kind, target_ref, path,
              installed_hash, status, first_seen_at, last_seen_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
              skill_id = excluded.skill_id,
              skill_version_id = excluded.skill_version_id,
              installed_hash = excluded.installed_hash,
              status = excluded.status,
              last_seen_at = excluded.last_seen_at,
              updated_at = excluded.updated_at
            """,
            (
                installation_id,
                skill_id,
                version_id,
                target_kind,
                target_ref,
                str(resolved),
                installed_hash,
                status,
                now,
                now,
                now,
                now,
            ),
        )

    @staticmethod
    def _parse_skill_file(path: Path) -> tuple[str, str, str] | None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not text.startswith("---\n"):
            return None
        end = text.find("\n---\n", 4)
        if end < 0:
            return None
        metadata: dict[str, str] = {}
        for line in text[4:end].splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in {"name", "description"}:
                raw = value.strip()
                try:
                    metadata[key.strip()] = (
                        str(json.loads(raw)) if raw.startswith(('"', "'")) else raw
                    )
                except json.JSONDecodeError:
                    metadata[key.strip()] = raw.strip("'\"")
        slug = _normalize_slug(metadata.get("name") or path.parent.name)
        description = metadata.get("description", "").strip()
        if not slug or not description:
            return None
        return slug, description, text

    @staticmethod
    def _installation_target(path: Path) -> tuple[str, str]:
        parts = path.parts
        for marker, kind in (
            (".codex", "codex"),
            (".claude", "claude"),
            (".cursor", "cursor"),
            (".agents", "repository"),
        ):
            if marker in parts:
                index = parts.index(marker)
                return kind, str(Path(*parts[:index]) or Path("/"))
        return "filesystem", str(path.parent)

    @staticmethod
    def _origin_for(source_kind: str) -> SkillOrigin:
        if source_kind in {"agent_authored", "skill_extraction"}:
            return SkillOrigin.AGENT_AUTHORED
        if source_kind in {"manual_skill_file", "filesystem"}:
            return SkillOrigin.IMPORTED
        return SkillOrigin.RULE_BLUEPRINT

    @staticmethod
    def _candidate_status(status: str) -> tuple[SkillVersionStatus, SkillLifecycleState]:
        mapping = {
            "active": (SkillVersionStatus.ACTIVE, SkillLifecycleState.ACTIVE),
            "pending": (SkillVersionStatus.PENDING, SkillLifecycleState.PENDING),
            "approved": (SkillVersionStatus.PENDING, SkillLifecycleState.PENDING),
            "stale": (SkillVersionStatus.STALE, SkillLifecycleState.STALE),
            "rejected": (SkillVersionStatus.REJECTED, SkillLifecycleState.REJECTED),
            "rolled_back": (SkillVersionStatus.ROLLED_BACK, SkillLifecycleState.RETIRED),
        }
        return mapping.get(status, (SkillVersionStatus.PENDING, SkillLifecycleState.PENDING))

    @staticmethod
    def _skill_record(row: sqlite3.Row | tuple) -> SkillRecord:
        return SkillRecord(
            id=str(row[0]),
            slug=str(row[1]),
            name=str(row[2]),
            description=str(row[3]),
            origin=SkillOrigin(str(row[4])),
            lifecycle_state=SkillLifecycleState(str(row[5])),
            current_version_id=row[6],
            current_version=int(row[7]) if row[7] is not None else None,
            current_version_status=(
                SkillVersionStatus(str(row[8])) if row[8] is not None else None
            ),
            source_agent=row[9],
            version_count=int(row[10]),
            evidence_count=int(row[11]),
            installation_count=int(row[12]),
            usage_count=int(row[13]),
            measurement_count=int(row[14]),
            first_seen_at=str(row[15]),
            last_seen_at=str(row[16]),
            updated_at=str(row[17]),
        )

    @staticmethod
    def _version_record(row: sqlite3.Row | tuple) -> SkillVersionRecord:
        return SkillVersionRecord(
            id=str(row[0]),
            skill_id=str(row[1]),
            version=int(row[2]),
            content_markdown=str(row[3]),
            content_hash=str(row[4]),
            workflow=_loads(row[5], {}),
            source_kind=str(row[6]),
            source_agent=row[7],
            source_loop_id=row[8],
            source_workflow_id=row[9],
            workflow_candidate_id=row[10],
            status=SkillVersionStatus(str(row[11])),
            created_at=str(row[12]),
            updated_at=str(row[13]),
        )

    @staticmethod
    def _semantic_version_key(workflow_json: str, content_hash: str) -> str:
        workflow = _loads(workflow_json, {})
        if not workflow:
            return content_hash
        behavior = {
            key: workflow[key]
            for key in (
                "schema_version",
                "slug",
                "behavior_type",
                "suggested_artifact",
                "steps",
                "abstain_when",
                "verification",
                "source_markdown",
            )
            if key in workflow
        }
        return _json(behavior)

    @classmethod
    def _with_semantic_version_summary(
        cls,
        skill: SkillRecord,
        rows: list[tuple[str, str, str]],
    ) -> SkillRecord:
        ordered_keys: list[str] = []
        version_keys: dict[str, str] = {}
        for version_id, content_hash, workflow_json in rows:
            key = cls._semantic_version_key(workflow_json, content_hash)
            version_keys[version_id] = key
            if key not in ordered_keys:
                ordered_keys.append(key)
        total = len(ordered_keys)
        semantic_numbers = {
            key: total - index for index, key in enumerate(ordered_keys)
        }
        current_key = version_keys.get(str(skill.current_version_id or ""))
        return skill.model_copy(
            update={
                "current_version": semantic_numbers.get(current_key) if current_key else None,
                "version_count": total,
            }
        )

    @staticmethod
    def _with_installation_targets(
        skill: SkillRecord,
        target_kinds: list[str],
    ) -> SkillRecord:
        return skill.model_copy(
            update={"installation_targets": sorted(set(target_kinds))}
        )

    @classmethod
    def _semantic_versions(
        cls,
        rows: list[sqlite3.Row | tuple],
    ) -> list[SkillVersionRecord]:
        selected: list[sqlite3.Row | tuple] = []
        seen: set[str] = set()
        for row in rows:
            key = cls._semantic_version_key(str(row[5]), str(row[4]))
            if key in seen:
                continue
            seen.add(key)
            selected.append(row)
        total = len(selected)
        return [
            cls._version_record(row).model_copy(update={"version": total - index})
            for index, row in enumerate(selected)
        ]
