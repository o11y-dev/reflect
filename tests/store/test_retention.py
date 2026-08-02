from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner

from reflect.core import main
from reflect.memory.models import MemoryItem, MemorySourceMetadata
from reflect.memory.sqlite_provider import LocalSQLiteMemoryProvider
from reflect.store.ingest import ingest_local_spans_file
from reflect.store.migrate import load_migrations, migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.retention import SessionPruner, SessionRetentionPolicy
from reflect.store.sqlite import connect_sqlite

NOW = datetime(2026, 7, 29, 12, tzinfo=UTC)
OLD = "2026-05-01T00:00:00+00:00"
RECENT = "2026-07-20T00:00:00+00:00"
EPOCH = "1970-01-01T00:00:00+00:00"


def _apply_migrations_through(conn, version: int) -> None:
    for migration in load_migrations():
        if migration.version > version:
            break
        conn.executescript(migration.sql)
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
            VALUES (?, ?, '2026-07-29T00:00:00+00:00')
            """,
            (migration.version, migration.name),
        )
    conn.commit()


def _seed_identity(conn) -> None:
    conn.execute(
        """
        INSERT INTO agents(id, name, raw_json, created_at, updated_at)
        VALUES ('agent', 'codex', '{}', ?, ?)
        """,
        (RECENT, RECENT),
    )
    conn.execute(
        """
        INSERT INTO repos(id, full_name, created_at, updated_at)
        VALUES ('repo', 'example/repo', ?, ?)
        """,
        (RECENT, RECENT),
    )
    conn.execute(
        """
        INSERT INTO workspaces(
          id, root_path, path_hash, label, repo_id, source_key, confidence,
          raw_json, created_at, updated_at
        ) VALUES ('workspace', '/workspace/repo', 'workspace-hash', 'repo',
                  'repo', 'test', 1, '{}', ?, ?)
        """,
        (RECENT, RECENT),
    )


def _insert_session(
    conn,
    session_id: str,
    *,
    started_at: str = EPOCH,
    last_observed_at: str | None = OLD,
    status: str = "completed",
    parent_session_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO sessions(
          id, agent_id, repo_id, workspace_id, parent_session_id,
          started_at, ended_at, last_observed_at, status, source_kind,
          source_ref, created_at, updated_at
        ) VALUES (?, 'agent', 'repo', 'workspace', ?, ?, ?, ?, ?, 'native_session',
                  ?, ?, ?)
        """,
        (
            session_id,
            parent_session_id,
            started_at,
            last_observed_at,
            last_observed_at,
            status,
            f"native_session:codex:/tmp/{session_id}.jsonl",
            RECENT,
            RECENT,
        ),
    )


