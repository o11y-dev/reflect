import json
from unittest.mock import patch

import pytest

from reflect.dashboard_sessions import load_session_detail
from reflect.store.graph_normalize import rebuild_graph
from reflect.store.graph_ontology import graph_id
from reflect.store.ingest import ingest_local_spans_file
from reflect.store.migrate import load_migrations, migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.provenance import telemetry_agent_name
from reflect.store.rollups import rebuild_rollups, rollup_rebuild_pending
from reflect.store.sqlite import connect_sqlite
from reflect.views.sessions import list_sessions


def _span(name, **attrs):
    return {
        "name": name, "traceId": "trace", "spanId": name,
        "start_time_ns": 1791183100000000000, "end_time_ns": 1791183101000000000,
        "attributes": attrs,
    }


def _ingest(conn, path, *spans):
    path.write_text("\n".join(json.dumps(span) for span in spans))
    ingest_local_spans_file(conn, file_path=path)
    result = normalize_pending_raw_events(conn)
    assert result["failed"] == 0


@pytest.mark.parametrize("service", [
    "codex_cli_rs", "codex-app-server", "codex-exec-server", "codex_exec", "Codex Desktop",
])
def test_codex_sources_share_product_identity_without_merging_conversations(tmp_path, service):
    db = tmp_path / "reflect.db"
    conn = connect_sqlite(db)
    try:
        migrate(conn)
        _ingest(conn, tmp_path / "events.jsonl",
            _span("native", **{"service.name": service, "conversation.id": "conversation-1"}),
            _span("hook", **{
                "service.name": "ide-agent", "gen_ai.client.name": "codex",
                "gen_ai.client.session_id": "conversation-1", "gen_ai.client.hook.event": "Stop",
                "gen_ai.request.model": "gpt-5.4", "gen_ai.usage.input_tokens": 11,
                "gen_ai.usage.output_tokens": 3,
            }),
            _span("other", **{"service.name": service, "conversation.id": "conversation-2"}),
        )
        rebuild_rollups(conn)
        page = list_sessions(conn, agent="codex")
        assert {row.session_id for row in page.rows} == {"conversation-1", "conversation-2"}
        assert {row.agent for row in page.rows} == {"codex"}
        assert sum(row.input_tokens for row in page.rows) == 11
        assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 3
        detail = load_session_detail(db, "conversation-1")
        assert detail["telemetry"]["summary"]["service_names"] == sorted([service, "ide-agent"])
    finally:
        conn.close()


