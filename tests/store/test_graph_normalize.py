import json

from reflect.store.graph_normalize import rebuild_graph
from reflect.store.ingest import ingest_local_spans_file
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.sqlite import connect_sqlite


def _write_spans(path):
    spans = [
        {
            "name": "UserPromptSubmit",
            "traceId": "trace-1",
            "spanId": "span-1",
            "start_time_ns": 100,
            "end_time_ns": 200,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-graph",
                "gen_ai.request.model": "claude-4.6-opus",
            },
        },
        {
            "name": "PreToolUse",
            "traceId": "trace-1",
            "spanId": "span-2",
            "start_time_ns": 300,
            "end_time_ns": 500,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-graph",
                "gen_ai.client.tool_name": "Read",
            },
        },
        {
            "name": "BeforeMCPExecution",
            "traceId": "trace-1",
            "spanId": "span-3",
            "start_time_ns": 600,
            "end_time_ns": 800,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-graph",
                "gen_ai.client.mcp_server": "mcp-github",
                "gen_ai.client.mcp_tool": "get_issue",
            },
        },
        {
            "name": "MemoryWrite",
            "traceId": "trace-1",
            "spanId": "span-4",
            "start_time_ns": 900,
            "end_time_ns": 1000,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-graph",
                "gen_ai.memory.id": "mem-graph",
                "gen_ai.memory.scope": "repo",
                "gen_ai.memory.type": "repo_convention",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(span) for span in spans) + "\n", encoding="utf-8")


def test_rebuild_graph_from_canonical_tables_is_idempotent(tmp_path):
    db = tmp_path / "reflect.db"
    spans = tmp_path / "spans.jsonl"
    _write_spans(spans)

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        ingest_local_spans_file(conn, file_path=spans)
        normalize_pending_raw_events(conn)

        first = rebuild_graph(conn)
        second = rebuild_graph(conn)

        assert first["nodes"] >= 8
        assert first["edges"] >= 7
        assert second == {"nodes": 0, "edges": 0}
        node_kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM graph_nodes")}
        assert {"Session", "Step", "Agent", "Tool", "MCPServer", "Memory"} <= node_kinds
        edge_kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM graph_edges")}
        assert {"ran_session", "has_step", "used_tool", "used_mcp", "recorded_memory"} <= edge_kinds
    finally:
        conn.close()


