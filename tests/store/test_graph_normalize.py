import json

import pytest

from reflect.store.graph_normalize import rebuild_graph, refresh_graph
from reflect.store.ingest import ingest_local_spans_file
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.sqlite import connect_sqlite


def _graph_content(conn):
    return {
        "nodes": [tuple(row) for row in conn.execute("SELECT id,kind,label,session_id,first_seen_at,last_seen_at,attrs_json,identity_key FROM graph_nodes ORDER BY id")],
        "edges": [tuple(row) for row in conn.execute("SELECT id,source_node_id,target_node_id,kind,session_id,weight,first_seen_at,last_seen_at,attrs_json FROM graph_edges ORDER BY id")],
    }


def _seed_refresh_graph(conn):
    now = "2026-05-01T00:00:00Z"
    conn.execute("INSERT INTO agents(id,name,created_at,updated_at) VALUES('agent','codex',?,?)", (now, now))
    conn.executemany("INSERT INTO workspaces(id,root_path,path_hash,label,source_key,created_at,updated_at) VALUES(?,?,?,?, 'fixture',?,?)", [(key, f"/workspace/{key}", key, key, now, now) for key in ("w1", "w2")])
    conn.executemany("INSERT INTO sessions(id,agent_id,workspace_id,parent_session_id,started_at,status,created_at,updated_at) VALUES(?,'agent','w1',?,?,'ok',?,?)", [(sid, parent, now, now, now) for sid, parent in (("s1", None), ("s2", None), ("child", "s1"))])
    for sid in ("s1", "s2", "child"):
        _add_refresh_tool(conn, sid, f"{sid}-1", 1)
    conn.commit()


def _add_refresh_tool(conn, sid, call_id, seq):
    now = f"2026-05-01T00:{seq % 60:02d}:00Z"
    attrs = json.dumps({"gen_ai.client.file_path": "src/common.py"})
    conn.execute("INSERT INTO steps(id,session_id,seq,type,started_at,status,raw_attrs_json,created_at,updated_at) VALUES(?,?,?,'tool_call',?,'ok',?,?,?)", (call_id, sid, seq, now, attrs, now, now))
    conn.execute("INSERT INTO tool_calls(id,step_id,session_id,tool_name,tool_type,status,input_preview_redacted,raw_attrs_json,created_at,updated_at) VALUES(?,?,?,'Read','file','ok','src/common.py',?,?,?)", (call_id, call_id, sid, attrs, now, now))


@pytest.mark.parametrize("change", ["append", "modified_result", "retraction", "moved_workspace", "child_session", "removed_parent", "nothing_changed"])
def test_incremental_graph_matches_full_history_rebuild(tmp_path, change):
    conn = connect_sqlite(tmp_path / "graph.db")
    try:
        migrate(conn)
        _seed_refresh_graph(conn)
        rebuild_graph(conn)
        selected = {"s1"}
        if change == "append":
            _add_refresh_tool(conn, "s1", "s1-2", 2)
        elif change == "modified_result":
            conn.execute("UPDATE tool_calls SET status='error',output_preview_redacted='pytest: 1 failed',output_hash='changed' WHERE id='s1-1'")
        elif change == "retraction":
            conn.execute("DELETE FROM tool_calls WHERE id='s1-1'")
            conn.execute("DELETE FROM steps WHERE id='s1-1'")
        elif change == "moved_workspace":
            conn.execute("UPDATE sessions SET workspace_id='w2' WHERE id='s1'")
        elif change == "child_session":
            selected = {"child"}
            _add_refresh_tool(conn, "child", "child-2", 2)
        elif change == "removed_parent":
            conn.execute("UPDATE sessions SET parent_session_id=NULL WHERE id='child'")
        refresh_graph(conn, selected)
        incremental = _graph_content(conn)
        refresh_graph(conn, selected)
        assert _graph_content(conn) == incremental
        rebuild_graph(conn)
        assert _graph_content(conn) == incremental
        assert not conn.execute("SELECT name FROM sqlite_temp_master WHERE name IN ('graph_nodes','graph_edges','sessions','mcp_calls','reflect_graph_seen_edges')").fetchall()
    finally:
        conn.close()


def _audit_graph_writes(conn):
    conn.execute("CREATE TEMP TABLE graph_write_audit(table_name TEXT, operation TEXT)")
    for table in ("graph_nodes", "graph_edges", "graph_edge_sources"):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            conn.execute(f"CREATE TEMP TRIGGER audit_{table}_{operation} AFTER {operation} ON main.{table} BEGIN INSERT INTO graph_write_audit VALUES('{table}','{operation}'); END")


