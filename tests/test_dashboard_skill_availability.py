from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_dashboard_sql_api import _seed_sql_report_db

from reflect.dashboard_improvements import DashboardImprovementAdapter
from reflect.dashboard_server import build_dashboard_app
from reflect.improvements.skills import SkillRegistryService
from reflect.store.sqlite import connect_sqlite, connect_sqlite_read_only, mark_snapshot

DERIVED_COUNTS = {"evidence_count", "usage_count", "measurement_count"}
DERIVED_DETAILS = {"evidence", "usage_occurrences", "measurements"}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "reflect.db"
    _seed_sql_report_db(path)
    skill_path = tmp_path / ".agents" / "skills" / "verify-first" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: verify-first\ndescription: Verify before reporting completion.\n"
        "---\n\n# Verify first\n\n1. Run the focused test.\n",
        encoding="utf-8",
    )
    with closing(connect_sqlite(path)) as conn:
        registry = SkillRegistryService(conn, initialize_schema=False)
        registry.sync_paths([skill_path.parent.parent])
        skill = registry.show("verify-first").skill
        conn.execute(
            """INSERT INTO execution_units(id, session_id, source, started_at,
               outcome, verification_passed, created_at, updated_at)
               VALUES ('skill-execution', 'sess-sql', 'session_fallback',
                       '2026-05-01', 'success', 1, '2026-05-01', '2026-05-01')"""
        )
        conn.execute(
            """INSERT INTO skill_evidence(id, skill_version_id, entity_type,
               entity_id, created_at) VALUES ('skill-evidence', ?, 'execution_unit',
               'skill-execution', '2026-05-01')""",
            (skill.current_version_id,),
        )
        conn.execute(
            """INSERT INTO skill_usage(id, skill_id, skill_version_id, session_id,
               execution_unit_id, state, outcome, confidence, observed_at, created_at, updated_at)
               VALUES ('skill-usage', ?, ?, 'sess-sql', 'skill-execution',
                       'observed', 'success', 0.9, '2026-05-01', '2026-05-01', '2026-05-01')""",
            (skill.id, skill.current_version_id),
        )
        conn.execute(
            """INSERT INTO skill_measurements(id, skill_id, skill_version_id,
               metric_name, before_value, after_value, verdict, confidence,
               measured_at, created_at, updated_at)
               VALUES ('skill-measurement', ?, ?, 'tokens', 10, 5, 'improved', 0.9,
                       '2026-05-01', '2026-05-01', '2026-05-01')""",
            (skill.id, skill.current_version_id),
        )
        mark_snapshot(conn, ready=True)
    from tests.test_dashboard_sql_api import _publish_report
    _publish_report(path)
    return path


@pytest.mark.parametrize("usage_ready", [False, True])
def test_pending_skills_browse_durable_records_during_normalization_and_analysis(db, usage_ready):
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=False)
        if usage_ready:
            mark_snapshot(conn, ready=True, profile="usage")
    client = TestClient(build_dashboard_app(
        docs_dir=db.parent, db_path=db,
    ))

    listed = client.get("/api/skills?limit=500")
    assert listed.status_code == 200
    listed = listed.json()
    assert listed["analysis_state"] == "ready"
    assert listed["total_count"] == 1
    assert listed["counts_by_lifecycle"] == {"active": 1}
    skill = listed["skills"][0]
    assert skill["slug"] == "verify-first"
    assert skill["version_count"] == skill["installation_count"] == 1
    assert skill["installation_targets"] == ["repository"]
    assert all(skill[field] == 1 for field in DERIVED_COUNTS)
    assert listed.get("analysis_pending", False) is False

    response = client.get(f"/api/skills/{skill['id']}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["analysis_state"] == "ready"
    assert detail["skill"] == skill
    assert "Run the focused test" in detail["versions"][0]["content_markdown"]
    assert detail["installations"][0]["status"] == "active"
    assert all(len(detail[field]) == 1 for field in DERIVED_DETAILS)
    assert detail["usage_occurrences"][0]["outcome"] == "success"
    measurement = detail["measurements"][0]
    assert measurement["verdict"] == "improved"
    assert measurement["before_value"] == 10
    assert client.get("/api/skills/not-registered").status_code == 404
    assert client.get("/api/findings").status_code == 200


def test_ready_skills_preserve_registry_content(db):
    with closing(connect_sqlite_read_only(db)) as conn:
        registry = SkillRegistryService(conn, initialize_schema=False)
        expected_list = [item.model_dump(mode="json") for item in registry.list()]
        expected_detail = registry.show("verify-first").model_dump(mode="json")
    adapter = DashboardImprovementAdapter(db)
    listed = adapter.skills(status=None, include_stale=True, limit=100)
    detail = adapter.skill("verify-first")

    assert listed["analysis_state"] == detail["analysis_state"] == "ready"
    assert listed["skills"] == expected_list
    assert detail == {**expected_detail, "analysis_state": "ready"}
    assert all(detail["skill"][field] == 1 for field in DERIVED_COUNTS)
    assert detail["usage_occurrences"][0]["outcome"] == "success"
    assert detail["measurements"][0]["verdict"] == "improved"


@pytest.mark.parametrize("ready", [False, True])
def test_skills_use_query_only_pinned_connections_without_mutations(db, monkeypatch, ready):
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=ready)
    connections = []

    def read_only(path, *, immutable):
        assert immutable is True
        assert path != db
        conn = connect_sqlite_read_only(path, immutable=immutable)
        assert conn.in_transaction
        assert conn.execute("PRAGMA query_only").fetchone() == (1,)
        connections.append(conn)
        return conn

    monkeypatch.setattr("reflect.store.snapshots.connect_sqlite_read_only", read_only)
    with closing(connect_sqlite_read_only(db, profile=None)) as observer:
        # data_version changes if another connection commits, even if values are restored.
        before = observer.execute("PRAGMA data_version").fetchone()
        adapter = DashboardImprovementAdapter(db)
        adapter.skills(status=None, include_stale=False, limit=100)
        adapter.skill("verify-first")
        observer.rollback()
        assert observer.execute("PRAGMA data_version").fetchone() == before
    assert len(connections) == 2


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("operation", ["list", "show"])
def test_skills_readiness_and_records_share_one_generation(db, monkeypatch, ready, operation):
    with closing(connect_sqlite(db)) as conn:
        mark_snapshot(conn, ready=ready)
    original = getattr(SkillRegistryService, operation)

    def concurrent_write(registry, *args, **kwargs):
        with closing(connect_sqlite(db)) as writer:
            writer.execute("UPDATE skills SET description = 'New generation'")
            mark_snapshot(writer, ready=not ready)
        return original(registry, *args, **kwargs)

    monkeypatch.setattr(SkillRegistryService, operation, concurrent_write)
    adapter = DashboardImprovementAdapter(db)
    if operation == "list":
        payload = adapter.skills(status=None, include_stale=True, limit=100)
        skill = payload["skills"][0]
    else:
        payload = adapter.skill("verify-first")
        skill = payload["skill"]
    assert payload["analysis_state"] == "ready"
    assert skill["description"] == "Verify before reporting completion."
    assert skill["usage_count"] == 1


@pytest.mark.parametrize("path", ["/api/skills", "/api/skills/verify-first"])
def test_skills_still_require_current_schema(db, path):
    with closing(connect_sqlite(db)) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = (SELECT MAX(version) FROM schema_migrations)")
        mark_snapshot(conn, ready=False)
    client = TestClient(build_dashboard_app(docs_dir=db.parent, db_path=db))
    response = client.get(path)
    assert response.status_code == 200
    assert response.json()["analysis_state"] == "ready"