def test_sessionless_runtime_is_skipped_and_unattributed_hooks_are_retained(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _ingest(conn, tmp_path / "unassigned.jsonl",
            _span("runtime", **{"service.name": "codex-exec-server", "code.module.name": "h2::codec"}),
            _span("stop", **{"gen_ai.client.name": "codex", "gen_ai.client.hook.event": "Stop"}),
            _span("tool", **{
                "service.name": "codex-app-server", "gen_ai.client.tool_name": "Read",
                "gen_ai.client.tool.output": "retained evidence",
            }),
        )
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        rows = conn.execute("SELECT session_id, normalized_status, attrs_json FROM raw_events").fetchall()
        assert len(rows) == 2
        assert all(row[:2] == (None, "ignored") for row in rows)
        assert all(json.loads(row[2])["reflect.telemetry.classification"] == "unattributed_session" for row in rows)
        assert any(json.loads(row[2]).get("gen_ai.client.tool.output") == "retained evidence" for row in rows)
    finally:
        conn.close()


@pytest.mark.parametrize("identity_key", [
    "session.id", "gen_ai.session.id", "gen_ai.client.session_id", "gen_ai.conversation.id",
    "conversation.id", "session_id", "ide.session_id",
])
def test_explicit_codex_identity_is_never_classified_as_runtime(tmp_path, identity_key):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _ingest(conn, tmp_path / "events.jsonl",
            _span("tool", **{"service.name": "codex-exec-server", identity_key: "actual-session"}),
        )
        assert conn.execute("SELECT id FROM sessions").fetchall() == [("actual-session",)]
    finally:
        conn.close()


def test_migration_preserves_usage_ids_and_unattributed_evidence(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrations = load_migrations()
        with patch("reflect.store.migrate.load_migrations", return_value=[m for m in migrations if m.version < 38]):
            migrate(conn)
        # Reproduce historical service-based identities and file-based sessions.
        with (
            patch("reflect.store.normalize.refresh_command_projection"),
            patch("reflect.store.normalize.telemetry_agent_name", side_effect=lambda attrs: attrs.get("gen_ai.client.name") or attrs.get("service.name", "codex")),
            patch("reflect.store.ingest.unassigned_codex_classification", return_value=None),
            patch("reflect.store.normalize.unassigned_codex_classification", return_value=None),
            patch("reflect.store.normalize.PendingRefreshState.mark"),
            patch("reflect.store.normalize.PendingRefreshState.mark_steps"),
            patch("reflect.store.normalize.PendingRefreshState.capture") as pending_capture,
            patch("reflect.store.normalize.PendingRefreshState.acknowledge"),
        ):
            pending_capture.return_value.pending_for.return_value = set()
            _ingest(conn, tmp_path / "real.jsonl",
                _span("first", **{"service.name": "codex-app-server", "conversation.id": "real-1", "gen_ai.usage.input_tokens": 31}),
                _span("second", **{"service.name": "codex_cli_rs", "conversation.id": "real-2", "gen_ai.usage.input_tokens": 17}),
            )
            _ingest(conn, tmp_path / "unassigned.jsonl",
                _span("runtime", **{"service.name": "codex-exec-server"}),
                _span("stop", **{"service.name": "ide-agent", "gen_ai.client.name": "codex", "gen_ai.client.hook.event": "Stop"}),
                _span("tool", **{"service.name": "codex-exec-server", "gen_ai.client.tool_name": "Read", "gen_ai.client.tool.output": "preserve me"}),
            )
            _ingest(conn, tmp_path / "mixed.jsonl",
                _span("runtime", **{"service.name": "codex-exec-server"}),
                _span("unknown", **{"service.name": "custom-agent", "gen_ai.client.tool_name": "Read"}),
            )
        # Seed only the historical projections exercised by migration 038.
        # Current graph/rollup builders require the current schema.
        conn.execute("""INSERT INTO session_rollups(session_id,agent,input_tokens,updated_at)
            SELECT s.id,a.name,s.input_tokens,s.updated_at FROM sessions s JOIN agents a ON a.id=s.agent_id""")
        for name, created in conn.execute("SELECT name,created_at FROM agents").fetchall():
            conn.execute("INSERT INTO graph_nodes(id,kind,label,created_at,updated_at) VALUES(?,'Agent',?,?,?)",
                         (graph_id("node", "Agent", name, ""), name, created, created))
        for sid, name, created in conn.execute("SELECT s.id,a.name,s.created_at FROM sessions s JOIN agents a ON a.id=s.agent_id").fetchall():
            source, target = graph_id("node", "Agent", name, ""), graph_id("node", "Session", sid, sid)
            conn.execute("INSERT INTO graph_nodes(id,kind,label,session_id,created_at,updated_at) VALUES(?,'Session',?,?,?,?)",
                         (target, sid, sid, created, created))
            conn.execute("""INSERT INTO graph_edges(id,source_node_id,target_node_id,kind,session_id,created_at,updated_at)
                VALUES(?,?,?,'ran_session',?,?,?)""", (graph_id("edge", "ran_session", source, target, sid), source, target, sid, created, created))
        real_before = conn.execute("SELECT id, input_tokens FROM sessions WHERE id LIKE 'real-%' ORDER BY id").fetchall()
        graph_before = conn.execute("SELECT id, session_id FROM graph_nodes WHERE kind = 'Session' AND session_id LIKE 'real-%' ORDER BY id").fetchall()
        unassigned_id = conn.execute("SELECT id FROM sessions WHERE source_ref LIKE '%unassigned.jsonl'").fetchone()[0]
        mixed_id = conn.execute("SELECT id FROM sessions WHERE source_ref LIKE '%mixed.jsonl'").fetchone()[0]
        # Retention already reclaimed one raw record, but its step still exists.
        conn.execute("DELETE FROM raw_events WHERE event_type = 'tool'")
        conn.commit()

        with patch("reflect.store.migrate.load_migrations", return_value=[m for m in migrations if m.version <= 38]):
            assert migrate(conn) == [38]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT id, input_tokens FROM sessions WHERE id LIKE 'real-%' ORDER BY id").fetchall() == real_before
        assert conn.execute("SELECT id, session_id FROM graph_nodes WHERE kind = 'Session' AND session_id LIKE 'real-%' ORDER BY id").fetchall() == graph_before
        assert conn.execute("SELECT COUNT(*) FROM graph_edges e JOIN graph_nodes n ON n.id=e.source_node_id WHERE n.kind='Agent' AND n.label='codex' AND e.session_id LIKE 'real-%'").fetchone()[0] == 2
        assert {row[0] for row in conn.execute("SELECT label FROM graph_nodes WHERE kind = 'Agent' AND label LIKE 'codex%'")} == {"codex"}
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", (unassigned_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id = ?", (mixed_id,)).fetchone()[0] == 1
        retained = [json.loads(row[0]) for row in conn.execute("SELECT attrs_json FROM raw_events WHERE normalized_status = 'ignored'")]
        assert any(row.get("gen_ai.client.tool.output") == "preserve me" and row.get("reflect.telemetry.recovered_from_step") for row in retained)
        assert {row[0] for row in conn.execute("SELECT name FROM agents WHERE name LIKE 'codex%'")} == {"codex"}
        assert migrate(conn) == [m.version for m in migrations if m.version > 38]
        assert conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0] == 0
        assert {row[0] for row in conn.execute("SELECT session_id FROM pending_refresh WHERE phase='graph'")} >= {"real-1", "real-2"}
        assert conn.execute("SELECT id, input_tokens FROM sessions WHERE id LIKE 'real-%' ORDER BY id").fetchall() == real_before
        assert normalize_pending_raw_events(conn) == {"processed": 0, "failed": 0, "skipped": 0}
        assert rollup_rebuild_pending(conn)
        rebuild_rollups(conn)
        rebuild_graph(conn)
        assert not rollup_rebuild_pending(conn)
        assert {row[0] for row in conn.execute("SELECT label FROM graph_nodes WHERE kind = 'Agent' AND label LIKE 'codex%'")} == {"codex"}
        assert {row.agent for row in list_sessions(conn, agent="codex").rows} == {"codex"}
        assert conn.execute("SELECT SUM(input_tokens) FROM session_rollups").fetchone()[0] == 48
        assert migrate(conn) == []
    finally:
        conn.close()


def test_agent_mapping_respects_explicit_non_codex_identity():
    assert telemetry_agent_name({"gen_ai.client.name": "custom-agent", "service.name": "codex-app-server"}) == "custom-agent"
