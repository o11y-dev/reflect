from __future__ import annotations

from datetime import UTC, datetime

from reflect.improvements.models import (
    EvidenceRef,
    ObservationDraft,
    RuleDefinition,
    Severity,
)
from reflect.improvements.scope import ImprovementScopeResolver
from reflect.improvements.service import ImprovementService
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite

NOW = "2026-07-29T10:00:00+00:00"


def _scoped_service(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    migrate(conn)
    conn.execute(
        """
        INSERT INTO agents(id, name, kind, raw_json, created_at, updated_at)
        VALUES ('agent', 'codex', 'cli', '{}', ?, ?)
        """,
        (NOW, NOW),
    )
    for repo_id, root in (
        ("repo-a", tmp_path / "project-a"),
        ("repo-b", tmp_path / "project-b"),
    ):
        root.mkdir()
        conn.execute(
            """
            INSERT INTO repos(id, full_name, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (repo_id, f"example/{repo_id}", NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO workspaces(
              id, root_path, path_hash, label, repo_id, source_key, confidence,
              raw_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'test', 1, '{}', ?, ?)
            """,
            (f"workspace-{repo_id}", str(root), f"hash-{repo_id}", repo_id, repo_id, NOW, NOW),
        )
    for index in range(25):
        session_id = f"session-a-{index}"
        parent_id = "session-a-0" if index == 1 else None
        conn.execute(
            """
            INSERT INTO sessions(
              id, agent_id, repo_id, workspace_id, parent_session_id,
              started_at, ended_at, status, title, created_at, updated_at
            ) VALUES (?, 'agent', 'repo-a', 'workspace-repo-a', ?, ?, ?, 'completed', ?, ?, ?)
            """,
            (
                session_id,
                parent_id,
                f"2026-07-{index + 1:02d}T10:00:00+00:00",
                f"2026-07-{index + 1:02d}T10:10:00+00:00",
                f"Project A task {index}",
                NOW,
                NOW,
            ),
        )
    conn.execute(
        """
        INSERT INTO sessions(
          id, agent_id, repo_id, workspace_id, started_at, ended_at,
          status, title, created_at, updated_at
        ) VALUES (
          'session-b', 'agent', 'repo-b', 'workspace-repo-b',
          '2026-05-01T10:00:00+00:00', '2026-05-01T10:10:00+00:00',
          'completed', 'Project B task', ?, ?
        )
        """,
        (NOW, NOW),
    )
    service = ImprovementService(conn, rules=[])
    definition = RuleDefinition(
        id="scoped_test",
        version=1,
        category="test",
        title="Scoped test",
        description="Scoped test observation.",
    )
    service.repository.sync_rule_definitions((definition,), now=NOW)
    observation_a = service.repository.upsert_observation(
        ObservationDraft(
            rule_id=definition.id,
            rule_version=1,
            scope_type="repository",
            scope_id="repo-a",
            repo_id="repo-a",
            fingerprint="project-a",
            category="test",
            title="Project A finding",
            summary="Project A evidence.",
            metric_name="sessions",
            metric_value=25,
            metric_unit="sessions",
            metric_direction="lower_is_better",
            impact_score=90,
            severity=Severity.CRITICAL,
            confidence=0.9,
            occurrence_count=25,
            affected_session_count=25,
            evidence=[
                EvidenceRef(
                    entity_type="session",
                    entity_id=f"session-a-{index}",
                    session_id=f"session-a-{index}",
                    summary_redacted=f"Project A evidence {index}",
                )
                for index in range(25)
            ],
        ),
        now=NOW,
    )
    observation_b = service.repository.upsert_observation(
        ObservationDraft(
            rule_id=definition.id,
            rule_version=1,
            scope_type="repository",
            scope_id="repo-b",
            repo_id="repo-b",
            fingerprint="project-b",
            category="test",
            title="Unrelated Project B finding",
            summary="Project B evidence.",
            metric_name="sessions",
            metric_value=1,
            metric_unit="sessions",
            metric_direction="lower_is_better",
            impact_score=100,
            severity=Severity.CRITICAL,
            confidence=0.99,
            evidence=[
                EvidenceRef(
                    entity_type="session",
                    entity_id="session-b",
                    session_id="session-b",
                    summary_redacted="Project B evidence",
                )
            ],
        ),
        now=NOW,
    )
    conn.commit()
    return service, conn, observation_a, observation_b


def test_path_scope_filters_before_ranking_and_keeps_complete_session_attribution(tmp_path):
    service, conn, observation_a, observation_b = _scoped_service(tmp_path)
    try:
        scope = ImprovementScopeResolver(
            conn,
            cwd=tmp_path / "project-a",
            now=datetime(2026, 7, 29, 12, tzinfo=UTC),
        ).path()
        summary = service.improve(refresh=False, scope=scope)
        ledger = service.finding_session_ledger(observation_a, scope=scope, limit=50)

        assert [item.id for item in summary.observations] == [observation_a]
        assert observation_b not in {item.id for item in summary.observations}
        assert summary.observations[0].scope_affected_session_count == 25
        assert summary.observations[0].affected_session_ratio == 1
        assert summary.observations[0].last_seen_at == "2026-07-25T10:10:00+00:00"
        assert ledger.source_session_count == 25
        assert len(ledger.source_sessions) == 25
        assert conn.execute(
            "SELECT COUNT(*) FROM observation_evidence WHERE observation_id = ?",
            (observation_a,),
        ).fetchone()[0] == 20
        assert conn.execute(
            "SELECT COUNT(*) FROM observation_sessions WHERE observation_id = ?",
            (observation_a,),
        ).fetchone()[0] == 25
    finally:
        conn.close()


def test_session_scope_includes_descendants_and_global_scope_is_bounded(tmp_path):
    service, conn, observation_a, _ = _scoped_service(tmp_path)
    try:
        resolver = ImprovementScopeResolver(
            conn,
            cwd=tmp_path,
            now=datetime(2026, 7, 29, 12, tzinfo=UTC),
        )
        session_scope = resolver.session("session-a-0")
        week_scope = resolver.global_period("week")
        all_scope = resolver.global_period("all")

        assert session_scope.eligible_session_count == 2
        assert service.finding_session_ledger(
            observation_a,
            scope=session_scope,
        ).source_session_count == 2
        assert week_scope.eligible_session_count == 3
        assert all_scope.eligible_session_count == 26
        assert resolver.path(tmp_path / "missing").matched is False
    finally:
        conn.close()


def test_context_query_does_not_select_higher_impact_unrelated_project(tmp_path):
    service, conn, observation_a, observation_b = _scoped_service(tmp_path)
    try:
        answer = service.ask(
            "Which project finding should I inspect?",
            path=tmp_path / "project-a",
        )

        assert observation_a in {item.id for item in answer.evidence}
        assert observation_b not in {item.id for item in answer.evidence}
    finally:
        conn.close()


def test_scoped_prevalence_is_ranked_before_limit(tmp_path):
    service, conn, observation_a, _ = _scoped_service(tmp_path)
    try:
        for index in range(20):
            service.repository.upsert_observation(
                ObservationDraft(
                    rule_id="scoped_test",
                    rule_version=1,
                    scope_type="repository",
                    scope_id="repo-a",
                    repo_id="repo-a",
                    fingerprint=f"single-session-{index}",
                    category="test",
                    title=f"High raw impact {index}",
                    summary="One supporting session.",
                    metric_name="sessions",
                    metric_value=1,
                    metric_unit="sessions",
                    metric_direction="lower_is_better",
                    impact_score=100,
                    severity=Severity.CRITICAL,
                    confidence=0.99,
                    evidence=[
                        EvidenceRef(
                            entity_type="session",
                            entity_id=f"session-a-0:{index}",
                            session_id="session-a-0",
                            summary_redacted="One supporting session",
                        )
                    ],
                ),
                now=NOW,
            )
        conn.commit()
        scope = ImprovementScopeResolver(
            conn,
            cwd=tmp_path / "project-a",
            now=datetime(2026, 7, 29, 12, tzinfo=UTC),
        ).path()

        observations = service.repository.list_observations(limit=5, scope=scope)

        assert observations[0].id == observation_a
        assert observations[0].affected_session_ratio == 1
    finally:
        conn.close()
