import hashlib
import json

from reflect.store.hook_facts import HookFactParser, HookFactRepository, backfill_hook_facts
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.sqlite import connect_sqlite


def _insert_raw_event(conn, event_id, event_name, attrs, observed_at):
    conn.execute(
        """
        INSERT INTO raw_events(
          id, source_id, source_type, event_type, trace_id, span_id, parent_span_id,
          session_id, observed_at, received_at, attrs_json, body_json,
          normalized_status, content_hash, created_at
        ) VALUES (?, 'hook-v1.jsonl', 'local_spans_jsonl', ?, 'trace-1', ?, '',
                  'session-v1', ?, ?, ?, '{}', 'pending', ?, ?)
        """,
        (
            event_id,
            f"gen_ai.client.hook.{event_name}",
            f"span-{event_id}",
            observed_at,
            observed_at,
            json.dumps(attrs, sort_keys=True),
            f"hash-{event_id}",
            observed_at,
        ),
    )


def test_hook_fact_parser_accepts_metadata_only_conversation():
    digest = hashlib.sha256(b"private prompt").hexdigest()
    contract = HookFactParser().parse(
        {
            "gen_ai.client.hook.event": "UserPromptSubmit",
            "gen_ai.client.telemetry_source": "hook",
            "gen_ai.client.hook_schema_version": 1,
            "gen_ai.client.prompt.length": 14,
            "gen_ai.client.prompt.sha256": digest,
        }
    )

    assert contract.schema_version == 1
    assert contract.telemetry_source == "hook"
    assert contract.conversation[0].kind == "prompt"
    assert contract.conversation[0].preview is None
    assert contract.conversation[0].content_hash == digest


def test_normalize_persists_hook_conversation_agent_and_native_facts(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        prompt_hash = hashlib.sha256(b"private prompt").hexdigest()
        response_hash = hashlib.sha256(b"safe response").hexdigest()
        common = {
            "gen_ai.client.name": "codex",
            "gen_ai.client.session_id": "session-v1",
            "gen_ai.client.telemetry_source": "hook",
            "gen_ai.client.hook_schema_version": 1,
            "gen_ai.client.hook.provider_adapter": "codex",
        }
        _insert_raw_event(
            conn,
            "prompt",
            "UserPromptSubmit",
            {
                **common,
                "gen_ai.client.hook.event": "UserPromptSubmit",
                "gen_ai.client.hook.event_id": "hook:prompt-1",
                "gen_ai.client.hook.event_id_source": "hook",
                "gen_ai.client.prompt.length": 14,
                "gen_ai.client.prompt.sha256": prompt_hash,
            },
            "2026-07-22T10:00:00+00:00",
        )
        _insert_raw_event(
            conn,
            "response",
            "Stop",
            {
                **common,
                "gen_ai.client.hook.event": "Stop",
                "gen_ai.client.hook.event_id": "provider-response-1",
                "gen_ai.client.hook.event_id_source": "provider",
                "gen_ai.client.response.length": 13,
                "gen_ai.client.response.sha256": response_hash,
                "gen_ai.client.response.text": "safe response",
                "gen_ai.client.native_trace_id": "1" * 32,
                "gen_ai.client.native_span_id": "2" * 16,
            },
            "2026-07-22T10:00:01+00:00",
        )
        _insert_raw_event(
            conn,
            "subagent",
            "SubagentStart",
            {
                **common,
                "gen_ai.client.hook.event": "SubagentStart",
                "gen_ai.client.hook.event_id": "provider-agent-1",
                "gen_ai.client.agent_id": "agent-child",
                "gen_ai.client.parent_agent_id": "agent-root",
                "gen_ai.client.agent_id_source": "provider",
                "gen_ai.client.subagent_type": "reviewer",
                "gen_ai.client.delegation.task.length": 12,
                "gen_ai.client.delegation.task.sha256": hashlib.sha256(
                    b"review tests"
                ).hexdigest(),
            },
            "2026-07-22T10:00:02+00:00",
        )

        assert normalize_pending_raw_events(conn) == {
            "processed": 3,
            "failed": 0,
            "skipped": 0,
        }

        facts = conn.execute(
            """
            SELECT kind, content_hash, content_length, content_preview_redacted
            FROM conversation_facts
            ORDER BY kind
            """
        ).fetchall()
        assert [tuple(row) for row in facts] == [
            (
                "delegation.task",
                hashlib.sha256(b"review tests").hexdigest(),
                12,
                None,
            ),
            ("prompt", prompt_hash, 14, None),
            ("response", response_hash, 13, "safe response"),
        ]
        llm = conn.execute(
            """
            SELECT operation_name, prompt_hash, response_hash, response_preview_redacted
            FROM llm_calls
            ORDER BY operation_name
            """
        ).fetchall()
        assert [tuple(row) for row in llm] == [
            ("Stop", None, response_hash, "safe response"),
            ("UserPromptSubmit", prompt_hash, None, None),
        ]
        agent = conn.execute(
            """
            SELECT event_name, event_id, agent_id, parent_agent_id, agent_type,
                   task_length, task_preview_redacted
            FROM agent_events
            """
        ).fetchone()
        assert tuple(agent) == (
            "SubagentStart",
            "provider-agent-1",
            "agent-child",
            "agent-root",
            "reviewer",
            12,
            None,
        )
        native = conn.execute(
            """
            SELECT telemetry_source, hook_schema_version, hook_provider_adapter,
                   native_trace_id, native_span_id
            FROM steps
            WHERE hook_event_id = 'provider-response-1'
            """
        ).fetchone()
        assert tuple(native) == ("hook", 1, "codex", "1" * 32, "2" * 16)
    finally:
        conn.close()


def test_hook_fact_backfill_is_idempotent(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        conn.execute(
            """
            INSERT INTO agents(id, name, created_at, updated_at)
            VALUES ('agent', 'claude', '2026-07-22', '2026-07-22')
            """
        )
        conn.execute(
            """
            INSERT INTO sessions(id, agent_id, started_at, status, created_at, updated_at)
            VALUES ('session', 'agent', '2026-07-22', 'ok', '2026-07-22', '2026-07-22')
            """
        )
        attrs = {
            "gen_ai.client.hook.event": "Stop",
            "gen_ai.client.response.length": 5,
            "gen_ai.client.response.sha256": hashlib.sha256(b"hello").hexdigest(),
        }
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, summary,
              raw_attrs_json, created_at, updated_at
            ) VALUES ('step', 'session', 0, 'llm_call', '2026-07-22', 'ok', 'Stop',
                      ?, '2026-07-22', '2026-07-22')
            """,
            (json.dumps(attrs),),
        )

        first = backfill_hook_facts(conn, session_ids={"session"}, timestamp="2026-07-22")
        second = backfill_hook_facts(conn, session_ids={"session"}, timestamp="2026-07-22")
        assert first["conversation_facts"] == 1
        assert second["conversation_facts"] == 0
        assert conn.execute("SELECT COUNT(*) FROM conversation_facts").fetchone()[0] == 1
    finally:
        conn.close()


def test_hook_fact_backfill_does_not_rewrite_unrelated_steps(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        conn.execute(
            """
            INSERT INTO agents(id, name, created_at, updated_at)
            VALUES ('agent', 'gemini', '2026-07-22', '2026-07-22')
            """
        )
        conn.execute(
            """
            INSERT INTO sessions(id, agent_id, started_at, status, created_at, updated_at)
            VALUES ('session', 'agent', '2026-07-22', 'ok', '2026-07-22', '2026-07-22')
            """
        )
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, summary,
              raw_attrs_json, created_at, updated_at
            ) VALUES (
              'ordinary-step', 'session', 0, 'tool_call', '2026-07-22', 'ok',
              'Read', '{"gen_ai.client.tool_name":"Read"}',
              '2026-07-22', '2026-07-22'
            )
            """
        )

        result = backfill_hook_facts(
            conn,
            session_ids={"session"},
            timestamp="2026-07-23",
        )

        assert result == {"steps": 0, "conversation_facts": 0, "agent_events": 0}
        assert conn.execute(
            "SELECT updated_at FROM steps WHERE id = 'ordinary-step'"
        ).fetchone()[0] == "2026-07-22"
    finally:
        conn.close()


