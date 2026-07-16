from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import Any

from reflect.improvements.models import WorkflowCandidateRecord
from reflect.improvements.repository import ImprovementRepository, utc_now

_SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class WorkflowService:
    """Review, render, apply, and safely roll back workflow candidates."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.repository = ImprovementRepository(conn)

    def list(
        self,
        *,
        limit: int = 100,
        behavior_types: set[str] | None = None,
        statuses: set[str] | None = None,
    ) -> list[WorkflowCandidateRecord]:
        candidates = self.repository.list_candidates(limit=500)
        if behavior_types:
            candidates = [
                candidate
                for candidate in candidates
                if str(candidate.content.get("behavior_type") or "proven_pattern") in behavior_types
            ]
        grouped: dict[str, list[WorkflowCandidateRecord]] = {}
        for candidate in candidates:
            slug = str(candidate.content.get("slug") or candidate.id)
            grouped.setdefault(slug, []).append(candidate)

        reviewable: list[WorkflowCandidateRecord] = []
        for members in grouped.values():
            eligible = members
            if statuses:
                if statuses == {"pending"} and any(item.status.value == "active" for item in members):
                    continue
                eligible = [item for item in members if item.status.value in statuses]
            if not eligible:
                continue
            representative = min(eligible, key=self._representative_sort_key)
            ledger = self.repository.workflow_session_ledger(representative.id, limit=1)
            ledger_observation_ids = set(ledger.observation_ids)
            evidence_members = [
                item for item in members if item.observation_id in ledger_observation_ids
            ] or [representative]
            reviewable.append(
                representative.model_copy(
                    update={
                        "support_count": ledger.source_session_count,
                        "confidence": max(item.confidence for item in evidence_members),
                        "variant_count": len(evidence_members),
                        "supporting_observation_count": len(ledger_observation_ids),
                        "source_scopes": sorted({item.scope for item in evidence_members}),
                    }
                )
            )
        reviewable.sort(key=self._representative_sort_key)
        return reviewable[:limit]

    def show(self, candidate_id: str) -> WorkflowCandidateRecord:
        candidate = self.repository.get_candidate(candidate_id)
        if candidate is None:
            raise KeyError(f"Workflow candidate not found: {candidate_id}")
        slug = str(candidate.content.get("slug") or candidate.id)
        members = [
            item
            for item in self.repository.list_candidates(limit=500)
            if str(item.content.get("slug") or item.id) == slug
        ]
        ledger = self.repository.workflow_session_ledger(candidate.id, limit=1)
        ledger_observation_ids = set(ledger.observation_ids)
        evidence_members = [
            item for item in members if item.observation_id in ledger_observation_ids
        ] or [candidate]
        return candidate.model_copy(
            update={
                "support_count": ledger.source_session_count,
                "confidence": max(item.confidence for item in evidence_members),
                "variant_count": len(evidence_members),
                "supporting_observation_count": max(1, len(ledger_observation_ids)),
                "source_scopes": sorted({item.scope for item in evidence_members}),
            }
        )

    @staticmethod
    def _representative_sort_key(candidate: WorkflowCandidateRecord) -> tuple[int, float, int, str]:
        status_priority = {
            "active": 0,
            "approved": 1,
            "pending": 2,
            "stale": 3,
            "rejected": 4,
            "rolled_back": 5,
        }
        return (
            status_priority.get(candidate.status.value, 9),
            -candidate.confidence,
            -candidate.support_count,
            candidate.id,
        )

    def preview(self, candidate_id: str, *, project_root: Path) -> dict[str, Any]:
        """Render the exact repository-local change without mutating the filesystem."""
        candidate = self.show(candidate_id)
        content = self._render_skill(candidate)
        root, target = self._target_for(candidate, project_root)
        previous_content = target.read_text(encoding="utf-8") if target.exists() else ""
        diff = "".join(
            difflib.unified_diff(
                previous_content.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=str(target),
                tofile=str(target),
            )
        )
        ledger = self.repository.workflow_session_ledger(candidate_id, limit=50)
        workspace_counts = Counter(
            item.workspace for item in ledger.source_sessions if item.workspace
        )
        suggested_roots = [
            {
                "path": workspace,
                "source_sessions": count,
                "is_repository": (Path(workspace) / ".git").exists(),
            }
            for workspace, count in workspace_counts.most_common()
        ]
        evidence_repository_paths = {
            str(Path(item["path"]).expanduser().resolve())
            for item in suggested_roots
            if item["is_repository"]
        }
        checks = self._target_checks(candidate, root=root, target=target)
        evidence_scope_match = (
            str(root) in evidence_repository_paths if evidence_repository_paths else None
        )
        checks["evidence_scope_match"] = evidence_scope_match
        checks["advisories"] = []
        if evidence_scope_match is False:
            checks["advisories"].append(
                "This application repository differs from the repositories visible in the source evidence."
            )
        return {
            "candidate_id": candidate_id,
            "project_root": str(root),
            "application_repository": str(root),
            "target_path": str(target),
            "target_relative_path": str(target.relative_to(root)),
            "change_kind": "no_change" if previous_content == content else "update" if target.exists() else "create",
            "previous_hash": _content_hash(previous_content) if target.exists() else None,
            "proposed_hash": _content_hash(content),
            "would_change": previous_content != content,
            "diff": diff,
            "content": content,
            "checks": checks,
            "suggested_project_roots": suggested_roots,
            "evidence_repository_paths": sorted(evidence_repository_paths),
            "source_session_count": ledger.source_session_count,
            "exposure_session_count": ledger.exposure_session_count,
        }

    def apply(self, candidate_id: str, *, project_root: Path) -> dict[str, Any]:
        candidate = self.show(candidate_id)
        if candidate.status.value in {"stale", "rejected", "rolled_back"}:
            raise RuntimeError(f"Workflow {candidate_id} is {candidate.status.value}; review it before applying")
        content = self._render_skill(candidate)
        root, target = self._target_for(candidate, project_root)

        active = self.conn.execute(
            """
            SELECT i.id, i.target_path, i.previous_hash, i.applied_hash
            FROM interventions i
            JOIN workflow_versions wv ON wv.id = i.workflow_version_id
            WHERE wv.candidate_id = ? AND i.status = 'active'
            ORDER BY i.created_at DESC LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
        if active:
            intervention_id, active_path, previous_hash, active_hash = active
            if Path(str(active_path)).resolve() != target.resolve():
                raise RuntimeError(
                    f"Workflow {candidate_id} is already active at {active_path}; roll it back before changing scope"
                )
            current_content = target.read_text(encoding="utf-8") if target.exists() else None
            current_hash = _content_hash(current_content) if current_content is not None else None
            proposed_hash = _content_hash(content)
            if current_hash != active_hash:
                self._mark_stale(candidate_id, str(intervention_id), now=utc_now())
                self.conn.commit()
                raise RuntimeError(
                    "The active workflow file differs from Reflect's applied version; review or roll back manually"
                )
            if proposed_hash != active_hash:
                raise RuntimeError(
                    "The active workflow candidate changed after application; roll it back before applying the new version"
                )
            return {
                "candidate_id": candidate_id,
                "intervention_id": str(intervention_id),
                "target_path": str(target),
                "previous_hash": previous_hash,
                "applied_hash": active_hash,
                "status": "active",
                "idempotent": True,
            }

        checks = self._target_checks(candidate, root=root, target=target)
        if not checks["apply_allowed"]:
            raise RuntimeError("; ".join(checks["issues"]))

        previous_content = target.read_text(encoding="utf-8") if target.exists() else None
        previous_hash = _content_hash(previous_content) if previous_content is not None else None
        applied_hash = _content_hash(content)
        now = utc_now()
        version_row = self.conn.execute(
            """
            SELECT id FROM workflow_versions
            WHERE candidate_id = ? AND content_hash = ?
            """,
            (candidate_id, applied_hash),
        ).fetchone()
        if version_row:
            version_id = str(version_row[0])
        else:
            version = int(
                self.conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM workflow_versions WHERE candidate_id = ?",
                    (candidate_id,),
                ).fetchone()[0]
            )
            version_id = f"workflow_version_{uuid.uuid4().hex}"
            self.conn.execute(
                """
                INSERT INTO workflow_versions(
                  id, candidate_id, version, content_json, content_hash,
                  render_targets_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, '["skill"]', 'approved', ?)
                """,
                (version_id, candidate_id, version, json.dumps(candidate.content, sort_keys=True), applied_hash, now),
            )

        self._atomic_write(target, content)
        intervention_id = f"intervention_{uuid.uuid4().hex}"
        try:
            evaluation_id = f"evaluation_{hashlib.sha256((version_id + ':artifact-integrity').encode()).hexdigest()[:24]}"
            self.conn.execute(
                """
                INSERT INTO evaluations(
                  id, workflow_version_id, name, kind, input_json, expected_json,
                  status, result_json, last_run_at, created_at, updated_at
                ) VALUES (?, ?, 'rendered_artifact_integrity', 'deterministic', ?, ?, 'passed', ?, ?, ?, ?)
                ON CONFLICT(workflow_version_id, name) DO UPDATE SET
                  input_json = excluded.input_json,
                  expected_json = excluded.expected_json,
                  status = excluded.status,
                  result_json = excluded.result_json,
                  last_run_at = excluded.last_run_at,
                  updated_at = excluded.updated_at
                """,
                (
                    evaluation_id,
                    version_id,
                    json.dumps({"target_path": str(target)}, sort_keys=True),
                    json.dumps({"content_hash": applied_hash}, sort_keys=True),
                    json.dumps({"content_hash": _content_hash(target.read_text(encoding="utf-8"))}, sort_keys=True),
                    now,
                    now,
                    now,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO interventions(
                  id, workflow_version_id, scope_type, scope_id, target_path,
                  previous_hash, applied_hash, previous_content, applied_content,
                  status, exposure_started_at, created_at, updated_at
                ) VALUES (?, ?, 'repository', ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    intervention_id,
                    version_id,
                    str(root),
                    str(target),
                    previous_hash,
                    applied_hash,
                    previous_content,
                    content,
                    now,
                    now,
                    now,
                ),
            )
            self.conn.execute(
                "UPDATE workflow_candidates SET status = 'active', reviewer = 'local_operator', reviewed_at = ?, updated_at = ? WHERE id = ?",
                (now, now, candidate_id),
            )
            self.conn.execute(
                "UPDATE observations SET status = 'active', updated_at = ? WHERE id = ?",
                (now, candidate.observation_id),
            )
            self.repository.record_event(
                entity_type="intervention",
                entity_id=intervention_id,
                event_type="applied",
                actor="local_operator",
                details={"candidate_id": candidate_id, "target_path": str(target)},
                now=now,
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            self._restore_file(target, previous_content)
            raise
        return {
            "candidate_id": candidate_id,
            "intervention_id": intervention_id,
            "target_path": str(target),
            "previous_hash": previous_hash,
            "applied_hash": applied_hash,
            "status": "active",
            "idempotent": False,
        }

    def edit(self, candidate_id: str, *, content: dict[str, Any]) -> WorkflowCandidateRecord:
        """Replace a pending candidate's structured content and return it to review."""
        candidate = self.show(candidate_id)
        if candidate.status.value == "active":
            raise RuntimeError("Roll back an active workflow before editing it")
        normalized = self._validate_content(content)
        now = utc_now()
        self.conn.execute(
            """
            UPDATE workflow_candidates
            SET content_json = ?, status = 'pending', reviewer = NULL,
                reviewed_at = NULL, checks_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(normalized, sort_keys=True),
                json.dumps({"schema": "valid", "review_required": True, "applied": False}),
                now,
                candidate_id,
            ),
        )
        self.conn.execute(
            "UPDATE observations SET status = 'proposal_ready', updated_at = ? WHERE id = ?",
            (now, candidate.observation_id),
        )
        self.repository.record_event(
            entity_type="workflow_candidate",
            entity_id=candidate_id,
            event_type="edited_pending",
            actor="local_operator",
            details={"content_hash": _content_hash(json.dumps(normalized, sort_keys=True))},
            now=now,
        )
        self.conn.commit()
        return self.show(candidate_id)

    def reject(self, candidate_id: str, *, reason: str = "operator_rejected") -> WorkflowCandidateRecord:
        candidate = self.show(candidate_id)
        if candidate.status.value == "active":
            raise RuntimeError("Roll back an active workflow before rejecting it")
        now = utc_now()
        self.conn.execute(
            """
            UPDATE workflow_candidates
            SET status = 'rejected', reviewer = 'local_operator', reviewed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, candidate_id),
        )
        self.conn.execute(
            "UPDATE observations SET status = 'rejected', updated_at = ? WHERE id = ?",
            (now, candidate.observation_id),
        )
        self.repository.record_event(
            entity_type="workflow_candidate",
            entity_id=candidate_id,
            event_type="rejected",
            actor="local_operator",
            details={"reason": reason},
            now=now,
        )
        self.conn.commit()
        return self.show(candidate_id)

    def refresh_integrity(self) -> dict[str, int]:
        """Mark active workflows stale when their rendered artifact drifts or disappears."""
        rows = self.conn.execute(
            """
            SELECT i.id, i.target_path, i.applied_hash, wv.candidate_id
            FROM interventions i
            JOIN workflow_versions wv ON wv.id = i.workflow_version_id
            WHERE i.status = 'active'
            """
        ).fetchall()
        stale = 0
        for intervention_id, target_path, applied_hash, candidate_id in rows:
            target = Path(str(target_path))
            current = target.read_text(encoding="utf-8") if target.exists() else None
            current_hash = _content_hash(current) if current is not None else None
            if current_hash == applied_hash:
                continue
            self._mark_stale(str(candidate_id), str(intervention_id), now=utc_now())
            stale += 1
        if stale:
            self.conn.commit()
        return {"stale": stale, "checked": len(rows)}

    def rollback(self, candidate_id: str, *, reason: str = "operator_requested") -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT i.id, i.target_path, i.previous_content, i.previous_hash, i.applied_hash,
                   wc.observation_id
            FROM interventions i
            JOIN workflow_versions wv ON wv.id = i.workflow_version_id
            JOIN workflow_candidates wc ON wc.id = wv.candidate_id
            WHERE wc.id = ? AND i.status = 'active'
            ORDER BY i.created_at DESC
            LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"No active intervention found for workflow: {candidate_id}")
        intervention_id, target_path, previous_content, previous_hash, applied_hash, observation_id = row
        target = Path(target_path)
        current_content = target.read_text(encoding="utf-8") if target.exists() else None
        current_hash = _content_hash(current_content) if current_content is not None else None
        if current_hash != applied_hash:
            raise RuntimeError(
                "The applied workflow file changed after Reflect wrote it; refusing to overwrite those edits"
            )
        self._restore_file(target, previous_content)
        now = utc_now()
        self.conn.execute(
            """
            UPDATE interventions
            SET status = 'rolled_back', rolled_back_at = ?, rollback_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, reason, now, intervention_id),
        )
        self.conn.execute(
            "UPDATE workflow_candidates SET status = 'rolled_back', updated_at = ? WHERE id = ?",
            (now, candidate_id),
        )
        self.conn.execute(
            "UPDATE observations SET status = 'rolled_back', updated_at = ? WHERE id = ?",
            (now, observation_id),
        )
        self.repository.record_event(
            entity_type="intervention",
            entity_id=str(intervention_id),
            event_type="rolled_back",
            actor="local_operator",
            details={"candidate_id": candidate_id, "reason": reason},
            now=now,
        )
        self.conn.commit()
        return {
            "candidate_id": candidate_id,
            "intervention_id": intervention_id,
            "target_path": str(target),
            "restored_hash": previous_hash,
            "status": "rolled_back",
        }

    @staticmethod
    def _validate_content(content: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(content)
        slug = str(normalized.get("slug") or "")
        if not _SAFE_SLUG.fullmatch(slug):
            raise ValueError(f"Unsafe workflow slug: {slug!r}")
        behavior_type = str(normalized.get("behavior_type") or "proven_pattern")
        if behavior_type not in {"loop", "recovery", "verification", "exploration", "proven_pattern"}:
            raise ValueError(f"Unsupported workflow behavior type: {behavior_type!r}")
        normalized["behavior_type"] = behavior_type
        suggested_artifact = str(normalized.get("suggested_artifact") or "skill")
        if suggested_artifact != "skill":
            raise ValueError(f"Unsupported workflow artifact: {suggested_artifact!r}")
        normalized["suggested_artifact"] = suggested_artifact
        steps = normalized.get("steps")
        if not isinstance(steps, list):
            raise ValueError("Workflow steps must be a list")
        if not any(str(step).strip() for step in steps) and not str(
            normalized.get("source_markdown") or ""
        ).strip():
            raise ValueError("Workflow content requires steps or reviewed source markdown")
        normalized["steps"] = [str(step).strip() for step in steps if str(step).strip()]
        for key in ("abstain_when", "verification"):
            values = normalized.get(key, [])
            if not isinstance(values, list):
                raise ValueError(f"Workflow {key} must be a list")
            normalized[key] = [str(value).strip() for value in values if str(value).strip()]
        normalized["schema_version"] = 1
        return normalized

    @classmethod
    def _target_for(
        cls,
        candidate: WorkflowCandidateRecord,
        project_root: Path,
    ) -> tuple[Path, Path]:
        cls._validate_content(candidate.content)
        root = project_root.expanduser().resolve()
        slug = str(candidate.content.get("slug") or "")
        skills_root = (root / ".agents" / "skills").resolve()
        target = skills_root / slug / "SKILL.md"
        resolved_target = target.resolve(strict=False)
        if not resolved_target.is_relative_to(skills_root):
            raise ValueError("Workflow target escapes the project skills directory")
        if target.is_symlink() or any(
            parent.is_symlink() for parent in target.parents if parent != root.parent
        ):
            raise ValueError("Refusing to apply a workflow through a symlink")
        return root, target

    def _target_checks(
        self,
        candidate: WorkflowCandidateRecord,
        *,
        root: Path,
        target: Path,
    ) -> dict[str, Any]:
        repository_root = root.is_dir() and (root / ".git").exists()
        active_owners = [
            {"candidate_id": str(row[0]), "title": str(row[1])}
            for row in self.conn.execute(
                """
                SELECT wc.id, wc.title
                FROM interventions i
                JOIN workflow_versions wv ON wv.id = i.workflow_version_id
                JOIN workflow_candidates wc ON wc.id = wv.candidate_id
                WHERE i.status = 'active' AND i.target_path = ?
                ORDER BY i.created_at DESC
                """,
                (str(target),),
            ).fetchall()
        ]
        active_conflicts = [owner for owner in active_owners if owner["candidate_id"] != candidate.id]
        slug = str(candidate.content.get("slug") or "")
        alternatives = [
            {"candidate_id": str(row[0]), "title": str(row[1]), "status": str(row[2])}
            for row in self.conn.execute(
                """
                SELECT id, title, status
                FROM workflow_candidates
                WHERE id <> ?
                  AND json_extract(content_json, '$.slug') = ?
                  AND status NOT IN ('rejected', 'rolled_back')
                ORDER BY confidence DESC, support_count DESC, updated_at DESC
                """,
                (candidate.id, slug),
            ).fetchall()
        ]
        issues: list[str] = []
        if not root.exists():
            issues.append(f"Target repository does not exist: {root}")
        elif not root.is_dir():
            issues.append(f"Target repository is not a directory: {root}")
        elif not repository_root:
            issues.append(f"Choose a repository root containing .git before applying: {root}")
        if active_conflicts:
            issues.append(
                f"Another active workflow already owns {target}: {active_conflicts[0]['title']}"
            )
        return {
            "schema_valid": True,
            "repository_root": repository_root,
            "target_available": not active_conflicts,
            "apply_allowed": repository_root and not active_conflicts,
            "issues": issues,
            "target_owner": active_owners[0] if active_owners else None,
            "active_conflicts": active_conflicts,
            "alternative_candidates": alternatives,
        }

    def _mark_stale(self, candidate_id: str, intervention_id: str, *, now: str) -> None:
        self.conn.execute(
            "UPDATE interventions SET status = 'stale', updated_at = ? WHERE id = ?",
            (now, intervention_id),
        )
        self.conn.execute(
            "UPDATE workflow_candidates SET status = 'stale', updated_at = ? WHERE id = ?",
            (now, candidate_id),
        )
        self.repository.record_event(
            entity_type="intervention",
            entity_id=intervention_id,
            event_type="artifact_stale",
            details={"candidate_id": candidate_id},
            now=now,
        )

    @staticmethod
    def _render_skill(candidate: WorkflowCandidateRecord) -> str:
        description = str(candidate.content.get("description") or candidate.hypothesis).replace("\n", " ").strip()
        description_yaml = json.dumps(description, ensure_ascii=False)
        steps = [str(step).strip() for step in candidate.content.get("steps", []) if str(step).strip()]
        abstain = [str(step).strip() for step in candidate.content.get("abstain_when", []) if str(step).strip()]
        verification = [
            str(step).strip() for step in candidate.content.get("verification", []) if str(step).strip()
        ]
        source_markdown = str(candidate.content.get("source_markdown") or "").strip()
        if source_markdown:
            return (
                "---\n"
                f"name: {candidate.content['slug']}\n"
                f"description: {description_yaml}\n"
                "---\n\n"
                f"{source_markdown}\n"
            )
        lines = [
            "---",
            f"name: {candidate.content['slug']}",
            f"description: {description_yaml}",
            "---",
            "",
            f"# {candidate.title}",
            "",
            "## Workflow",
            "",
            *[f"{index}. {step}" for index, step in enumerate(steps, start=1)],
        ]
        if abstain:
            lines.extend(["", "## Abstain when", "", *[f"- {item}" for item in abstain]])
        if verification:
            lines.extend(["", "## Verification", "", *[f"- {item}" for item in verification]])
        lines.extend(["", "Generated by Reflect as a reviewed local workflow.", ""])
        return "\n".join(lines)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=".reflect-workflow-",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    @classmethod
    def _restore_file(cls, path: Path, previous_content: str | None) -> None:
        if previous_content is None:
            if path.exists():
                path.unlink()
            with suppress(OSError):
                path.parent.rmdir()
            return
        cls._atomic_write(path, previous_content)