def test_unchanged_full_and_scoped_refresh_do_not_write_persisted_graph(tmp_path):
    conn = connect_sqlite(tmp_path / "graph.db")
    try:
        migrate(conn)
        _seed_refresh_graph(conn)
        rebuild_graph(conn)
        _audit_graph_writes(conn)
        before = [tuple(row) for row in conn.execute("SELECT id,created_at,updated_at FROM graph_nodes UNION ALL SELECT id,created_at,updated_at FROM graph_edges ORDER BY id")]
        rebuild_graph(conn)
        refresh_graph(conn, {"s1"})
        assert conn.execute("SELECT COUNT(*) FROM graph_write_audit").fetchone()[0] == 0
        assert [tuple(row) for row in conn.execute("SELECT id,created_at,updated_at FROM graph_nodes UNION ALL SELECT id,created_at,updated_at FROM graph_edges ORDER BY id")] == before
    finally:
        conn.close()


@pytest.mark.parametrize("retract", [False, "memory", "exposure"])
def test_refresh_preserves_or_retracts_cross_session_memory_edges(tmp_path, retract):
    conn = connect_sqlite(tmp_path / "graph.db")
    try:
        migrate(conn)
        _seed_refresh_graph(conn)
        now = "2026-05-01T00:00:00Z"
        conn.execute("INSERT INTO memories(id,scope,type,session_id,content_hash,source,last_seen_at,raw_attrs_json,created_at,updated_at) VALUES('m','project','instruction','s1','hash','fixture',?,'{}',?,?)", (now, now, now))
        conn.execute("INSERT INTO memory_exposures(memory_id,session_id,source,observed_at,created_at,updated_at) VALUES('m','s2','fixture',?,?,?)", (now, now, now))
        rebuild_graph(conn)
        if retract == "memory":
            conn.execute("DELETE FROM memories WHERE id='m'")
        elif retract == "exposure":
            conn.execute("DELETE FROM memory_exposures WHERE memory_id='m' AND session_id='s2'")
        refresh_graph(conn, {"s1"})
        incremental = _graph_content(conn)
        assert conn.execute("SELECT COUNT(*) FROM graph_edges WHERE session_id='s2' AND kind='recorded_memory'").fetchone()[0] == (0 if retract else 1)
        rebuild_graph(conn)
        assert _graph_content(conn) == incremental
    finally:
        conn.close()


@pytest.mark.parametrize("owner", ["s1", None])
@pytest.mark.parametrize("remove_exposure", [False, True])
def test_refresh_of_memory_recipient_preserves_owner_and_exposure_history(tmp_path, owner, remove_exposure):
    conn = connect_sqlite(tmp_path / "graph.db")
    try:
        migrate(conn)
        _seed_refresh_graph(conn)
        now = "2026-05-01T00:00:00Z"
        conn.execute("INSERT INTO memories(id,scope,type,session_id,content_hash,source,last_seen_at,raw_attrs_json,created_at,updated_at) VALUES('m','project','instruction',?,'hash','fixture',?,'{}',?,?)", (owner, now, now, now))
        conn.execute("INSERT INTO memory_exposures(memory_id,session_id,source,observed_at,created_at,updated_at) VALUES('m','s2','fixture',?,?,?)", (now, now, now))
        rebuild_graph(conn)
        if remove_exposure:
            conn.execute("DELETE FROM memory_exposures WHERE memory_id='m' AND session_id='s2'")
        refresh_graph(conn, {"s2"})
        incremental = _graph_content(conn)
        assert conn.execute("SELECT COUNT(*) FROM graph_edges WHERE session_id='s2' AND kind='recorded_memory'").fetchone()[0] == (0 if remove_exposure else 1)
        rebuild_graph(conn)
        assert _graph_content(conn) == incremental
    finally:
        conn.close()


