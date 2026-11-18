from __future__ import annotations

import json
from datetime import UTC, datetime

from reflect.store.cursor_usage import (
    apply_cursor_transcript_usage_estimates,
    repair_misattributed_cursor_transcript_usage,
)
from reflect.store.migrate import migrate
from reflect.store.pending_refresh import PendingRefreshState
from reflect.store.sqlite import connect_sqlite

NOW = datetime(2026, 8, 6, tzinfo=UTC).isoformat()


def _seed_session(conn, *, agent: str, session_id: str, input_tokens: int = 0) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO agents(id, name, raw_json, created_at, updated_at)
        VALUES (?, ?, '{}', ?, ?)
        """,
        (agent, agent, NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO sessions(
          id, agent_id, started_at, status, input_tokens, source_kind,
          source_ref, created_at, updated_at
        ) VALUES (?, ?, ?, 'ok', ?, 'native_session', ?, ?, ?)
        """,
        (session_id, agent, NOW, input_tokens, f"native_session:{agent}:/tmp/source", NOW, NOW),
    )


def test_cursor_estimate_refuses_session_owned_by_another_agent(tmp_path):
    transcript = tmp_path / "shared-session.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "message": {"content": "Please fix it"}}),
                json.dumps({"role": "assistant", "message": {"content": "Fixed"}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _seed_session(conn, agent="claude", session_id="shared-session")

        result = apply_cursor_transcript_usage_estimates(conn, [transcript])

        assert result == {
            "updated": 0,
            "skipped": 1,
            "missing": 0,
            "session_ids": [],
        }
        assert conn.execute(
            "SELECT input_tokens FROM sessions WHERE id = 'shared-session'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM steps WHERE session_id = 'shared-session'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_repair_removes_misattributed_cursor_estimate_and_tokens(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _seed_session(conn, agent="claude", session_id="contaminated", input_tokens=123)
        conn.execute(
            "UPDATE sessions SET output_tokens = 45, estimated_cost_usd = 1.5 WHERE id = 'contaminated'"
        )
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, summary,
              raw_attrs_json, created_at, updated_at
            ) VALUES (
              'estimate-step', 'contaminated', 0, 'token_estimate', ?, 'ok',
              'cursor.transcript.token_estimate',
              '{"reflect.token.source":"estimated_cursor_transcript"}', ?, ?
            )
            """,
            (NOW, NOW, NOW),
        )
        conn.commit()

        result = repair_misattributed_cursor_transcript_usage(conn)

        assert result == {"repaired": 1, "session_ids": ["contaminated"]}
        session = conn.execute(
            """
            SELECT input_tokens, output_tokens, estimated_cost_usd
            FROM sessions WHERE id = 'contaminated'
            """
        ).fetchone()
        assert tuple(session) == (0, 0, 0.0)
        assert conn.execute(
            "SELECT COUNT(*) FROM steps WHERE id = 'estimate-step'"
        ).fetchone()[0] == 0
        reader = connect_sqlite(tmp_path / "reflect.db")
        try:
            assert PendingRefreshState(reader).capture().pending_for("cost") == {"contaminated"}
        finally:
            reader.close()
    finally:
        conn.close()


def test_repair_uses_estimate_index_without_decoding_unrelated_step_json(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _seed_session(conn, agent="claude", session_id="contaminated", input_tokens=123)
        _seed_session(conn, agent="cursor", session_id="cursor", input_tokens=456)
        estimate = json.dumps({"reflect.token.source": "estimated_cursor_transcript"})
        unrelated = json.dumps({"large_tool_output": "x" * 65536})
        conn.executemany(
            """INSERT INTO steps(
                 id, session_id, seq, type, raw_attrs_json, started_at, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("bad-estimate", "contaminated", 0, "token_estimate", estimate, NOW, NOW, NOW),
                ("valid-estimate", "cursor", 0, "token_estimate", estimate, NOW, NOW, NOW),
                *[(f"tool-{i}", "contaminated", i + 1, "tool_call", unrelated, NOW, NOW, NOW)
                  for i in range(16)],
            ],
        )
        conn.commit()

        def extract_estimate_source(document, path):
            if path != '$."reflect.token.source"':
                return None  # Other indexed attributes are absent from this fixture.
            assert document != unrelated, "Repair decoded an unrelated tool payload"
            return json.loads(document).get("reflect.token.source")

        conn.create_function("json_extract", 2, extract_estimate_source, deterministic=True)
        queries = []
        conn.set_trace_callback(queries.append)
        assert repair_misattributed_cursor_transcript_usage(conn) == {
            "repaired": 1, "session_ids": ["contaminated"],
        }
        candidate_query = next(sql for sql in queries if "SELECT DISTINCT st.session_id" in sql)
        plan = conn.execute("EXPLAIN QUERY PLAN " + candidate_query).fetchall()
        assert any("SEARCH st USING INDEX" in row[3] for row in plan)
        assert conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 17
        assert conn.execute("SELECT input_tokens FROM sessions WHERE id='cursor'").fetchone()[0] == 456

        before = conn.total_changes
        assert repair_misattributed_cursor_transcript_usage(conn) == {
            "repaired": 0, "session_ids": [],
        }
        assert conn.total_changes == before
    finally:
        conn.close()


def test_estimate_commits_dirty_scope_with_canonical_tokens(tmp_path):
    transcript = tmp_path / "cursor-session.jsonl"
    transcript.write_text(json.dumps({"role": "user", "message": {"content": "Estimate these captured tokens"}}))
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _seed_session(conn, agent="cursor", session_id="cursor-session")
        assert apply_cursor_transcript_usage_estimates(conn, [transcript])["updated"] == 1
        reader = connect_sqlite(tmp_path / "reflect.db")
        try:
            assert reader.execute("SELECT input_tokens FROM sessions").fetchone()[0] > 0
            assert PendingRefreshState(reader).capture().pending_for("cost") == {"cursor-session"}
            assert PendingRefreshState(reader).capture().pending_for("improvements") == {"cursor-session"}
        finally:
            reader.close()
    finally:
        conn.close()
