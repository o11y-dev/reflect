import json
from contextlib import closing

from reflect.preparation_pipeline import prepare_sql_report_db
from reflect.store.pending_refresh import PendingRefreshState
from reflect.store.sqlite import connect_sqlite


def test_report_discovers_fresh_checked_procedure_before_phase_ack(tmp_path, monkeypatch):
    monkeypatch.setenv("REFLECT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("reflect.pricing._fetch_json_url", lambda *args, **kwargs: {})
    spans = tmp_path / "spans"
    spans.mkdir()
    for session in range(3):
        records = []
        events = [
            ("Implement feature build feature add feature", {"gen_ai.client.hook.event": "UserPromptSubmit"}),
            ("PostToolUse", {"gen_ai.client.tool_name": "Read", "gen_ai.client.file_path": "src/payments.py"}),
            ("PostToolUse", {"gen_ai.client.tool_name": "Edit", "gen_ai.client.file_path": "src/payments.py"}),
            ("PostToolUse", {"gen_ai.client.tool_name": "exec_command", "gen_ai.client.command": "pytest tests/payments",
                             "gen_ai.client.tool.input": json.dumps({"cmd": "pytest tests/payments"}),
                             "process.exit.code": 0}),
            ("Stop", {"reflect.turn.terminal": True}),
        ]
        for index, (name, attrs) in enumerate(events):
            at = 1780000000000000000 + (session * 100 + index) * 1_000_000_000
            records.append({"name": name, "spanId": f"{session}-{index}", "traceId": str(session),
                "start_time_ns": at, "end_time_ns": at + 100,
                "attributes": {"gen_ai.client.name": "claude", "gen_ai.client.session_id": str(session),
                    "gen_ai.client.turn_id": str(session), "gen_ai.client.cwd": str(tmp_path / "project"),
                    **attrs}})
        (spans / f"{session}.jsonl").write_text("".join(json.dumps(record) + "\n" for record in records))
    db = tmp_path / "reflect.db"
    prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    with closing(connect_sqlite(db)) as conn:
        assessments = [json.loads(row[0])["assessment"] for row in conn.execute(
            "SELECT boundary_json FROM execution_units WHERE eligible=1"
        )]
        assert len(assessments) == 3
        assert [assessment["verification"] for assessment in assessments] == ["passed"] * 3
        assert conn.execute("SELECT COUNT(*) FROM observations WHERE rule_id='high_performing_repeated_workflow' AND status='proposal_ready'").fetchone()[0] == 1
        assert not PendingRefreshState(conn).capture().pending_for("improvements")
        original = list(conn.execute("SELECT id,boundary_json FROM execution_units ORDER BY id"))
        PendingRefreshState(conn).mark({"0", "1", "2"}, phases=("improvements",))
        conn.commit()
    # A retry/config repair can process the same unchanged units. It must not
    # retire the finding merely because its own phase remains unacknowledged.
    prepare_sql_report_db(db, otlp_traces=None, spans_dir=spans)
    with closing(connect_sqlite(db)) as conn:
        assert list(conn.execute("SELECT id,boundary_json FROM execution_units ORDER BY id")) == original
        assert conn.execute("SELECT COUNT(*) FROM observations WHERE rule_id='high_performing_repeated_workflow' AND status='proposal_ready'").fetchone()[0] == 1
        assert not PendingRefreshState(conn).capture().pending_for("improvements")
