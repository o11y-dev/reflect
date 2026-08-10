from __future__ import annotations

import json
from datetime import UTC, datetime

from reflect.store.cursor_usage import (
    apply_cursor_transcript_usage_estimates,
    repair_misattributed_cursor_transcript_usage,
)
from reflect.store.migrate import migrate
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
    finally:
        conn.close()