@pytest.mark.parametrize("scoped", [False, True])
def test_failed_reconciliation_restores_graph_and_removes_temporary_objects(tmp_path, monkeypatch, scoped):
    import reflect.store.graph_normalize as graph_module

    conn = connect_sqlite(tmp_path / "graph.db")
    try:
        migrate(conn)
        _seed_refresh_graph(conn)
        rebuild_graph(conn)
        before = _graph_content(conn)
        _add_refresh_tool(conn, "s1", "s1-2", 2)
        original = graph_module._copy_changed_graph_rows

        def fail_after_node_copy(connection, source, target, *, edges):
            if target == "main.graph_edges":
                raise RuntimeError("fixture copy failure")
            return original(connection, source, target, edges=edges)

        monkeypatch.setattr(graph_module, "_copy_changed_graph_rows", fail_after_node_copy)
        with pytest.raises(RuntimeError, match="fixture copy failure"):
            refresh_graph(conn, {"s1"}) if scoped else rebuild_graph(conn)
        assert _graph_content(conn) == before
        assert conn.execute("SELECT COUNT(*) FROM steps WHERE id='s1-2'").fetchone()[0] == 1
        assert not conn.execute("SELECT name FROM sqlite_temp_master WHERE name LIKE 'reflect_graph_%' OR name IN ('graph_nodes','graph_edges','sessions','mcp_calls','reflect_changed_sessions')").fetchall()
        monkeypatch.setattr(graph_module, "_copy_changed_graph_rows", original)
        refresh_graph(conn, {"s1"}) if scoped else rebuild_graph(conn)
        assert _graph_content(conn) != before
    finally:
        conn.close()


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("cache_size", [0, -4096, 1024])
def test_staging_cache_is_bounded_and_restored_even_on_failure(tmp_path, monkeypatch, scoped, fail, cache_size):
    import reflect.store.graph_normalize as graph_module

    conn = connect_sqlite(tmp_path / "graph.db")
    try:
        migrate(conn)
        _seed_refresh_graph(conn)
        rebuild_graph(conn)
        conn.execute(f"PRAGMA temp.cache_size={cache_size}")
        original_temp_store = conn.execute("PRAGMA temp_store").fetchone()[0]
        original = graph_module._derive_graph

        def derive(connection, **kwargs):
            assert connection.execute("PRAGMA temp.cache_size").fetchone()[0] == (cache_size or -32768)
            if fail:
                raise RuntimeError("fixture staging failure")
            return original(connection, **kwargs)

        monkeypatch.setattr(graph_module, "_derive_graph", derive)
        if fail:
            with pytest.raises(RuntimeError, match="fixture staging failure"):
                refresh_graph(conn, {"s1"}) if scoped else rebuild_graph(conn)
        else:
            refresh_graph(conn, {"s1"}) if scoped else rebuild_graph(conn)
        assert conn.execute("PRAGMA temp.cache_size").fetchone()[0] == cache_size
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == original_temp_store
    finally:
        conn.close()


def test_full_rebuild_replaces_obsolete_identity_without_unique_conflicts(tmp_path):
    conn = connect_sqlite(tmp_path / "graph.db")
    try:
        migrate(conn)
        _seed_refresh_graph(conn)
        conn.execute("INSERT INTO graph_nodes(id,kind,label,identity_key,created_at,updated_at) VALUES('legacy','Workspace','old workspace','w1','old','old')")
        rebuild_graph(conn)
        assert not conn.execute("SELECT 1 FROM graph_nodes WHERE id='legacy'").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM graph_nodes WHERE kind='Workspace' AND identity_key='w1'").fetchone()[0] == 1
    finally:
        conn.close()


def test_scoped_refresh_does_not_prune_unrelated_isolated_shared_nodes(tmp_path):
    conn = connect_sqlite(tmp_path / "graph.db")
    try:
        migrate(conn)
        _seed_refresh_graph(conn)
        rebuild_graph(conn)
        conn.execute("INSERT INTO graph_nodes(id,kind,label,created_at,updated_at) VALUES('unrelated','Tool','external tool','old','old')")
        refresh_graph(conn, {"s1"})
        assert conn.execute("SELECT updated_at FROM graph_nodes WHERE id='unrelated'").fetchone()[0] == "old"
    finally:
        conn.close()


