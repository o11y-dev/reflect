from __future__ import annotations

from pathlib import Path

from reflect.improvements.models import (
    SkillLifecycleState,
    SkillOrigin,
    SkillVersionStatus,
)
from reflect.improvements.service import ImprovementService
from reflect.improvements.skills import SkillRegistryService
from reflect.store.sqlite import connect_sqlite


def _write_skill(root: Path, *, body: str = "1. Verify the change.") -> Path:
    path = root / "verify-first" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "name: verify-first\n"
        "description: Verify a change before reporting completion.\n"
        "---\n\n"
        f"# Verify first\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_skill_registry_tracks_filesystem_versions_and_missing_installations(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    root = tmp_path / ".agents" / "skills"
    path = _write_skill(root)
    try:
        registry = SkillRegistryService(conn)

        first = registry.refresh(scan_paths=[root])
        skill = registry.list()[0]

        assert first["filesystem_skills"] == 1
        assert skill.slug == "verify-first"
        assert skill.origin == SkillOrigin.IMPORTED
        assert skill.lifecycle_state == SkillLifecycleState.ACTIVE
        assert skill.version_count == 1
        assert skill.installation_count == 1

        _write_skill(root, body="1. Run the focused test.\n2. Record the result.")
        registry.refresh(scan_paths=[root])
        detail = registry.show(skill.id)
        assert [item.version for item in detail.versions] == [2, 1]
        assert detail.skill.current_version == 2

        path.unlink()
        missing = registry.refresh(scan_paths=[root])
        detail = registry.show(skill.id)
        assert missing["missing_installations"] == 1
        assert detail.installations[0].status == "missing"
    finally:
        conn.close()


def test_agent_discovery_stages_a_pending_versioned_skill(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        service = ImprovementService(conn)
        candidate_id = service.stage_extracted_skills(
            [
                {
                    "name": "bounded-recovery",
                    "description": "Recover from repeated failures with a bounded state change.",
                    "content": "# Bounded recovery\n\n1. Observe.\n2. Change state.\n3. Verify.",
                    "behavior_type": "recovery",
                }
            ],
            session_ids=[],
            source_agent="codex",
        )[0]

        skill = service.skills.skill_for_candidate(candidate_id)
        detail = service.skills.show(skill.id)

        assert skill.slug == "bounded-recovery"
        assert skill.origin == SkillOrigin.AGENT_AUTHORED
        assert skill.lifecycle_state == SkillLifecycleState.PENDING
        assert detail.versions[0].source_agent == "codex"
        assert detail.versions[0].workflow_candidate_id == candidate_id
        assert {item["entity_type"] for item in detail.evidence} == {"observation"}
    finally:
        conn.close()


def test_registry_summarizes_evidence_variants_as_one_semantic_version(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        registry = SkillRegistryService(conn)
        workflow = {
            "schema_version": 1,
            "slug": "verify-first",
            "description": "Verify before completion.",
            "behavior_type": "verification",
            "suggested_artifact": "skill",
            "steps": ["Run the smallest relevant verification."],
            "abstain_when": ["No change was made."],
            "verification": ["Record the result."],
        }
        for suffix in ("a", "b"):
            registry._track_version(
                slug="verify-first",
                name="verify-first",
                description=f"Verify before completion after {suffix} evidence.",
                origin=SkillOrigin.RULE_BLUEPRINT,
                content=f"# Verify first\n\nEvidence variant {suffix}",
                workflow={
                    **workflow,
                    "description": f"Verify after {suffix} evidence.",
                    "source": {
                        "kind": "rule_blueprint",
                        "observation_id": f"observation-{suffix}",
                    },
                },
                source_kind="rule_blueprint",
                source_agent=None,
                source_loop_id=None,
                source_workflow_id=None,
                workflow_candidate_id=None,
                version_status=SkillVersionStatus.ACTIVE,
                lifecycle=SkillLifecycleState.ACTIVE,
                now=f"2026-07-16T10:00:0{int(suffix == 'b')}+00:00",
            )
        conn.commit()

        listed = registry.list()[0]
        detail = registry.show(listed.id)

        assert conn.execute("SELECT COUNT(*) FROM skill_versions").fetchone()[0] == 2
        assert listed.version_count == 1
        assert listed.current_version == 1
        assert detail.skill.version_count == 1
        assert [item.version for item in detail.versions] == [1]
    finally:
        conn.close()


def test_registry_records_observed_skill_usage_without_a_local_file(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        registry = SkillRegistryService(conn)
        now = "2026-07-16T10:00:00+00:00"
        conn.execute(
            "INSERT INTO agents(id, name, created_at, updated_at) VALUES ('agent-1', 'codex', ?, ?)",
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions(id, agent_id, started_at, status, created_at, updated_at)
            VALUES ('session-1', 'agent-1', ?, 'completed', ?, ?)
            """,
            (now, now, now),
        )
        conn.execute(
            """
            INSERT INTO graph_nodes(
              id, kind, label, session_id, identity_key, attrs_json, created_at, updated_at
            ) VALUES
              ('session-node', 'Session', 'Session', 'session-1', 'session:session-1', '{}', ?, ?),
              ('skill-node', 'Skill', 'observed-skill', NULL, 'skill:observed-skill', '{}', ?, ?)
            """,
            (now, now, now, now),
        )
        conn.execute(
            """
            INSERT INTO graph_edges(
              id, source_node_id, target_node_id, kind, session_id, weight,
              attrs_json, created_at, updated_at
            ) VALUES ('skill-edge', 'session-node', 'skill-node', 'used_skill',
                      'session-1', 1, '{}', ?, ?)
            """,
            (now, now),
        )
        conn.commit()

        assert registry.sync_usage() == 1
        conn.commit()
        detail = registry.show("observed-skill")
        skill = detail.skill
        assert skill.usage_count == 1
        assert skill.version_count == 0
        assert skill.lifecycle_state == SkillLifecycleState.STALE
        assert len(detail.usage_sessions) == 1
        usage = detail.usage_sessions[0]
        assert usage.session_id == "session-1"
        assert usage.agent == "codex"
        assert usage.state == "observed"
        assert usage.evidence == {
            "edge_kind": "used_skill",
            "source": "behavioral_memory_graph",
        }
        assert registry.list(include_stale=False) == []
        assert registry.counts_by_lifecycle() == {"stale": 1}
    finally:
        conn.close()
