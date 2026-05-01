import json

from reflect.store.ingest import ingest_otlp_traces_file
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


def _write_otlp_file(path):
    payload = {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "gen_ai.client.name", "value": {"stringValue": "claude"}}]},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "UserPromptSubmit",
                                "traceId": "t1",
                                "spanId": "s1",
                                "parentSpanId": "",
                                "startTimeUnixNano": "100",
                                "endTimeUnixNano": "200",
                                "attributes": [{"key": "session.id", "value": {"stringValue": "sess-1"}}],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def test_ingest_otlp_traces_dedupes(tmp_path):
    db = tmp_path / "reflect.db"
    otlp = tmp_path / "traces.json"
    _write_otlp_file(otlp)

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        first = ingest_otlp_traces_file(conn, file_path=otlp)
        second = ingest_otlp_traces_file(conn, file_path=otlp)

        assert first == {"inserted": 1, "skipped": 0}
        assert second == {"inserted": 0, "skipped": 1}

        row = conn.execute("SELECT source_type, event_type, session_id FROM raw_events").fetchone()
        assert row == ("otlp_traces_json", "UserPromptSubmit", "sess-1")
    finally:
        conn.close()