def test_uncommitted_rebuild_can_be_rolled_back_without_preexisting_transaction(tmp_path):
    conn = connect_sqlite(tmp_path / "graph.db")
    try:
        migrate(conn)
        _seed_refresh_graph(conn)
        rebuild_graph(conn)
        previous = _graph_content(conn)
        _add_refresh_tool(conn, "s1", "s1-2", 2)
        conn.commit()
        assert not conn.in_transaction
        conn.execute("PRAGMA temp.cache_size=0")
        rebuild_graph(conn, commit=False)
        assert conn.in_transaction
        assert conn.execute("PRAGMA temp.cache_size").fetchone()[0] == 0
        assert _graph_content(conn) != previous
        conn.rollback()
        assert _graph_content(conn) == previous
        assert not conn.execute("SELECT name FROM sqlite_temp_master WHERE name LIKE 'reflect_stage_%' OR name IN ('graph_nodes','graph_edges')").fetchall()
    finally:
        conn.close()


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
                "gen_ai.memory.id": f"mem-graph-{session_id}",
                "gen_ai.memory.content_hash": f"fixture-{session_id}",
                "gen_ai.memory.content_preview": f"Convention for {session_id}",
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

        assert first["nodes"] >= 6
        assert first["edges"] >= 5
        assert second == first
        assert second_state == first_state
        node_kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM graph_nodes")}
        assert {"Session", "Agent", "Tool", "MCPServer", "Memory"} <= node_kinds
        edge_kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM graph_edges")}
        assert {"ran_session", "used_tool", "used_mcp", "recorded_memory"} <= edge_kinds
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
        assert {"Repo", "Path", "Folder", "ToolCall", "Skill", "Subagent", "Spec"} <= node_kinds
        assert "Step" not in node_kinds
        edge_kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM graph_edges")}
        assert {
            "worked_in_repo",
            "contains_path",
            "used_skill",
            "spawned_subagent",
            "touched_path",
            "touched_folder",
            "contains_touched_path",
            "described_by_path",
            "defines_spec",
            "addressed_spec",
            "planned_spec",
        } <= edge_kinds
        outcomes = {row[0] for row in conn.execute("SELECT label FROM graph_nodes WHERE kind = 'Outcome'")}
        assert "tests_passed" not in outcomes
        assert "pr_opened" not in outcomes
        assert "commit_made" not in outcomes
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


def _graph_sources(conn):
    return [tuple(row) for row in conn.execute("SELECT * FROM graph_edge_sources ORDER BY edge_id,source_kind,source_id")]


def test_source_append_matches_rebuild_and_corrections_retract(tmp_path, monkeypatch):
    from reflect.store.hook_facts import HookFactRepository

    conn = connect_sqlite(tmp_path / "delta.db")
    try:
        migrate(conn)
        _seed_refresh_graph(conn)
        rebuild_graph(conn)
        _add_refresh_tool(conn, "s1", "new-call", 2)
        with monkeypatch.context() as patch:
            def forbid_history_read(*args):
                raise AssertionError("append reopened session agent history")

            patch.setattr(HookFactRepository, "load_agent_events", forbid_history_read)
            result = refresh_graph(conn, {"s1"}, changed_step_ids={"new-call"})
        assert result["source_scoped_sessions"] == 1
        incremental, sources = _graph_content(conn), _graph_sources(conn)
        rebuild_graph(conn)
        assert _graph_content(conn) == incremental
        assert _graph_sources(conn) == sources
        _audit_graph_writes(conn)
        refresh_graph(conn, {"s1"}, changed_step_ids={"new-call"})
        assert conn.execute("SELECT COUNT(*) FROM graph_write_audit").fetchone()[0] == 0
        conn.execute("DELETE FROM steps WHERE id='new-call'")
        refresh_graph(conn, {"s1"}, changed_step_ids={"new-call"})
        assert not conn.execute("SELECT 1 FROM graph_edge_sources WHERE source_id='new-call'").fetchone()
        retracted = _graph_content(conn)
        rebuild_graph(conn)
        assert _graph_content(conn) == retracted
    finally:
        conn.close()


def test_plain_calls_are_virtual_bounded_and_use_full_canonical_identity(tmp_path):
    from reflect.store.graph_ontology import virtual_tool_graph

    conn = connect_sqlite(tmp_path / "virtual.db")
    try:
        migrate(conn)
        _seed_refresh_graph(conn)
        for number in range(2, 50):
            _add_refresh_tool(conn, "s1", f"prefix-{number}-same-suffix", number)
        conn.execute("UPDATE steps SET raw_attrs_json='{}'")
        conn.execute("UPDATE tool_calls SET raw_attrs_json='{}',input_preview_redacted='echo ok',tool_name='exec_command',tool_type='shell'")
        rebuild_graph(conn)
        assert conn.execute("SELECT COUNT(*) FROM graph_nodes WHERE kind IN ('Step','ToolCall')").fetchone()[0] == 0
        nodes, edges = virtual_tool_graph(conn, ["s1"], limit=10000)
        calls = [node for node in nodes if node["kind"] == "ToolCall"]
        assert len(calls) == 40
        assert len({node["id"] for node in calls}) == 40
        assert len({node["label"] for node in calls}) == 1
        assert len(edges) == 80
        assert all(json.loads(node["attrs_json"])["source_id"] in node["identity_key"] for node in calls)
    finally:
        conn.close()


