import json

import pytest

from reflect.store import normalize
from reflect.store.ingest import ingest_local_spans_file
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


@pytest.fixture
def conn(tmp_path):
    connection = connect_sqlite(tmp_path / "provenance.db")
    migrate(connection)
    yield connection
    connection.close()


def raw(conn, key, *, origin=None, source="otlp_logs_json", attrs=None, status="ok"):
    data = {"service.name": "claude-code", "session.id": "session", **(attrs or {})}
    conn.execute(
        """INSERT INTO raw_events(id,source_id,source_type,event_type,session_id,
        observed_at,received_at,origin_kind,attrs_json,normalized_status,content_hash,created_at)
        VALUES(?,'fixture',?,'Usage','session','2026-01-01','2026-01-01',?,?,?,?, '2026-01-01')""",
        (key, source, origin, json.dumps(data), status, key),
    )


def marker(conn):
    return conn.execute("SELECT value FROM store_metadata WHERE key=?", (normalize._PROVENANCE_REPAIR_KEY,)).fetchone()


@pytest.mark.parametrize("json_origin", [None, "native_otlp_log"])
def test_legacy_json_mismatch_is_repaired_once_and_routine_query_uses_index(conn, json_origin):
    attrs = {"reflect.telemetry.origin": json_origin} if json_origin else {}
    raw(conn, "legacy", origin="native_otlp_trace", attrs=attrs)
    conn.commit()
    queries = []
    conn.set_trace_callback(queries.append)
    normalize.normalize_pending_raw_events(conn, limit=0)
    assert any("json_extract(attrs_json" in sql for sql in queries)
    value = conn.execute("SELECT origin_kind,attrs_json FROM raw_events WHERE id='legacy'").fetchone()
    assert value[0] == "native_otlp_log"
    assert json.loads(value[1])["reflect.telemetry.origin"] == "native_otlp_log"
    assert marker(conn)[0] == "complete"
    queries.clear()
    normalize.normalize_pending_raw_events(conn, limit=0)
    conn.set_trace_callback(None)
    assert not any("json_extract(attrs_json" in sql for sql in queries)
    candidate_query = next(sql for sql in queries if "SELECT id, session_id, source_type, origin_kind, attrs_json" in sql)
    plan = " ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + candidate_query))
    assert "idx_raw_events_origin_kind" in plan
    assert "SCAN raw_events" not in plan


def test_new_raw_and_ingested_events_after_checkpoint_are_classified(conn, tmp_path):
    normalize.normalize_pending_raw_events(conn, limit=0)
    raw(conn, "new", status="pending", attrs={"gen_ai.usage.input_tokens": 12, "gen_ai.usage.output_tokens": 7})
    conn.commit()
    spans = tmp_path / "hooks.jsonl"
    spans.write_text(json.dumps({"name": "Stop", "spanId": "hook", "traceId": "trace", "start_time_ns": 1_000_000_000,
        "end_time_ns": 2_000_000_000, "attributes": {"gen_ai.client.name": "claude", "gen_ai.client.session_id": "hook-session"}}) + "\n")
    ingest_local_spans_file(conn, file_path=spans)
    result = normalize.normalize_pending_raw_events(conn)
    assert result["processed"] == 2
    assert conn.execute("SELECT origin_kind FROM raw_events WHERE id='new'").fetchone()[0] == "native_otlp_log"
    assert conn.execute("SELECT origin_kind FROM raw_events WHERE source_type='local_spans_jsonl'").fetchone()[0] == "hook_jsonl"
    assert conn.execute("SELECT COUNT(*) FROM steps WHERE origin_kind IS NULL").fetchone()[0] == 0


def test_unknown_context_remains_unknown_without_repeating_legacy_scan(conn):
    raw(conn, "context", source="native_context")
    conn.commit()
    before = tuple(conn.execute("SELECT * FROM raw_events WHERE id='context'").fetchone())
    normalize.normalize_pending_raw_events(conn, limit=0)
    normalize.normalize_pending_raw_events(conn, limit=0)
    assert tuple(conn.execute("SELECT * FROM raw_events WHERE id='context'").fetchone()) == before
    assert marker(conn)[0] == "complete"


def test_interrupted_repair_rolls_back_changes_and_does_not_checkpoint(conn, monkeypatch):
    raw(conn, "first")
    raw(conn, "second")
    conn.commit()
    # Caller work predating the repair savepoint must remain its own transaction.
    conn.execute("INSERT INTO store_metadata(key,value,updated_at) VALUES('caller','pending','now')")
    classify = normalize.classify_origin_kind
    calls = 0

    def interrupted(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return classify(*args)

    monkeypatch.setattr(normalize, "classify_origin_kind", interrupted)
    with pytest.raises(KeyboardInterrupt):
        normalize.normalize_pending_raw_events(conn, limit=0)
    assert marker(conn) is None
    assert conn.execute("SELECT COUNT(*) FROM raw_events WHERE origin_kind IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT value FROM store_metadata WHERE key='caller'").fetchone()[0] == "pending"
    assert conn.in_transaction
    monkeypatch.setattr(normalize, "classify_origin_kind", classify)
    normalize.normalize_pending_raw_events(conn, limit=0)
    assert marker(conn)[0] == "complete"


def test_explicit_repair_remains_full_and_preserves_canonical_usage_and_identity(conn):
    raw(conn, "usage", status="pending", attrs={"gen_ai.usage.input_tokens": 100, "gen_ai.usage.output_tokens": 50})
    conn.commit()
    normalize.normalize_pending_raw_events(conn)
    before_calls = [tuple(row) for row in conn.execute("SELECT * FROM llm_calls ORDER BY id")]
    before_sessions = [tuple(row) for row in conn.execute("SELECT id,input_tokens,output_tokens FROM sessions ORDER BY id")]
    step_id = conn.execute("SELECT id FROM steps").fetchone()[0]
    conn.execute("UPDATE steps SET raw_attrs_json=json_set(raw_attrs_json,'$.canonical_marker','preserved')")
    conn.execute("UPDATE raw_events SET attrs_json=json_remove(attrs_json,'$.\"reflect.telemetry.origin\"')")
    conn.commit()
    assert normalize.repair_telemetry_provenance(conn) == {"raw_events": 1, "steps": 1}
    assert [tuple(row) for row in conn.execute("SELECT * FROM llm_calls ORDER BY id")] == before_calls
    assert [tuple(row) for row in conn.execute("SELECT id,input_tokens,output_tokens FROM sessions ORDER BY id")] == before_sessions
    step = conn.execute("SELECT id,raw_attrs_json FROM steps").fetchone()
    assert step[0] == step_id
    assert json.loads(step[1])["canonical_marker"] == "preserved"
