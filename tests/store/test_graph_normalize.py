import json

from reflect.store.graph_normalize import rebuild_graph, refresh_graph
from reflect.store.ingest import ingest_local_spans_file
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.sqlite import connect_sqlite


def _write_spans(path, session_id="sess-graph"):
    spans = [
        {
            "name": "UserPromptSubmit",
            "traceId": "trace-1",
            "spanId": "span-1",
            "start_time_ns": 100,
            "end_time_ns": 200,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": session_id,
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
                "gen_ai.client.session_id": session_id,
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
                "gen_ai.client.session_id": session_id,
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
                "gen_ai.client.session_id": session_id,
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
        first_state = {
            "nodes": conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0],
            "edges": conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0],
            "weight": conn.execute("SELECT COALESCE(SUM(weight), 0) FROM graph_edges").fetchone()[0],
        }
        second = rebuild_graph(conn)
        second_state = {
            "nodes": conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0],
            "edges": conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0],
            "weight": conn.execute("SELECT COALESCE(SUM(weight), 0) FROM graph_edges").fetchone()[0],
        }

        assert first["nodes"] >= 8
        assert first["edges"] >= 7
        assert second == first
        assert second_state == first_state
        node_kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM graph_nodes")}
        assert {"Session", "Step", "Agent", "Tool", "MCPServer", "Memory"} <= node_kinds
        edge_kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM graph_edges")}
        assert {"ran_session", "has_step", "used_tool", "used_mcp", "recorded_memory"} <= edge_kinds
    finally:
        conn.close()


def test_refresh_graph_scopes_high_volume_tables_to_changed_sessions(tmp_path):
    db = tmp_path / "reflect.db"
    first_spans = tmp_path / "first.jsonl"
    second_spans = tmp_path / "second.jsonl"
    _write_spans(first_spans)
    _write_spans(second_spans, session_id="sess-other")

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        ingest_local_spans_file(conn, file_path=first_spans)
        ingest_local_spans_file(conn, file_path=second_spans)
        normalize_pending_raw_events(conn)
        rebuild_graph(conn)
        other_nodes_before = conn.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE session_id = 'sess-other'"
        ).fetchone()[0]

        changed_spans = tmp_path / "changed.jsonl"
        changed_spans.write_text(
            json.dumps(
                {
                    "name": "PreToolUse",
                    "traceId": "trace-changed",
                    "spanId": "span-changed",
                    "start_time_ns": 1_000,
                    "end_time_ns": 1_100,
                    "attributes": {
                        "gen_ai.client.name": "claude",
                        "gen_ai.client.session_id": "sess-graph",
                        "gen_ai.client.tool_name": "Write",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        ingest_local_spans_file(conn, file_path=changed_spans)
        changed_session_ids: set[str] = set()
        normalize_pending_raw_events(conn, changed_session_ids=changed_session_ids)

        result = refresh_graph(conn, changed_session_ids)

        assert result["refreshed_sessions"] == 1
        assert changed_session_ids == {"sess-graph"}
        assert conn.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE session_id = 'sess-other'"
        ).fetchone()[0] == other_nodes_before
        assert conn.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE kind = 'Tool' AND label = 'Write'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_temp_master WHERE type = 'view' AND name = 'sessions'"
        ).fetchone()[0] == 0
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
        conn.execute(
            """
            INSERT INTO memories(
              id, scope, type, content_hash, content_preview_redacted,
              confidence, sensitivity, source, last_seen_at, raw_attrs_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "mem-inferred",
                "project",
                "agent_instruction",
                "hash-inferred",
                "Repo instruction file",
                0.9,
                "unknown",
                "filesystem_instruction_scan",
                now,
                '{"path":"/workspace/reflect/AGENTS.md","workspace_root":"/workspace/reflect","kind":"agent_instruction"}',
                now,
                now,
            ),
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
                "mem-plan",
                "project",
                "cursor_plan",
                "sess-1",
                "repo-1",
                "hash-plan",
                "Implement SQL graph spec nodes",
                1.0,
                "unknown",
                "cursor",
                now,
                '{"name":"execution.plan.md","path":"docs/specs/execution.plan.md"}',
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO specs(id, repo_id, title, status, owner, source_path, created_at, updated_at)
            VALUES ('spec-1', 'repo-1', 'SQL report parity', 'active', 'semyont', 'docs/specs/sql-report-parity.md', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO requirements(
              id, spec_id, title, description, status, priority, evidence_status, confidence, created_at, updated_at
            )
            VALUES ('req-1', 'spec-1', 'Expose semantic graph parity', 'Specs should render in the semantic graph.', 'done', 'high', 'present', 1.0, ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO evidence(
              id, requirement_id, session_id, step_id, repo_id, file_id, kind, summary, confidence, raw_json, created_at, updated_at
            )
            VALUES ('ev-1', 'req-1', 'sess-1', 'step-4', 'repo-1', 'file-1', 'file_change', 'Updated graph normalization', 1.0, '{}', ?, ?)
            """,
            (now, now),
        )
        conn.commit()

        rebuild_graph(conn)

        node_kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM graph_nodes")}
        assert {"Repo", "Path", "Folder", "ToolCall", "Skill", "Subagent", "Outcome", "Spec"} <= node_kinds
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
            "defines_spec",
            "addressed_spec",
            "planned_spec",
        } <= edge_kinds
        outcomes = {row[0] for row in conn.execute("SELECT label FROM graph_nodes WHERE kind = 'Outcome'")}
        assert {"pr_opened", "tests_passed"} <= outcomes
        skills = {row[0] for row in conn.execute("SELECT label FROM graph_nodes WHERE kind = 'Skill'")}
        assert {"reflect", "review-skill"} <= skills
        memory_paths = {row[0] for row in conn.execute("SELECT label FROM graph_nodes WHERE kind = 'Path'")}
        assert {"AGENTS.md", "docs/specs/sql-report-parity.md", "docs/specs/execution.plan.md"} <= memory_paths
        specs = {row[0] for row in conn.execute("SELECT label FROM graph_nodes WHERE kind = 'Spec'")}
        assert {"SQL report parity", "execution"} <= specs
        cursor_plan_memory_nodes = conn.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE kind = 'Memory' AND label = 'mem-plan'"
        ).fetchone()[0]
        assert cursor_plan_memory_nodes == 0
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
        assert repo_memory_edges >= 2
        repo_spec_edges = conn.execute(
            """
            SELECT COUNT(*)
            FROM graph_edges e
            JOIN graph_nodes s ON s.id = e.source_node_id
            JOIN graph_nodes t ON t.id = e.target_node_id
            WHERE e.kind = 'defines_spec'
              AND s.kind = 'Repo'
              AND t.kind = 'Spec'
            """
        ).fetchone()[0]
        assert repo_spec_edges >= 2
        folders = {row[0] for row in conn.execute("SELECT label FROM graph_nodes WHERE kind = 'Folder'")}
        assert {"src", "src/reflect"} <= folders
    finally:
        conn.close()