def test_hook_fact_repository_builds_session_read_model(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        conn.execute(
            """
            INSERT INTO agents(id, name, created_at, updated_at)
            VALUES ('agent', 'codex', '2026-07-22', '2026-07-22')
            """
        )
        conn.execute(
            """
            INSERT INTO sessions(id, agent_id, started_at, status, created_at, updated_at)
            VALUES ('session', 'agent', '2026-07-22', 'ok', '2026-07-22', '2026-07-22')
            """
        )
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, summary,
              telemetry_source, hook_schema_version, hook_provider_adapter,
              native_trace_id, raw_attrs_json, created_at, updated_at
            ) VALUES (
              'step', 'session', 0, 'llm_call', '2026-07-22', 'ok', 'Stop',
              'hook', 1, 'codex', 'native-trace', '{}', '2026-07-22', '2026-07-22'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO conversation_facts(
              id, step_id, session_id, kind, role, content_length,
              raw_attrs_json, created_at, updated_at
            ) VALUES (
              'fact', 'step', 'session', 'response', 'assistant', 5,
              '{}', '2026-07-22', '2026-07-22'
            )
            """
        )

        view = HookFactRepository(conn).load_session("session")
        step = {
            "hook_schema_version": 1,
            "hook_provider_adapter": "codex",
            "telemetry_source": "hook",
            "native_trace_id": "native-trace",
        }

        assert view.responses_for_step("step")[0]["id"] == "fact"
        assert view.summary([step]) == {
            "hook_schema_versions": [1],
            "provider_adapters": ["codex"],
            "telemetry_sources": ["hook"],
            "native_linked_spans": 1,
            "conversation_facts": 1,
            "agent_relationships": 0,
        }
    finally:
        conn.close()