def _seed_assessment(conn, unit_id, step_id, *, assessment=None):
    now = "2026-05-01T00:00:00Z"
    boundary = json.dumps({"assessment": assessment or {
        "verification": "invalidated", "outcome": "unknown", "input_digest": "digest",
        "checks": [{"step_id": step_id, "exit_code": 0, "at": now,
                    "invalidated": True, "invalidated_by": ["edit-after-check"]}],
    }})
    conn.execute("""INSERT INTO execution_units(id,session_id,source,started_at,boundary_json,created_at,updated_at)
        VALUES(?,'s1','native',?,?,?,?)""", (unit_id, now, boundary, now, now))
    if step_id:
        conn.execute("INSERT INTO execution_unit_steps VALUES(?,?, 's1',?)", (unit_id, step_id, now))


def test_execution_graph_projects_current_assessment_and_retracts_retired_scope(tmp_path):
    from reflect.store.graph_execution import refresh_execution_graph

    conn = connect_sqlite(tmp_path / "execution.db")
    try:
        migrate(conn)
        _seed_refresh_graph(conn)
        _add_refresh_tool(conn, "s1", "other-check", 2)
        _seed_assessment(conn, "unit", "s1-1")
        _seed_assessment(conn, "other-unit", "other-check")
        _seed_assessment(conn, "report-only", None)
        rebuild_graph(conn)
        assert refresh_execution_graph(conn, {"s1"}, commit=True) == {"executions": 2, "checks": 2}
        attrs = [json.loads(row[0]) for row in conn.execute("SELECT attrs_json FROM graph_edges WHERE kind='checked_by'")]
        assert all(row["invalidated_by"] == ["edit-after-check"] and row["assessment_digest"] == "digest" for row in attrs)
        snapshot = _graph_content(conn)
        _audit_graph_writes(conn)
        refresh_execution_graph(conn, {"s1"}, commit=True)
        assert conn.execute("SELECT COUNT(*) FROM graph_write_audit").fetchone()[0] == 0
        rebuild_graph(conn)
        assert _graph_content(conn) == snapshot
        conn.execute("DELETE FROM execution_units WHERE id='unit'")
        refresh_execution_graph(conn, {"s1"}, execution_unit_ids={"unit"})
        assert [row[0] for row in conn.execute("SELECT label FROM graph_nodes WHERE kind='Execution'")] == ["other-unit"]
        conn.rollback()
        assert _graph_content(conn) == snapshot
    finally:
        conn.close()


def test_execution_graph_keeps_pending_and_ambiguous_checks_unknown(tmp_path):
    from reflect.store.graph_execution import refresh_execution_graph

    conn = connect_sqlite(tmp_path / "pending.db")
    try:
        migrate(conn)
        _seed_refresh_graph(conn)
        _seed_assessment(conn, "unit", "s1-1", assessment={"verification": "unknown", "outcome": "unknown", "pending_check_step_ids": ["s1-1"]})
        rebuild_graph(conn)
        assert refresh_execution_graph(conn, {"s1"}, commit=True)["checks"] == 1
        assert json.loads(conn.execute("SELECT attrs_json FROM graph_edges WHERE kind='checked_by'").fetchone()[0])["pending"] is True
        conn.execute("""INSERT INTO tool_calls(id,step_id,session_id,tool_name,tool_type,status,created_at,updated_at)
            VALUES('ambiguous','s1-1','s1','pytest','shell','ok','t','t')""")
        assert refresh_execution_graph(conn, {"s1"})["checks"] == 0
        assert not conn.execute("SELECT 1 FROM graph_edges WHERE kind='checked_by'").fetchone()
    finally:
        conn.close()


@pytest.mark.parametrize('kind', ['unknown_relation', 'checked_by'])
def test_typed_graph_rejects_invalid_endpoint_pairs(tmp_path, kind):
    from reflect.store.graph_ontology import validate_graph

    conn = connect_sqlite(tmp_path / 'invalid.db')
    try:
        migrate(conn)
        _seed_refresh_graph(conn)
        rebuild_graph(conn)
        conn.execute("UPDATE graph_edges SET kind=? WHERE id=(SELECT id FROM graph_edges LIMIT 1)", (kind,))
        with pytest.raises(ValueError, match='Invalid graph relationship'):
            validate_graph(conn)
    finally:
        conn.close()