def test_retention_policy_keeps_boundary_active_valid_and_parent_sessions(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _seed_identity(conn)
        _insert_session(conn, "old-parent")
        _insert_session(
            conn,
            "recent-child",
            last_observed_at=RECENT,
            parent_session_id="old-parent",
        )
        _insert_session(conn, "old-active", status="active")
        _insert_session(conn, "valid-history", started_at="2020-01-01T00:00:00+00:00")
        _insert_session(conn, "unknown-activity", last_observed_at=None)
        _insert_session(conn, "stale-invalid")
        _insert_session(
            conn,
            "exact-boundary",
            last_observed_at=(NOW.replace(microsecond=0) - timedelta(days=60)).isoformat(),
        )
        conn.commit()

        candidates = SessionPruner(
            conn,
            SessionRetentionPolicy(older_than_days=60, now=NOW),
        ).preview()

        assert [candidate.session_id for candidate in candidates] == ["stale-invalid"]
    finally:
        conn.close()


def test_pruning_preserves_durable_provenance_and_prevents_old_resurrection(tmp_path):
    db_path = tmp_path / "reflect.db"
    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        _seed_identity(conn)
        _insert_session(conn, "stale-invalid")
        conn.execute(
            """
            INSERT INTO raw_events(
              id, source_id, source_type, event_type, session_id, observed_at,
              received_at, attrs_json, body_json, normalized_status,
              content_hash, created_at
            ) VALUES ('raw', 'source', 'native_session', 'event', 'stale-invalid',
                      ?, ?, '{}', '{}', 'ok', 'raw-hash', ?)
            """,
            (OLD, OLD, OLD),
        )
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, raw_attrs_json,
              created_at, updated_at
            ) VALUES ('step', 'stale-invalid', 0, 'unknown', ?, 'ok', '{}', ?, ?)
            """,
            (OLD, OLD, OLD),
        )
        memory_provider = LocalSQLiteMemoryProvider(conn)
        memory_provider.remember(
            MemoryItem(
                id="memory",
                content="retained memory provenance",
                type="fact",
                scope="repo",
                session_id="stale-invalid",
                step_id="step",
                source_metadata=MemorySourceMetadata(
                    source_kind="native_session",
                    source_ref="/tmp/stale-invalid.jsonl",
                    session_id="stale-invalid",
                    step_id="step",
                ),
            )
        )
        conn.execute(
            """
            INSERT INTO evidence(
              id, session_id, step_id, kind, created_at, updated_at
            ) VALUES ('evidence', 'stale-invalid', 'step', 'test', ?, ?)
            """,
            (OLD, OLD),
        )
        conn.execute(
            """
            INSERT INTO operator_feedback(
              id, session_id, outcome, reason_redacted, actor, created_at
            ) VALUES ('feedback', 'stale-invalid', 'corrected',
                      'operator correction', 'local_operator', ?)
            """,
            (OLD,),
        )
        conn.execute(
            """
            INSERT INTO session_outcomes(
              id, session_id, outcome, source, confidence,
              verification_json, created_at, updated_at
            ) VALUES ('outcome', 'stale-invalid', 'corrected',
                      'operator_feedback', 1, '{}', ?, ?)
            """,
            (OLD, OLD),
        )
        conn.execute(
            """
            INSERT INTO rule_definitions(
              id, version, category, title, description, created_at, updated_at
            ) VALUES ('rule', 1, 'test', 'test', 'test', ?, ?)
            """,
            (OLD, OLD),
        )
        conn.execute(
            """
            INSERT INTO observations(
              id, rule_id, rule_version, scope_type, scope_id, category, title,
              summary, metric_name, metric_value, metric_unit, metric_direction,
              severity, confidence, first_seen_at, last_seen_at, last_evaluated_at,
              fingerprint, created_at, updated_at
            ) VALUES ('observation', 'rule', 1, 'repository', 'repo', 'test',
                      'test', 'test', 'count', 1, 'sessions', 'lower_is_better',
                      'low', 1, ?, ?, ?, 'fingerprint', ?, ?)
            """,
            (OLD, OLD, OLD, OLD, OLD),
        )
        conn.execute(
            """
            INSERT INTO observation_evidence(
              id, observation_id, entity_type, entity_id, session_id, step_id,
              summary_redacted, created_at
            ) VALUES ('observation-evidence', 'observation', 'session',
                      'stale-invalid', 'stale-invalid', 'step', 'test', ?)
            """,
            (OLD,),
        )
        conn.commit()

        result = SessionPruner(
            conn,
            SessionRetentionPolicy(older_than_days=60, now=NOW),
        ).run(apply=True)

        assert result.pruned_session_ids == ("stale-invalid",)
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = 'stale-invalid'"
        ).fetchone()[0] == 0
        assert tuple(
            conn.execute(
                "SELECT session_id, pruned_session_id FROM memories WHERE id = 'memory'"
            ).fetchone()
        ) == (None, "stale-invalid")
        memory_result = memory_provider.search("retained", limit=1)[0].item
        assert memory_result["pruned_session_id"] == "stale-invalid"
        assert memory_result["source_metadata"]["session_id"] == "stale-invalid"
        assert tuple(
            conn.execute(
                """
                SELECT session_id, pruned_session_id
                FROM observation_evidence
                WHERE id = 'observation-evidence'
                """
            ).fetchone()
        ) == (None, "stale-invalid")
        assert tuple(
            conn.execute(
                """
                SELECT session_id, pruned_session_id, outcome
                FROM operator_feedback
                WHERE id = 'feedback'
                """
            ).fetchone()
        ) == (None, "stale-invalid", "corrected")
        assert tuple(
            conn.execute(
                """
                SELECT session_id, pruned_session_id, outcome
                FROM session_outcomes
                WHERE id = 'outcome'
                """
            ).fetchone()
        ) == (None, "stale-invalid", "corrected")
        assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute(
            """
            UPDATE pruned_sessions
            SET last_observed_at = '2026-05-01T00:00:00Z'
            WHERE id = 'stale-invalid'
            """
        )
        conn.commit()

        spans = tmp_path / "spans.jsonl"
        old_ns = int(datetime.fromisoformat(OLD).timestamp() * 1_000_000_000)
        new_at = "2026-08-01T00:00:00+00:00"
        new_ns = int(datetime.fromisoformat(new_at).timestamp() * 1_000_000_000)
        spans.write_text(
            "\n".join(
                json.dumps(
                    {
                        "name": "event",
                        "start_time_ns": timestamp,
                        "end_time_ns": timestamp,
                        "attributes": {
                            "gen_ai.client.name": "codex",
                            "gen_ai.client.session_id": "stale-invalid",
                        },
                    }
                )
                for timestamp in (old_ns, new_ns)
            )
            + "\n",
            encoding="utf-8",
        )
        ingestion = ingest_local_spans_file(conn, file_path=spans)
        assert ingestion == {"inserted": 1, "skipped": 1}
        assert normalize_pending_raw_events(conn)["processed"] == 1
        resurrected = conn.execute(
            """
            SELECT s.started_at, p.resurrected_at, p.newer_observed_at
            FROM sessions s
            JOIN pruned_sessions p ON p.id = s.id
            WHERE s.id = 'stale-invalid'
            """
        ).fetchone()
        assert resurrected[0] == new_at
        assert resurrected[1]
        assert resurrected[2] == new_at
    finally:
        conn.close()


def test_dry_run_makes_no_mutations(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _seed_identity(conn)
        _insert_session(conn, "stale-invalid")
        conn.commit()

        result = SessionPruner(
            conn,
            SessionRetentionPolicy(older_than_days=60, now=NOW),
        ).run()

        assert result.dry_run is True
        assert [candidate.session_id for candidate in result.candidates] == [
            "stale-invalid"
        ]
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM pruned_sessions").fetchone()[0] == 0
    finally:
        conn.close()


def test_pruner_preserves_caller_owned_transaction(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        conn.execute("CREATE TABLE caller_owned(value TEXT)")
        conn.commit()
        conn.execute("INSERT INTO caller_owned(value) VALUES ('pending')")

        result = SessionPruner(
            conn,
            SessionRetentionPolicy(older_than_days=60, now=NOW),
        ).run(apply=True)

        assert result.candidates == ()
        assert conn.in_transaction is True
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM caller_owned").fetchone()[0] == 0
    finally:
        conn.close()


def test_prune_apply_rolls_back_when_derived_rebuild_fails(
    tmp_path,
    monkeypatch,
):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _seed_identity(conn)
        _insert_session(conn, "stale-invalid")
        conn.commit()

        def fail_rebuild(*_args, **_kwargs):
            raise RuntimeError("rollup rebuild failed")

        monkeypatch.setattr(
            "reflect.store.rollups.rebuild_rollups",
            fail_rebuild,
        )
        with pytest.raises(RuntimeError, match="rollup rebuild failed"):
            SessionPruner(
                conn,
                SessionRetentionPolicy(older_than_days=60, now=NOW),
            ).run(apply=True)

        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = 'stale-invalid'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM pruned_sessions WHERE id = 'stale-invalid'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_valid_source_timestamp_repairs_epoch_session_start(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    spans = tmp_path / "spans.jsonl"
    valid_at = "2026-07-29T00:00:00+00:00"
    valid_ns = int(datetime.fromisoformat(valid_at).timestamp() * 1_000_000_000)
    spans.write_text(
        "\n".join(
            json.dumps(
                {
                    "name": "event",
                    "start_time_ns": timestamp,
                    "end_time_ns": timestamp,
                    "attributes": {
                        "gen_ai.client.name": "codex",
                        "gen_ai.client.session_id": "sticky-epoch",
                    },
                }
            )
            for timestamp in (0, valid_ns)
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        migrate(conn)
        assert ingest_local_spans_file(conn, file_path=spans)["inserted"] == 2
        assert normalize_pending_raw_events(conn)["processed"] == 2
        row = conn.execute(
            """
            SELECT started_at, last_observed_at
            FROM sessions
            WHERE id = 'sticky-epoch'
            """
        ).fetchone()
        assert tuple(row) == (valid_at, valid_at)
    finally:
        conn.close()


def test_prune_cli_is_dry_run_first_and_creates_consistent_backup(tmp_path):
    db_path = tmp_path / "reflect.db"
    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        _seed_identity(conn)
        _insert_session(conn, "stale-invalid")
        conn.commit()
    finally:
        conn.close()

    runner = CliRunner()
    preview = runner.invoke(
        main,
        [
            "db",
            "prune-sessions",
            "--older-than-days",
            "1",
            "--json",
            "--db-path",
            str(db_path),
        ],
    )
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["candidate_count"] == 1
    conn = connect_sqlite(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    finally:
        conn.close()

    applied = runner.invoke(
        main,
        [
            "db",
            "prune-sessions",
            "--older-than-days",
            "1",
            "--apply",
            "--json",
            "--db-path",
            str(db_path),
        ],
    )
    assert applied.exit_code == 0, applied.output
    payload = json.loads(applied.stdout)
    assert payload["pruned_session_ids"] == ["stale-invalid"]
    backup_path = payload["backup_path"]
    assert backup_path

    conn = connect_sqlite(db_path)
    backup_conn = connect_sqlite(backup_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert backup_conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert backup_conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        backup_conn.close()
        conn.close()


def test_prune_cli_holds_write_lock_across_backup_and_apply(
    tmp_path,
    monkeypatch,
):
    from reflect.store.sqlite import backup_sqlite as real_backup_sqlite

    db_path = tmp_path / "reflect.db"
    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        _seed_identity(conn)
        _insert_session(conn, "before-backup")
        conn.commit()
    finally:
        conn.close()

    writer_errors = []

    def backup_then_attempt_write(source_path, target_path, *, progress=None):
        real_backup_sqlite(source_path, target_path, progress=progress)
        writer = sqlite3.connect(source_path, timeout=0.05)
        try:
            _insert_session(writer, "after-backup")
            writer.commit()
        except sqlite3.OperationalError as exc:
            writer_errors.append(str(exc))
        finally:
            writer.close()

    monkeypatch.setattr(
        "reflect.store.sqlite.backup_sqlite",
        backup_then_attempt_write,
    )
    result = CliRunner().invoke(
        main,
        [
            "db",
            "prune-sessions",
            "--older-than-days",
            "60",
            "--apply",
            "--json",
            "--db-path",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["pruned_session_ids"] == ["before-backup"]
    assert writer_errors == ["database is locked"]
    assert "Backing up the local telemetry store... 100%" in result.stderr
    assert "Pruning eligible sessions and rebuilding derived data..." in result.stderr
    assert "Session pruning complete." in result.stderr
    backup_conn = sqlite3.connect(payload["backup_path"])
    conn = sqlite3.connect(db_path)
    try:
        assert backup_conn.execute(
            "SELECT id FROM sessions ORDER BY id"
        ).fetchall() == [("before-backup",)]
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    finally:
        conn.close()
        backup_conn.close()


def test_prune_cli_dry_run_does_not_migrate_an_outdated_store(tmp_path):
    db_path = tmp_path / "reflect.db"
    conn = connect_sqlite(db_path)
    try:
        _apply_migrations_through(conn, 21)
    finally:
        conn.close()

    result = CliRunner().invoke(
        main,
        ["db", "prune-sessions", "--json", "--db-path", str(db_path)],
    )

    assert result.exit_code == 1
    assert "pending migrations: 22" in result.output
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 21
    finally:
        conn.close()


def test_prune_cli_backs_up_before_migrating_for_apply(tmp_path):
    db_path = tmp_path / "reflect.db"
    conn = connect_sqlite(db_path)
    try:
        _apply_migrations_through(conn, 21)
    finally:
        conn.close()

    result = CliRunner().invoke(
        main,
        [
            "db",
            "prune-sessions",
            "--apply",
            "--json",
            "--db-path",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    backup_path = json.loads(result.stdout)["backup_path"]
    assert backup_path
    conn = sqlite3.connect(db_path)
    backup_conn = sqlite3.connect(backup_path)
    try:
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 22
        assert backup_conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 21
        assert backup_conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        backup_conn.close()
        conn.close()