def test_rebuild_graph_adds_semantic_workflow_nodes(tmp_path):
    db = tmp_path / "reflect.db"
    conn = connect_sqlite(db)
    try:
        migrate(conn)
        now = "2026-05-26T00:00:00+00:00"
        conn.execute(
            """
            INSERT INTO agents(id, name, kind, raw_json, created_at, updated_at)
            VALUES ('agent-1', 'codex', 'cli', '{}', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO repos(id, provider, owner, name, full_name, branch, raw_json, created_at, updated_at)
            VALUES ('repo-1', 'github', 'o11y-dev', 'reflect', 'o11y-dev/reflect', 'main', '{}', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO files(id, repo_id, path, extension, language, role, read_count, write_count, raw_json, created_at, updated_at)
            VALUES ('file-1', 'repo-1', 'src/reflect/core.py', '.py', 'python', 'source', 3, 1, '{}', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions(id, agent_id, repo_id, started_at, status, created_at, updated_at)
            VALUES ('sess-1', 'agent-1', 'repo-1', ?, 'completed', ?, ?)
            """,
            (now, now, now),
        )
        conn.executemany(
            """
            INSERT INTO steps(id, session_id, seq, type, started_at, status, summary, raw_attrs_json, created_at, updated_at)
            VALUES (?, 'sess-1', ?, ?, ?, 'completed', ?, ?, ?, ?)
            """,
            [
                (
                    "step-1",
                    1,
                    "llm_call",
                    now,
                    "UserPromptSubmit",
                    '{"gen_ai.client.prompt":"Use /review-skill and the `research-helper` subagent"}',
                    now,
                    now,
                ),
                (
                    "step-2",
                    2,
                    "tool_call",
                    now,
                    "PreToolUse",
                    '{"gen_ai.client.hook.event":"SubagentStart","gen_ai.client.subagent_type":"worker"}',
                    now,
                    now,
                ),
                ("step-3", 3, "tool_call", now, "PreToolUse", "{}", now, now),
                ("step-4", 4, "tool_call", now, "PostToolUse", "{}", now, now),
            ],
        )
        conn.executemany(
            """
            INSERT INTO tool_calls(
              id, step_id, session_id, tool_name, tool_type, status, duration_ms,
              input_hash, output_hash, input_preview_redacted, output_preview_redacted,
              raw_attrs_json, created_at, updated_at
            )
            VALUES (?, ?, 'sess-1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "tool-1",
                    "step-3",
                    "skill",
                    "local",
                    "completed",
                    10,
                    "in-1",
                    "out-1",
                    '{"skill":"reflect"}',
                    "",
                    "{}",
                    now,
                    now,
                ),
                (
                    "tool-2",
                    "step-3",
                    "Task",
                    "agent",
                    "completed",
                    20,
                    "in-2",
                    "out-2",
                    '{"agent_type":"explorer","name":"repo-map"}',
                    '{"status":"success","toolCallCount":4}',
                    "{}",
                    now,
                    now,
                ),
                (
                    "tool-3",
                    "step-4",
                    "exec_command",
                    "shell",
                    "completed",
                    30,
                    "in-3",
                    "out-3",
                    '{"cmd":"poetry run pytest tests/test_core.py && gh pr create --fill"}',
                    "3 passed",
                    "{}",
                    now,
                    now,
                ),
                (
                    "tool-4",
                    "step-4",
                    "Read",
                    "file",
                    "error",
                    5,
                    "in-4",
                    "",
                    '{"file_path":"src/reflect/core.py"}',
                    "",
                    "{}",
                    now,
                    now,
                ),
            ],
        )
        conn.execute(
            """
            INSERT INTO memories(
              id, scope, type, session_id, repo_id, content_hash, content_preview_redacted,
              confidence, sensitivity, source, last_seen_at, raw_attrs_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "mem-graph",
                "project",
                "agent_instruction",
                "sess-1",
                "repo-1",
                "hash-instruction",
                "Use the graph memory files",
                1.0,
                "unknown",
                "filesystem_instruction_scan",
                now,
                '{"path":"AGENTS.md","kind":"agent_instruction"}',
                now,
                now,
            ),
        )
        conn.commit()

        rebuild_graph(conn)

        node_kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM graph_nodes")}
        assert {"Repo", "Path", "Folder", "ToolCall", "Skill", "Subagent", "Outcome"} <= node_kinds
        edge_kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM graph_edges")}
        assert {
            "worked_in_repo",
            "contains_path",
            "executed_tool_call",
            "instance_of_tool",
            "used_skill",
            "spawned_subagent",
            "touched_path",
            "touched_folder",
            "contains_touched_path",
            "achieved_outcome",
            "produced_outcome",
            "described_by_path",
        } <= edge_kinds
        outcomes = {row[0] for row in conn.execute("SELECT label FROM graph_nodes WHERE kind = 'Outcome'")}
        assert {"pr_opened", "tests_passed"} <= outcomes
        skills = {row[0] for row in conn.execute("SELECT label FROM graph_nodes WHERE kind = 'Skill'")}
        assert {"reflect", "review-skill"} <= skills
        memory_paths = {row[0] for row in conn.execute("SELECT label FROM graph_nodes WHERE kind = 'Path'")}
        assert "AGENTS.md" in memory_paths
        repo_memory_edges = conn.execute(
            """
            SELECT COUNT(*)
            FROM graph_edges e
            JOIN graph_nodes s ON s.id = e.source_node_id
            JOIN graph_nodes t ON t.id = e.target_node_id
            WHERE e.kind = 'recorded_memory'
              AND s.kind = 'Repo'
              AND t.kind = 'Memory'
            """
        ).fetchone()[0]
        assert repo_memory_edges >= 1
        folders = {row[0] for row in conn.execute("SELECT label FROM graph_nodes WHERE kind = 'Folder'")}
        assert {"src", "src/reflect"} <= folders
    finally:
        conn.close()
